"""Evidence-bound motor policy support for the synthetic household demonstration."""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional

from . import household, jurisdiction

_DEMO_AS_OF = date(2026, 10, 9)
_TWO = Decimal("0.01")


class MotorError(ValueError):
    pass


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, float):
        raise MotorError(f"{name} must be Decimal text, never float.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MotorError(f"{name} is not a Decimal value.") from exc
    if not result.is_finite() or result < 0:
        raise MotorError(f"{name} must be a non-negative finite Decimal.")
    return result


def _fixture_spec(policy: Mapping[str, Any]) -> Optional[household.PolicySpec]:
    """Return the one server-stamped synthetic India motor policy, or nothing.

    ``source`` is produced only by the portfolio repository after its encrypted seed
    binding verifies the uploaded generated PDF.  Matching display fields alone is never
    sufficient for a user-uploaded policy to inherit the Rao schedule's evidence.
    """
    if policy.get("source") != "SYNTHETIC_EXAMPLE":
        return None
    documents = policy.get("source_document_ids")
    if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], str) or not documents[0]:
        return None
    period = policy.get("period")
    if not isinstance(period, Mapping):
        return None
    for spec in household.POLICIES:
        if (spec.country == "IN" and spec.line_of_business == "MOTOR"
                and str(policy.get("country") or "").upper() == spec.country
                and str(policy.get("line_of_business") or "").upper() == spec.line_of_business
                and policy.get("product") == spec.product
                and policy.get("masked_number") == spec.masked_number
                and period.get("end") == spec.period_end
                and spec.document_filename == "rao-india-motor-schedule.pdf"):
            return spec
    return None


def _schedule_citation(policy: Mapping[str, Any], spec: household.PolicySpec,
                       quote: str) -> Optional[dict[str, Any]]:
    documents = policy.get("source_document_ids")
    if not isinstance(documents, list) or len(documents) != 1 or not documents[0]:
        return None
    return {"document": spec.document_filename, "source_document_id": documents[0],
            "page": 1, "quote": quote}


def _motor_schedule(policy: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Only the exact persisted synthetic policy receives schedule facts."""
    spec = _fixture_spec(policy)
    if spec is None:
        return None
    citation = _schedule_citation(
        policy, spec,
        "Depreciation schedule: plastic parts 50%; metal parts 10% for a vehicle aged up to five years.")
    if citation is None:
        return None
    return {
        "spec": spec,
        "rates": {"PLASTIC": Decimal("0.50"), "METAL": Decimal("0.10")},
        "citation": citation,
    }


def compulsory_source() -> Optional[dict[str, Any]]:
    entry = jurisdiction.pack("IN").entry("IN-COMP-MOTOR-THIRD-PARTY")
    if not entry.owner_facing:
        return None
    return entry.provenance()


def third_party_alert(policy: Mapping[str, Any], *, as_of: date = _DEMO_AS_OF) -> dict[str, Any]:
    """A statutory alert only for the verified seeded policy and sourced statutory entry."""
    schedule = _motor_schedule(policy)
    if schedule is None:
        return {"state": "INFORMATION_MISSING", "alert": None, "source": None,
                "notice": "The verified motor policy schedule is needed before a compulsory-cover alert is shown."}
    source = compulsory_source()
    if source is None:
        return {"state": "INFORMATION_MISSING", "alert": None, "source": None,
                "notice": "The compulsory-cover source is not verified for owner display."}
    period = policy.get("period") if isinstance(policy.get("period"), Mapping) else {}
    try:
        expiry = date.fromisoformat(str(period.get("end") or ""))
    except ValueError:
        return {"state": "INFORMATION_MISSING", "alert": None, "source": source,
                "notice": "The policy schedule is needed to establish the motor cover end date."}
    delta = (expiry - as_of).days
    if delta < 0:
        state, alert = "COMPULSORY_GAP", "Third-party policy period has ended in this synthetic demo timeline."
    elif delta <= 30:
        state, alert = "EXPIRING", f"Third-party policy period ends in {delta} days in this synthetic demo timeline."
    else:
        state, alert = "IN_FORCE", "Third-party policy period is in force on this synthetic demo date."
    return {
        "state": state, "alert": alert, "expires_on": expiry.isoformat(), "days_to_expiry": delta,
        "source": source,
        # Section 196 remains a CANDIDATE in IN.yaml. Never invent a penalty merely to make
        # an alert more dramatic; the limitation travels with the otherwise sourced alert.
        "penalty": None,
        "penalty_notice": "No penalty is shown because the current primary-source text is not yet verified.",
    }


def depreciation_what_if(policy: Mapping[str, Any], *, part_material: str,
                          repair_cost: object) -> dict[str, Any]:
    schedule = _motor_schedule(policy)
    if schedule is None:
        return {"output_state": "INFORMATION_MISSING", "illustration": False,
                "missing_operands": ["the policy depreciation schedule"], "lines": [],
                "notice": "No repair illustration is shown until the policy schedule establishes a depreciation table."}
    material = str(part_material or "").upper()
    rate = schedule["rates"].get(material)
    if rate is None:
        return {"output_state": "INFORMATION_MISSING", "illustration": False,
                "missing_operands": ["a depreciation rate for that part material"], "lines": [],
                "notice": "No repair illustration is shown for an unlisted part material."}
    cost = _decimal(repair_cost, "repair_cost")
    depreciation = (cost * rate).quantize(_TWO, rounding=ROUND_HALF_UP)
    after = (cost - depreciation).quantize(_TWO, rounding=ROUND_HALF_UP)
    citation = schedule["citation"]
    return {
        "output_state": "DOCUMENTED", "evidence_class": "PROVEN", "illustration": True,
        "part_material": material,
        "lines": [
            {"key": "repair_cost", "label": "Declared repair estimate", "amount": format(cost, "f"),
             "evidence_class": "DECLARED"},
            {"key": "depreciation", "label": "Cited depreciation for this part material",
             "amount": format(depreciation, "f"), "rate": format(rate * Decimal(100), "f") + "%",
             "evidence_class": "PROVEN", "source": citation},
            {"key": "after_depreciation", "label": "Illustrated repair value after schedule depreciation",
             "amount": format(after, "f"), "evidence_class": "DECLARED"},
        ],
        "citation": citation,
        "notice": "Deterministic illustration from the cited synthetic schedule; it does not decide a claim outcome.",
    }


def enrich_portfolio(portfolio: Mapping[str, Any], *, as_of: date = _DEMO_AS_OF) -> dict[str, Any]:
    out = copy.deepcopy(dict(portfolio))
    for policy in out.get("policies", []) if isinstance(out.get("policies"), list) else []:
        if not isinstance(policy, dict) or str(policy.get("line_of_business") or "").upper() != "MOTOR":
            continue
        schedule = _motor_schedule(policy)
        if schedule is None:
            policy["motor_items"] = []
            policy["motor_alert"] = {"state": "INFORMATION_MISSING", "alert": None,
                                     "notice": "No verified motor policy schedule is bound to this record."}
            continue
        if str(policy.get("country") or "").upper() == "IN":
            alert = third_party_alert(policy, as_of=as_of)
            expiry_citation = _schedule_citation(
                policy, schedule["spec"], "Third-party cover expiry: 18 October 2026")
            policy["motor_alert"] = alert
            policy["motor_items"] = [
                {"mechanism_code": "COVERED.MOTOR_THIRD_PARTY", "state": "DOCUMENTED",
                 "evidence_class": "PROVEN", "value": None, "citation": expiry_citation},
                {"mechanism_code": "RENEWAL.THIRD_PARTY", "state": "DOCUMENTED",
                 "evidence_class": "PROVEN", "value": alert.get("expires_on"),
                 "display": "DAYS_END_DATE", "citation": expiry_citation,
                 "statute_source": alert.get("source")},
            ]
        else:
            policy["motor_items"] = []
            policy["motor_alert"] = {"state": "INFORMATION_MISSING", "alert": None,
                                      "notice": "No sourced Singapore motor notification timing is available."}
    return out
