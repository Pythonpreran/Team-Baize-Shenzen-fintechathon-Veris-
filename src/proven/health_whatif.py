"""Deterministic, evidence-bound health what-if illustrations.

This is not a claim decision or an amount-payable calculator.  It applies stated or
explicitly labelled market-convention arithmetic to supplied, cited operands and returns
no figure at all when one is missing.  Money is Decimal throughout.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional


OUTPUT_DOCUMENTED = "DOCUMENTED"
OUTPUT_MISSING = "INFORMATION_MISSING"
PROVEN = "PROVEN"
DECLARED = "DECLARED"
MARKET_CONVENTION = "MARKET_CONVENTION"
_TWO = Decimal("0.01")


class WhatIfError(ValueError):
    """A malformed illustration request or unavailable deterministic operand."""


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, float):
        raise WhatIfError(f"{name} must be Decimal text, never float.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise WhatIfError(f"{name} is not a Decimal value.") from exc
    if not amount.is_finite() or amount < 0:
        raise WhatIfError(f"{name} must be a non-negative finite Decimal.")
    return amount


def _q(value: Decimal) -> str:
    return format(value.quantize(_TWO, rounding=ROUND_HALF_UP), "f")


def _citation(item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    citation = item.get("citation")
    if not isinstance(citation, Mapping) or not citation.get("quote"):
        return None
    return {
        "document": citation.get("document"), "page": citation.get("page"),
        "quote": citation.get("quote"), "source_url": citation.get("source_url"),
    }


def _item(policy: Mapping[str, Any], code: str) -> Optional[Mapping[str, Any]]:
    items = policy.get("health_items")
    if not isinstance(items, list):
        return None
    return next((item for item in items if isinstance(item, Mapping)
                 and item.get("mechanism_code") == code), None)


def _policy_decimal(policy: Mapping[str, Any], code: str, *, unit: Optional[str] = None
                    ) -> tuple[Optional[Decimal], Optional[dict[str, Any]]]:
    item = _item(policy, code)
    citation = _citation(item) if isinstance(item, Mapping) else None
    if not isinstance(item, Mapping) or item.get("state") != OUTPUT_DOCUMENTED or citation is None:
        return None, citation
    if unit is not None and str(item.get("unit") or "").lower() != unit.lower():
        return None, citation
    try:
        return _decimal(item.get("value"), code), citation
    except WhatIfError:
        return None, citation


def _missing(*names: str, citations: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    return {
        "output_state": OUTPUT_MISSING,
        "evidence_class": "UNESTABLISHED",
        "illustration": False,
        "lines": [],
        "illustrated_insurer_portion": None,
        "illustrated_owner_portion": None,
        "missing_operands": list(names),
        "citations": citations or [],
        "notice": "No figure is shown. Add the named policy wording or declared estimate to establish every operand.",
    }


def _declared_line(key: str, label: str, value: Decimal, unit: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "amount": _q(value), "unit": unit,
            "evidence_class": DECLARED, "source": {"kind": "USER_DECLARED"}}


def _proven_line(key: str, label: str, value: Decimal, citation: Optional[dict[str, Any]],
                 unit: str = "") -> dict[str, Any]:
    return {"key": key, "label": label, "amount": _q(value), "unit": unit,
            "evidence_class": PROVEN, "source": citation}


def _result(lines: list[dict[str, Any]], insurer: Decimal, loss: Decimal, *,
            order_basis: str, citations: list[dict[str, Any]]) -> dict[str, Any]:
    owner = max(Decimal(0), loss - insurer)
    basis = str(order_basis or MARKET_CONVENTION).upper()
    if basis not in {"STATED_IN_WORDING", MARKET_CONVENTION}:
        basis = MARKET_CONVENTION
    illustration = basis == MARKET_CONVENTION
    return {
        "output_state": OUTPUT_DOCUMENTED,
        "evidence_class": DECLARED,
        "illustration": illustration,
        "basis": basis,
        "lines": lines,
        "illustrated_insurer_portion": _q(insurer),
        "illustrated_owner_portion": _q(owner),
        "missing_operands": [],
        "citations": citations,
        "notice": ("Deterministic illustration using a market-convention order; it does not decide a claim outcome."
                   if illustration else
                   "Deterministic illustration from the stated order; it does not decide a claim outcome."),
    }


def india(policy: Mapping[str, Any], *, loss: object, room_charge_per_day: object,
          days: object, deductible: object, proportionate_factor: Optional[object] = None,
          order_basis: str = MARKET_CONVENTION) -> dict[str, Any]:
    """Apply an India health waterfall only from cited terms and declared hospital inputs."""
    declared: dict[str, Decimal] = {}
    for name, value in (("loss", loss), ("room_charge_per_day", room_charge_per_day),
                        ("days", days), ("deductible", deductible)):
        try:
            declared[name] = _decimal(value, name)
        except WhatIfError:
            return _missing(name)
    room_cap, room_citation = _policy_decimal(policy, "LIMIT.ROOM_RENT")
    co_pay_pct, co_pay_citation = _policy_decimal(policy, "SHARE.CO_PAYMENT", unit="%")
    missing = []
    if room_cap is None:
        missing.append("room-rent cap in the policy wording")
    if co_pay_pct is None:
        missing.append("co-payment condition in the policy wording")
    if missing:
        return _missing(*missing, citations=[citation for citation in (room_citation, co_pay_citation) if citation])
    lines = [
        _declared_line("loss", "Declared hospital estimate", declared["loss"]),
        _declared_line("room_charge", "Declared room charge per day", declared["room_charge_per_day"]),
        _declared_line("days", "Declared stay days", declared["days"], "days"),
        _proven_line("room_cap", "Cited room-rent cap per day", room_cap, room_citation),
    ]
    room_excess = max(Decimal(0), declared["room_charge_per_day"] - room_cap) * declared["days"]
    eligible = max(Decimal(0), declared["loss"] - room_excess)
    lines.append(_declared_line("room_excess", "Illustrated room-charge excess", room_excess))
    # A proportionate factor is accepted only when the policy actually contains that
    # condition. Without both an explicit condition and the declared factor there is no
    # substitute assumption and therefore no figure.
    proportionate = _item(policy, "CONDITION.PROPORTIONATE_DEDUCTION")
    if proportionate is not None and proportionate.get("state") == OUTPUT_DOCUMENTED:
        if proportionate_factor is None:
            return _missing("declared associated-medical-expense factor", citations=[
                citation for citation in (_citation(proportionate), room_citation, co_pay_citation) if citation])
        factor = _decimal(proportionate_factor, "proportionate_factor")
        if factor > Decimal(1):
            raise WhatIfError("proportionate_factor cannot exceed 1.")
        deduction = eligible * factor
        eligible = max(Decimal(0), eligible - deduction)
        lines.append(_declared_line("proportionate", "Declared associated-expense factor", factor, "factor"))
        lines.append(_declared_line("proportionate_deduction", "Illustrated associated-expense deduction", deduction))
    co_pay = eligible * co_pay_pct / Decimal(100)
    insurer = max(Decimal(0), eligible - co_pay - declared["deductible"])
    lines.extend([
        _proven_line("co_payment", "Cited co-payment", co_pay, co_pay_citation),
        _declared_line("deductible", "Declared deductible", declared["deductible"]),
        _declared_line("insurer_portion", "Illustrated insurer portion", insurer),
        _declared_line("owner_portion", "Illustrated owner portion", max(Decimal(0), declared["loss"] - insurer)),
    ])
    citations = [citation for citation in (room_citation, co_pay_citation, _citation(proportionate)
                                             if proportionate is not None else None) if citation]
    return _result(lines, insurer, declared["loss"], order_basis=order_basis, citations=citations)


def _singapore_policy_operand(policy: Mapping[str, Any], codes: tuple[str, ...], *,
                              supplied: Optional[object], name: str,
                              unit: Optional[str] = None) -> tuple[Optional[Decimal], Optional[dict[str, Any]]]:
    """Use a cited policy value and refuse a caller override that does not match it."""
    last_citation: Optional[dict[str, Any]] = None
    for code in codes:
        value, citation = _policy_decimal(policy, code, unit=unit)
        if citation is not None:
            last_citation = citation
        if value is None:
            continue
        if supplied is not None and _decimal(supplied, name) != value:
            return None, citation
        return value, citation
    return None, last_citation


def singapore(policy: Mapping[str, Any], *, loss: object, deductible: Optional[object] = None,
              coinsurance_percent: Optional[object] = None, proration_factor: Optional[object] = None,
              rider_copayment_percent: Optional[object] = None,
              order_basis: str = MARKET_CONVENTION) -> dict[str, Any]:
    """Apply a Singapore illustration only from cited policy operands and a supplied loss."""
    try:
        declared_loss = _decimal(loss, "loss")
    except WhatIfError:
        return _missing("declared hospital estimate")
    deductible_value, deductible_citation = _singapore_policy_operand(
        policy, ("SHARE.DEDUCTIBLE",), supplied=deductible, name="deductible")
    coinsurance_value, coinsurance_citation = _singapore_policy_operand(
        policy, ("SHARE.CO_INSURANCE",), supplied=coinsurance_percent,
        name="coinsurance_percent", unit="%")
    proration_value, proration_citation = _singapore_policy_operand(
        policy, ("CONDITION.PRO_RATION",), supplied=proration_factor, name="proration_factor")
    rider_value, rider_citation = _singapore_policy_operand(
        policy, ("SHARE.RIDER_CO_PAYMENT", "SHARE.RIDER_COPAYMENT"),
        supplied=rider_copayment_percent, name="rider_copayment_percent", unit="%")
    missing = []
    if deductible_value is None:
        missing.append("cited deductible in the policy wording")
    if coinsurance_value is None:
        missing.append("cited co-insurance percentage in the policy wording")
    if proration_value is None:
        missing.append("cited ward pro-ration factor in the policy wording")
    if rider_value is None:
        missing.append("cited rider co-payment percentage in the policy wording")
    citations = [citation for citation in (
        deductible_citation, coinsurance_citation, proration_citation, rider_citation) if citation]
    if missing:
        return _missing(*missing, citations=citations)
    assert deductible_value is not None and coinsurance_value is not None
    assert proration_value is not None and rider_value is not None
    if proration_value > Decimal(1):
        raise WhatIfError("cited proration_factor cannot exceed 1.")
    for name, value in (("coinsurance_percent", coinsurance_value),
                        ("rider_copayment_percent", rider_value)):
        if value > Decimal(100):
            raise WhatIfError(f"cited {name} cannot exceed 100.")
    lines = [
        _declared_line("loss", "Declared hospital estimate", declared_loss),
        _proven_line("deductible", "Cited deductible", deductible_value, deductible_citation),
        _proven_line("coinsurance", "Cited co-insurance percentage", coinsurance_value,
                     coinsurance_citation, "%"),
        _proven_line("proration", "Cited ward pro-ration factor", proration_value,
                     proration_citation, "factor"),
        _proven_line("rider_copayment", "Cited rider co-payment percentage", rider_value,
                     rider_citation, "%"),
    ]
    after_deductible = max(Decimal(0), declared_loss - deductible_value)
    coinsurance = after_deductible * coinsurance_value / Decimal(100)
    after_coinsurance = max(Decimal(0), after_deductible - coinsurance)
    prorated = after_coinsurance * proration_value
    rider_share = prorated * rider_value / Decimal(100)
    insurer = max(Decimal(0), prorated - rider_share)
    lines.extend([
        _proven_line("coinsurance_share", "Illustrated co-insurance share", coinsurance, coinsurance_citation),
        _proven_line("prorated", "Illustrated ward pro-ration result", prorated, proration_citation),
        _proven_line("rider_share", "Illustrated rider co-payment", rider_share, rider_citation),
        _declared_line("insurer_portion", "Illustrated insurer portion", insurer),
        _declared_line("owner_portion", "Illustrated owner portion", max(Decimal(0), declared_loss - insurer)),
    ])
    return _result(lines, insurer, declared_loss, order_basis=order_basis, citations=citations)


def simulate(policy: Mapping[str, Any], *, inputs: Mapping[str, Any],
             order_basis: str = MARKET_CONVENTION) -> dict[str, Any]:
    country = str(policy.get("country") or "").upper()
    if country == "IN":
        return india(policy, loss=inputs.get("loss"),
                     room_charge_per_day=inputs.get("room_charge_per_day"),
                     days=inputs.get("days"), deductible=inputs.get("deductible"),
                     proportionate_factor=inputs.get("proportionate_factor"),
                     order_basis=order_basis)
    if country == "SG":
        return singapore(policy, loss=inputs.get("loss"), deductible=inputs.get("deductible"),
                         coinsurance_percent=inputs.get("coinsurance_percent"),
                         proration_factor=inputs.get("proration_factor"),
                         rider_copayment_percent=inputs.get("rider_copayment_percent"),
                         order_basis=order_basis)
    return _missing("a supported health jurisdiction pack")
