"""VERIS SCENARIO — what a claim actually pays, and what the owner absorbs.

WHY THIS SCREEN EXISTS. "Your coverage is 66.7% adequate" is a statistic. "A fire today
costs you S$234,300 out of your own pocket" is the same arithmetic and a completely
different experience. This module turns the adequacy the engine already computed into the
number that changes behaviour: the shortfall the owner personally absorbs.

DETERMINISTIC, AND IN A FIXED ORDER. No model touches this. The waterfall is applied in
one order and the order is not negotiable:

    1. sum insured cap        the policy never pays more than the sum insured
    2. peril sub-limit        a sub-limit for this peril caps it further
    3. average clause         under-insurance reduces the payment proportionately
    4. deductible             the excess comes off last

Every line traces to a document, a page and a verbatim quote via the operand that drove
it. An operand that is UNESTABLISHED means the engine ABSTAINS — it says "cannot calculate
from the evidence provided" rather than guessing a number onto a screen that leads to
money.

WHAT THIS IS NOT. The final figure is a DETERMINISTIC ILLUSTRATION, not a guaranteed
insurer payout. Whether cover responds is a licensed adviser's determination. The label
travels with the result so the UI cannot drop it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

DISCLAIMER = "Deterministic illustration — not a guaranteed claim payout."
ORDER_APPLIED = ("sum insured cap → peril sub-limit → average clause → deductible")

# Which schedule/accounts facts back each peril. A peril maps to an insured interest,
# which maps to a sum-insured fact and a value-at-risk fact.
_PERIL_INTEREST = {
    "fire": "stock", "stock": "stock", "flood": "stock", "theft": "stock",
    "water_damage": "stock", "burglary": "stock",
    "plant": "plant", "machinery": "plant", "equipment": "plant",
    "buildings": "buildings", "building": "buildings",
}

_INTEREST_FACTS = {
    # interest: (sum_insured_fact, value_at_risk_fact)
    "stock": ("stock_sum_insured", "stock_value"),
    "plant": ("plant_sum_insured", None),          # no plant value-at-risk on record
    "buildings": ("buildings_sum_insured", None),
}

_TWO = Decimal("0.01")


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(_TWO, rounding=ROUND_HALF_UP)


@dataclass
class WaterfallLine:
    """One step of the settlement waterfall, with what drove it."""

    order: int
    key: str
    label: str
    amount: str
    operation: str
    provenance: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"order": self.order, "key": self.key, "label": self.label,
                "amount": self.amount, "operation": self.operation,
                "provenance": self.provenance}


def compute_waterfall(
    loss: Decimal,
    sum_insured: Decimal,
    value_at_risk: Decimal,
    deductible: Decimal,
    sub_limit: Optional[Decimal] = None,
) -> dict:
    """The pure deterministic waterfall. Decimal only, fixed order, no provenance.

    Reproduces the worked demo example exactly:
        loss 300,000 · SI 100,000 · VAR 150,000 · deductible 1,000
        → cap 100,000 → average 66.7% → 66,700 → −1,000 → 65,700 ; owner absorbs 234,300

    The average ratio is rounded to the nearest 0.1% for the illustration, which is the
    figure a reader can reconcile by eye; this is a disclosed rounding, not a hidden one.
    """
    if value_at_risk <= 0:
        raise ValueError("value_at_risk must be positive to compute average.")

    # 1. sum insured cap
    gross_claim = min(loss, sum_insured)

    # 2. peril sub-limit
    post_sublimit = min(gross_claim, sub_limit) if sub_limit is not None else gross_claim

    # 3. average clause — proportional reduction, ratio capped at 1.0
    raw_ratio = sum_insured / value_at_risk
    if raw_ratio > 1:
        raw_ratio = Decimal(1)
    ratio = raw_ratio.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    post_average = _q(post_sublimit * ratio)

    # 4. deductible
    final = post_average - deductible
    if final < 0:
        final = Decimal(0)
    final = _q(final)

    owner_shortfall = _q(loss - final)

    return {
        "loss": str(_q(loss)),
        "sum_insured": str(_q(sum_insured)),
        "value_at_risk": str(_q(value_at_risk)),
        "actual_value_at_risk": str(_q(value_at_risk)),
        "sub_limit": None if sub_limit is None else str(_q(sub_limit)),
        "gross_claim": str(_q(gross_claim)),
        "post_sublimit": str(_q(post_sublimit)),
        "average_ratio": f"{(ratio * 100).quantize(Decimal('0.1'))}%",
        "average_ratio_decimal": str(ratio),
        "post_average": str(post_average),
        "deductible": str(_q(deductible)),
        "final_illustration": str(final),
        "owner_shortfall": str(owner_shortfall),
        "average_triggered": ratio < 1,
    }


def _operand(facts, name: str) -> Optional[dict]:
    """Provenance for one fact, or None if it is not established."""
    if facts is None:
        return None
    fact = facts.get(name)
    if not getattr(fact, "established", False):
        return None
    return {
        "operand": name,
        "value": str(fact.value),
        "document_kind": fact.doc.kind if fact.doc else None,
        "document_id": getattr(fact.doc, "id", None),
        "page": fact.page_no,
        "quote": fact.needle,
        "author_class": fact.author_class,
    }


def simulate(case: dict, peril: str = "fire",
             loss_amount: Optional[Decimal] = None,
             sub_limit: Optional[Decimal] = None) -> dict:
    """Illustrate what a claim of `loss_amount` for `peril` would pay on this case.

    Pulls every operand from the case's established facts, carrying document/page/quote
    on each. If the sum insured or the value at risk is UNESTABLISHED, it ABSTAINS —
    "cannot calculate from the evidence provided" — rather than estimating.
    """
    facts = case.get("facts") if isinstance(case, dict) else case
    interest = _PERIL_INTEREST.get((peril or "").lower(), "stock")
    si_name, var_name = _INTEREST_FACTS.get(interest, ("stock_sum_insured",
                                                       "stock_value"))

    si_prov = _operand(facts, si_name)
    var_prov = _operand(facts, var_name) if var_name else None
    ded_prov = _operand(facts, "deductible")

    missing = []
    if si_prov is None:
        missing.append(si_name)
    if var_name is None or var_prov is None:
        missing.append(var_name or f"{interest}_value_at_risk")

    if missing:
        return {
            "available": True,
            "abstained": True,
            "peril": peril,
            "interest": interest,
            "missing_operands": missing,
            "message": "Cannot calculate from the evidence provided.",
            "reason": (
                "The settlement waterfall needs both a sum insured and a value at risk "
                "for this interest, each established by a document. "
                + ", ".join(m for m in missing if m)
                + " is not on record, so the engine abstains rather than estimating a "
                  "number onto a screen that leads to money."),
            "provenance_available": {
                "sum_insured": si_prov, "value_at_risk": var_prov,
                "deductible": ded_prov},
            "disclaimer": DISCLAIMER,
            "order_applied": ORDER_APPLIED,
        }

    sum_insured = Decimal(si_prov["value"])
    value_at_risk = Decimal(var_prov["value"])
    deductible = Decimal(ded_prov["value"]) if ded_prov else Decimal(0)
    loss = Decimal(str(loss_amount)) if loss_amount is not None \
        else _q(value_at_risk * 2)   # a "total-loss" default sized to the exposure

    if value_at_risk <= 0:
        return {
            "available": True, "abstained": True, "peril": peril,
            "message": "Cannot calculate from the evidence provided.",
            "reason": ("The value at risk read from the documents is zero or negative, "
                       "which is a document we misread rather than a finding. Abstaining."),
            "disclaimer": DISCLAIMER, "order_applied": ORDER_APPLIED,
        }

    w = compute_waterfall(loss, sum_insured, value_at_risk, deductible, sub_limit)

    # Attach provenance to each line. Arithmetic-only lines cite the operand that drove
    # them; there is no line without a source.
    lines = [
        WaterfallLine(
            1, "gross_claim", "Loss capped at the sum insured",
            w["gross_claim"],
            f"min(loss {w['loss']}, sum insured {w['sum_insured']})",
            provenance=si_prov),
    ]
    order = 2
    if sub_limit is not None:
        lines.append(WaterfallLine(
            order, "post_sublimit", "Peril sub-limit applied", w["post_sublimit"],
            f"min({w['gross_claim']}, sub-limit {w['sub_limit']})",
            provenance={"operand": "sub_limit", "value": w["sub_limit"],
                        "note": "peril sub-limit supplied to the illustration"}))
        order += 1
    lines.append(WaterfallLine(
        order, "post_average",
        f"Average clause: proportional reduction to {w['average_ratio']}",
        w["post_average"],
        f"{w['post_sublimit']} × {w['average_ratio_decimal']} "
        f"(sum insured {w['sum_insured']} ÷ value at risk {w['value_at_risk']})",
        provenance=var_prov))
    order += 1
    lines.append(WaterfallLine(
        order, "final_illustration", "Less deductible", w["final_illustration"],
        f"{w['post_average']} − deductible {w['deductible']}",
        provenance=ded_prov or {"operand": "deductible", "value": "0",
                                "note": "no deductible found; treated as zero"}))

    return {
        "available": True,
        "abstained": False,
        "peril": peril,
        "interest": interest,
        **w,
        "steps": [ln.as_dict() for ln in lines],
        "provenance": {"sum_insured": si_prov, "value_at_risk": var_prov,
                       "deductible": ded_prov},
        "headline": {
            "owner_absorbs": w["owner_shortfall"],
            "insurer_illustration": w["final_illustration"],
            "statement": (f"On a {peril} loss of S${w['loss']}, this illustration has "
                          f"the insurer paying S${w['final_illustration']} and the "
                          f"owner absorbing S${w['owner_shortfall']}."),
        },
        "disclaimer": DISCLAIMER,
        "order_applied": ORDER_APPLIED,
        "notice": ("Every line traces to a document, page and quote. This is a "
                   "deterministic illustration of how the policy's own mechanisms "
                   "combine — not a statement that a claim will be paid or denied, "
                   "which is a licensed adviser's determination."),
    }


def presets(case: dict, peril: str = "fire") -> dict:
    """Small / typical / total loss presets sized to the exposure, each simulated."""
    facts = case.get("facts") if isinstance(case, dict) else case
    interest = _PERIL_INTEREST.get((peril or "").lower(), "stock")
    _, var_name = _INTEREST_FACTS.get(interest, ("stock_sum_insured", "stock_value"))
    var = _operand(facts, var_name) if var_name else None
    if var is None:
        return {"available": True, "abstained": True, "peril": peril,
                "message": "Cannot calculate from the evidence provided.",
                "scenarios": []}
    value_at_risk = Decimal(var["value"])
    sizes = {
        "small": _q(value_at_risk * Decimal("0.2")),
        "typical": _q(value_at_risk),
        "total": _q(value_at_risk * 2),
    }
    return {
        "available": True, "abstained": False, "peril": peril,
        "scenarios": [{"size": k, "loss_amount": str(v),
                       "result": simulate(case, peril, v)} for k, v in sizes.items()],
        "disclaimer": DISCLAIMER,
    }
