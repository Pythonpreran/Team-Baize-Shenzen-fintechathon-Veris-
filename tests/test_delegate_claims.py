"""T24 delegate role: member-scoped local claim preparation only."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from proven import api, auth, persistence


OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}
DELEGATE = {"email": "delegate@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


def test_delegate_can_only_prepare_one_consented_members_claim(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    api.SESSIONS.clear()
    with TestClient(api.app) as client:
        assert client.post("/api/auth/login", json=OWNER).status_code == 200
        session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
        assert session is not None
        with session.lock:
            scope = api._portfolio_scope(session)
        repository = api._portfolio_repository()
        member = repository.add_member(
            scope, name="Delegated adult", relationship="parent", date_of_birth="1970-01-01")
        schedule = repository.add_document(
            scope, kind="SCHEDULE", blob=b"%PDF-delegated-schedule", language="en", country="IN",
            filename="delegated-schedule.pdf")
        policy = repository.create_policy(
            scope, country="IN", line_of_business="HEALTH", insurer="Lotus General (Sandbox)",
            product="Delegated individual health plan", masked_number="****-DELEGATE", status="ACTIVE",
            insured_member_ids=[member["id"]], source_document_ids=[schedule["id"]])
        start, end = datetime.now(timezone.utc).date().isoformat(), (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
        assert client.post("/api/privacy/consents", json={
            "purpose": "CLAIM_MANAGEMENT", "data_categories": ["HEALTH_DATA"],
            "recipient": "Synthetic household delegate", "valid_from": start, "valid_until": end,
            "member_id": member["id"],
        }).status_code == 200
        delegate_id = auth.ACCOUNTS[DELEGATE["email"]].principal.id
        assert client.post("/api/privacy/delegates", json={
            "member_id": member["id"], "delegate_id": delegate_id,
            "scopes": ["claims:read", "claims:manage"], "valid_from": start, "valid_until": end,
        }).status_code == 200
        intake = client.post("/api/claims/intake", json={
            "policy_id": policy["id"], "incident_at": "2026-10-10T09:00:00+00:00",
            "mode": "REIMBURSEMENT",
        })
        assert intake.status_code == 200, intake.text
        claim = intake.json()["claim"]
        uploaded = client.post(f"/api/claims/{claim['id']}/documents", files=[
            ("files", ("discharge.pdf", b"%PDF-delegate-discharge", "application/pdf")),
            ("kinds", (None, "HOSPITAL_DISCHARGE_SUMMARY")),
            ("files", ("bill.pdf", b"%PDF-delegate-bill", "application/pdf")),
            ("kinds", (None, "HOSPITAL_BILL")),
        ])
        assert uploaded.status_code == 200, uploaded.text
        document_ids = [schedule["id"], *[item["id"] for item in uploaded.json()["documents"]]]
        assert client.post("/api/auth/logout").status_code == 200
        assert client.post("/api/auth/login", json=DELEGATE).status_code == 200
        listed = client.get("/api/delegate/claims")
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()["claims"]] == [claim["id"]]
        assert client.get(f"/api/delegate/claims/{claim['id']}").status_code == 200
        packet = client.post(f"/api/delegate/claims/{claim['id']}/packet", json={
            "document_ids": document_ids,
        })
        assert packet.status_code == 200, packet.text
        ready = client.post(f"/api/delegate/claims/{claim['id']}/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["claim"]["state"] == "READY"
        assert client.post(f"/api/claims/{claim['id']}/submit").status_code == 403
    api.SESSIONS.clear()
