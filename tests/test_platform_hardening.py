"""Hosted-mode security boundaries added for the final delivery package."""
from __future__ import annotations

import base64
import json
import sqlite3

import pdfbuild
import pyotp
import pytest
from fastapi.testclient import TestClient

from proven import api, auth, ingest, persistence, pipeline, sandbox, security
from proven.db import init_db
from proven.errors import PdfParseTimeout


@pytest.fixture()
def hosted_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIS_MODE", "hosted")
    monkeypatch.setenv(auth.HOSTED_AUTH_DB_ENV, str(tmp_path / "hosted-auth.sqlite"))
    monkeypatch.setenv(auth.HOSTED_INVITES_ENV, json.dumps({
        "judge-invite": "SME", "reviewer-invite": "REVIEWER",
    }))
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(
        persistence.MASTER_KEY_ENV,
        base64.urlsafe_b64encode(b"p" * 32).decode("ascii"),
    )
    monkeypatch.setenv(security.RECORD_HMAC_KEY_ENV, "test-only-record-integrity-material-32-bytes")
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox-operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, base64.urlsafe_b64encode(b"s" * 32).decode("ascii"))
    api.SESSIONS.clear()
    auth.LOGIN_RATE_LIMITER.clear()
    with TestClient(api.app, base_url="https://testserver") as client:
        yield client
    api.SESSIONS.clear()
    auth.LOGIN_RATE_LIMITER.clear()


def _csrf(client: TestClient) -> dict[str, str]:
    response = client.get("/api/auth/csrf")
    assert response.status_code == 200
    token = client.cookies.get(security.CSRF_COOKIE)
    assert token
    return {security.CSRF_HEADER: token}


def _signup_and_login(client: TestClient) -> dict[str, str]:
    headers = _csrf(client)
    signup = client.post("/api/auth/signup", headers=headers, json={
        "email": "judge@example.test", "password": "long-enough-test-password",
        "invite_code": "judge-invite", "name": "Judge Account",
    })
    assert signup.status_code == 200, signup.text
    login = client.post("/api/auth/login", headers=headers, json={
        "email": "judge@example.test", "password": "long-enough-test-password",
    })
    assert login.status_code == 200, login.text
    return _csrf(client)


def test_hosted_workspace_requires_login_and_hides_interactive_docs(hosted_client):
    anonymous = hosted_client.get("/api/session")
    assert anonymous.status_code == 401
    assert hosted_client.get("/app").status_code == 401
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert hosted_client.get(path).status_code == 404

    health = hosted_client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["authenticated"] is True
    assert health.headers["Strict-Transport-Security"].startswith("max-age=")
    assert "Access-Control-Allow-Origin" not in health.headers


def test_hosted_signup_login_uses_secure_cookies_and_csrf(hosted_client):
    headers = _signup_and_login(hosted_client)
    assert hosted_client.get("/api/session").status_code == 200
    cookie_header = hosted_client.post("/api/auth/logout", headers=headers).headers.get("set-cookie", "")
    assert "secure" in cookie_header.lower()
    assert "httponly" in cookie_header.lower()


def test_hosted_rejects_unsafe_requests_without_matching_csrf(hosted_client):
    _signup_and_login(hosted_client)
    refused = hosted_client.post("/api/auth/logout")
    assert refused.status_code == 403
    assert "csrf" in refused.json()["detail"].lower()


def test_hosted_auth_store_locks_after_repeated_bad_passwords(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.HOSTED_INVITES_ENV, json.dumps({"single-use": "SME"}))
    store = auth.HostedAccountStore(tmp_path / "auth.sqlite")
    store.signup(email="owner@example.test", password="long-enough-test-password",
                 invite_code="single-use", name="Owner")
    for _ in range(auth.LOGIN_FAILURE_LIMIT):
        result = store.authenticate("owner@example.test", "wrong-password", now=1000)
    assert result.principal is None and result.locked is True
    assert store.authenticate("owner@example.test", "long-enough-test-password", now=1001).locked


def test_login_rate_limiter_is_bounded_per_account_and_trusted_client():
    limiter = auth.LoginRateLimiter(limit=1, window_seconds=60)
    first = auth.login_rate_limit_keys("first@example.test", "198.51.100.1")
    second = auth.login_rate_limit_keys("second@example.test", "198.51.100.2")
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert "first@example.test" not in first[0]
    assert limiter.allow(*first, now=100)
    assert not limiter.allow(*first, now=101)
    assert limiter.allow(*second, now=101)
    assert limiter.allow(*first, now=161)


def test_hosted_login_does_not_trust_direct_forwarded_headers(hosted_client, monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_RATE_LIMITER", auth.LoginRateLimiter(limit=1, window_seconds=60))
    headers = _csrf(hosted_client)
    first = hosted_client.post("/api/auth/login", headers={
        **headers, "X-Forwarded-For": "198.51.100.10",
    }, json={"email": "one@example.test", "password": "wrong-password"})
    second = hosted_client.post("/api/auth/login", headers={
        **headers, "X-Forwarded-For": "203.0.113.20",
    }, json={"email": "two@example.test", "password": "wrong-password"})
    assert first.status_code == 401
    assert second.status_code == 429


def test_hosted_account_can_enrol_totp_then_confirm_a_tier_two_action(hosted_client):
    headers = _signup_and_login(hosted_client)
    assert hosted_client.post("/api/agent/consent", headers=headers, json={
        "scopes": ["agent:act"],
    }).status_code == 200
    refused = hosted_client.post("/api/agent/totp/enrol", headers=headers, json={
        "password": "wrong-password",
    })
    assert refused.status_code == 403
    enrolled = hosted_client.post("/api/agent/totp/enrol", headers=headers, json={
        "password": "long-enough-test-password",
    })
    assert enrolled.status_code == 200, enrolled.text
    code = pyotp.TOTP(enrolled.json()["secret"]).now()
    prepared = hosted_client.post("/api/agent/prepare", headers=headers, json={
        "tool_name": "endorsement.request",
        "payload": {"policy_id": "synthetic-health-policy"},
    })
    assert prepared.status_code == 200, prepared.text
    pending = prepared.json()["pending_action"]
    confirmed = hosted_client.post("/api/agent/confirm", headers=headers, json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"], "totp_code": code,
    })
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["state"] == "SENT"
    assert confirmed.json()["result"]["counterparty"] == "SYNTHETIC_SANDBOX"


def test_hosted_outbound_allowlist_rejects_unreviewed_destination(monkeypatch):
    monkeypatch.setenv("VERIS_MODE", "hosted")
    with pytest.raises(security.SecurityConfigurationError):
        security.require_outbound_url("http://127.0.0.1:9999/private", purpose="test")
    security.require_outbound_url("https://api.openai.com/v1/chat/completions", purpose="test")


def test_hosted_pdf_parse_timeout_fails_closed(monkeypatch):
    monkeypatch.setenv("VERIS_MODE", "hosted")
    monkeypatch.setenv(security.PDF_PARSE_TIMEOUT_ENV, "0.001")
    with pytest.raises(PdfParseTimeout):
        ingest.extract_pdf_pages(pdfbuild.schedule_pdf(), "slow.pdf")


def test_portable_record_hmac_detects_export_tampering():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    # The record writer only needs an immutable basis identifier for this structural test.
    pipeline.append_record_fact(connection, "business", "registered_name", "Example",
                                "document", "INSURER")
    key = b"test-only-record-integrity-material-32-bytes"
    exported = pipeline.export_record(connection, "business", hmac_key=key)
    assert exported["verification"]["chain_valid"] is True
    assert exported["verification"]["hmac_valid"] is True
    exported["facts"][0]["value"] = "Changed"
    verified = pipeline.verify_record(exported, hmac_key=key)
    assert verified["hmac_valid"] is False


def test_front_end_sends_csrf_token_through_the_api_transport():
    source = (api.WEB_DIR / "app.js").read_text(encoding="utf-8")
    assert "function apiHeaders(" in source
    assert "X-CSRF-Token" in source
    assert 'headers: apiHeaders({}, "POST")' in source
