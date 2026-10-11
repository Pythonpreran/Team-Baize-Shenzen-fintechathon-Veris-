"""T16 India motor alerts and policy-schedule depreciation illustrations."""
from __future__ import annotations

from datetime import date

from proven import motor


def _policy() -> dict:
    return {
        "source": "SYNTHETIC_EXAMPLE", "country": "IN", "line_of_business": "MOTOR",
        "product": "Private car plan (synthetic schedule)", "masked_number": "****-MOTOR7",
        "source_document_ids": ["motor-doc"], "period": {"end": "2026-10-18"},
    }


def test_india_third_party_expiry_alert_uses_a_sourced_statute_and_omits_unsourced_penalty():
    alert = motor.third_party_alert(_policy(), as_of=date(2026, 10, 9))
    assert alert["state"] == "EXPIRING"
    assert alert["days_to_expiry"] == 9
    assert alert["source"]["status"] == "SOURCE_QUOTED"
    assert alert["source"]["quote"]
    assert alert["penalty"] is None
    assert "not yet verified" in alert["penalty_notice"].lower()


def test_expired_synthetic_motor_period_is_a_compulsory_gap_only_with_sourced_statute():
    alert = motor.third_party_alert(_policy(), as_of=date(2026, 10, 19))
    assert alert["state"] == "COMPULSORY_GAP"
    assert alert["source"]["owner_facing"] is True


def test_depreciation_what_if_uses_the_policy_schedule_table_with_decimal_money():
    result = motor.depreciation_what_if(_policy(), part_material="plastic", repair_cost="10000.00")
    assert result["output_state"] == "DOCUMENTED"
    assert result["illustration"] is True
    assert result["lines"][1]["rate"] == "50.00%"
    assert result["lines"][2]["amount"] == "5000.00"
    assert "amount payable" not in result["notice"].lower()


def test_unbound_india_motor_policy_cannot_inherit_rao_schedule_evidence():
    unbound = {
        **_policy(), "source": "PORTFOLIO", "source_document_ids": ["owner-doc"],
    }
    assert motor.third_party_alert(unbound)["state"] == "INFORMATION_MISSING"
    assert motor.depreciation_what_if(unbound, part_material="plastic", repair_cost="100")["output_state"] == "INFORMATION_MISSING"
    enriched = motor.enrich_portfolio({"policies": [unbound]})["policies"][0]
    assert enriched["motor_items"] == []
    assert enriched["motor_alert"]["state"] == "INFORMATION_MISSING"


def test_motor_portfolio_enrichment_keeps_bound_policy_evidence_and_alert_separate():
    enriched = motor.enrich_portfolio({"policies": [_policy()]})["policies"][0]
    assert enriched["motor_alert"]["days_to_expiry"] == 9
    assert {item["mechanism_code"] for item in enriched["motor_items"]} == {
        "COVERED.MOTOR_THIRD_PARTY", "RENEWAL.THIRD_PARTY"}
    assert all(item["evidence_class"] == "PROVEN" for item in enriched["motor_items"])
