"""PRIORITY 2B — the claim scenario simulator.

The screen that converts "66.7% adequate" into "you personally absorb S$234,300".
Deterministic arithmetic in a fixed order, every line sourced, and an honest abstention
when the evidence is missing.
"""
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from proven import scenario, pipeline
from proven.api import SESSIONS, app


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_worked_example_reproduces_exactly():
    """The demo figure the checklist pins: 300k loss, 100k SI, 150k VAR, 1k deductible
    → insurer 65,700, owner absorbs 234,300."""
    w = scenario.compute_waterfall(D("300000"), D("100000"), D("150000"), D("1000"))
    assert w["gross_claim"] == "100000.00"          # capped at sum insured
    assert w["average_ratio"] == "66.7%"
    assert w["post_average"] == "66700.00"
    assert w["final_illustration"] == "65700.00"
    assert w["owner_shortfall"] == "234300.00"


def test_deductible_comes_after_average_not_before():
    """Order matters. Applying the deductible before average would give a different,
    higher figure; the engine must subtract it last."""
    w = scenario.compute_waterfall(D("300000"), D("100000"), D("150000"), D("1000"))
    # post_average is computed on the capped amount, THEN the deductible comes off
    assert D(w["post_average"]) == D("66700.00")
    assert D(w["final_illustration"]) == D(w["post_average"]) - D("1000")


def test_sub_limit_caps_before_average():
    w = scenario.compute_waterfall(D("300000"), D("100000"), D("150000"), D("1000"),
                                   sub_limit=D("50000"))
    assert w["post_sublimit"] == "50000.00"
    # average then applies to the sub-limited amount
    assert w["post_average"] == "33350.00"          # 50,000 × 0.667


def test_fully_insured_shows_zero_shortfall():
    """Sum insured >= value at risk, loss within the sum insured, no deductible →
    the owner absorbs nothing."""
    w = scenario.compute_waterfall(D("40000"), D("200000"), D("150000"), D("0"))
    assert w["average_ratio_decimal"] == "1.000"    # ratio capped at 1.0
    assert w["final_illustration"] == "40000.00"
    assert w["owner_shortfall"] == "0.00"


def test_zero_value_at_risk_raises():
    with pytest.raises(ValueError):
        scenario.compute_waterfall(D("1"), D("1"), D("0"), D("0"))


# ── simulate() over the real demo case, with provenance ──────────────────────

def test_simulate_demo_case_every_line_has_provenance():
    st = pipeline.run_case()
    result = scenario.simulate(st, peril="fire", loss_amount=D("300000"))
    assert result["abstained"] is False
    assert result["steps"]
    for step in result["steps"]:
        prov = step["provenance"]
        # every line points at a document/operand — none is unsourced
        assert prov.get("operand") or prov.get("document_kind") or prov.get("note")
    # the headline shortfall is present and correct for the demo deductible (5,000)
    # 100,000 × 0.667 = 66,700 − 5,000 = 61,700 ; absorb 300,000 − 61,700 = 238,300
    assert result["final_illustration"] == "61700.00"
    assert result["owner_shortfall"] == "238300.00"


def test_simulate_carries_the_disclaimer():
    st = pipeline.run_case()
    result = scenario.simulate(st, peril="fire", loss_amount=D("100000"))
    assert result["disclaimer"] == \
        "Deterministic illustration — not a guaranteed claim payout."


def test_missing_sum_insured_abstains_not_guesses():
    """Plant has a sum insured but no value at risk on record → abstain."""
    st = pipeline.run_case()
    result = scenario.simulate(st, peril="plant", loss_amount=D("100000"))
    assert result["abstained"] is True
    assert result["message"] == "Cannot calculate from the evidence provided."
    assert "plant" in " ".join(result["missing_operands"]).lower() or \
        result["missing_operands"]


def test_simulate_provenance_quotes_are_verbatim():
    st = pipeline.run_case()
    result = scenario.simulate(st, peril="fire", loss_amount=D("300000"))
    si = result["provenance"]["sum_insured"]
    assert si["quote"] and si["page"] and si["document_kind"] == "SCHEDULE"


# ── the endpoints ────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


def test_scenario_endpoint_returns_waterfall(client):
    body = client.get("/api/scenario", params={"peril": "fire",
                                               "loss": 300000}).json()
    assert body["abstained"] is False
    assert body["owner_shortfall"] == "238300.00"
    assert body["order_applied"].startswith("sum insured cap")
    assert "not a guaranteed" in body["disclaimer"]


def test_scenario_endpoint_abstains_for_plant(client):
    body = client.get("/api/scenario", params={"peril": "plant",
                                               "loss": 100000}).json()
    assert body["abstained"] is True


def test_scenario_presets_endpoint(client):
    body = client.get("/api/scenario/presets", params={"peril": "fire"}).json()
    assert body["available"] is True
    sizes = {s["size"] for s in body["scenarios"]}
    assert {"small", "typical", "total"} <= sizes
    for s in body["scenarios"]:
        assert s["result"]["abstained"] is False
