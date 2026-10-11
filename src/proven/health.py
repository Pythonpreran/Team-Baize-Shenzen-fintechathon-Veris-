"""Deterministic, evidence-bound health plan-layout mapping.

The mapper reads a configured public wording only for the explicitly labelled synthetic
household plans. It does not treat a similar product name as evidence for an uploaded
policy, and it does not decide a claim outcome. Every displayed term comes from a matched
verbatim clause; a required but missing policy-start or member operand produces no date
or amount and names the schedule that would establish it.
"""
from __future__ import annotations

import calendar
import copy
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

from . import corpus, household

LOB_PATH = Path(__file__).resolve().parent / "lob" / "HEALTH.yaml"
_OUTPUT_STATES = frozenset({
    "DOCUMENTED", "NOT_CONFIRMED", "INFORMATION_MISSING",
    "REQUIRES_AUTHORISED_REVIEW",
})
_EVIDENCE_CLASSES = frozenset({
    "PROVEN", "DECLARED", "MARKET_CONVENTION", "UNESTABLISHED",
})
_PAGE_MARKER = re.compile(r"^=== PAGE (\d+) ===\r?$", re.MULTILINE)
_MEMBER_SPECS = {member.key: member for member in household.MEMBERS}


class HealthConfigError(ValueError):
    """The health term data is malformed and cannot safely be applied."""


@dataclass(frozen=True)
class ClauseMatch:
    """One structural match in a cited source page."""

    page: int
    quote: str
    captures: dict[str, str]


@dataclass(frozen=True)
class ScheduleContext:
    """Verified schedule operands retained beside a derived health item.

    This context exists only for the server-stamped synthetic fixture after the loader
    has verified the uploaded document bytes. It prevents a public wording citation
    from standing in for a schedule-derived date, member scope, or shared-floater fact.
    """

    policy_start: date
    members: tuple[dict[str, str], ...]
    declared_member: Optional[dict[str, str]]
    declared_condition: Optional[str]
    family_floater: bool
    citations: Mapping[str, dict[str, Any]]


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
    """Load text only after both the PDF original and extraction are hash-verified."""
    # The page-marked text can be altered independently of the PDF. Requiring the
    # original first makes the pair a single evidence unit; require_text then verifies
    # the manifest's expected extraction hash where the profile declares one.
    corpus.require_original(document_id)
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


def _days(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    words = {"thirty": 30}
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


def _policy_start(policy: Mapping[str, Any]) -> Optional[date]:
    period = policy.get("period")
    raw = period.get("start") if isinstance(period, Mapping) else None
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def _fixture_policy_for(policy: Mapping[str, Any]) -> Optional[household.PolicySpec]:
    """Return the immutable synthetic policy spec only for a stamped fixture record."""
    if policy.get("source") != "SYNTHETIC_EXAMPLE":
        return None
    period = policy.get("period")
    if not isinstance(period, Mapping):
        return None
    for spec in household.POLICIES:
        if (str(policy.get("country") or "").upper() == spec.country
                and str(policy.get("line_of_business") or "").upper() == spec.line_of_business
                and policy.get("product") == spec.product
                and policy.get("masked_number") == spec.masked_number
                and period.get("start") == spec.period_start
                and period.get("end") == spec.period_end):
            return spec
    return None


def _schedule_citation(spec: household.PolicySpec, source_document_id: str,
                       quote: str, key: str) -> dict[str, Any]:
    return {
        "document": spec.document_filename,
        "page": 1,
        "quote": quote,
        "source_document_id": source_document_id,
        "evidence_span_id": f"synthetic:{spec.document_filename}:health-{key}",
    }


def schedule_context_for(policy: Mapping[str, Any],
                         members_by_id: Mapping[str, Mapping[str, Any]]) -> Optional[ScheduleContext]:
    """Bind a health policy's derived terms to its uploaded schedule operands.

    The lookup deliberately fails closed unless the persisted policy is the exact
    server-stamped household fixture, its source document is singular, and every member
    retained in the portfolio still matches the fixture's name, date of birth, and
    schedule-stated age. An uploaded lookalike therefore cannot inherit these facts.
    """
    spec = _fixture_policy_for(policy)
    if spec is None or spec.health_schedule is None:
        return None
    source_ids = policy.get("source_document_ids")
    if not isinstance(source_ids, list) or len(source_ids) != 1:
        return None
    source_document_id = source_ids[0]
    if not isinstance(source_document_id, str) or not source_document_id:
        return None
    start = _policy_start(policy)
    if start is None:
        return None
    insured_ids = policy.get("insured_member_ids")
    if not isinstance(insured_ids, list) or len(insured_ids) != len(spec.member_keys):
        return None

    schedule = spec.health_schedule
    configured_ages = dict(schedule.member_ages)
    scoped_members: list[dict[str, str]] = []
    member_by_key: dict[str, dict[str, str]] = {}
    for key in spec.member_keys:
        expected = _MEMBER_SPECS.get(key)
        if expected is None:
            return None
        matches = [member_id for member_id in insured_ids
                   if isinstance(member_id, str)
                   and isinstance(members_by_id.get(member_id), Mapping)
                   and members_by_id[member_id].get("name") == expected.name
                   and members_by_id[member_id].get("date_of_birth") == expected.date_of_birth]
        if len(matches) != 1:
            return None
        expected_age = configured_ages.get(key)
        if expected_age is not None and (
                f"{expected.name} (age {expected_age})" not in schedule.insured_members_quote):
            return None
        member = {"id": matches[0], "name": expected.name}
        if expected_age is not None:
            member["age"] = str(expected_age)
        scoped_members.append(member)
        member_by_key[key] = member

    citations: dict[str, dict[str, Any]] = {
        "policy_start": _schedule_citation(
            spec, source_document_id, schedule.policy_start_quote, "policy-start"),
        "members": _schedule_citation(
            spec, source_document_id, schedule.insured_members_quote, "members"),
    }
    if schedule.declared_condition_quote:
        citations["declared_condition"] = _schedule_citation(
            spec, source_document_id, schedule.declared_condition_quote, "declared-condition")
    if schedule.family_floater_quote:
        citations["family_floater"] = _schedule_citation(
            spec, source_document_id, schedule.family_floater_quote, "family-floater")
    declared_member = (member_by_key.get(schedule.declared_member_key)
                       if schedule.declared_member_key else None)
    if schedule.declared_member_key and (declared_member is None or not schedule.declared_condition
                                         or "declared_condition" not in citations):
        return None
    return ScheduleContext(
        policy_start=start,
        members=tuple(scoped_members),
        declared_member=declared_member,
        declared_condition=schedule.declared_condition,
        family_floater=schedule.family_floater,
        citations=citations,
    )


def _base_item(term: Mapping[str, Any], document: corpus.Document,
               clause: ClauseMatch) -> dict[str, Any]:
    state = str(term.get("state") or "DOCUMENTED")
    if state not in _OUTPUT_STATES:
        raise HealthConfigError(f"invalid health output state {state!r}")
    evidence_class = _evidence_class(document)
    if evidence_class not in _EVIDENCE_CLASSES:
        raise HealthConfigError("derived an unsupported evidence class")
    return {
        "mechanism_code": term["mechanism_code"],
        "label_key": term["label_key"],
        "display": str(term["display"]),
        "state": state,
        "evidence_class": evidence_class,
        "value": None,
        "unit": None,
        "params": {},
        "citation": _citation(document, clause),
    }


def _missing_schedule_item(term: Mapping[str, Any], document: corpus.Document,
                           clause: ClauseMatch) -> dict[str, Any]:
    item = _base_item(term, document, clause)
    item.update({
        "state": "INFORMATION_MISSING",
        "evidence_class": "UNESTABLISHED",
        "value": None,
        "unit": None,
        "params": {},
        "missing_document_key": str(term.get(
            "missing_document_key") or "health.document.policy_schedule"),
    })
    return item


def _attach_schedule(item: dict[str, Any], context: ScheduleContext,
                     citation_keys: Iterable[str], *,
                     scope: Iterable[Mapping[str, str]] = (),
                     scope_reason_key: Optional[str] = None) -> None:
    citations = [dict(context.citations[key]) for key in citation_keys
                 if key in context.citations]
    if citations:
        item["operand_citations"] = citations
    scoped = [dict(member) for member in scope]
    if scoped:
        item["member_scope"] = scoped
    if scope_reason_key:
        item["scope_reason_key"] = scope_reason_key


def _item_from_clause(term: Mapping[str, Any], document: corpus.Document,
                      clause: ClauseMatch,
                      context: Optional[ScheduleContext]) -> dict[str, Any]:
    """Build one term, withholding a derived value until every operand is cited."""
    def temporal_operands() -> Optional[tuple[date, int, int, bool]]:
        """Read the date controls from the schedule span that establishes all four.

        A current policy period does not establish the first-policy date, prior
        continuity, portability or migration credits, or whether the sum insured was
        enhanced. The synthetic fixture records all of them in one exact schedule span;
        any other shape returns no date rather than treating the current period as the
        first policy.
        """
        if context is None:
            return None
        citation = context.citations.get("policy_start")
        quote = citation.get("quote") if isinstance(citation, Mapping) else None
        if not isinstance(quote, str):
            return None
        first_policy = re.search(
            r"\bfirst\s+policy\s+commencement\s*:\s*"
            r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b", quote, re.IGNORECASE)
        continuity = re.search(
            r"\bcontinuous\s+coverage\s+before\s+this\s+period\s*:\s*"
            r"(\d+)\s+months?\b", quote, re.IGNORECASE)
        portability = re.search(
            r"\bportability\s+or\s+migration\s+credit\s*:\s*"
            r"(\d+)\s+months?\b", quote, re.IGNORECASE)
        enhancement = re.search(
            r"\bsum[-\s]insured\s+enhancement\s*:\s*(none|no|yes|present)\b",
            quote, re.IGNORECASE)
        if not all((first_policy, continuity, portability, enhancement)):
            return None
        try:
            first_start = datetime.strptime(first_policy.group(1), "%d %B %Y").date()
            continuous_months = int(continuity.group(1))
            portability_months = int(portability.group(1))
        except ValueError:
            return None
        if continuous_months < 0 or portability_months < 0:
            return None
        enhanced = enhancement.group(1).lower() in {"yes", "present"}
        return first_start, continuous_months, portability_months, enhanced

    def unadjusted_date_base() -> Optional[date]:
        operands = temporal_operands()
        if operands is None:
            return None
        first_start, continuous_months, portability_months, enhanced = operands
        if continuous_months or portability_months or enhanced:
            return None
        return first_start

    def has_initial_waiting_exception() -> bool:
        operands = temporal_operands()
        if operands is None:
            return False
        _first_start, continuous_months, _portability_months, enhanced = operands
        # The wording removes the initial wait after more than twelve months of
        # continuous coverage. An enhancement has a separate application, so it stays
        # withheld instead of flattening two different limits into one date.
        return continuous_months > 12 and not enhanced

    if term.get("requires_declared_ped") and (
            context is None or context.declared_member is None
            or not context.declared_condition):
        return _missing_schedule_item(term, document, clause)
    if term.get("requires_policy_start") and context is None:
        return _missing_schedule_item(term, document, clause)
    if term.get("requires_member_scope") and (context is None or not context.members):
        return _missing_schedule_item(term, document, clause)
    if term.get("requires_specific_schedule_selection"):
        # The public wording records a 24/36-month range. This schedule has no selected
        # disease/period operand, so displaying either duration would invent a value.
        return _missing_schedule_item(term, document, clause)

    item = _base_item(term, document, clause)
    display = item["display"]
    captures = clause.captures
    if display in {"PED_END_DATE", "MORATORIUM_END_DATE"}:
        months = _months(captures.get("months"))
        base = unadjusted_date_base()
        if months is None or base is None:
            return _missing_schedule_item(term, document, clause)
        item["value"] = add_months(base, months).isoformat()
        item["params"] = {"months": str(months)}
        citation_keys = ("policy_start",)
        scope: tuple[Mapping[str, str], ...] = ()
        reason = None
        if display == "PED_END_DATE":
            citation_keys = ("policy_start", "declared_condition")
            scope = (context.declared_member,) if context.declared_member else ()
            reason = "health.scope.declared_condition"
        _attach_schedule(item, context, citation_keys, scope=scope,
                         scope_reason_key=reason)
    elif display == "DAYS_END_DATE":
        if has_initial_waiting_exception():
            item.update({
                "label_key": "health.item.initial_waiting_exception",
                "display": "CONDITION",
                "value": None,
                "unit": None,
                "params": {},
            })
            _attach_schedule(item, context, ("policy_start",))
            return item
        days = _days(captures.get("days"))
        base = unadjusted_date_base()
        if days is None or base is None:
            return _missing_schedule_item(term, document, clause)
        item["value"] = date.fromordinal(base.toordinal() + days).isoformat()
        item["params"] = {"days": str(days)}
        _attach_schedule(item, context, ("policy_start",))
    elif display in {"MONEY", "MONEY_PER_DAY"}:
        value = _decimal(captures.get("amount"))
        if value is None:
            item["state"] = "INFORMATION_MISSING"
            item["evidence_class"] = "UNESTABLISHED"
            item["missing_document_key"] = "health.document.policy_wording"
        else:
            item["value"] = value
            item["unit"] = "INR" if str(term.get("currency") or "").upper() == "INR" \
                else "INR" if document.country == "IN" else "SGD"
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
            if context is not None and term.get("requires_member_scope"):
                citation_keys = ["members"]
                if context.family_floater:
                    citation_keys.append("family_floater")
                _attach_schedule(
                    item, context, citation_keys, scope=context.members,
                    scope_reason_key=str(term.get(
                        "scope_reason_key") or "health.scope.all_named_members"))
    elif display == "PERCENT_OF_SUM_INSURED":
        value = _decimal(captures.get("percent"))
        if value is None:
            item["state"] = "INFORMATION_MISSING"
            item["evidence_class"] = "UNESTABLISHED"
            item["missing_document_key"] = "health.document.policy_wording"
        else:
            item["value"] = value
            item["unit"] = "%"
            if captures.get("maximum"):
                item["params"] = {"maximum": captures["maximum"]}
    elif display in {"MONTHS_CONDITION", "DAYS_CONDITION", "HOURS_CONDITION"}:
        key = {"MONTHS_CONDITION": "months", "DAYS_CONDITION": "days",
               "HOURS_CONDITION": "hours"}[display]
        parser = _months if key == "months" else _days
        value = parser(captures.get(key))
        if value is None:
            item["state"] = "INFORMATION_MISSING"
            item["evidence_class"] = "UNESTABLISHED"
            item["missing_document_key"] = "health.document.policy_wording"
        else:
            item["value"] = str(value)
            item["unit"] = key
    return item


def _family_floater_item(context: ScheduleContext) -> Optional[dict[str, Any]]:
    if not context.family_floater or "family_floater" not in context.citations:
        return None
    return {
        "mechanism_code": "LIMIT.FAMILY_FLOATER_SHARED",
        "label_key": "health.item.family_floater_shared",
        "display": "CONDITION",
        "state": "DOCUMENTED",
        "evidence_class": "PROVEN",
        "value": None,
        "unit": None,
        "params": {},
        "citation": dict(context.citations["family_floater"]),
        "member_scope": [dict(member) for member in context.members],
        "scope_reason_key": "health.scope.family_floater",
    }


def assess_policy(policy: Mapping[str, Any], *,
                  schedule: Optional[ScheduleContext] = None) -> dict[str, Any]:
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
        clause = _find_clause(pages, str(term["pattern"]))
        if clause is not None:
            items.append(_item_from_clause(term, document, clause, schedule))
    floater = _family_floater_item(schedule) if schedule is not None else None
    if floater is not None:
        items.append(floater)
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
    members = out.get("members")
    members_by_id = {
        str(member.get("id")): member for member in (members or [])
        if isinstance(member, Mapping) and isinstance(member.get("id"), str)
    }
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        if (policy.get("source") != "SYNTHETIC_EXAMPLE"
                or str(policy.get("line_of_business") or "").upper() != "HEALTH"):
            continue
        result = assess_policy(policy, schedule=schedule_context_for(policy, members_by_id))
        policy["health_mapping"] = {
            key: value for key, value in result.items() if key != "items"
        }
        policy["health_items"] = result["items"]
    return out
