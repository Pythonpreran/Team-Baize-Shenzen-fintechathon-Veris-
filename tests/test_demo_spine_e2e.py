"""Integrated T10–T17 synthetic owner journey through the public API."""
from __future__ import annotations

import base64
import json
import os

import pdfbuild_household
import pytest
from fastapi.testclient import TestClient

from proven import api, auth, household, persistence, sandbox


@pytest.fixture()
def client(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox.operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_ADMIN_TOKEN_ENV, "local-admin")
    monkeypatch.setenv("VERIS_LLM_PROFILE", "none")
    monkeypatch.setenv("VERIS_MODE", "local")
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        login = instance.post("/api/auth/login", json={
            "email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD,
        })
        assert login.status_code == 200, login.text
        yield instance
    api.SESSIONS.clear()


def _household_parts() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("files", (name, builder(), "application/pdf"))
        for name, builder in pdfbuild_household.BUILDERS.items()
    ]


def _confirm(client: TestClient, pending: dict) -> dict:
    response = client.post("/api/agent/confirm", json={
        "action_id": pending["id"],
        "payload_hash": pending["payload_hash"],
        "password": auth.DEMO_PASSWORD,
    })
    assert response.status_code == 200, response.text
    return response.json()


def _sandbox_headers(counterparty: TestClient) -> dict[str, str]:
    tenant_id = "org-hocklee"
    token = counterparty.post("/oauth/token", data={
        "grant_type": "client_credentials",
        "client_id": os.environ[sandbox.SANDBOX_CLIENT_ID_ENV],
        "client_secret": os.environ[sandbox.SANDBOX_CLIENT_SECRET_ENV],
        "tenant_id": tenant_id,
    })
    assert token.status_code == 200, token.text
    return {
        "Authorization": f"Bearer {token.json()['access_token']}",
        "X-VERIS-Tenant": tenant_id, "Idempotency-Key": "demo-spine-e2e",
        "X-VERIS-Sandbox-Admin": "local-admin",
    }


@pytest.mark.requires_corpus("in-arogya-sanjeevani-policy-wording")
def test_owner_can_complete_the_cited_synthetic_claim_correction_journey(client, sandbox_counterparty):
    """Portfolio → cited Q&A → consent → local action → records → correction.

    The test uses only generated household documents and temporary local databases. The
    insurer, webhooks, transfer, bank statement, and both chains are sandbox artifacts.
    A separate local-admin capability emits the query/decision events; the owner cannot.
    """
    assert client.post("/api/reset").status_code == 200
    assert client.get("/api/portfolio").status_code == 200
    uploaded = client.post("/api/bundle", files=_household_parts(), data={
        "business_name": "Rao household (synthetic)",
        "kinds": json.dumps([None, "SCHEDULE", "SCHEDULE", "SCHEDULE", "SCHEDULE", "SCHEDULE"]),
    })
    assert uploaded.status_code == 200, uploaded.text
    loaded = client.post("/api/portfolio/example-household")
    assert loaded.status_code == 200, loaded.text
    portfolio = loaded.json()["portfolio"]
    assert len(portfolio["members"]) == len(household.MEMBERS)
    health_policy = next(policy for policy in portfolio["policies"]
                         if policy["masked_number"] == "****-HEALTH6")

    answer = client.post("/api/ai/ask", json={
        "question": "When does Mum's diabetes waiting period end?",
        "policy_id": health_policy["id"],
    })
    assert answer.status_code == 200, answer.text
    cited_answer = answer.json()
    assert cited_answer["output_state"] == "DOCUMENTED"
    assert cited_answer["retrieval"]["method"] == "POLICY_EVIDENCE"
    citation = next(item for item in cited_answer["citations"]
                    if item.get("viewer") == "PORTFOLIO")
    cited_page = client.get(
        f"/api/portfolio/documents/{citation['document_id']}/page/{citation['page']}",
        params={"citation_id": citation["citation_id"]},
    )
    assert cited_page.status_code == 200, cited_page.text
    assert cited_page.json()["quote"] == citation["quote"]

    protection = client.get("/api/protection-map")
    assert protection.status_code == 200, protection.text
    cells = {(row["entity"], row["risk"]): row for row in protection.json()["cells"]}
    assert cells[("Bengaluru family car", "motor_third_party")]["state"] == "COMPULSORY_GAP"
    assert cells[("Anya Rao", "accident")]["state"] == "NOT_ASSESSED"

    consent = client.post("/api/agent/consent", json={
        "scopes": ["agent:read", "agent:prepare", "agent:act"],
    })
    assert consent.status_code == 200, consent.text
    intake = client.post("/api/claims/intake", json={
        "policy_id": health_policy["id"],
        "incident_at": "2026-10-10T09:00:00+00:00",
        "mode": "REIMBURSEMENT",
    })
    assert intake.status_code == 200, intake.text
    claim = intake.json()["claim"]
    claim_id = claim["id"]
    uploads = client.post(f"/api/claims/{claim_id}/documents", files=[
        ("files", ("discharge.pdf", b"%PDF-synthetic-discharge", "application/pdf")),
        ("kinds", (None, "HOSPITAL_DISCHARGE_SUMMARY")),
        ("files", ("itemised-bill.jpg", b"\xff\xd8\xffsynthetic-photo", "image/jpeg")),
        ("kinds", (None, "HOSPITAL_BILL")),
    ])
    assert uploads.status_code == 200, uploads.text
    packet = client.post(f"/api/claims/{claim_id}/packet", json={
        "document_ids": [*health_policy["source_document_ids"],
                         *[row["id"] for row in uploads.json()["documents"]]],
    })
    assert packet.status_code == 200, packet.text
    assert packet.json()["packet"]["manifest_sha256"]
    assert packet.json()["packet"]["checklist_complete"] is True
    assert client.post(f"/api/claims/{claim_id}/ready").status_code == 200

    prepared_submit = client.post(f"/api/claims/{claim_id}/submit")
    assert prepared_submit.status_code == 200, prepared_submit.text
    submitted = _confirm(client, prepared_submit.json()["pending_action"])
    sandbox_claim_id = submitted["result"]["local_claim"]["sandbox_claim_id"]
    assert sandbox_claim_id.startswith("SBX-CLM-")
    assert submitted["result"]["acknowledgement"]["accepted"] is True
    status = client.get(f"/api/claims/{claim_id}")
    assert status.status_code == 200, status.text
    assert status.json()["claim"]["state"] == "ACKNOWLEDGED"

    headers = _sandbox_headers(sandbox_counterparty)
    query = sandbox_counterparty.post(
        f"/claims/{sandbox_claim_id}/events/query", headers=headers)
    assert query.status_code == 200, query.text
    assert client.post("/api/sandbox/webhook", json=query.json()["webhook"]).status_code == 200
    reply = client.post(f"/api/claims/{claim_id}/query/send")
    assert reply.status_code == 200, reply.text
    _confirm(client, reply.json()["pending_action"])
    decision = sandbox_counterparty.post(
        f"/claims/{sandbox_claim_id}/events/decision", headers=headers)
    assert decision.status_code == 200, decision.text
    assert client.post("/api/sandbox/webhook", json=decision.json()["webhook"]).status_code == 200

    checked = client.post(f"/api/claims/{claim_id}/settlement/check", json={
        "loss": "50000", "room_charge_per_day": "1000", "days": "5", "deductible": "0",
        "proportionate_factor": "0",
    })
    assert checked.status_code == 200, checked.text
    settlement = checked.json()["check"]
    assert settlement["state"] == "READY_FOR_REVIEW"
    assert settlement["expected_illustration"] == "47500.00"
    assert any(item["classification"] == "NOT_FOUND" for item in settlement["deductions"])

    dispute = client.post(f"/api/claims/{claim_id}/settlement/dispute")
    assert dispute.status_code == 200, dispute.text
    disputed = _confirm(client, dispute.json()["pending_action"])
    assert disputed["result"]["settlement_correction"]["accepted"] is True
    reconciliation = client.get(f"/api/claims/{claim_id}/settlement/reconcile")
    assert reconciliation.status_code == 200, reconciliation.text
    assert reconciliation.json()["state"] == "RECONCILED"
    assert reconciliation.json()["received"] == "47500.00"
    assert reconciliation.json()["receipt"]["to_account"] == sandbox.beneficiary_account_id("org-hocklee")
    assert reconciliation.json()["receipt"]["reference"] == f"corrected-settlement:{sandbox_claim_id}"

    ledger = client.get("/api/agent/ledger/verify")
    operations = client.get("/api/sandbox/operations")
    assert ledger.status_code == 200, ledger.text
    assert operations.status_code == 200, operations.text
    assert ledger.json()["valid"] is True
    assert operations.json()["verification"]["valid"] is True
    assert operations.json()["operations"]
    assert operations.json()["synthetic"] is True
