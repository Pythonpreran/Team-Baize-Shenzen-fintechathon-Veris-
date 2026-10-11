"""T13 API path: intake → packet → confirmation → synthetic claim tracking."""
from __future__ import annotations

import base64
import os

import pdfbuild_household
import pytest
from fastapi.testclient import TestClient

from proven import api, auth, claims, persistence, sandbox

OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox.operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_ADMIN_TOKEN_ENV, "local-admin")
    monkeypatch.setenv("VERIS_MODE", "local")
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        assert instance.post("/api/auth/login", json=OWNER).status_code == 200
        session = api.SESSIONS.get(instance.cookies.get(api.SESSION_COOKIE))
        assert session is not None
        with session.lock:
            scope = api._portfolio_scope(session)
        repository = api._portfolio_repository()
        document = repository.add_document(
            scope, kind="SCHEDULE", blob=pdfbuild_household.india_health_schedule_pdf(),
            language="en", country="IN", filename="rao-india-health-schedule.pdf")
        policy = repository.create_policy(
            scope, country="IN", line_of_business="HEALTH", insurer="Lotus General (Sandbox)",
            product="Synthetic family health", masked_number="****-HEALTH6", status="ACTIVE",
            period_start="2026-04-01", period_end="2027-03-31",
            source_document_ids=[document["id"]])
        yield instance, policy, document
    api.SESSIONS.clear()


def _sandbox_headers(counterparty: TestClient, *, admin: bool = False) -> dict[str, str]:
    tenant_id = "org-hocklee"
    token = counterparty.post("/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": os.environ[sandbox.SANDBOX_CLIENT_ID_ENV],
        "client_secret": os.environ[sandbox.SANDBOX_CLIENT_SECRET_ENV],
        "tenant_id": tenant_id,
    })
    assert token.status_code == 200, token.text
    headers = {
        "Authorization": f"Bearer {token.json()['access_token']}",
        "X-VERIS-Tenant": tenant_id, "Idempotency-Key": "claims-api-test",
    }
    if admin:
        headers["X-VERIS-Sandbox-Admin"] = "local-admin"
    return headers


def _grant(client: TestClient):
    response = client.post("/api/agent/consent", json={
        "scopes": ["agent:read", "agent:prepare", "agent:act"],
    })
    assert response.status_code == 200, response.text


def _complete_packet(instance: TestClient, claim_id: str, schedule_id: str) -> None:
    uploaded = instance.post(f"/api/claims/{claim_id}/documents", files=[
        ("files", ("discharge.pdf", b"%PDF-synthetic-discharge", "application/pdf")),
        ("kinds", (None, "HOSPITAL_DISCHARGE_SUMMARY")),
        ("files", ("hospital-bill.jpg", b"\xff\xd8\xffsynthetic-photo", "image/jpeg")),
        ("kinds", (None, "HOSPITAL_BILL")),
    ])
    assert uploaded.status_code == 200, uploaded.text
    document_ids = [schedule_id, *[row["id"] for row in uploaded.json()["documents"]]]
    packet = instance.post(f"/api/claims/{claim_id}/packet", json={"document_ids": document_ids})
    assert packet.status_code == 200, packet.text
    assert packet.json()["packet"]["manifest_sha256"]
    assert packet.json()["packet"]["checklist_complete"] is True


def _submitted_claim(instance: TestClient, policy: dict, document: dict) -> tuple[dict, str]:
    claim = instance.post("/api/claims/intake", json={
        "policy_id": policy["id"], "incident_at": "2026-10-10T09:00:00+00:00",
        "mode": "REIMBURSEMENT",
    }).json()["claim"]
    _complete_packet(instance, claim["id"], document["id"])
    assert instance.post(f"/api/claims/{claim['id']}/ready").status_code == 200
    pending = instance.post(f"/api/claims/{claim['id']}/submit").json()["pending_action"]
    preview_fields = {field["name"]: field["value"] for field in pending["preview"]["fields"]}
    expected_manifest = instance.get(f"/api/claims/{claim['id']}").json()["claim"]["packet_manifest_sha256"]
    assert pending["preview"]["tenant_id"] == "org-hocklee"
    assert preview_fields == {
        "claim_id": claim["id"], "packet_manifest_sha256": expected_manifest,
    }
    assert expected_manifest in pending["preview"]["canonical_payload"]
    sent = instance.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
        "password": auth.DEMO_PASSWORD,
    })
    assert sent.status_code == 200, sent.text
    return claim, sent.json()["result"]["local_claim"]["sandbox_claim_id"]


def test_health_claim_walkthrough_reaches_signed_acknowledged_sandbox_state(client, sandbox_counterparty):
    instance, policy, document = client
    _grant(instance)
    headers = _sandbox_headers(sandbox_counterparty, admin=True)
    assert sandbox_counterparty.post("/clock/advance", json={"days": 0}, headers=headers).status_code == 200
    claim, sandbox_claim_id = _submitted_claim(instance, policy, document)
    assert sandbox_claim_id.startswith("SBX-CLM-")
    status = instance.get(f"/api/claims/{claim['id']}")
    assert status.status_code == 200, status.text
    local = status.json()["claim"]
    assert local["state"] == "ACKNOWLEDGED"
    assert local["transitions"][-1]["actor"] == "INSURER"


def test_signed_query_and_decision_events_come_from_the_separate_sandbox(client, sandbox_counterparty):
    instance, policy, document = client
    _grant(instance)
    headers = _sandbox_headers(sandbox_counterparty, admin=True)
    claim, sandbox_claim = _submitted_claim(instance, policy, document)
    assert instance.post(f"/api/claims/{claim['id']}/sandbox/query").status_code == 404
    opened = sandbox_counterparty.post(f"/claims/{sandbox_claim}/events/query", headers=headers)
    assert opened.status_code == 200, opened.text
    assert instance.post("/api/sandbox/webhook", json=opened.json()["webhook"]).status_code == 200
    query_draft = instance.post(f"/api/claims/{claim['id']}/query/draft", json={"note": "Please review."})
    assert query_draft.status_code == 200, query_draft.text
    assert all(row["classification"] == "NEEDS_REVIEW" for row in query_draft.json()["draft"]["deductions"])

    reply = instance.post(f"/api/claims/{claim['id']}/query/send")
    assert reply.status_code == 200, reply.text
    confirmation = reply.json()["pending_action"]
    acknowledged = instance.post("/api/agent/confirm", json={
        "action_id": confirmation["id"], "payload_hash": confirmation["payload_hash"],
        "password": auth.DEMO_PASSWORD,
    })
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["result"]["local_claim"]["state"] == "UNDER_ASSESSMENT"

    decided = sandbox_counterparty.post(f"/claims/{sandbox_claim}/events/decision", headers=headers)
    assert decided.status_code == 200, decided.text
    assert instance.post("/api/sandbox/webhook", json=decided.json()["webhook"]).status_code == 200
    final = instance.get(f"/api/claims/{claim['id']}").json()["claim"]
    assert final["state"] == "DECIDED"


def test_claim_escalation_fails_closed_until_a_sourced_ladder_is_available(client, sandbox_counterparty, monkeypatch):
    instance, policy, document = client
    _grant(instance)
    claim, _sandbox_claim = _submitted_claim(instance, policy, document)
    sla = instance.get(f"/api/claims/{claim['id']}/sla")
    assert sla.status_code == 200, sla.text
    assert sla.json()["escalation"]["output_state"] == "INFORMATION_MISSING"
    assert instance.post(f"/api/claims/{claim['id']}/escalation").status_code == 409

    monkeypatch.setattr(claims, "escalation_gate", lambda *_args, **_kwargs: {
        "output_state": "DOCUMENTED", "eligible": True,
        "deadline_due_at": "2026-10-10T14:30:00+05:30",
        "entry": {"entry_id": "IN-CL-TEST", "quote": "Synthetic cited ladder step."},
    })
    prepared = instance.post(f"/api/claims/{claim['id']}/escalation")
    assert prepared.status_code == 200, prepared.text
    pending = prepared.json()["pending_action"]
    confirmed = instance.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
        "password": auth.DEMO_PASSWORD,
    })
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["result"]["local_claim"]["state"] == "ESCALATED"


def test_claim_submission_requires_complete_verified_packet_and_exact_confirmation(client):
    instance, policy, document = client
    _grant(instance)
    naive = instance.post("/api/claims/intake", json={
        "policy_id": policy["id"], "incident_at": "2026-10-10T09:00:00", "mode": "REIMBURSEMENT",
    })
    assert naive.status_code == 422
    claim = instance.post("/api/claims/intake", json={
        "policy_id": policy["id"], "incident_at": "2026-10-10T09:00:00+00:00",
        "mode": "REIMBURSEMENT",
    }).json()["claim"]
    incomplete = instance.post(f"/api/claims/{claim['id']}/packet", json={"document_ids": [document["id"]]})
    assert incomplete.status_code == 422
    assert instance.post(f"/api/claims/{claim['id']}/ready").status_code == 409
    assert instance.post(f"/api/claims/{claim['id']}/submit").status_code == 409
