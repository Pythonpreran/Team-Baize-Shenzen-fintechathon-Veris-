"""T14 Decimal-only health what-if illustrations and abstention gates."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from hypothesis import given, strategies as st

from proven import health_whatif


def _india_policy() -> dict:
    return {"country": "IN", "health_items": [
        {"mechanism_code": "LIMIT.ROOM_RENT", "state": "DOCUMENTED", "value": "1000",
         "unit": "INR", "citation": {"document": "Policy wording", "page": 6,
                                        "quote": "Room rent is limited to one thousand rupees per day."}},
        {"mechanism_code": "SHARE.CO_PAYMENT", "state": "DOCUMENTED", "value": "10",
         "unit": "%", "citation": {"document": "Policy wording", "page": 11,
                                        "quote": "The insured shall bear ten per cent."}},
    ]}


def _singapore_policy() -> dict:
    def item(code: str, value: str, unit: str, quote: str) -> dict:
        return {"mechanism_code": code, "state": "DOCUMENTED", "value": value, "unit": unit,
                "citation": {"document": "Synthetic Singapore policy wording", "page": 4, "quote": quote}}

    return {"country": "SG", "health_items": [
        item("SHARE.DEDUCTIBLE", "1000", "SGD", "A deductible of S$1,000 applies."),
        item("SHARE.CO_INSURANCE", "10", "%", "Co-insurance is ten per cent."),
        item("CONDITION.PRO_RATION", "0.8", "factor", "The ward pro-ration factor is 0.8."),
        item("SHARE.RIDER_CO_PAYMENT", "20", "%", "Rider co-payment is twenty per cent."),
    ]}


def test_india_room_cap_and_copayment_are_a_cited_illustration():
    result = health_whatif.india(
        _india_policy(), loss="10000", room_charge_per_day="1500", days="2",
        deductible="500", order_basis="STATED_IN_WORDING")
    assert result["output_state"] == "DOCUMENTED"
    assert result["illustration"] is False
    assert result["illustrated_insurer_portion"] == "7600.00"
    assert result["illustrated_owner_portion"] == "2400.00"
    assert len(result["citations"]) == 2
    assert all("amount payable" not in line["label"].lower() for line in result["lines"])


def test_missing_policy_operand_produces_no_figure():
    policy = _india_policy()
    policy["health_items"] = policy["health_items"][:1]
    result = health_whatif.india(policy, loss="10000", room_charge_per_day="1500", days="2",
                                 deductible="500")
    assert result["output_state"] == "INFORMATION_MISSING"
    assert result["illustrated_insurer_portion"] is None
    assert "co-payment" in result["missing_operands"][0]


@given(loss=st.decimals(min_value=Decimal("0"), max_value=Decimal("1000000"), places=2),
       room=st.decimals(min_value=Decimal("0"), max_value=Decimal("10000"), places=2),
       days=st.integers(min_value=0, max_value=30),
       deductible=st.decimals(min_value=Decimal("0"), max_value=Decimal("10000"), places=2))
def test_india_illustrated_insurer_portion_never_exceeds_the_loss(loss, room, days, deductible):
    result = health_whatif.india(_india_policy(), loss=str(loss), room_charge_per_day=str(room),
                                 days=str(days), deductible=str(deductible))
    assert Decimal(result["illustrated_insurer_portion"]) <= loss
    assert Decimal(result["illustrated_owner_portion"]) >= Decimal("0")


def test_singapore_refuses_an_empty_policy_even_when_callers_supply_values():
    result = health_whatif.singapore(
        {"country": "SG", "health_items": []}, loss="10000", deductible="1000",
        coinsurance_percent="10", proration_factor="0.8", rider_copayment_percent="20")
    assert result["output_state"] == "INFORMATION_MISSING"
    assert result["illustrated_insurer_portion"] is None
    assert result["citations"] == []


def test_singapore_waterfall_requires_cited_policy_operands_and_labels_market_convention():
    result = health_whatif.singapore(_singapore_policy(), loss="10000")
    assert result["output_state"] == "DOCUMENTED"
    assert result["illustration"] is True
    assert result["basis"] == "MARKET_CONVENTION"
    assert result["illustrated_insurer_portion"] == "5184.00"
    assert len(result["citations"]) == 4
    assert result["lines"][1]["evidence_class"] == "PROVEN"
    assert "does not decide a claim outcome" in result["notice"].lower()


def test_what_if_engine_has_no_model_dependency():
    source = (Path(__file__).resolve().parents[1] / "src" / "proven" / "health_whatif.py").read_text(encoding="utf-8")
    assert "llm" not in source.lower()
    assert "float" in source  # explicit rejection, not silent conversion
