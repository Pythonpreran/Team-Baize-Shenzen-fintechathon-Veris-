"""T12 synthetic insurer/bank contracts, idempotency, signing and reconciliation."""
from __future__ import annotations

import base64
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proven import api, auth, persistence, sandbox
from proven.sandbox_transport import SandboxHttpConnector


@pytest.fixture()
def service(tmp_path):
    return sandbox.SandboxService(
        tmp_path / "sandbox.sqlite", signing_key=b"s" * 32,
        log_path=tmp_path / "operations.jsonl")


def _claim_payload(tenant_id: str = "tenant-a") -> dict[str, str]:
    return {"policy_id": "policy-health", "claim_type": "HEALTH_REIMBURSEMENT", "tenant_id": tenant_id}


def test_claim_submission_is_idempotent_and_emits_a_signed_acknowledgement(service):
    first = service.submit_claim(_claim_payload(), idempotency_key="submit-1")
    second = service.submit_claim(_claim_payload(), idempotency_key="submit-1")
    assert first["claim_id"] == second["claim_id"]
    assert second["idempotent_replay"] is True
    assert first["webhook"]["event"]["event_type"] == "CLAIM_ACKNOWLEDGED"
    assert service.verify_webhook(first["webhook"]["event"], first["webhook"]["signature"]) is True
    log = service.operation_log("tenant-a")
    assert [entry["operation"] for entry in log].count("CLAIM_SUBMITTED") == 1
    assert service.verify_operation_log("tenant-a")["valid"] is True


def test_clock_only_advances_through_the_local_admin_control(service):
    before = service.clock()["current_at"]
    after = service.advance_clock(2)["current_at"]
    assert before != after
    with pytest.raises(sandbox.SandboxError):
        service.advance_clock(-1)


def test_settlement_correction_reconciles_to_the_sandbox_bank_and_emits_signed_event(service):
    claim = service.submit_claim(_claim_payload(), idempotency_key="submit-2")
    status = service.claim_status(claim["claim_id"], tenant_id="tenant-a")
    unsupported = next(item for item in status["settlement"]["deductions"]
                       if item["id"] == "room-category")
    assert unsupported["status"] == "NOT_FOUND_IN_SCENARIO_WORDING"

    service.open_claim_query(claim["claim_id"], tenant_id="tenant-a")
    service.reply_to_query(claim["claim_id"], {"note": "Prepared clarification"},
                           tenant_id="tenant-a", idempotency_key="reply-2")
    service.decide_claim(claim["claim_id"], tenant_id="tenant-a")
    corrected = service.dispute_settlement(
        claim["claim_id"], {"note": "Prepared query"}, tenant_id="tenant-a",
        idempotency_key="dispute-2")
    assert corrected["status"] == "SETTLED"
    assert corrected["settlement"]["corrected_payable"] == "47500.00"
    assert corrected["webhook"]["event"]["event_type"] == "SETTLEMENT_CORRECTED"
    assert service.verify_webhook(corrected["webhook"]["event"], corrected["webhook"]["signature"]) is True
    account_id = sandbox.beneficiary_account_id("tenant-a")
    statement = service.bank_statement(account_id, tenant_id="tenant-a")
    received = sum(
        Decimal(row["amount"]) for row in statement["transfers"]
        if row["to_account"] == account_id)
    assert received == Decimal(statement["balance"])
    assert service.verify_operation_log("tenant-a")["valid"] is True


def test_tenant_bank_accounts_and_receipts_reject_foreign_identifiers(service):
    def corrected(tenant_id: str, suffix: str) -> dict:
        claim = service.submit_claim(_claim_payload(tenant_id), idempotency_key=f"submit-{suffix}")
        service.open_claim_query(claim["claim_id"], tenant_id=tenant_id)
        service.reply_to_query(claim["claim_id"], {"note": "Prepared clarification"},
                               tenant_id=tenant_id, idempotency_key=f"reply-{suffix}")
        service.decide_claim(claim["claim_id"], tenant_id=tenant_id)
        return service.dispute_settlement(
            claim["claim_id"], {"note": "Prepared query"}, tenant_id=tenant_id,
            idempotency_key=f"dispute-{suffix}")

    first = corrected("tenant-a", "tenant-a")
    second = corrected("tenant-b", "tenant-b")
    first_account = sandbox.beneficiary_account_id("tenant-a")
    second_account = sandbox.beneficiary_account_id("tenant-b")
    assert service.bank_statement(first_account, tenant_id="tenant-a")["balance"] == "47500.00"
    assert service.bank_statement(second_account, tenant_id="tenant-b")["balance"] == "47500.00"
    receipt = service.bank_transfer_receipt(
        first["settlement"]["bank_transfer_id"], tenant_id="tenant-a")
    assert receipt["to_account"] == first_account
    assert receipt["amount"] == "47500.00"
    with pytest.raises(sandbox.SandboxNotFound):
        service.bank_statement(second_account, tenant_id="tenant-a")
    with pytest.raises(sandbox.SandboxNotFound):
        service.bank_transfer_receipt(second["settlement"]["bank_transfer_id"], tenant_id="tenant-a")


def test_webhook_signature_and_timestamp_are_verified(service):
    claim = service.submit_claim(_claim_payload(), idempotency_key="submit-3")
    signed = service.sign_webhook("CLAIM_ACKNOWLEDGED", claim["claim_id"], tenant_id="tenant-a")
    assert service.verify_webhook(signed["event"], signed["signature"]) is True
    assert service.verify_webhook(signed["event"], "bad") is False
    stale = dict(signed["event"])
    stale["timestamp"] = "2000-01-01T00:00:00+00:00"
    assert service.verify_webhook(stale, signed["signature"]) is False


def test_tenant_scoped_jsonl_log_hides_other_tenants_and_detects_artifact_tampering(service, tmp_path):
    service.submit_claim(_claim_payload("tenant-a"), idempotency_key="submit-a")
    service.submit_claim(_claim_payload("tenant-b"), idempotency_key="submit-b")
    owner_a = service.operation_log("tenant-a")
    assert owner_a and {row["tenant_id"] for row in owner_a} == {"tenant-a"}
    assert service.verify_operation_log("tenant-a")["valid"] is True
    assert service.verify_operation_log("tenant-b")["valid"] is True

    path = tmp_path / "operations.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    tampered = next(row for row in rows if row["tenant_id"] == "tenant-a")
    tampered["hmac"] = "0" * 64
    path.write_text("\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n",
                    encoding="utf-8")
    assert service.verify_operation_log("tenant-a")["valid"] is False
    assert service.verify_operation_log("tenant-b")["valid"] is True


def test_signed_jsonl_checkpoint_rejects_tail_truncation(service, tmp_path):
    service.submit_claim(_claim_payload(), idempotency_key="submit-tail-a")
    service.submit_claim(_claim_payload(), idempotency_key="submit-tail-b")
    assert service.verify_operation_log("tenant-a")["valid"] is True

    path = tmp_path / "operations.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    verification = service.verify_operation_log("tenant-a")
    assert verification["valid"] is False
    assert verification["checkpoint_count"] > verification["count"]
    assert "incomplete" in str(verification["reason"]).lower()


def test_external_operation_anchor_detects_full_jsonl_and_checkpoint_deletion(service, tmp_path):
    service.submit_claim(_claim_payload(), idempotency_key="submit-full-delete")
    anchor_path = service._operation_anchor_path()
    assert anchor_path.exists() and anchor_path != service.db_path and anchor_path != service.log_path

    with service._connection() as conn:  # reproduce deletion of both mutable operation artifacts
        conn.execute("DELETE FROM sandbox_operation_log WHERE tenant_id=?", ("tenant-a",))
        conn.execute("DELETE FROM sandbox_operation_checkpoint WHERE tenant_id=?", ("tenant-a",))
    assert service.log_path is not None
    service.log_path.write_text("", encoding="utf-8")
    verification = service.verify_operation_log("tenant-a")
    assert verification["valid"] is False
    assert verification["count"] == 0
    assert "external" in str(verification["reason"]).lower()
    with pytest.raises(sandbox.SandboxError, match="anchor"):
        service.submit_claim(_claim_payload(), idempotency_key="submit-after-full-delete")


def test_signed_jsonl_log_contains_no_raw_payload_and_verifies(service, tmp_path):
    service.submit_claim({**_claim_payload(), "member_name": "Synthetic name"},
                         idempotency_key="submit-4")
    path = tmp_path / "operations.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows and "payload_sha256" in rows[0]
    assert "member_name" not in path.read_text(encoding="utf-8")
    assert service.verify_operation_log("tenant-a")["valid"] is True


def test_connector_opens_a_circuit_after_repeated_transport_failures(service):
    class FailingService:
        def submit_claim(self, *_args, **_kwargs):
            raise RuntimeError("offline")

    connector = sandbox.SandboxConnector(FailingService(), retries=1)  # type: ignore[arg-type]
    with pytest.raises(sandbox.SandboxUnavailable):
        connector.submit_claim(_claim_payload(), "failure-1")
    with pytest.raises(sandbox.SandboxUnavailable, match="circuit is open"):
        connector.submit_claim(_claim_payload(), "failure-2")


def test_http_connector_rejects_nonlocal_sandbox_destinations(monkeypatch):
    monkeypatch.setenv(sandbox.SANDBOX_URL_ENV, "https://outside.example:8200")
    with pytest.raises(sandbox.SandboxError, match="local sandbox"):
        SandboxHttpConnector.from_environment()
    monkeypatch.setenv(sandbox.SANDBOX_URL_ENV, "http://sandbox:8200")
    assert SandboxHttpConnector.from_environment().base_url == "http://sandbox:8200"
    with pytest.raises(sandbox.SandboxError, match="local sandbox"):
        SandboxHttpConnector("http://sandbox:8200@outside.example:8200")


def test_standalone_fastapi_contract_uses_tenant_bound_generated_credentials_and_local_admin_clock(tmp_path, monkeypatch):
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "standalone.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "standalone.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, "c3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3M=")
    monkeypatch.setenv(sandbox.SANDBOX_ADMIN_TOKEN_ENV, "local-admin")
    from sandbox.app import app

    with TestClient(app) as client:
        rejected = client.post("/oauth/token", data={
            "grant_type": "client_credentials", "client_id": "veris-demo-client",
            "client_secret": "-".join(("synthetic", "demo", "only")), "tenant_id": "tenant-http",
        })
        assert rejected.status_code == 401
        client_id, client_secret, _raw_secret = sandbox.local_service_credentials()
        token = client.post("/oauth/token", data={
            "grant_type": "client_credentials", "client_id": client_id,
            "client_secret": client_secret, "tenant_id": "tenant-http",
        })
        assert token.status_code == 200, token.text
        headers = {
            "Authorization": f"Bearer {token.json()['access_token']}",
            "Idempotency-Key": "http-1", "X-VERIS-Tenant": "tenant-http",
        }
        scenarios = client.get("/scenarios", headers=headers)
        assert scenarios.status_code == 200
        assert {item["id"] for item in scenarios.json()["scenarios"]} >= {
            "HEALTH_REIMBURSEMENT", "HEALTH_CASHLESS", "MOTOR_OWN_DAMAGE", "TRAVEL_BAGGAGE_DELAY"}
        cross_tenant = client.post("/invoices", json={"amount": "1.00"}, headers={
            **headers, "X-VERIS-Tenant": "another-tenant"})
        assert cross_tenant.status_code == 401
        assert client.post("/clock/advance", json={"days": 0}, headers=headers).status_code == 403
        admin_headers = {**headers, "X-VERIS-Sandbox-Admin": "local-admin"}
        assert client.post("/clock/advance", json={"days": 0}, headers=admin_headers).status_code == 200
        submitted = client.post("/claims", json={"policy_id": "policy-health"}, headers=headers)
        assert submitted.status_code == 200, submitted.text
        replay = client.post("/claims", json={"policy_id": "policy-health"}, headers=headers)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
        query = client.post(f"/claims/{submitted.json()['claim_id']}/events/query", headers=admin_headers)
        assert query.status_code == 200, query.text
        verified = client.post("/webhooks/verify", json=query.json()["webhook"], headers=headers)
        assert verified.status_code == 200 and verified.json()["valid"] is True
        operations = client.get("/operations", headers=headers)
        assert operations.status_code == 200 and operations.json()["verification"]["valid"] is True


def test_veris_webhook_receiver_rejects_bad_signatures_and_replays(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox.operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    service = sandbox.default_service()
    signed = service.sign_webhook("CLAIM_ACKNOWLEDGED", "SBX-CLM-WEBHOOK", tenant_id="tenant-webhook")
    api.SESSIONS.clear()
    with TestClient(api.app) as client:
        accepted = client.post("/api/sandbox/webhook", json=signed)
        assert accepted.status_code == 200, accepted.text
        replay = client.post("/api/sandbox/webhook", json=signed)
        assert replay.status_code == 409
        rejected = client.post("/api/sandbox/webhook", json={
            "event": signed["event"], "signature": "0" * 64,
        })
        assert rejected.status_code == 403
    store = api._agent_store()
    assert store.verify_ledger("tenant-webhook")["valid"] is True
    api.SESSIONS.clear()


def test_local_compose_declares_private_sandbox_authority_and_cited_demo_inputs():
    import yaml
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    assert {"veris", "sandbox"} <= set(compose["services"])
    veris = compose["services"]["veris"]
    counterparty = compose["services"]["sandbox"]
    assert veris["ports"] == ["127.0.0.1:8000:8000"]
    assert "--host 0.0.0.0" in veris["command"]
    assert "ports" not in counterparty
    assert counterparty["expose"] == ["8200"]
    assert "sandbox.app:app" in counterparty["command"]
    assert veris["environment"]["VERIS_SANDBOX_URL"] == "http://sandbox:8200"
    assert veris["environment"]["VERIS_CORPUS_DIR"] == "/corpus"
    assert any(str(volume).endswith(":/corpus:ro") for volume in veris["volumes"])
    assert sandbox.SANDBOX_CLIENT_ID_ENV in veris["environment"]
    assert sandbox.SANDBOX_CLIENT_SECRET_ENV in veris["environment"]
    assert sandbox.SANDBOX_CLIENT_ID_ENV in counterparty["environment"]
    assert sandbox.SANDBOX_CLIENT_SECRET_ENV in counterparty["environment"]
    assert sandbox.SANDBOX_ADMIN_TOKEN_ENV not in veris["environment"]
    assert sandbox.SANDBOX_ADMIN_TOKEN_ENV in counterparty["environment"]
    assert "sandbox_data:/sandbox-data" not in veris.get("volumes", [])
    assert not {"VERIS_SANDBOX_DB_PATH", "VERIS_SANDBOX_LOG_PATH", "VERIS_SANDBOX_SIGNING_KEY"} & set(
        veris["environment"])


def test_hosted_compose_keeps_the_synthetic_sandbox_private_and_sanitises_proxy_identity():
    import yaml

    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.hosted.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"veris", "sandbox", "reverse-proxy"} <= set(services)
    veris = services["veris"]
    counterparty = services["sandbox"]
    proxy = services["reverse-proxy"]
    assert counterparty.get("ports") is None
    assert counterparty["expose"] == ["8200"]
    assert counterparty["networks"] == ["hosted_sandbox"]
    assert set(veris["networks"]) == {"hosted_proxy", "hosted_sandbox"}
    assert veris["environment"][sandbox.SANDBOX_URL_ENV] == "http://sandbox:8200"
    assert sandbox.SANDBOX_CLIENT_ID_ENV in veris["environment"]
    assert sandbox.SANDBOX_CLIENT_SECRET_ENV in veris["environment"]
    assert sandbox.SANDBOX_KEY_ENV not in veris["environment"]
    assert sandbox.SANDBOX_KEY_ENV in counterparty["environment"]
    assert compose["networks"]["hosted_sandbox"]["internal"] is True
    assert proxy["networks"]["hosted_proxy"]["ipv4_address"] == "172.30.0.2"

    nginx = (root / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "--forwarded-allow-ips=172.30.0.2" in dockerfile
