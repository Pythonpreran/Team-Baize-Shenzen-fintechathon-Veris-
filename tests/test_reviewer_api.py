"""Reviewer-console role and encrypted-label API coverage."""
from __future__ import annotations

import base64
import json
import os

import pytest
from fastapi.testclient import TestClient

from proven import api, auth, persistence, reviewer, security


@pytest.fixture()
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIS_MODE", "hosted")
    monkeypatch.setenv(auth.HOSTED_AUTH_DB_ENV, str(tmp_path / "accounts.sqlite"))
    monkeypatch.setenv(auth.HOSTED_INVITES_ENV, json.dumps({
        "owner-invite": "SME", "reviewer-invite": "REVIEWER",
    }))
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(reviewer.REVIEW_DB_ENV, str(tmp_path / "reviews.sqlite"))
    monkeypatch.setenv(security.RECORD_HMAC_KEY_ENV, "test-only-record-integrity-material-32-bytes")
    api.SESSIONS.clear()
    auth.LOGIN_RATE_LIMITER.clear()
    yield tmp_path
    api.SESSIONS.clear()
    auth.LOGIN_RATE_LIMITER.clear()


def _csrf(client: TestClient) -> dict[str, str]:
    client.get("/api/auth/csrf")
    return {security.CSRF_HEADER: client.cookies.get(security.CSRF_COOKIE)}


def _signup(client: TestClient, code: str, email: str, *, credentials: str = "") -> dict:
    headers = _csrf(client)
    response = client.post("/api/auth/signup", headers=headers, json={
        "email": email, "password": "long-enough-test-password", "invite_code": code,
        "name": email.split("@")[0], "credentials": credentials,
    })
    assert response.status_code == 200, response.text
    return response.json()["user"]


def _login(client: TestClient, email: str) -> dict[str, str]:
    headers = _csrf(client)
    response = client.post("/api/auth/login", headers=headers, json={
        "email": email, "password": "long-enough-test-password",
    })
    assert response.status_code == 200, response.text
    return headers


def test_reviewer_console_scopes_queue_and_exports_human_labels(environment):
    with TestClient(api.app, base_url="https://testserver") as owner_client:
        owner = _signup(owner_client, "owner-invite", "owner@example.test")
        owner_headers = _login(owner_client, "owner@example.test")
        assert owner_client.get("/api/reviewer/queue").status_code == 403

    with TestClient(api.app, base_url="https://testserver") as reviewer_client:
        reviewer_user = _signup(reviewer_client, "reviewer-invite", "reviewer@example.test",
                                credentials="licensed reviewer")
        store = reviewer.store_from_environment()
        finding = store.enqueue(tenant_id=owner["organisation_id"],
                                reviewer_org_id=reviewer_user["organisation_id"], finding={
            "rule_id": "RULE-REVIEW", "output_state": "DOCUMENTED", "evidence_class": "PROVEN",
            "headline": "Synthetic reviewer finding", "evidence": [{"quote": "Exact citation", "page": 1}],
        })
        headers = _login(reviewer_client, "reviewer@example.test")
        page = reviewer_client.get("/reviewer")
        assert page.status_code == 200 and "reviewer-console" in page.text
        queue = reviewer_client.get("/api/reviewer/queue")
        assert queue.status_code == 200
        assert [item["id"] for item in queue.json()["findings"]] == [finding["id"]]
        decision = reviewer_client.post(
            f"/api/reviewer/findings/{finding['id']}/review", headers=headers,
            json={"decision": "AGREE", "severity": "HIGH", "comment": "Checked."})
        assert decision.status_code == 200, decision.text
        exported = reviewer_client.get("/api/reviewer/export")
        assert exported.status_code == 200
        assert exported.json()["exportable_count"] == 1
        assert exported.json()["labels"][0]["derivation"] == "REVIEWED_BY_EXPERT"
