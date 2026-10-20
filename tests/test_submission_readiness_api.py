"""Local preflight and evaluator-evidence export contracts."""
from __future__ import annotations

import base64
import json
import os

import pdfbuild_household
import pytest
from fastapi.testclient import TestClient

from proven import api, auth, household, household_loader, persistence, sandbox


OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox.operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    monkeypatch.setenv("VERIS_MODE", "local")
    monkeypatch.setenv("PROVEN_LIVE_SOURCES", "0")
    monkeypatch.setenv("VERIS_LLM_PROFILE", "none")
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        assert instance.post("/api/auth/login", json=OWNER).status_code == 200
        yield instance
    api.SESSIONS.clear()


def _scope(client: TestClient) -> persistence.TenantScope:
    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    with session.lock:
        return api._portfolio_scope(session)


def _load_household_fixture(client: TestClient) -> None:
    sources = {
        name: builder()
        for name, builder in pdfbuild_household.BUILDERS.items()
    }
    household_loader.load_example(api._portfolio_repository(), _scope(client), sources)


def test_preflight_reports_only_non_secret_local_prerequisites(client, tmp_path):
    response = client.get("/api/demo/preflight")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["format"] == "veris.demo-preflight.v1"
    assert body["role"] == auth.SME
    assert body["storage"]["configured"] is True
    assert body["fixtures"]["household_fixture_expected"] == len(household.DOCUMENTS)
    assert body["fixtures"]["household_fixture_complete"] is True
    assert body["offline_boundary"] == {
        "mode": "local",
        "local_mode": True,
        "live_sources_disabled": True,
        "deterministic_only": True,
        "external_services_checked": False,
        "notice": body["offline_boundary"]["notice"],
    }
    serialized = json.dumps(body)
    assert str(tmp_path) not in serialized
    assert '"master_key":' not in serialized
    assert "tenant_id" not in serialized
    assert "not a readiness assertion" in body["notice"]


def test_preflight_requires_the_owner_role(client):
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/demo/preflight").status_code == 401
    assert client.post("/api/auth/login", json={
        "email": "adviser@lionbrokers.demo", "password": auth.DEMO_PASSWORD,
    }).status_code == 200
    assert client.get("/api/demo/preflight").status_code == 403


def test_evaluation_export_projects_existing_tenant_evidence_and_action_state(client):
    _load_household_fixture(client)
    assert client.post("/api/agent/consent", json={"scopes": ["agent:act"]}).status_code == 200
    prepared = client.post("/api/agent/prepare", json={
        "tool_name": "endorsement.request", "payload": {"policy_id": "synthetic-policy"},
    })
    assert prepared.status_code == 200, prepared.text

    response = client.get("/api/evaluation/evidence-export")
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()

    assert body["format"] == "veris.evaluation-evidence.v1"
    assert body["scope"]["access"] == "Signed-in SME tenant only."
    assert "professional insurance validation" in body["scope"]["limitations"][0]
    assert len(body["portfolio_evidence"]) == sum(
        len(policy.coverage) for policy in household.POLICIES)
    cited = [item for item in body["portfolio_evidence"] if item["citation"]]
    assert cited
    assert all(item["citation"]["kind"] == "SYNTHETIC_SCHEDULE_QUOTE" for item in cited)
    receipt = body["action_receipt"]
    assert receipt["consent"]["granted"] is True
    assert receipt["pending_confirmations"]
    assert receipt["pending_confirmations"][0]["preview"]["tenant_scoped"] is True
    assert receipt["ledger_verification"]["valid"] is True
    assert body["session_findings"]["available"] is False


def test_evaluation_export_refuses_an_anonymous_or_adviser_session(client):
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/evaluation/evidence-export").status_code == 401
    assert client.post("/api/auth/login", json={
        "email": "adviser@lionbrokers.demo", "password": auth.DEMO_PASSWORD,
    }).status_code == 200
    assert client.get("/api/evaluation/evidence-export").status_code == 403
