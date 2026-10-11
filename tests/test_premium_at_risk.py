"""The former illustrative premium calculator stays removed and the carrier refuses safely."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proven import api, auth

ROOT = Path(__file__).resolve().parents[1]
CARRIER = {"email": "underwriter@carrier.demo", "password": auth.DEMO_PASSWORD}


@pytest.fixture()
def client():
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        assert instance.post("/api/auth/login", json=CARRIER).status_code == 200
        yield instance
    api.SESSIONS.clear()


def _body(client: TestClient) -> dict:
    response = client.get("/api/carrier/premium-at-risk")
    assert response.status_code == 200, response.text
    return response.json()


def test_the_illustrative_premium_module_is_removed():
    assert not (ROOT / "src" / "proven" / "premium_at_risk.py").exists()


def test_carrier_premium_surface_is_role_guarded():
    api.SESSIONS.clear()
    with TestClient(api.app) as anonymous:
        assert anonymous.get("/api/carrier/premium-at-risk").status_code == 401
    api.SESSIONS.clear()


def test_carrier_surface_refuses_instead_of_returning_a_premium(client):
    body = _body(client)
    assert body["available"] is False
    assert body["operation"] == "premium_at_risk"


def test_refusal_names_the_missing_operands(client):
    body = _body(client)
    text = " ".join(str(value) for value in body.values()).lower()
    assert "rate" in text
    assert "gap" in text


def test_refusal_carries_no_money_result_fields(client):
    body = _body(client)
    forbidden = {"total_premium_at_risk_sgd", "total_exposure_gap_sgd", "rates_used", "gaps"}
    assert not (forbidden & set(body))


def test_refusal_does_not_describe_a_quote_or_premium_result(client):
    body = _body(client)
    text = " ".join(str(value) for value in body.values()).lower()
    assert "quotation" not in text
    assert "premium rate" not in text


def test_refusal_explains_what_is_real_today(client):
    body = _body(client)
    assert body["what_is_real_today"]
    assert "wording portfolio" in body["what_is_real_today"].lower()


def test_refusal_counts_only_real_case_rows(client):
    body = _body(client)
    assert isinstance(body["accounts_counted"], int)
    assert body["accounts_counted"] >= 0


def test_frontend_has_no_dormant_implied_premium_renderer():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "total_premium_at_risk_sgd" not in script
    assert "implied_premium_sgd" not in script
