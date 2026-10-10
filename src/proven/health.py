"""Deterministic, evidence-bound health plan-layout mapping.

The mapper reads a configured public wording only for the explicitly labelled synthetic
household plans. It does not treat a similar product name as evidence for an uploaded
policy, and it does not decide a claim outcome. Every displayed term comes from a matched
verbatim clause; a required but missing policy-start operand produces no date or amount.
"""
from __future__ import annotations

import calendar
import copy
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

from . import corpus

LOB_PATH = Path(__file__).resolve().parent / "lob" / "HEALTH.yaml"
_OUTPUT_STATES = frozenset({
    "DOCUMENTED", "NOT_CONFIRMED", "INFORMATION_MISSING",
    "REQUIRES_AUTHORISED_REVIEW",
})
_EVIDENCE_CLASSES = frozenset({
    "PROVEN", "DECLARED", "MARKET_CONVENTION", "UNESTABLISHED",
})
_PAGE_MARKER = re.compile(r"^=== PAGE (\d+) ===\r?$", re.MULTILINE)


class HealthConfigError(ValueError):
    """The health term data is malformed and cannot safely be applied."""


@dataclass(frozen=True)
class ClauseMatch:
    """One structural match in a cited source page."""

    page: int
    quote: str
    captures: dict[str, str]


@lru_cache(maxsize=1)
def _profiles() -> tuple[dict[str, Any], ...]:
    try:
        raw = yaml.safe_load(LOB_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HealthConfigError(f"could not load health term data: {exc}") from exc
    profiles = raw.get("profiles")
    if raw.get("version") != 1 or not isinstance(profiles, list):
        raise HealthConfigError("health term data must declare version 1 and a profiles list")
    checked: list[dict[str, Any]] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            raise HealthConfigError("each health profile must be an object")
        required = ("id", "country", "product_contains", "corpus_id", "terms")
        if any(not profile.get(key) for key in required) or not isinstance(profile["terms"], list):
            raise HealthConfigError("a health profile is missing its selection or terms")
        for term in profile["terms"]:
            if not isinstance(term, dict) or not all(term.get(key) for key in (
                    "mechanism_code", "label_key", "display", "pattern")):
                raise HealthConfigError(f"{profile['id']}: a term is incomplete")
            try:
                re.compile(str(term["pattern"]))
            except re.error as exc:
                raise HealthConfigError(
                    f"{profile['id']} {term.get('mechanism_code')}: invalid pattern: {exc}") from exc
        checked.append(profile)
    return tuple(checked)


def _profile_for(policy: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    country = str(policy.get("country") or "").upper()
    product = str(policy.get("product") or "").lower()
    for profile in _profiles():
        if profile["country"] == country and str(profile["product_contains"]).lower() in product:
            return profile
    return None


def _pages(document_id: str) -> list[tuple[int, str]]:
    """Load the hash-verified page-marked source text without exposing the PDF."""
    raw = corpus.require_text(document_id).read_text(encoding="utf-8", errors="replace")
    pieces = _PAGE_MARKER.split(raw)
    # [header, page-number, page-text, page-number, page-text, ...]
    out: list[tuple[int, str]] = []
    for index in range(1, len(pieces), 2):
        try:
            page = int(pieces[index])
        except ValueError:
            continue
        out.append((page, pieces[index + 1] if index + 1 < len(pieces) else ""))
    return out or [(1, raw)]


def _find_clause(pages: Iterable[tuple[int, str]], pattern: str) -> Optional[ClauseMatch]:
    expression = re.compile(pattern)
    for page, text in pages:
        match = expression.search(text)
        if match is None:
            continue
        # The exact matched source text is the citation. A deterministic maximum avoids
        # turning a malformed broad pattern into an entire-document disclosure.
        quote = match.group(0).strip()[:600]
        return ClauseMatch(page=page, quote=quote,
                            captures={key: value for key, value in match.groupdict().items()
                                      if value is not None})
    return None


def match_term_pages(country: str, mechanism_code: str,
                     pages: Iterable[tuple[int, str]]) -> Optional[ClauseMatch]:
    """Find one configured mechanism in supplied pages for structural regression tests."""
    code = str(country).upper()
    for profile in _profiles():
        if profile["country"] != code:
            continue
        for term in profile["terms"]:
            if term["mechanism_code"] == mechanism_code:
                return _find_clause(pages, str(term["pattern"]))
    return None


def add_months(start: date, months: int) -> date:
    """Calendar arithmetic for a wording-stated duration, without a default duration."""
    if isinstance(months, bool) or not isinstance(months, int) or months < 0:
        raise ValueError("month offset must be a non-negative integer")
    index = (start.month - 1) + months
    year, month = start.year + index // 12, index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _decimal(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        parsed = Decimal(value.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
    return format(parsed, "f")


def _months(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    words = {"sixty": 60}
    text = value.strip().lower()
    if text in words:
        return words[text]
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number >= 0 else None


def _evidence_class(document: corpus.Document) -> str:
    """Derive the class from the source's author role, never from a caller field."""
    if document.document_class in {"POLICY_WORDING", "BENEFITS_SCHEDULE"}:
        return "PROVEN"
    return "UNESTABLISHED"


def _citation(document: corpus.Document, clause: ClauseMatch) -> dict[str, Any]:
    return {
        "document": document.title,
        "publisher": document.publisher,
        "source_url": document.source_url,
        "page": clause.page,
        "quote": clause.quote,
    }


def _has_declared_ped(policy: Mapping[str, Any]) -> bool:
    return any(str(item.get("mechanism_code") or "") == "WAITING.PED"
               for item in (policy.get("coverage_items") or [])
               if isinstance(item, Mapping))


def _policy_start(policy: Mapping[str, Any]) -> Optional[date]:
    period = policy.get("period")
    raw = period.get("start") if isinstance(period, Mapping) else None
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def _missing_waiting_item(term: Mapping[str, Any], document: corpus.Document,
                          clause: ClauseMatch) -> dict[str, Any]:
    return {
        "mechanism_code": term["mechanism_code"],
        "label_key": term["label_key"],
        "display": term["display"],
        "state": "INFORMATION_MISSING",
        "evidence_class": "UNESTABLISHED",
        "value": None,
        "unit": None,
        "params": {},
        "missing_document_key": "health.document.policy_schedule",
        "citation": _citation(document, clause),
    }


def _item_from_clause(term: Mapping[str, Any], document: corpus.Document,
                      clause: ClauseMatch, policy: Mapping[str, Any]) -> dict[str, Any]:
    display = str(term["display"])
    state = str(term.get("state") or "DOCUMENTED")
    if state not in _OUTPUT_STATES:
        raise HealthConfigError(f"invalid health output state {state!r}")
    evidence_class = _evidence_class(document)
    if evidence_class not in _EVIDENCE_CLASSES:
        raise HealthConfigError("derived an unsupported evidence class")
    item: dict[str, Any] = {
        "mechanism_code": term["mechanism_code"],
        "label_key": term["label_key"],
        "display": display,
        "state": state,
        "evidence_class": evidence_class,
        "value": None,
        "unit": None,
        "params": {},
        "citation": _citation(document, clause),
    }
    captures = clause.captures
    if display == "PED_END_DATE":
        start = _policy_start(policy)
        months = _months(captures.get("months"))
        if start is None or months is None:
            return _missing_waiting_item(term, document, clause)
        item["value"] = add_months(start, months).isoformat()
        item["params"] = {"months": str(months)}
    elif display in {"MONEY", "MONEY_PER_DAY"}:
        value = _decimal(captures.get("amount"))
        if value is None:
            item["state"] = "INFORMATION_MISSING"
            item["evidence_class"] = "UNESTABLISHED"
            item["missing_document_key"] = "health.document.policy_wording"
        else:
            item["value"] = value
            item["unit"] = "INR" if str(policy.get("country")).upper() == "IN" else "SGD"
            if captures.get("percent"):
                item["params"] = {"percent": captures["percent"]}
    elif display == "PERCENT_CONDITION":
        value = _decimal(captures.get("percent"))
        if value is None:
            item["state"] = "INFORMATION_MISSING"
            item["evidence_class"] = "UNESTABLISHED"
            item["missing_document_key"] = "health.document.policy_wording"
        else:
            item["value"] = value
            item["unit"] = "%"
    elif display == "MONTHS_CONDITION":
        months = _months(captures.get("months"))
        if months is None:
            item["state"] = "INFORMATION_MISSING"
            item["evidence_class"] = "UNESTABLISHED"
            item["missing_document_key"] = "health.document.policy_wording"
        else:
            item["value"] = str(months)
            item["unit"] = "months"
    return item


def assess_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Return mapped health terms for one known synthetic policy.

    Corpus absence is an explicit incomplete-evidence result. Hash drift is deliberately
    not swallowed: a present-but-wrong source cannot support a citation and needs the
    corpus remediation path, rather than a reassuring empty card.
    """
    profile = _profile_for(policy)
    if profile is None:
        return {"mapped": False, "items": []}
    try:
        document = corpus.resolve(str(profile["corpus_id"]))
        pages = _pages(document.id)
    except corpus.CorpusMissing:
        return {
            "mapped": False,
            "items": [],
            "notice_key": "health.notice.wording_missing",
            "missing_document_key": "health.document.policy_wording",
        }

    items: list[dict[str, Any]] = []
    for term in profile["terms"]:
        if term.get("requires_declared_ped") and not _has_declared_ped(policy):
            continue
        clause = _find_clause(pages, str(term["pattern"]))
        if clause is not None:
            items.append(_item_from_clause(term, document, clause, policy))
    return {
        "mapped": bool(items),
        "profile": profile["id"],
        "items": items,
        "notice_key": None if items else "health.notice.wording_not_matched",
    }


def enrich_portfolio(portfolio: Mapping[str, Any]) -> dict[str, Any]:
    """Add read-only health mapping to the verified synthetic household response.

    Matching only the server-stamped synthetic origin prevents an uploaded policy from
    inheriting a term merely because its user-entered product name resembles a public plan.
    """
    out = copy.deepcopy(dict(portfolio))
    policies = out.get("policies")
    if not isinstance(policies, list):
        return out
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        if (policy.get("source") != "SYNTHETIC_EXAMPLE"
                or str(policy.get("line_of_business") or "").upper() != "HEALTH"):
            continue
        result = assess_policy(policy)
        policy["health_mapping"] = {
            key: value for key, value in result.items() if key != "items"
        }
        policy["health_items"] = result["items"]
    return out
