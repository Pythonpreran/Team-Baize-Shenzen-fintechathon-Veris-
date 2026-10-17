"""T11 deterministic agent controls: tiers, consent, step-up and HMAC ledger."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from fastapi.testclient import TestClient

from proven import api, auth, persistence, sandbox
from proven.agent import AgentPolicyError, AgentRuntime, AgentStore


OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox.operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        assert instance.post("/api/auth/login", json=OWNER).status_code == 200
        yield instance
    api.SESSIONS.clear()


def _grant(client: TestClient, *scopes: str) -> None:
    response = client.post("/api/agent/consent", json={"scopes": list(scopes)})
    assert response.status_code == 200, response.text


def test_deterministic_hospital_claim_plan_drafts_then_stops_before_submission(client):
    _grant(client, "agent:read", "agent:prepare", "agent:act")
    plan = client.post("/api/agent/plan", json={
        "intent": "Get Dad's hospital claim ready",
    })
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert body["mode"] == "DETERMINISTIC"
    assert [step["tool"] for step in body["steps"]][-1] == "claim.submit"
    assert body["steps"][-1]["state"] == "NEEDS_YOUR_OK"

    draft = client.post("/api/agent/prepare", json={
        "tool_name": "claim.prepare",
        "payload": {"policy_id": "synthetic-health", "claim_type": "HEALTH_REIMBURSEMENT"},
    })
    assert draft.status_code == 200, draft.text
    assert draft.json()["state"] == "DRAFTED"
    assert draft.json()["data"]["checklist"]

    letter = client.post("/api/agent/prepare", json={
        "tool_name": "letter.draft",
        "payload": {"policy_id": "synthetic-health"},
    })
    assert letter.status_code == 200, letter.text
    assert letter.json()["state"] == "DRAFTED"
    draft_text = letter.json()["data"]["draft"].lower()
    assert "will be paid" not in draft_text
    assert "covered" not in draft_text


def test_consent_role_and_document_origin_boundaries(client):
    blocked = client.post("/api/agent/prepare", json={
        "tool_name": "portfolio.read", "payload": {},
    })
    assert blocked.status_code == 403
    assert "consent" in blocked.json()["error"].lower()

    _grant(client, "agent:read")
    injected = client.post("/api/agent/plan", json={
        "intent": "submit a claim", "origin": "DOCUMENT",
    })
    assert injected.status_code == 403
    assert "document text" in injected.json()["error"].lower()

    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={
        "email": "adviser@lionbrokers.demo", "password": auth.DEMO_PASSWORD,
    }).status_code == 200
    wrong_role = client.get("/api/agent/status")
    assert wrong_role.status_code == 403


def test_confirmation_hash_step_up_and_replay_are_enforced(client):
    _grant(client, "agent:act")
    prepared = client.post("/api/agent/prepare", json={
        "tool_name": "endorsement.request", "payload": {"policy_id": "policy-synthetic-1"},
    })
    assert prepared.status_code == 200, prepared.text
    pending = prepared.json()["pending_action"]
    inbox = client.get("/api/agent/confirmations")
    assert inbox.status_code == 200
    listed = inbox.json()["confirmations"][0]
    assert listed["id"] == pending["id"]
    assert "payload" not in listed
    assert listed["preview"] == {
        "tenant_id": "org-hocklee", "tenant_scoped": True,
        "payload_hash": pending["payload_hash"],
        "canonical_payload": '{"policy_id":"policy-synthetic-1"}',
        "fields": [{"name": "policy_id", "value": "policy-synthetic-1"}],
    }

    mutated = client.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": "0" * 64,
        "password": auth.DEMO_PASSWORD,
    })
    assert mutated.status_code == 409
    assert "exact action payload" in mutated.json()["error"].lower()

    no_step_up = client.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
        "password": "wrong",
    })
    assert no_step_up.status_code == 403

    confirmed = client.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
        "password": auth.DEMO_PASSWORD,
    })
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["state"] == "SENT"
    assert confirmed.json()["result"]["accepted"] is True
    assert confirmed.json()["result"]["request"]["request_id"].startswith("SBX-REQ-")

    replay = client.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
        "password": auth.DEMO_PASSWORD,
    })
    assert replay.status_code == 409
    assert "already used" in replay.json()["error"].lower()


def test_canonical_claim_preview_is_tenant_scoped_before_step_up(tmp_path):
    runtime = AgentRuntime(AgentStore(tmp_path / "agent.sqlite", os.urandom(32)))
    owner = persistence.TenantScope("tenant-a", "user-a", "Owner", "owner@example.test", "SME")
    other = persistence.TenantScope("tenant-b", "user-b", "Other", "other@example.test", "SME")
    runtime.store.grant_consent(owner.tenant_id, ["agent:act"])
    runtime.store.grant_consent(other.tenant_id, ["agent:act"])

    prepared = runtime.prepare(owner, tool_name="claim.submit", payload={
        "claim_id": "claim-a", "packet_manifest_sha256": "a" * 64,
    })
    preview = prepared["pending_action"]["preview"]
    assert preview["tenant_id"] == owner.tenant_id
    assert preview["tenant_scoped"] is True
    assert preview["canonical_payload"] == (
        '{"claim_id":"claim-a","packet_manifest_sha256":"' + "a" * 64 + '"}')
    assert preview["fields"] == [
        {"name": "claim_id", "value": "claim-a"},
        {"name": "packet_manifest_sha256", "value": "a" * 64},
    ]
    assert runtime.confirmation_inbox(other) == []


def test_tier_three_has_no_tool_and_deterministic_budgets_hold(client):
    _grant(client, "agent:read")
    status = client.get("/api/agent/status")
    assert status.status_code == 200
    assert status.json()["tools"]
    assert all(tool["tier"] < 3 for tool in status.json()["tools"])

    plan = client.post("/api/agent/plan", json={"intent": "read my policy"}).json()
    assert len(plan["steps"]) <= plan["budgets"]["max_steps"]
    assert plan["budgets"]["max_tokens"] > 0


def test_mandate_changes_require_step_up_and_totp_can_supply_it(client):
    valid_from = datetime.now(timezone.utc).date().isoformat()
    valid_until = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    first = client.post("/api/agent/mandates", json={
        "scopes": ["invoice.pay"], "per_payment_cap": "500.00",
        "payee_allowlist": ["Lotus General (Sandbox)"],
        "valid_from": valid_from, "valid_until": valid_until,
        "advance_notice_required": True, "password": "wrong",
    })
    assert first.status_code == 403

    enrolled = client.post("/api/agent/totp/enrol", json={"password": auth.DEMO_PASSWORD})
    assert enrolled.status_code == 200, enrolled.text
    secret = enrolled.json()["secret"]
    code = pyotp.TOTP(secret).now()
    created = client.post("/api/agent/mandates", json={
        "scopes": ["invoice.pay"], "per_payment_cap": "500.00",
        "payee_allowlist": ["Lotus General (Sandbox)"],
        "valid_from": valid_from, "valid_until": valid_until,
        "advance_notice_required": True, "totp_code": code,
    })
    assert created.status_code == 200, created.text
    mandate = created.json()["mandate"]
    assert mandate["per_payment_cap"] == "500.00"

    rejected_revoke = client.request("DELETE", f"/api/agent/mandates/{mandate['id']}", json={
        "password": "wrong",
    })
    assert rejected_revoke.status_code == 403
    revoked = client.request("DELETE", f"/api/agent/mandates/{mandate['id']}", json={
        "password": auth.DEMO_PASSWORD,
    })
    assert revoked.status_code == 200, revoked.text


def test_invoice_payment_resolves_a_tenant_invoice_and_binds_the_mandate_snapshot(client):
    _grant(client, "agent:act")
    valid_from = datetime.now(timezone.utc).date().isoformat()
    valid_until = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
    created = client.post("/api/agent/mandates", json={
        "scopes": ["invoice.pay"], "per_payment_cap": "1.00",
        "payee_allowlist": ["Lotus General (Sandbox)"],
        "valid_from": valid_from, "valid_until": valid_until,
        "advance_notice_required": True, "password": auth.DEMO_PASSWORD,
    })
    assert created.status_code == 200, created.text
    first_mandate = created.json()["mandate"]
    store = api._agent_store()
    assert store.active_mandate("org-hocklee", scope="invoice.pay",
                                payee="different payee", amount="0.00") is None
    assert store.active_mandate("org-hocklee", scope="invoice.pay",
                                payee="Lotus General (Sandbox)", amount="2.00") is None

    invoice = sandbox.default_service().issue_invoice({
        "tenant_id": "org-hocklee", "payee": "Lotus General (Sandbox)", "amount": "2.00",
    }, idempotency_key="mandate-invoice")
    foreign = sandbox.default_service().issue_invoice({
        "tenant_id": "another-tenant", "payee": "Lotus General (Sandbox)", "amount": "2.00",
    }, idempotency_key="foreign-invoice")
    caller_terms = client.post("/api/agent/prepare", json={
        "tool_name": "invoice.pay",
        "payload": {"invoice_id": invoice["invoice_id"], "payee": "different", "amount": "0.00"},
    })
    assert caller_terms.status_code == 422
    foreign_prepare = client.post("/api/agent/prepare", json={
        "tool_name": "invoice.pay", "payload": {"invoice_id": foreign["invoice_id"]},
    })
    assert foreign_prepare.status_code == 404

    prepared = client.post("/api/agent/prepare", json={
        "tool_name": "invoice.pay", "payload": {"invoice_id": invoice["invoice_id"]},
    })
    assert prepared.status_code == 200, prepared.text
    pending = prepared.json()["pending_action"]
    stored_pending = api._agent_store().pending_action("org-hocklee", pending["id"])
    assert stored_pending["payload"] == {
        "invoice_id": invoice["invoice_id"], "invoice_payee": invoice["payee"],
        "invoice_amount": invoice["amount"], "invoice_tenant_id": "org-hocklee",
    }
    blocked = client.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
        "password": auth.DEMO_PASSWORD,
    })
    assert blocked.status_code == 403

    second = client.post("/api/agent/mandates", json={
        "scopes": ["invoice.pay"], "per_payment_cap": "10.00",
        "payee_allowlist": ["Lotus General (Sandbox)"],
        "valid_from": valid_from, "valid_until": valid_until,
        "advance_notice_required": True, "password": auth.DEMO_PASSWORD,
    })
    assert second.status_code == 200, second.text
    mandate = second.json()["mandate"]
    invoice_preview = next(item["preview"] for item in client.get("/api/agent/confirmations").json()["confirmations"]
                           if item["id"] == pending["id"])
    preview_fields = {field["name"]: field["value"] for field in invoice_preview["fields"]}
    assert preview_fields["invoice_id"] == invoice["invoice_id"]
    assert preview_fields["invoice_payee"] == invoice["payee"]
    assert preview_fields["invoice_amount"] == invoice["amount"]
    assert preview_fields["invoice_tenant_id"] == "org-hocklee"
    assert preview_fields["mandate_status"] == "MATCHED"
    assert preview_fields["mandate_id"] == mandate["id"]
    confirmed = client.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
        "password": auth.DEMO_PASSWORD,
    })
    assert confirmed.status_code == 200, confirmed.text
    paid = confirmed.json()["result"]["invoice"]
    assert paid["status"] == "PAID"
    assert paid["transfer"]["amount"] == "2.00"
    assert paid["payer_account"] == "sandbox-payer:org-hocklee"
    records = client.get("/api/agent/ledger").json()["records"]
    authorised = [record for record in records if record["kind"] == "MANDATE_AUTHORISED"]
    assert authorised and authorised[-1]["detail"]["mandate_id"] == mandate["id"]
    assert authorised[-1]["detail"] == {
        "tool": "invoice.pay", "mandate_id": mandate["id"], "scope": "invoice.pay",
        "payee": invoice["payee"], "amount": invoice["amount"], "invoice_id": invoice["invoice_id"],
        "advance_notice_required": True,
    }
    assert f"under mandate:{mandate['id']}" in authorised[-1]["actor"]
    assert first_mandate["id"] != mandate["id"]


def test_generic_grievance_preparation_cannot_bypass_the_cited_escalation_gate(client):
    _grant(client, "agent:act")
    bypass = client.post("/api/agent/prepare", json={
        "tool_name": "grievance.lodge", "payload": {"claim_id": "claim-any"},
    })
    assert bypass.status_code == 403
    assert "escalation" in bypass.json()["error"].lower()


def test_ledger_is_tenant_scoped_and_tampering_is_localised(client, tmp_path):
    _grant(client, "agent:read")
    assert client.post("/api/agent/plan", json={"intent": "read my policy"}).status_code == 200
    ledger = client.get("/api/agent/ledger")
    assert ledger.status_code == 200, ledger.text
    assert ledger.json()["verification"]["valid"] is True
    assert ledger.json()["records"]

    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    with session.lock:
        scope = api._portfolio_scope(session)
    store = api._agent_store()
    assert store.ledger("another-tenant") == []
    with store._connection() as conn:  # reproduce storage tampering, not a public API action
        conn.execute("UPDATE agent_ledger SET hmac='tampered' WHERE tenant_id=? AND seq=1",
                     (scope.tenant_id,))
    verification = store.verify_ledger(scope.tenant_id)
    assert verification == {"valid": False, "count": 2, "first_invalid_seq": 1}


def test_ledger_checkpoint_detects_tail_deletion_and_refuses_a_rewritten_head(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite", os.urandom(32))
    store.append_ledger("tenant-a", kind="PLAN", actor="user:a", detail={"step": 1})
    store.append_ledger("tenant-a", kind="TOOL_RESULT", actor="user:a", detail={"step": 2})
    assert store.verify_ledger("tenant-a")["valid"] is True

    with store._connection() as conn:  # reproduce storage tampering, not a public API action
        conn.execute("DELETE FROM agent_ledger WHERE tenant_id=? AND seq=2", ("tenant-a",))
    verification = store.verify_ledger("tenant-a")
    assert verification["valid"] is False
    assert verification["checkpoint_count"] == 2
    assert verification["count"] == 1
    assert "incomplete" in verification["reason"].lower()
    with pytest.raises(RuntimeError, match="protected agent ledger checkpoint"):
        store.append_ledger("tenant-a", kind="TOOL_RESULT", actor="user:a", detail={"step": 3})


def test_ledger_anchor_detects_full_history_and_checkpoint_deletion(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite", os.urandom(32))
    store.append_ledger("tenant-a", kind="PLAN", actor="user:a", detail={"step": 1})
    anchor_path = store._ledger_anchor_path()
    assert anchor_path.exists() and anchor_path != store.db_path

    with store._connection() as conn:  # reproduce deletion of both mutable ledger artifacts
        conn.execute("DELETE FROM agent_ledger WHERE tenant_id=?", ("tenant-a",))
        conn.execute("DELETE FROM agent_ledger_checkpoint WHERE tenant_id=?", ("tenant-a",))
    verification = store.verify_ledger("tenant-a")
    assert verification["valid"] is False
    assert verification["count"] == 0
    assert "external" in verification["reason"].lower()
    with pytest.raises(RuntimeError, match="checkpoint"):
        store.append_ledger("tenant-a", kind="TOOL_RESULT", actor="user:a", detail={"step": 2})


def test_runtime_refuses_unknown_and_tier_three_actions(tmp_path):
    key = os.urandom(32)
    runtime = AgentRuntime(AgentStore(tmp_path / "agent.sqlite", key))
    scope = persistence.TenantScope("tenant-a", "user-a", "Owner", "owner@example.test", "SME")
    runtime.store.grant_consent(scope.tenant_id, ["agent:read"])
    with pytest.raises(AgentPolicyError):
        runtime.prepare(scope, tool_name="claim.decide", payload={})
