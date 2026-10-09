"""Layer 3 — settlement arithmetic. No model. Decimal only, never float for money.

Order of operations is READ from Chubb cl 3.12, not assumed:
    average -> sub-limits -> deductible.

Two parameters are nullable for reasons measured across three carriers, and
NEITHER null may be defaulted:

  threshold_pct is None -> the clause states NO threshold, so any shortfall bites
      (MSIG GC12). Defaulting to 0.85 because Chubb says 85% would UNDER-report
      every MSIG insured — a wrong answer in the direction that costs money.

  formula_span is None  -> the wording states the trigger but not the arithmetic
      (Chubb cl 4.2, MSIG GC12). Not None -> the wording states the arithmetic and
      we quote it (Liberty p.6 / cl 5 p.46).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .models import Settlement


def settle(
    loss: Decimal,
    sum_insured: Decimal,
    value_at_risk: Decimal,
    threshold_pct: Optional[Decimal],
    sub_limit: Optional[Decimal],
    deductible: Decimal,
    formula_span: Optional[str],
) -> Settlement:
    adequacy = sum_insured / value_at_risk

    if threshold_pct is None:
        triggered = adequacy < Decimal(1)          # any shortfall at all
        threshold_basis = "NO_THRESHOLD_STATED"
    else:
        triggered = adequacy < threshold_pct
        threshold_basis = "THRESHOLD_STATED"

    if triggered:
        reduced = loss * adequacy
        if formula_span is None:
            # The clause states the TRIGGER, not the FORMULA. Illustration only.
            illustration, basis, formula_class = (
                reduced, "MARKET_CONVENTION", "MARKET_CONVENTION")
        else:
            # The insurer wrote the arithmetic down. Quoting, not inventing.
            illustration, basis, formula_class = (
                reduced, "STATED_IN_WORDING", "PROVEN")
    else:
        illustration, basis, formula_class = loss, "NOT_TRIGGERED", "PROVEN"

    if sub_limit is not None:
        illustration = min(illustration, sub_limit)
    illustration = max(Decimal(0), illustration - deductible)

    return Settlement(
        adequacy_ratio=adequacy,
        average_triggered=triggered,
        illustration=illustration,
        formula_basis=basis,
        formula_basis_class=formula_class,
        threshold_basis=threshold_basis,
        order_applied="average -> sub-limit -> deductible (cl 3.12)",
    )
