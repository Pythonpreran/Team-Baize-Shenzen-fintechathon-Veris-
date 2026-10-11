"""Local hosted-mode smoke check; it never performs a real deployment or network call."""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _csrf(client, security) -> dict[str, str]:
    response = client.get("/api/auth/csrf")
    assert response.status_code == 200
    token = client.cookies.get(security.CSRF_COOKIE)
    assert token
    return {security.CSRF_HEADER: token}


def main() -> int:
    import pyotp
    from fastapi.testclient import TestClient

    from proven import api, auth, persistence, sandbox, security
    from proven.sandbox_transport import SandboxHttpConnector
    from sandbox.app import app as sandbox_app

    names = (
        "VERIS_MODE", auth.HOSTED_AUTH_DB_ENV, auth.HOSTED_INVITES_ENV,
        persistence.PORTFOLIO_DB_ENV, persistence.MASTER_KEY_ENV,
        security.RECORD_HMAC_KEY_ENV, sandbox.SANDBOX_URL_ENV,
        sandbox.SANDBOX_DB_ENV, sandbox.SANDBOX_LOG_ENV, sandbox.SANDBOX_KEY_ENV,
        sandbox.SANDBOX_CLIENT_ID_ENV, sandbox.SANDBOX_CLIENT_SECRET_ENV,
        "PROVEN_LIVE_SOURCES",
    )
    original = {name: os.environ.get(name) for name in names}
    original_connector = api._sandbox_connector
    try:
        with tempfile.TemporaryDirectory(prefix="veris-hosted-smoke-") as directory:
            root = Path(directory)
            invite = secrets.token_urlsafe(24)
            master_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            sandbox_secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
            os.environ.update({
                "VERIS_MODE": "hosted",
                auth.HOSTED_AUTH_DB_ENV: str(root / "auth.sqlite"),
                auth.HOSTED_INVITES_ENV: json.dumps({invite: "SME"}),
                persistence.PORTFOLIO_DB_ENV: str(root / "portfolio.sqlite"),
                persistence.MASTER_KEY_ENV: master_key,
                security.RECORD_HMAC_KEY_ENV: secrets.token_urlsafe(32),
                sandbox.SANDBOX_URL_ENV: "http://sandbox:8200",
                sandbox.SANDBOX_DB_ENV: str(root / "sandbox.sqlite"),
                sandbox.SANDBOX_LOG_ENV: str(root / "sandbox-operations.jsonl"),
                sandbox.SANDBOX_KEY_ENV: base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
                sandbox.SANDBOX_CLIENT_ID_ENV: "hosted-smoke-service",
                sandbox.SANDBOX_CLIENT_SECRET_ENV: sandbox_secret,
                "PROVEN_LIVE_SOURCES": "0",
            })
            api.SESSIONS.clear()
            auth.LOGIN_RATE_LIMITER.clear()
            with TestClient(sandbox_app) as counterparty:
                connector = SandboxHttpConnector(client=counterparty)
                api._sandbox_connector = lambda: connector
                with TestClient(api.app, base_url="https://testserver") as client:
                    health = client.get("/api/health")
                    assert health.status_code == 200 and health.headers.get("Strict-Transport-Security")
                    assert client.get("/api/session").status_code == 401
                    csrf = _csrf(client, security)
                    signup = client.post("/api/auth/signup", headers=csrf, json={
                        "email": "smoke@example.test", "password": "long-enough-smoke-password",
                        "invite_code": invite, "name": "Smoke account",
                    })
                    assert signup.status_code == 200, signup.text
                    login = client.post("/api/auth/login", headers=csrf, json={
                        "email": "smoke@example.test", "password": "long-enough-smoke-password",
                    })
                    assert login.status_code == 200, login.text
                    assert client.get("/api/session").status_code == 200
                    csrf = _csrf(client, security)
                    assert client.post("/api/auth/logout").status_code == 403
                    csrf = _csrf(client, security)
                    assert client.post("/api/agent/consent", headers=csrf, json={
                        "scopes": ["agent:act"],
                    }).status_code == 200
                    rejected = client.post("/api/agent/totp/enrol", headers=csrf, json={
                        "password": "wrong-password",
                    })
                    assert rejected.status_code == 403
                    enrolled = client.post("/api/agent/totp/enrol", headers=csrf, json={
                        "password": "long-enough-smoke-password",
                    })
                    assert enrolled.status_code == 200, enrolled.text
                    code = pyotp.TOTP(enrolled.json()["secret"]).now()
                    prepared = client.post("/api/agent/prepare", headers=csrf, json={
                        "tool_name": "endorsement.request",
                        "payload": {"policy_id": "synthetic-health-policy"},
                    })
                    assert prepared.status_code == 200, prepared.text
                    pending = prepared.json()["pending_action"]
                    confirmed = client.post("/api/agent/confirm", headers=csrf, json={
                        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
                        "totp_code": code,
                    })
                    assert confirmed.status_code == 200, confirmed.text
                    result = confirmed.json()["result"]
                    assert result["accepted"] is True
                    assert result["counterparty"] == "SYNTHETIC_SANDBOX"
        traces = json.loads((ROOT / "evidence" / "agent-traces" / "canonical-task-traces.json").read_text(encoding="utf-8"))
        assert len(traces) == 12
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        api._sandbox_connector = original_connector
        api.SESSIONS.clear()
        auth.LOGIN_RATE_LIMITER.clear()
    print("Hosted-mode local smoke passed: invite login, first TOTP enrollment and a confirmed private synthetic action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
