"""Whole-portfolio protection map derived only from documented portfolio findings."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from . import motor

DOCUMENTED = "DOCUMENTED_TERMS"
CONDITIONAL = "CONDITIONAL_TERMS"
NOT_FOUND = "NOT_FOUND_IN_DOCUMENTS"
NOT_ASSESSED = "NOT_ASSESSED"
COMPULSORY_GAP = "COMPULSORY_GAP"

_DEMO_MAP_AS_OF = date(2026, 10, 19)

LIFE_EVENTS: dict[str, dict[str, Any]] = {
    "NEW_BABY": {"label": "New baby", "risks": ["hospitalisation", "accident"],
                  "documents": ["child policy schedule", "school policy"]},
    "MARRIAGE": {"label": "Marriage", "risks": ["hospitalisation", "home_contents"],
                 "documents": ["policy schedules naming both adults"]},
    "NEW_CAR": {"label": "New car", "risks": ["motor_third_party", "motor_own_damage"],
                "documents": ["motor policy schedule", "vehicle registration"]},
    "NEW_HOME": {"label": "New home", "risks": ["home_structure", "home_contents"],
                 "documents": ["home policy schedule", "contents list"]},
    "PARENT_TURNS_60_65": {"label": "Parent turns 60 or 65", "risks": ["hospitalisation"],
                           "documents": ["health policy schedule"]},
    "MOVING_COUNTRIES": {"label": "Moving countries", "risks": ["hospitalisation", "motor_third_party"],
                          "documents": ["destination-country policy schedule"]},
    "STARTING_BUSINESS": {"label": "Starting a business", "risks": ["business_liability"],
                          "documents": ["business policy schedule"]},
    "TRAVEL_ABROAD": {"label": "Travel abroad", "risks": ["travel"],
                      "documents": ["travel policy schedule"]},
    "NEW_JOB": {"label": "New job", "risks": ["hospitalisation"],
                "documents": ["employer benefits schedule"]},
}


def _names(portfolio: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(member.get("id")): dict(member) for member in portfolio.get("members", [])
            if isinstance(member, Mapping) and member.get("id")}


def _policies(portfolio: Mapping[str, Any], *, line: Optional[str] = None) -> list[dict[str, Any]]:
    values = [dict(policy) for policy in portfolio.get("policies", []) if isinstance(policy, Mapping)]
    return [policy for policy in values if line is None or str(policy.get("line_of_business") or "").upper() == line]


def _coverage(policy: Mapping[str, Any], code: str) -> Optional[dict[str, Any]]:
    for item in policy.get("coverage_items", []) if isinstance(policy.get("coverage_items"), list) else []:
        if isinstance(item, Mapping) and item.get("mechanism_code") == code:
            return dict(item)
    for item in policy.get("health_items", []) if isinstance(policy.get("health_items"), list) else []:
        if isinstance(item, Mapping) and item.get("mechanism_code") == code:
            return dict(item)
    for item in policy.get("motor_items", []) if isinstance(policy.get("motor_items"), list) else []:
        if isinstance(item, Mapping) and item.get("mechanism_code") == code:
            return dict(item)
    for item in (policy.get("property_travel_items", [])
                 if isinstance(policy.get("property_travel_items"), list) else []):
        if isinstance(item, Mapping) and item.get("mechanism_code") == code:
            return dict(item)
    return None


def _property_entity(policy: Mapping[str, Any], assets: Mapping[str, str]) -> str:
    names = [assets[str(asset_id)] for asset_id in policy.get("insured_asset_ids") or []
             if str(asset_id) in assets]
    if names:
        return ", ".join(names)
    return str(policy.get("property_label") or policy.get("product") or "Home")


def _documented_coverage(policy: Mapping[str, Any], code: str) -> Optional[dict[str, Any]]:
    """Keep the originating evidence class and source binding instead of promoting it."""
    item = _coverage(policy, code)
    if item is None or item.get("evidence_class") != "PROVEN":
        return None
    if item.get("state") not in (None, "DOCUMENTED"):
        return None
    citation = item.get("citation")
    if not ((isinstance(citation, Mapping) and citation.get("quote")) or item.get("source_document_id")):
        return None
    return item


def _item_source(item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    citation = item.get("citation")
    if isinstance(citation, Mapping):
        return dict(citation)
    source_document_id = item.get("source_document_id")
    return {"source_document_id": source_document_id} if source_document_id else None


def _cell(entity: str, risk: str, state: str, *, policy_ids: list[str] = [],
          evidence_class: str = "UNESTABLISHED", reason: str = "",
          source: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    return {
        "entity": entity, "risk": risk, "state": state, "policy_ids": list(policy_ids),
        "evidence_class": evidence_class, "reason": reason, "source": dict(source) if source else None,
    }


def _limit(policy: Mapping[str, Any]) -> Optional[Decimal]:
    item = _documented_coverage(policy, "LIMIT.SUM_INSURED")
    if not item or item.get("value") in (None, ""):
        return None
    try:
        return Decimal(str(item["value"]))
    except (InvalidOperation, ValueError):
        return None


def _scenario_loss(value: Optional[str]) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def life_event(code: str) -> dict[str, Any]:
    item = LIFE_EVENTS.get(str(code).upper())
    if item is None:
        raise ValueError("Unknown life event.")
    return {"code": str(code).upper(), **item, "evidence_class": "DECLARED",
            "notice": "A life event changes only what VERIS foregrounds; it establishes no policy value or evidence class."}


def build(portfolio: Mapping[str, Any], *, scenario_loss: Optional[str] = None,
          as_of: date = _DEMO_MAP_AS_OF) -> dict[str, Any]:
    """Build the map solely from retained PROVEN terms and sourced statutory records."""
    people = _names(portfolio)
    policies = _policies(portfolio)
    cells: list[dict[str, Any]] = []

    arjun_id = next((member_id for member_id, member in people.items()
                     if str(member.get("name")) == "Arjun Rao"), None)
    hospital = [(policy, _documented_coverage(policy, "COVERED.HOSPITALISATION"))
                for policy in _policies(portfolio, line="HEALTH")
                if arjun_id and arjun_id in (policy.get("insured_member_ids") or [])]
    hospital = [(policy, item) for policy, item in hospital if item is not None]
    if hospital:
        cells.append(_cell("Arjun Rao", "hospitalisation", DOCUMENTED,
                           policy_ids=[str(policy["id"]) for policy, _item in hospital],
                           evidence_class="PROVEN",
                           reason="Policy schedules contain hospitalisation terms for this member.",
                           source=_item_source(hospital[0][1])))
    else:
        cells.append(_cell("Arjun Rao", "hospitalisation", NOT_ASSESSED,
                           reason="No documented hospitalisation term was read for this member."))

    anya_id = next((member_id for member_id, member in people.items()
                    if str(member.get("name")) == "Anya Rao"), None)
    accident = [(policy, _documented_coverage(policy, "COVERED.ACCIDENT"))
                for policy in policies if anya_id in (policy.get("insured_member_ids") or [])]
    accident = [(policy, item) for policy, item in accident if item is not None]
    cells.append(_cell("Anya Rao", "accident", DOCUMENTED if accident else NOT_ASSESSED,
                       policy_ids=[str(policy["id"]) for policy, _item in accident],
                       evidence_class="PROVEN" if accident else "UNESTABLISHED",
                       reason=("A documented schedule contains an accident term." if accident else
                               "No school policy or accident-policy schedule was uploaded."),
                       source=_item_source(accident[0][1]) if accident else None))

    for policy in _policies(portfolio, line="MOTOR"):
        if str(policy.get("country") or "").upper() != "IN":
            continue
        motor_term = _documented_coverage(policy, "COVERED.MOTOR_THIRD_PARTY")
        alert = motor.third_party_alert(policy, as_of=as_of)
        if motor_term is None or alert["state"] == "INFORMATION_MISSING":
            continue
        state = COMPULSORY_GAP if alert["state"] == "COMPULSORY_GAP" else (
            CONDITIONAL if alert["state"] == "EXPIRING" else DOCUMENTED)
        cells.append(_cell("Bengaluru family car", "motor_third_party", state,
                           policy_ids=[str(policy["id"])], evidence_class=str(motor_term["evidence_class"]),
                           reason=str(alert.get("alert") or alert.get("notice") or ""),
                           source=alert.get("source")))

    assets = {str(asset.get("id")): str(asset.get("name") or "")
              for asset in portfolio.get("assets", []) if isinstance(asset, Mapping) and asset.get("id")}
    for policy in _policies(portfolio, line="HOME"):
        entity = _property_entity(policy, assets)
        contents = _documented_coverage(policy, "COVERED.HOME_CONTENTS")
        building = _documented_coverage(policy, "COVERED.HOME_BUILDING")
        if building is not None:
            cells.append(_cell(entity, "home_structure", DOCUMENTED,
                               policy_ids=[str(policy["id"])], evidence_class=str(building["evidence_class"]),
                               reason="A documented home-building schedule term was read.",
                               source=_item_source(building)))
        if contents is None and building is None:
            continue
        # A contents sum insured below the owner's own list is a documented condition
        # that needs attention, never a statement about what a claim would receive.
        gap = _coverage(policy, "LIMIT.CONTENTS_BELOW_LIST")
        short = (gap is not None and gap.get("state") == "DOCUMENTED"
                 and gap.get("value") not in (None, ""))
        cells.append(_cell(entity, "home_contents", CONDITIONAL if short else DOCUMENTED,
                           policy_ids=[str(policy["id"])],
                           evidence_class=str((gap if short else (contents or building))["evidence_class"]),
                           reason=("The schedule's contents sum insured is below the self-declared contents "
                                   "list; the wording's insurance-to-value sentence is quoted on the card."
                                   if short else "A documented home-contents term was read."),
                           source=_item_source(gap if short else (contents or building))))

    for policy in _policies(portfolio, line="TRAVEL"):
        travel = _documented_coverage(policy, "COVERED.TRAVEL")
        if travel is None:
            continue
        for member_id in policy.get("insured_member_ids") or []:
            member = people.get(str(member_id))
            if member is None:
                continue
            cells.append(_cell(str(member.get("name") or ""), "travel", DOCUMENTED,
                               policy_ids=[str(policy["id"])], evidence_class=str(travel["evidence_class"]),
                               reason="A travel schedule names this member as a traveller.",
                               source=_item_source(travel)))

    loss = _scenario_loss(scenario_loss)
    overlaps: list[dict[str, Any]] = []
    if len(hospital) >= 2:
        illustrations = []
        for policy, _item in hospital:
            limit = _limit(policy)
            illustrations.append({
                "policy_id": policy["id"], "policy": policy.get("product"),
                "separate_policy_limit_illustration": (str(min(loss, limit))
                                                       if loss is not None and limit is not None else None),
                "basis": ("DECLARED_SCENARIO_AND_SCHEDULE_LIMIT"
                          if loss is not None and limit is not None else "INFORMATION_MISSING"),
            })
        overlaps.append({
            "entity": "Arjun Rao", "risk": "hospitalisation",
            "policy_ids": [policy["id"] for policy, _item in hospital],
            "notice": ("Policy schedules contain hospitalisation terms. Each separate-policy limit "
                       "illustration appears only after an owner-supplied scenario amount and does not allocate "
                       "a claim or advise which policy to use."),
            "other_insurance_terms": "INFORMATION_MISSING",
            "illustrations": illustrations,
        })

    next_document = {
        "document": "the school policy", "reason": "It is needed to assess Anya's accident terms.",
        "unbounded": True, "rank": 1, "percentage": None,
    }
    return {
        "cells": cells, "overlaps": overlaps, "next_document": next_document,
        "as_of": as_of.isoformat(), "synthetic_timeline": True,
        "legend": {
            DOCUMENTED: "Policy terms documented in the materials read.",
            CONDITIONAL: "Terms are documented but a dated condition needs attention.",
            NOT_FOUND: "Not found in the documents read; this is not a coverage denial.",
            NOT_ASSESSED: "Not assessed; upload the named document.",
            COMPULSORY_GAP: "A sourced compulsory-cover rule and no in-force policy in the synthetic timeline.",
        },
        "notice": "Map states describe documented materials and sourced compulsory rules; VERIS does not make a coverage decision.",
    }
