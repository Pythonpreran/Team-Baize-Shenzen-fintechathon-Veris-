"""T23 motor own-damage copilot and counterparty-owned surveyor state tests."""
from __future__ import annotations

import base64
import os

from fastapi.testclient import TestClient

from proven import api, auth, claims, persistence, sandbox


OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


def _scope() -> persistence.TenantScope:
    return persistence.TenantScope("tenant-motor", "user-motor", role="SME")


def _policy() -> dict:
    return {
        "id": "policy-motor", "country": "IN", "line_of_business": "MOTOR",
        "source": "SYNTHETIC_EXAMPLE", "source_document_ids": ["schedule"],
        "motor_items": [
            {"mechanism_code": "COVERED.MOTOR_THIRD_PARTY", "state": "DOCUMENTED",
             "citation": {"document": "Synthetic motor schedule", "page": 1,
                          "quote": "Synthetic third-party motor term."}},
            {"mechanism_code": "CONDITION.DEPRECIATION_SCHEDULE", "state": "DOCUMENTED",
             "citation": {"document": "Synthetic motor schedule", "page": 1,
                          "quote": "Synthetic depreciation schedule."}},
        ],
    }


def test_motor_intake_has_a_packet_checklist_but_withholds_an_unsourced_deadline(tmp_path):
    repository = claims.ClaimsRepository(tmp_path / "claims.sqlite", b"m" * 32)
    claim = repository.create(
        _scope(), policy=_policy(), incident_at="2026-10-10T09:00:00+00:00",
        mode="MOTOR_OWN_DAMAGE")
    assert claim["line_of_business"] == "MOTOR"
    assert {item["document_kind"] for item in claim["checklist"] if item["packet_required"]} == {
        "SCHEDULE", "RC", "SUPPLIER_INVOICE"}
    assert claim["deadline"]["state"] == "INFORMATION_MISSING"
    assert claim["deadline"]["due_at"] is None
    ready = repository.transition(_scope(), claim["id"], to_state="READY", actor="OWNER")
    submitted = repository.transition(_scope(), ready["id"], to_state="SUBMITTED", actor="AGENT")
    survey = repository.transition(_scope(), submitted["id"], to_state="SURVEYOR_REQUIRED", actor="INSURER")
    assessed = repository.transition(_scope(), survey["id"], to_state="UNDER_ASSESSMENT", actor="INSURER")
    assert assessed["state"] == "UNDER_ASSESSMENT"


def _sandbox_headers(counterparty: TestClient) -> dict[str, str]:
    client_id, client_secret, _raw_secret = sandbox.local_service_credentials()
    response = counterparty.post("/oauth/token", data={
        "grant_type": "client_credentials", "client_id": client_id,
        "client_secret": client_secret, "tenant_id": "org-hocklee",
    })
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-VERIS-Tenant": "org-hocklee", "Idempotency-Key": "motor-claim-test",
        "X-VERIS-Sandbox-Admin": "local-admin",
    }


def test_motor_claim_submission_enters_and_leaves_counterparty_surveyor_step(tmp_path, monkeypatch, sandbox_counterparty):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox.operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_ADMIN_TOKEN_ENV, "local-admin")
    monkeypatch.setenv("VERIS_MODE", "local")
    api.SESSIONS.clear()
    with TestClient(api.app) as client:
        assert client.post("/api/auth/login", json=OWNER).status_code == 200
        session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
        assert session is not None
        with session.lock:
            scope = api._portfolio_scope(session)
        portfolio = api._portfolio_repository()
        schedule = portfolio.add_document(
            scope, kind="SCHEDULE", blob=b"%PDF-synthetic-motor-schedule",
            language="en", country="IN", filename="motor-schedule.pdf")
        policy = portfolio.create_policy(
            scope, country="IN", line_of_business="MOTOR", insurer="Lotus General (Sandbox)",
            product="Synthetic motor own-damage plan", masked_number="****-MTR23", status="ACTIVE",
            source_document_ids=[schedule["id"]])
        assert client.post("/api/agent/consent", json={
            "scopes": ["agent:read", "agent:prepare", "agent:act"],
        }).status_code == 200
        intake = client.post("/api/claims/intake", json={
            "policy_id": policy["id"], "incident_at": "2026-10-10T09:00:00+00:00",
            "mode": "MOTOR_OWN_DAMAGE",
        })
        assert intake.status_code == 200, intake.text
        claim = intake.json()["claim"]
        uploaded = client.post(f"/api/claims/{claim['id']}/documents", files=[
            ("files", ("registration.pdf", b"%PDF-synthetic-registration", "application/pdf")),
            ("kinds", (None, "RC")),
            ("files", ("repair.pdf", b"%PDF-synthetic-repair", "application/pdf")),
            ("kinds", (None, "SUPPLIER_INVOICE")),
        ])
        assert uploaded.status_code == 200, uploaded.text
        packet = client.post(f"/api/claims/{claim['id']}/packet", json={
            "document_ids": [schedule["id"], *[row["id"] for row in uploaded.json()["documents"]]],
        })
        assert packet.status_code == 200, packet.text
        assert client.post(f"/api/claims/{claim['id']}/ready").status_code == 200
        pending = client.post(f"/api/claims/{claim['id']}/submit").json()["pending_action"]
        sent = client.post("/api/agent/confirm", json={
            "action_id": pending["id"], "payload_hash": pending["payload_hash"],
            "password": auth.DEMO_PASSWORD,
        })
        assert sent.status_code == 200, sent.text
        local = sent.json()["result"]["local_claim"]
        assert local["state"] == "SURVEYOR_REQUIRED"
        headers = _sandbox_headers(sandbox_counterparty)
        completed = sandbox_counterparty.post(
            f"/claims/{local['sandbox_claim_id']}/events/survey-complete", headers=headers)
        assert completed.status_code == 200, completed.text
        webhook = client.post("/api/sandbox/webhook", json=completed.json()["webhook"])
        assert webhook.status_code == 200, webhook.text
        assert client.get(f"/api/claims/{claim['id']}").json()["claim"]["state"] == "UNDER_ASSESSMENT"
    api.SESSIONS.clear()
