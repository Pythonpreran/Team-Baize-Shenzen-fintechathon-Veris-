"""Evidence-bound home and travel plan-layout mapping (T26).

Every wording-derived item here comes out of align.py's propose -> verify -> bind over a
public wording held in the corpus: the mechanism is recognised by the structure of its
sentence, never by a heading or a clause number, and the card quotes the bound sentence
with its page. Schedule operands come only from exact, cited schedule lines of the
server-stamped synthetic household; a missing or inconsistent operand produces no
number and names the document that would establish it.

Two deterministic findings sit on top of the clauses:

  * the Singapore contents comparison: the schedule's contents sum insured against the
    household's own (self-declared) contents list, read through the wording's
    insurance-to-value sentence. The figures are DECLARED because one operand is
    self-authored, and the proportion is labelled an illustration, never an amount;
  * the India built-in contents cover: the wording's own stated share of the building
    sum insured and its cap, applied to the schedule's cited building sum insured.

Nothing in this module decides cover, states a claim outcome or recommends a change.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

from . import align, corpus, grading, household
from .health import _pages as _verified_pages
from .models import Operand

LOB_DIR = Path(__file__).resolve().parent / "lob"
LINES = {"HOME": LOB_DIR / "HOME.yaml", "TRAVEL": LOB_DIR / "TRAVEL.yaml"}
_DISPLAYS = frozenset({"CONDITION", "MONEY", "DAYS_CONDITION", "HOURS_CONDITION", "PLAN_LIMIT"})
_CURRENCY = {"SG": "SGD", "IN": "INR"}
_MONEY = re.compile(r"\b(SGD|INR)\s*([\d,]+(?:\.\d+)?)")
# A sentence ends at a full stop or semicolon before whitespace, or just before the next
# bullet: these wordings list conditions as bullets ending in ";" rather than ".".
_SENTENCE_END = re.compile(r"[.;](?=\s|$)|(?=\u2022)")
_QUOTE_LIMIT = 600
_LAKH = Decimal("100000")
_TWO = Decimal("0.01")


class HomeTravelConfigError(ValueError):
    """The home or travel profile data is malformed and cannot safely be applied."""


@lru_cache(maxsize=1)
def _profiles() -> tuple[dict[str, Any], ...]:
    checked: list[dict[str, Any]] = []
    known = align.mechanisms()
    for line, path in LINES.items():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise HomeTravelConfigError(f"could not load {path.name}: {exc}") from exc
        if raw.get("version") != 1 or not isinstance(raw.get("profiles"), list):
            raise HomeTravelConfigError(f"{path.name} must declare version 1 and a profiles list")
        for profile in raw["profiles"]:
            if not isinstance(profile, dict) or any(not profile.get(key) for key in (
                    "id", "country", "line_of_business", "product_contains", "corpus_id", "terms")):
                raise HomeTravelConfigError(f"{path.name}: a profile is missing its selection or terms")
            if profile["line_of_business"] != line:
                raise HomeTravelConfigError(f"{profile['id']}: declared in the wrong line file")
            for term in profile["terms"]:
                if not isinstance(term, dict) or not all(term.get(key) for key in (
                        "mechanism", "mechanism_code", "label_key", "display")):
                    raise HomeTravelConfigError(f"{profile['id']}: a term is incomplete")
                if term["display"] not in _DISPLAYS:
                    raise HomeTravelConfigError(f"{profile['id']}: unknown display {term['display']!r}")
                if term["mechanism"] not in known:
                    raise HomeTravelConfigError(
                        f"{profile['id']}: {term['mechanism']} is not a structural mechanism")
            checked.append(profile)
    return tuple(checked)


def _fixture_spec(policy: Mapping[str, Any]) -> Optional[household.PolicySpec]:
    """The immutable synthetic spec, only for a server-stamped household record.

    `source` is SYNTHETIC_EXAMPLE only after the repository's encrypted seed binding
    verified the uploaded generated PDF, so a lookalike upload never inherits these terms.
    """
    if policy.get("source") != "SYNTHETIC_EXAMPLE":
        return None
    documents = policy.get("source_document_ids")
    if not isinstance(documents, list) or len(documents) != 1 or not documents[0]:
        return None
    period = policy.get("period") if isinstance(policy.get("period"), Mapping) else {}
    for spec in household.POLICIES:
        if (spec.line_of_business in LINES
                and str(policy.get("country") or "").upper() == spec.country
                and str(policy.get("line_of_business") or "").upper() == spec.line_of_business
                and policy.get("product") == spec.product
                and policy.get("masked_number") == spec.masked_number
                and period.get("start") == spec.period_start
                and period.get("end") == spec.period_end):
            return spec
    return None


def _profile_for(spec: household.PolicySpec) -> Optional[dict[str, Any]]:
    for profile in _profiles():
        if (profile["country"] == spec.country
                and profile["line_of_business"] == spec.line_of_business
                and str(profile["product_contains"]).lower() in spec.product.lower()):
            return profile
    return None


def _evidence_class(document: corpus.Document) -> str:
    """Derived from the source's authorship, never from a caller field."""
    return "PROVEN" if document.document_class in {"POLICY_WORDING", "BENEFITS_SCHEDULE"} else "UNESTABLISHED"


def _sentence(page_text: str, alignment: align.Alignment, mechanism: align.Mechanism) -> str:
    """The bound sentence: from the sentence holding the first structural slot to the end
    of the sentence holding the last, verbatim apart from whitespace."""
    region = page_text[alignment.char_start:alignment.char_end]
    spans = [m.span() for m in (slot.find(region) for slot in mechanism.mandatory) if m]
    if not spans:
        return alignment.quote[:_QUOTE_LIMIT]
    low = alignment.char_start + min(start for start, _end in spans)
    high = alignment.char_start + max(end for _start, end in spans)
    boundary = -1
    for marker in (". ", ".\r\n", ".\n", "\u2022", "\r\n\r\n", "\n\n"):
        found = page_text.rfind(marker, max(0, low - 400), low)
        if found >= 0:
            boundary = max(boundary, found + len(marker))
    start = boundary if boundary >= 0 else low
    end_match = _SENTENCE_END.search(page_text, high, min(len(page_text), high + 400))
    end = end_match.end() if end_match else high
    return " ".join(page_text[start:end].split())[:_QUOTE_LIMIT]


def _citation(document: corpus.Document, page_no: int, quote: str) -> dict[str, Any]:
    return {"document": document.title, "publisher": document.publisher,
            "source_url": document.source_url, "page": page_no, "quote": quote}


def _schedule_citation(policy: Mapping[str, Any], spec: household.PolicySpec,
                       quote: str, key: str) -> dict[str, Any]:
    return {"document": spec.document_filename, "page": 1, "quote": quote,
            "source_document_id": policy["source_document_ids"][0],
            "evidence_span_id": f"synthetic:{spec.document_filename}:{key}"}


def _number(raw: object) -> Optional[Decimal]:
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


def _money(line: Optional[str]) -> list[tuple[str, Decimal]]:
    out = []
    for currency, amount in _MONEY.findall(line or ""):
        value = _number(amount)
        if value is not None:
            out.append((currency, value))
    return out


def _persisted_limit(policy: Mapping[str, Any]) -> Optional[tuple[str, Decimal]]:
    for item in policy.get("coverage_items") or []:
        if isinstance(item, Mapping) and item.get("mechanism_code") == "LIMIT.SUM_INSURED":
            value = _number(item.get("value"))
            if value is not None and item.get("unit"):
                return str(item["unit"]), value
    return None


def _base_item(term: Mapping[str, Any], document: corpus.Document,
               alignment: align.Alignment, quote: str) -> dict[str, Any]:
    return {
        "mechanism_code": term["mechanism_code"], "label_key": term["label_key"],
        "display": term["display"], "state": "DOCUMENTED",
        "evidence_class": _evidence_class(document), "value": None, "unit": None, "params": {},
        "citation": _citation(document, alignment.page_no, quote),
        "structural_mechanism": alignment.mechanism, "taxonomy_code": alignment.taxonomy_code,
        "binding": "PROPOSE_VERIFY_BIND",
    }


def _missing(item: dict[str, Any], document_key: str) -> dict[str, Any]:
    item.update({"state": "INFORMATION_MISSING", "evidence_class": "UNESTABLISHED",
                 "value": None, "unit": None, "params": {}, "missing_document_key": document_key})
    return item


def _term_item(term: Mapping[str, Any], document: corpus.Document, alignment: align.Alignment,
               quote: str, policy: Mapping[str, Any], spec: household.PolicySpec) -> dict[str, Any]:
    item = _base_item(term, document, alignment, quote)
    display = term["display"]
    if display == "PLAN_LIMIT":
        # The wording points at a per-plan benefits table. With no selected plan on the
        # schedule there is no figure to show, so the item names what would establish it.
        missing = _missing(item, "travel.document.selected_plan")
        plan_quote = dict(spec.schedule_quotes).get("selected_plan")
        if plan_quote:
            missing["operand_citations"] = [_schedule_citation(policy, spec, plan_quote, "selected-plan")]
        return missing
    slot = term.get("value_slot")
    if not slot:
        return item
    value = _number(alignment.values.get(slot))
    if value is None:
        return _missing(item, "home_travel.document.policy_wording")
    item["value"] = format(value, "f")
    if display == "MONEY":
        item["unit"] = _CURRENCY.get(document.country)
        if item["unit"] is None:
            return _missing(item, "home_travel.document.policy_wording")
    else:
        item["unit"] = "days" if display == "DAYS_CONDITION" else "hours"
    return item


def _grade(operands: Iterable[tuple[str, Optional[str], str]], computation: Optional[str]) -> str:
    """Weakest link over (name, span id, author class), via the grading module."""
    graded = [Operand(name=name, value=None, unit=None, span_id=span,
                      evidence_class=grading.grade_operand(span, author))
              for name, span, author in operands]
    return grading.grade_finding(graded, computation, has_computation=True)


def contents_comparison(policy: Mapping[str, Any], spec: household.PolicySpec,
                        profile: Mapping[str, Any], bound: Mapping[str, align.Alignment],
                        pages: list[str], document: corpus.Document) -> Optional[dict[str, Any]]:
    """Schedule contents sum insured against the owner's own contents list."""
    config = profile.get("contents_comparison")
    contents = household.CONTENTS_LIST
    if not isinstance(config, Mapping) or contents.for_masked_number != spec.masked_number:
        return None
    item: dict[str, Any] = {
        "mechanism_code": "LIMIT.CONTENTS_BELOW_LIST", "label_key": "home.item.contents_below_list",
        "display": "MONEY_GAP", "state": "DOCUMENTED", "evidence_class": "UNESTABLISHED",
        "value": None, "unit": None, "params": {}, "illustration": True,
        "notice_key": "home.notice.contents_illustration",
    }
    sum_quote = dict(spec.schedule_quotes).get(str(config.get("sum_insured_quote") or ""))
    schedule_amounts = _money(sum_quote)
    persisted = _persisted_limit(policy)
    if len(schedule_amounts) != 1 or persisted != schedule_amounts[0]:
        return _missing(item, "home.document.contents_schedule")
    currency, sum_insured = schedule_amounts[0]

    item_amounts = [amount for line in contents.item_lines for amount in _money(line)]
    stated_total = _money(contents.total_line)
    if (not item_amounts or len(stated_total) != 1
            or any(code != currency for code, _value in [*item_amounts, *stated_total])):
        return _missing(item, "home.document.contents_list")
    added = sum((value for _code, value in item_amounts), Decimal("0"))
    if added != stated_total[0][1]:
        # The list's own lines and its total disagree. Neither figure is used.
        return _missing(item, "home.document.contents_list")

    alignment = bound.get(str(config.get("condition_mechanism") or ""))
    if alignment is None:
        return _missing(item, "home_travel.document.policy_wording")
    mechanism = align.mechanisms()[alignment.mechanism]
    quote = _sentence(pages[alignment.page_no - 1], alignment, mechanism)
    item["citation"] = _citation(document, alignment.page_no, quote)
    item["operand_citations"] = [
        _schedule_citation(policy, spec, sum_quote, "contents-sum-insured"),
        {"document": contents.document_filename, "page": 1,
         "quote": " ".join([*contents.item_lines, contents.total_line]),
         "evidence_span_id": f"synthetic:{contents.document_filename}:contents-list",
         "author": "SELF"},
    ]
    item["evidence_class"] = _grade(
        [("contents_sum_insured", f"synthetic:{spec.document_filename}:contents-sum-insured", "INSURER"),
         ("contents_list_total", f"synthetic:{contents.document_filename}:contents-list", "SELF")],
        computation=f"{document.id}:p{alignment.page_no}:{alignment.char_start}")
    shortfall = added - sum_insured
    if shortfall <= 0:
        item.update({"label_key": "home.item.contents_within_list", "display": "CONDITION",
                     "illustration": False, "notice_key": None})
        return item
    proportion = (sum_insured / added * Decimal(100)).quantize(_TWO, rounding=ROUND_HALF_UP)
    item.update({"value": format(shortfall, "f"), "unit": currency, "params": {
        "sum_insured": format(sum_insured, "f"), "contents_list": format(added, "f"),
        "proportion_percent": format(proportion, "f")}})
    return item


def built_in_contents(policy: Mapping[str, Any], spec: household.PolicySpec,
                      profile: Mapping[str, Any], bound: Mapping[str, align.Alignment],
                      pages: list[str], document: corpus.Document) -> Optional[dict[str, Any]]:
    """The wording's built-in general-contents cover from its own share and cap."""
    config = profile.get("built_in_contents")
    if not isinstance(config, Mapping):
        return None
    item: dict[str, Any] = {
        "mechanism_code": "LIMIT.CONTENTS_BUILT_IN", "label_key": "home.item.built_in_contents",
        "display": "MONEY", "state": "DOCUMENTED", "evidence_class": "UNESTABLISHED",
        "value": None, "unit": None, "params": {},
    }
    alignment = bound.get(str(config.get("mechanism") or ""))
    if alignment is None:
        return _missing(item, "home_travel.document.policy_wording")
    mechanism = align.mechanisms()[alignment.mechanism]
    item["citation"] = _citation(document, alignment.page_no,
                                 _sentence(pages[alignment.page_no - 1], alignment, mechanism))
    percent = _number(alignment.values.get("percent"))
    cap_lakh = _number(alignment.values.get("maximum_lakh"))
    building_quote = dict(spec.schedule_quotes).get(str(config.get("building_quote") or ""))
    building = _money(building_quote)
    persisted = _persisted_limit(policy)
    if percent is None or cap_lakh is None:
        return _missing(item, "home_travel.document.policy_wording")
    if len(building) != 1 or persisted != building[0] or building[0][0] != "INR":
        return _missing(item, "home.document.building_schedule")
    share = (building[0][1] * percent / Decimal(100)).quantize(_TWO, rounding=ROUND_HALF_UP)
    cap = cap_lakh * _LAKH
    item.update({
        "value": format(min(share, cap).normalize(), "f"), "unit": "INR",
        "evidence_class": _grade(
            [("building_sum_insured", f"synthetic:{spec.document_filename}:building-sum-insured", "INSURER")],
            computation=f"{document.id}:p{alignment.page_no}:{alignment.char_start}"),
        "params": {"percent": format(percent, "f"), "cap": format(cap, "f"),
                   "building_sum_insured": format(building[0][1], "f")},
        "operand_citations": [_schedule_citation(policy, spec, building_quote, "building-sum-insured")],
    })
    return item


def _travellers(policy: Mapping[str, Any], spec: household.PolicySpec,
                profile: Mapping[str, Any],
                members_by_id: Mapping[str, Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    quote = dict(spec.schedule_quotes).get(str(profile.get("travellers_quote") or ""))
    if not quote:
        return None
    named = []
    for member_id in policy.get("insured_member_ids") or []:
        member = members_by_id.get(str(member_id))
        name = str(member.get("name") or "") if isinstance(member, Mapping) else ""
        if not name or name not in quote:
            # A traveller the schedule does not name is never added to the scope.
            return None
        named.append({"id": str(member_id), "name": name})
    if not named:
        return None
    return {
        "mechanism_code": "COVERED.TRAVELLERS", "label_key": "travel.item.travellers",
        "display": "CONDITION", "state": "DOCUMENTED", "evidence_class": "PROVEN",
        "value": None, "unit": None, "params": {},
        "citation": _schedule_citation(policy, spec, quote, "travellers"),
        "member_scope": named, "scope_reason_key": "travel.scope.schedule_travellers",
    }


def assess_policy(policy: Mapping[str, Any], *,
                  members_by_id: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, Any]:
    """Mapped home or travel terms for one server-stamped synthetic policy."""
    spec = _fixture_spec(policy)
    profile = _profile_for(spec) if spec is not None else None
    if spec is None or profile is None:
        return {"mapped": False, "items": []}
    try:
        document = corpus.resolve(str(profile["corpus_id"]))
        pages = [text for _page, text in _verified_pages(document.id)]
    except corpus.CorpusMissing:
        return {"mapped": False, "items": [], "notice_key": "home_travel.notice.wording_missing",
                "missing_document_key": "home_travel.document.policy_wording"}

    wanted = {term["mechanism"] for term in profile["terms"]}
    for key in ("contents_comparison", "built_in_contents"):
        config = profile.get(key)
        if isinstance(config, Mapping):
            wanted.add(str(config.get("condition_mechanism") or config.get("mechanism") or ""))
    result = align.align_pages(pages, carrier=str(profile["id"]), only=sorted(m for m in wanted if m))
    bound: dict[str, align.Alignment] = {}
    for alignment in sorted(result.alignments, key=lambda a: (a.page_no, a.char_start)):
        bound.setdefault(alignment.mechanism, alignment)

    items: list[dict[str, Any]] = []
    for term in profile["terms"]:
        alignment = bound.get(term["mechanism"])
        if alignment is None:
            continue                     # not found in this wording: no item, no default
        mechanism = align.mechanisms()[alignment.mechanism]
        quote = _sentence(pages[alignment.page_no - 1], alignment, mechanism)
        items.append(_term_item(term, document, alignment, quote, policy, spec))
    for finding in (contents_comparison(policy, spec, profile, bound, pages, document),
                    built_in_contents(policy, spec, profile, bound, pages, document),
                    _travellers(policy, spec, profile, members_by_id or {})):
        if finding is not None:
            items.append(finding)
    return {"mapped": bool(items), "profile": profile["id"], "items": items,
            "notice_key": None if items else "home_travel.notice.wording_not_matched",
            "property_label": spec.property_label}


def enrich_portfolio(portfolio: Mapping[str, Any]) -> dict[str, Any]:
    """Add read-only home and travel mapping to the verified synthetic household response."""
    out = copy.deepcopy(dict(portfolio))
    policies = out.get("policies")
    if not isinstance(policies, list):
        return out
    members_by_id = {str(member.get("id")): member for member in out.get("members") or []
                     if isinstance(member, Mapping) and member.get("id")}
    for policy in policies:
        if (not isinstance(policy, dict)
                or str(policy.get("line_of_business") or "").upper() not in LINES
                or policy.get("source") != "SYNTHETIC_EXAMPLE"):
            continue
        result = assess_policy(policy, members_by_id=members_by_id)
        policy["property_travel_mapping"] = {key: value for key, value in result.items()
                                             if key not in {"items", "property_label"}}
        policy["property_travel_items"] = result["items"]
        if result.get("property_label"):
            policy["property_label"] = result["property_label"]
    return out
