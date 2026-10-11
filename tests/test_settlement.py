"""T15 deterministic settlement check, non-adjudicating draft and reconciliation."""
from __future__ import annotations

from proven import settlement


def _policy() -> dict:
    return {"country": "IN", "health_items": [
        {"mechanism_code": "LIMIT.ROOM_RENT", "state": "DOCUMENTED", "value": "1000", "unit": "INR",
         "citation": {"document": "Policy wording", "page": 6, "quote": "Room rent is limited."}},
        {"mechanism_code": "SHARE.CO_PAYMENT", "state": "DOCUMENTED", "value": "5", "unit": "%",
         "citation": {"document": "Policy wording", "page": 11, "quote": "The insured shall bear five per cent."}},
        {"mechanism_code": "CONDITION.PROPORTIONATE_DEDUCTION", "state": "DOCUMENTED",
         "citation": {"document": "Policy wording", "page": 12,
                      "quote": "A proportionate deduction applies only to associated medical expenses."}},
    ]}


def _sandbox_settlement() -> dict:
    return {"proposed_payable": "42500.00", "deductions": [
        {"id": "co-payment", "amount": "2500.00", "status": "SUPPORTED_BY_SCENARIO",
         "evidence_mechanism": "SHARE.CO_PAYMENT"},
        {"id": "room-category", "amount": "5000.00", "status": "NOT_FOUND_IN_SCENARIO_WORDING",
         "evidence_mechanism": "CONDITION.PROPORTIONATE_DEDUCTION",
         "evidence_mode": "ABSENCE_FROM_DEFINED_SCOPE"},
    ]}


def test_planted_unsupported_deduction_is_classified_without_adjudicating():
    checked = settlement.check(_policy(), scenario_inputs={
        "loss": "50000", "room_charge_per_day": "1000", "days": "5", "deductible": "0",
        "proportionate_factor": "0",
    }, sandbox_settlement=_sandbox_settlement())
    assert checked["state"] == "READY_FOR_REVIEW"
    assert checked["expected_illustration"] == "47500.00"
    by_id = {row["id"]: row for row in checked["deductions"]}
    assert by_id["co-payment"]["classification"] == "SUPPORTED"
    assert by_id["room-category"]["classification"] == "NOT_FOUND"
    assert by_id["room-category"]["citation"]["quote"] == (
        "A proportionate deduction applies only to associated medical expenses.")
    assert "insurer is wrong" not in checked["draft"]["text"].lower()


def test_unsupported_sandbox_label_is_downgraded_without_a_verified_definition():
    policy = _policy()
    policy["health_items"] = policy["health_items"][:2]
    checked = settlement.check(policy, scenario_inputs={
        "loss": "50000", "room_charge_per_day": "1000", "days": "5", "deductible": "0",
    }, sandbox_settlement=_sandbox_settlement())
    by_id = {row["id"]: row for row in checked["deductions"]}
    assert by_id["room-category"]["classification"] == "NEEDS_REVIEW"
    assert by_id["room-category"]["citation"] is None
    assert checked["draft"]["deductions"] == []


def test_settlement_check_abstains_when_an_illustration_operand_is_missing():
    policy = _policy()
    policy["health_items"] = policy["health_items"][:1]
    checked = settlement.check(policy, scenario_inputs={
        "loss": "50000", "room_charge_per_day": "1000", "days": "5", "deductible": "0",
    }, sandbox_settlement=_sandbox_settlement())
    assert checked["state"] == "INFORMATION_MISSING"
    assert checked["draft"] is None


def test_reconciliation_closes_the_sandbox_loop_only_when_decimal_amounts_match():
    closed = settlement.reconcile("47500.00", "47500.00")
    assert closed["state"] == "RECONCILED"
    assert closed["difference"] == "0.00"
    open_item = settlement.reconcile("47500.00", "42500.00")
    assert open_item["state"] == "REQUIRES_AUTHORISED_REVIEW"
    assert open_item["difference"] == "5000.00"
