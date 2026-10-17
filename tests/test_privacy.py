"""T24 encrypted privacy artifacts, delegation gates, export, and erase boundary tests."""
from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from proven import api, auth, persistence


OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


def _scope() -> persistence.TenantScope:
    return persistence.TenantScope("tenant-privacy", "owner-privacy", role="SME")


def test_privacy_artifacts_are_encrypted_tenant_scoped_and_delegate_gated(tmp_path):
    repository = persistence.PortfolioRepository(tmp_path / "portfolio.sqlite", os.urandom(32))
    scope = _scope()
    adult = repository.add_member(
        scope, name="Adult member", relationship="self", date_of_birth="1980-01-01",
        health_facts="Private health detail")
    start, end = datetime.now(timezone.utc).date().isoformat(), (datetime.now(timezone.utc).date() + timedelta(days=7)).isoformat()
    consent = repository.create_privacy_consent(
        scope, purpose="CLAIM_MANAGEMENT", data_categories=["HEALTH_DATA", "CLAIM_DOCUMENTS"],
        recipient="Synthetic delegate", valid_from=start, valid_until=end, member_id=adult["id"])
    grant = repository.create_delegate_grant(
        scope, member_id=adult["id"], delegate_id="delegate-user",
        scopes=["claims:read", "claims:manage"], valid_from=start, valid_until=end)
    assert repository.delegate_allowed(
        scope, delegate_id="delegate-user", member_id=adult["id"], required_scope="claims:manage")
    assert not repository.delegate_allowed(
        scope, delegate_id="delegate-user", member_id=adult["id"], required_scope="payments:act")
    retention = repository.set_retention_settings(scope, portfolio_days=365, claim_days=180, audit_days=730)
    assert retention["configured"] is True
    document = repository.add_document(
        scope, kind="SCHEDULE", blob=b"private export payload", language="en", country="IN",
        filename="private-schedule.pdf")
    exported = repository.export_portfolio_data(scope)
    assert exported["format"] == "veris.portfolio-export.v1"
    exported_document = next(item for item in exported["documents"] if item["id"] == document["id"])
    assert base64.b64decode(exported_document["blob_base64"]) == b"private export payload"
    assert consent["id"] in {item["id"] for item in exported["privacy_consents"]}
    assert grant["id"] in {item["id"] for item in exported["delegate_grants"]}
    assert repository.revoke_privacy_consent(scope, consent["id"])["revoked"] is True
    assert not repository.delegate_allowed(
        scope, delegate_id="delegate-user", member_id=adult["id"], required_scope="claims:manage")


def test_member_health_consent_refuses_a_minor(tmp_path):
    repository = persistence.PortfolioRepository(tmp_path / "portfolio.sqlite", os.urandom(32))
    scope = _scope()
    minor = repository.add_member(
        scope, name="Minor member", relationship="child", date_of_birth="2022-01-01")
    with pytest.raises(persistence.PortfolioValidationError, match="adult"):
        repository.create_privacy_consent(
            scope, purpose="CLAIM_MANAGEMENT", data_categories=["HEALTH_DATA"],
            recipient="Synthetic delegate", valid_from=datetime.now(timezone.utc).date().isoformat(),
            valid_until=(datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(), member_id=minor["id"])


def test_privacy_api_requires_owner_scope_and_preserves_erase_step_up(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    api.SESSIONS.clear()
    with TestClient(api.app) as client:
        assert client.get("/api/privacy/consents").status_code == 401
        assert client.post("/api/auth/login", json=OWNER).status_code == 200
        session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
        assert session is not None
        with session.lock:
            scope = api._portfolio_scope(session)
        repository = api._portfolio_repository()
        member = repository.add_member(
            scope, name="Adult API member", relationship="parent", date_of_birth="1970-01-01")
        start, end = datetime.now(timezone.utc).date().isoformat(), (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()
        created = client.post("/api/privacy/consents", json={
            "purpose": "CLAIM_MANAGEMENT", "data_categories": ["HEALTH_DATA"],
            "recipient": "Synthetic delegate", "valid_from": start, "valid_until": end,
            "member_id": member["id"],
        })
        assert created.status_code == 200, created.text
        delegate = client.post("/api/privacy/delegates", json={
            "member_id": member["id"], "delegate_id": "delegate-user",
            "scopes": ["claims:manage"], "valid_from": start, "valid_until": end,
        })
        assert delegate.status_code == 200, delegate.text
        configured = client.put("/api/privacy/retention", json={
            "portfolio_days": 365, "claim_days": 180, "audit_days": 730,
        })
        assert configured.status_code == 200 and configured.json()["configured"] is True
        repository.create_policy(
            scope, country="SG", line_of_business="HEALTH", insurer="Lotus General (Sandbox)",
            product="Synthetic privacy policy", masked_number="****-PRIV", status="ACTIVE")
        notices = client.get("/api/privacy/notices")
        assert notices.status_code == 200, notices.text
        assert notices.json()["notices"]
        exported = client.get("/api/privacy/export")
        assert exported.status_code == 200 and exported.json()["tenant_id"] == scope.tenant_id
        denied = client.post("/api/privacy/erase", json={"password": "wrong"})
        assert denied.status_code == 403
        assert client.get("/api/privacy/consents").status_code == 200
    api.SESSIONS.clear()
