"""T04 regression coverage for policy-aware deterministic money formatting."""
from decimal import Decimal

from proven import actions, ai, bi, compounding, presenter, scenario, voi


def test_existing_sgd_presentation_is_byte_identical():
    assert presenter._sgd("1234") == "S$1,234"
    assert ai._sgd("150000") == "S$150,000"
    assert scenario._money("300000.00") == "S$300,000"
    assert actions._money(Decimal("100000")) == "S$100,000"
    assert bi._money(Decimal("60000")) == "S$60,000"
    assert compounding._money(Decimal("62000")) == "S$62,000"
    assert voi._money("50000") == "S$50,000"
