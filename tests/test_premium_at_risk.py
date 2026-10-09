"""Tests for the carrier premium-at-risk computation module.

The module was written to say "accounts have drifted, here is the premium that should
be on the book and isn't". The API no longer serves that figure — the route refuses and
names the missing operands — but the module is still in the tree, so its arithmetic is
still pinned until it is removed.
"""
from decimal import Decimal

import pytest

from proven import premium_at_risk


def test_compute_produces_a_headline_number():
    result = premium_at_risk.compute_premium_at_risk(premium_at_risk.DEMO_PORTFOLIO)
    assert "headline" in result
    assert "premium" in result["headline"].lower()
    assert Decimal(result["total_premium_at_risk_sgd"]) > 0


def test_accounts_with_no_gap_are_not_counted_as_drifted():
    result = premium_at_risk.compute_premium_at_risk(premium_at_risk.DEMO_PORTFOLIO)
    # Bugis Print and Bishan Tech have no exposures
    assert result["accounts_drifted"] < result["accounts_total"]


def test_total_premium_is_sum_of_individual_gaps():
    result = premium_at_risk.compute_premium_at_risk(premium_at_risk.DEMO_PORTFOLIO)
    total = sum(Decimal(g["implied_premium_sgd"]) for g in result["gaps"])
    assert total == Decimal(result["total_premium_at_risk_sgd"])


def test_by_interest_breakdown_sums_correctly():
    result = premium_at_risk.compute_premium_at_risk(premium_at_risk.DEMO_PORTFOLIO)
    total_from_interests = sum(
        Decimal(v["premium_sgd"]) for v in result["by_interest"].values())
    assert total_from_interests == Decimal(result["total_premium_at_risk_sgd"])


def test_rates_are_labelled_as_illustrative():
    result = premium_at_risk.compute_premium_at_risk(premium_at_risk.DEMO_PORTFOLIO)
    assert "illustrative" in result["rate_notice"].lower()
    assert "not quotations" in result["rate_notice"].lower()


def test_no_breach_or_violation_language():
    """The carrier sees revenue opportunity, not accusations."""
    result = premium_at_risk.compute_premium_at_risk(premium_at_risk.DEMO_PORTFOLIO)
    blob = str(result).lower()
    for banned in ("breach", "violation", "blacklist", "penalty"):
        assert banned not in blob, f"premium-at-risk contains '{banned}'"


def test_empty_portfolio_produces_zero():
    result = premium_at_risk.compute_premium_at_risk([])
    assert result["total_premium_at_risk_sgd"] == "0"
    assert result["accounts_drifted"] == 0
    assert result["gaps"] == []


def test_fully_insured_account_produces_no_gap():
    result = premium_at_risk.compute_premium_at_risk([{
        "account_id": "TEST",
        "business_name": "Test Co",
        "exposures": [
            {"interest": "stock", "evidenced_value": 100000, "insured_value": 100000}
        ],
    }])
    assert result["accounts_drifted"] == 0
    assert result["gaps"] == []


def test_the_demo_portfolio_is_realistic():
    """The demo should show a meaningful number — not zero, not absurd."""
    result = premium_at_risk.compute_premium_at_risk(premium_at_risk.DEMO_PORTFOLIO)
    total = Decimal(result["total_premium_at_risk_sgd"])
    # Should be in the thousands, not pennies or millions
    assert total > 100
    assert total < 100_000
    assert result["accounts_drifted"] >= 5
