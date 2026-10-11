"""T15 end-to-end synthetic correction: check → confirmed dispute → corrected transfer."""
from __future__ import annotations

import base64
import copy
import os

import pdfbuild_household
from fastapi.testclient import TestClient

from proven import api, auth, health, persistence, sandbox


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
        "X-VERIS-Tenant": tenant_id, "Idempotency-Key": "settlement-test",
    }
    if admin:
        headers["X-VERIS-Sandbox-Admin"] = "local-admin"
    return headers


def test_synthetic_settlement_correction_closes_the_loop(tmp_path, monkeypatch, sandbox_counterparty):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox.operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_ADMIN_TOKEN_ENV, "local-admin")
    monkeypatch.setenv("VERIS_MODE", "local")
    headers = _sandbox_headers(sandbox_counterparty, admin=True)
    api.SESSIONS.clear()
    with TestClient(api.app) as client:
        assert client.post("/api/auth/login", json={
            "email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD,
        }).status_code == 200
        session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
        assert session is not None
        with session.lock:
            scope = api._portfolio_scope(session)
        portfolio = api._portfolio_repository()
        document = portfolio.add_document(scope, kind="SCHEDULE", blob=pdfbuild_household.india_health_schedule_pdf(),
                                          language="en", country="IN", filename="schedule.pdf")
        policy = portfolio.create_policy(scope, country="IN", line_of_business="HEALTH",
                                         insurer="Lotus General (Sandbox)", product="Synthetic", masked_number="****-1",
                                         status="ACTIVE", source_document_ids=[document["id"]])

        def enrich(raw):
            value = copy.deepcopy(raw)
            selected = next(item for item in value["policies"] if item["id"] == policy["id"])
            selected["health_items"] = [
                {"mechanism_code": "LIMIT.ROOM_RENT", "state": "DOCUMENTED", "value": "1000", "unit": "INR",
                 "citation": {"document": "Policy wording", "page": 6, "quote": "Room rent is limited."}},
                {"mechanism_code": "SHARE.CO_PAYMENT", "state": "DOCUMENTED", "value": "5", "unit": "%",
                 "citation": {"document": "Policy wording", "page": 11, "quote": "The insured shall bear five per cent."}},
                {"mechanism_code": "CONDITION.PROPORTIONATE_DEDUCTION", "state": "DOCUMENTED",
                 "citation": {"document": "Policy wording", "page": 12,
                              "quote": "A proportionate deduction applies only to associated medical expenses."}},
            ]
            return value

        monkeypatch.setattr(health, "enrich_portfolio", enrich)
        assert client.post("/api/agent/consent", json={
            "scopes": ["agent:read", "agent:prepare", "agent:act"],
        }).status_code == 200
        claim = client.post("/api/claims/intake", json={
            "policy_id": policy["id"], "incident_at": "2026-10-10T09:00:00+00:00", "mode": "REIMBURSEMENT",
        }).json()["claim"]
        uploads = client.post(f"/api/claims/{claim['id']}/documents", files=[
            ("files", ("discharge.pdf", b"%PDF-synthetic-discharge", "application/pdf")),
            ("kinds", (None, "HOSPITAL_DISCHARGE_SUMMARY")),
            ("files", ("hospital-bill.jpg", b"\xff\xd8\xffsynthetic-photo", "image/jpeg")),
            ("kinds", (None, "HOSPITAL_BILL")),
        ])
        assert uploads.status_code == 200, uploads.text
        packet_ids = [document["id"], *[row["id"] for row in uploads.json()["documents"]]]
        assert client.post(f"/api/claims/{claim['id']}/packet", json={"document_ids": packet_ids}).status_code == 200
        assert client.post(f"/api/claims/{claim['id']}/ready").status_code == 200
        pending = client.post(f"/api/claims/{claim['id']}/submit").json()["pending_action"]
        submitted = client.post("/api/agent/confirm", json={
            "action_id": pending["id"], "payload_hash": pending["payload_hash"], "password": auth.DEMO_PASSWORD,
        }).json()
        sandbox_claim = submitted["result"]["local_claim"]["sandbox_claim_id"]
        query_event = sandbox_counterparty.post(
            f"/claims/{sandbox_claim}/events/query", headers=headers)
        assert query_event.status_code == 200, query_event.text
        assert client.post("/api/sandbox/webhook", json=query_event.json()["webhook"]).status_code == 200
        reply = client.post(f"/api/claims/{claim['id']}/query/send")
        assert reply.status_code == 200
        pending_reply = reply.json()["pending_action"]
        assert client.post("/api/agent/confirm", json={
            "action_id": pending_reply["id"], "payload_hash": pending_reply["payload_hash"], "password": auth.DEMO_PASSWORD,
        }).status_code == 200
        decision_event = sandbox_counterparty.post(
            f"/claims/{sandbox_claim}/events/decision", headers=headers)
        assert decision_event.status_code == 200, decision_event.text
        assert client.post("/api/sandbox/webhook", json=decision_event.json()["webhook"]).status_code == 200

        checked = client.post(f"/api/claims/{claim['id']}/settlement/check", json={
            "loss": "50000", "room_charge_per_day": "1000", "days": "5", "deductible": "0",
            "proportionate_factor": "0",
        })
        assert checked.status_code == 200, checked.text
        assert checked.json()["check"]["expected_illustration"] == "47500.00"
        deduction = next(row for row in checked.json()["check"]["deductions"] if row["id"] == "room-category")
        assert deduction["classification"] == "NOT_FOUND"
        assert deduction["citation"]["quote"] == "A proportionate deduction applies only to associated medical expenses."
        dispute = client.post(f"/api/claims/{claim['id']}/settlement/dispute")
        assert dispute.status_code == 200, dispute.text
        first_confirmation = dispute.json()["pending_action"]
        dispute_preview = first_confirmation["preview"]
        dispute_fields = {field["name"]: field["value"] for field in dispute_preview["fields"]}
        assert dispute_preview["tenant_id"] == "org-hocklee"
        assert dispute_fields["claim_id"] == claim["id"]
        assert dispute_fields["deduction_ids"] == '["room-category"]'
        assert "did not locate the marked deduction" in dispute_fields["dispute_text"]
        assert len(dispute_fields["dispute_sha256"]) == 64

        class FailingConnector:
            def dispute_settlement(self, *_args, **_kwargs):
                raise sandbox.SandboxUnavailable("synthetic connector offline")

        real_connector = api._sandbox_connector
        monkeypatch.setattr(api, "_sandbox_connector", lambda: FailingConnector())
        failed = client.post("/api/agent/confirm", json={
            "action_id": first_confirmation["id"], "payload_hash": first_confirmation["payload_hash"],
            "password": auth.DEMO_PASSWORD,
        })
        assert failed.status_code == 503
        assert client.get(f"/api/claims/{claim['id']}").json()["claim"]["state"] == "DECIDED"
        retry = client.post(f"/api/claims/{claim['id']}/settlement/dispute")
        assert retry.status_code == 200, retry.text
        monkeypatch.setattr(api, "_sandbox_connector", real_connector)
        confirmation = retry.json()["pending_action"]
        corrected = client.post("/api/agent/confirm", json={
            "action_id": confirmation["id"], "payload_hash": confirmation["payload_hash"], "password": auth.DEMO_PASSWORD,
        })
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["result"]["local_claim"]["state"] == "SETTLED"
        transfer_id = corrected.json()["result"]["claim"]["settlement"]["bank_transfer_id"]

        connector = api._sandbox_connector()

        class MissingReceiptConnector:
            def claim_status(self, *args, **kwargs):
                return connector.claim_status(*args, **kwargs)

            def bank_transfer_receipt(self, *_args, **_kwargs):
                raise sandbox.SandboxNotFound("Synthetic receipt absent for this regression.")

        monkeypatch.setattr(api, "_sandbox_connector", lambda: MissingReceiptConnector())
        missing_receipt = client.get(f"/api/claims/{claim['id']}/settlement/reconcile")
        assert missing_receipt.status_code == 200, missing_receipt.text
        assert missing_receipt.json()["state"] == "INFORMATION_MISSING"
        assert missing_receipt.json()["transfer_id"] == transfer_id

        monkeypatch.setattr(api, "_sandbox_connector", lambda: connector)
        reconciliation = client.get(f"/api/claims/{claim['id']}/settlement/reconcile")
        assert reconciliation.status_code == 200, reconciliation.text
        assert reconciliation.json()["state"] == "RECONCILED"
        assert reconciliation.json()["receipt"] == {
            "transfer_id": transfer_id,
            "to_account": sandbox.beneficiary_account_id("org-hocklee"),
            "amount": "47500.00",
            "reference": f"corrected-settlement:{sandbox_claim}",
            "created_at": "2026-10-10T09:00:00+00:00",
            "synthetic": True,
        }
        bank = sandbox_counterparty.get(
            f"/bank/accounts/{sandbox.beneficiary_account_id('org-hocklee')}/statement", headers=headers)
        assert bank.status_code == 200, bank.text
        assert bank.json()["balance"] == "47500.00"
    api.SESSIONS.clear()
