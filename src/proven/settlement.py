"""Deterministic settlement check, non-adjudicating dispute draft and sandbox reconciliation."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from . import claims, health_whatif


class SettlementError(ValueError):
    pass


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, float):
        raise SettlementError(f"{name} must be Decimal text, never float.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SettlementError(f"{name} is not a Decimal value.") from exc
    if not result.is_finite() or result < 0:
        raise SettlementError(f"{name} must be a non-negative finite amount.")
    return result


def check(policy: Mapping[str, Any], *, scenario_inputs: Mapping[str, Any],
          sandbox_settlement: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute an illustration and classify each sandbox deduction without adjudicating."""
    illustration = health_whatif.simulate(policy, inputs=scenario_inputs,
                                          order_basis="MARKET_CONVENTION")
    if illustration["illustrated_insurer_portion"] is None:
        return {
            "state": "INFORMATION_MISSING", "illustration": illustration,
            "deductions": [], "draft": None,
            "notice": "No settlement comparison is made until every illustration operand is established.",
        }
    expected = _decimal(illustration["illustrated_insurer_portion"], "illustrated insurer portion")
    proposed = _decimal(sandbox_settlement.get("proposed_payable"), "sandbox proposed amount")
    deductions = claims.classify_deductions(policy, sandbox_settlement)
    not_found = [row for row in deductions if row["classification"] == "NOT_FOUND"]
    draft = {
        "state": "DRAFTED",
        "text": (
            "Please review the listed settlement deductions against the cited policy wording. "
            "VERIS did not locate the marked deduction in the materials supplied to it. "
            "This is a request for authorised review and makes no adjudication."),
        "deductions": not_found,
    }
    return {
        "state": "READY_FOR_REVIEW", "expected_illustration": format(expected, "f"),
        "sandbox_proposed_amount": format(proposed, "f"), "illustration": illustration,
        "deductions": deductions, "draft": draft,
        "notice": "The comparison is deterministic and evidence-bound; it is not an insurance outcome decision.",
    }


def reconcile(expected_illustration: object, received: object) -> dict[str, Any]:
    expected = _decimal(expected_illustration, "expected illustration")
    actual = _decimal(received, "received sandbox transfer")
    return {
        "state": "RECONCILED" if expected == actual else "REQUIRES_AUTHORISED_REVIEW",
        "expected_illustration": format(expected, "f"), "received": format(actual, "f"),
        "difference": format(expected - actual, "f"),
        "notice": ("The synthetic bank transfer equals the deterministic illustration."
                   if expected == actual else
                   "The synthetic bank transfer differs from the deterministic illustration; no conclusion is drawn."),
    }
