"""T14 API surface: tenant-scoped Decimal health illustrations."""
from __future__ import annotations

import base64
import copy
import os

import pdfbuild_household
import pytest
from fastapi.testclient import TestClient

from proven import api, auth, health, persistence, sandbox

OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        assert instance.post("/api/auth/login", json=OWNER).status_code == 200
        session = api.SESSIONS.get(instance.cookies.get(api.SESSION_COOKIE))
        assert session is not None
        with session.lock:
            scope = api._portfolio_scope(session)
        repository = api._portfolio_repository()
        document = repository.add_document(scope, kind="SCHEDULE", blob=pdfbuild_household.india_health_schedule_pdf(),
                                           language="en", country="IN", filename="schedule.pdf")
        policy = repository.create_policy(scope, country="IN", line_of_business="HEALTH",
                                          insurer="Lotus General (Sandbox)", product="Synthetic", masked_number="****-1",
                                          status="ACTIVE", source_document_ids=[document["id"]])

        def enrich(portfolio):
            output = copy.deepcopy(portfolio)
            selected = next(item for item in output["policies"] if item["id"] == policy["id"])
            selected["health_items"] = [
                {"mechanism_code": "LIMIT.ROOM_RENT", "state": "DOCUMENTED", "value": "1000", "unit": "INR",
                 "citation": {"document": "Policy wording", "page": 6, "quote": "Room rent is limited."}},
                {"mechanism_code": "SHARE.CO_PAYMENT", "state": "DOCUMENTED", "value": "10", "unit": "%",
                 "citation": {"document": "Policy wording", "page": 11, "quote": "The insured shall bear ten per cent."}},
            ]
            return output

        monkeypatch.setattr(health, "enrich_portfolio", enrich)
        yield instance, policy
    api.SESSIONS.clear()


def test_api_runs_a_decimal_illustration_after_agent_read_consent(client):
    instance, policy = client
    assert instance.post("/api/agent/consent", json={"scopes": ["agent:read"]}).status_code == 200
    response = instance.post("/api/health/what-if", json={
        "policy_id": policy["id"], "loss": "10000", "room_charge_per_day": "1500",
        "days": "2", "deductible": "500",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "DETERMINISTIC"
    assert body["illustrated_insurer_portion"] == "7600.00"
    assert "amount payable" not in body["notice"].lower()


def test_api_refuses_a_figure_without_consent_or_with_json_float_money(client):
    instance, policy = client
    no_consent = instance.post("/api/health/what-if", json={
        "policy_id": policy["id"], "loss": "100", "room_charge_per_day": "100", "days": "1", "deductible": "0",
    })
    assert no_consent.status_code == 403
    assert instance.post("/api/agent/consent", json={"scopes": ["agent:read"]}).status_code == 200
    floating = instance.post("/api/health/what-if", json={
        "policy_id": policy["id"], "loss": 100.0, "room_charge_per_day": "100", "days": "1", "deductible": "0",
    })
    assert floating.status_code == 422
