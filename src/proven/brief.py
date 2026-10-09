"""Layer 4 — Review Brief assembly.

Every Brief carries a computed (not asserted) accounting header, then graded
findings sorted PROVEN-first, then a searched / not-searched statement.

The `never` flags (assert_breach, assert_claim_outcome) are enforced HERE, in the
renderer — not left to prompt wording. A rule carrying assert_breach physically
cannot emit a breach conclusion: this module raises if a headline for such a rule
contains adjudicating language.
"""
from __future__ import annotations

import re
from typing import Optional

from .account import Accounting
from .grading import CLASS_SORT_ORDER
from .models import Finding

# Language a finding flagged `assert_breach` may never contain.
_BREACH_WORDS = re.compile(
    r"\b(is in breach|has breached|breaches the|void(?:ed)?|will not be paid|"
    r"claim will fail|is not covered|forfeits? cover|denied)\b",
    re.IGNORECASE,
)

STATE_LABEL = {
    "DOCUMENTED": "Documented",
    "NOT_CONFIRMED": "Not confirmed in reviewed documents",
    "INFORMATION_MISSING": "Information missing",
    "REQUIRES_AUTHORISED_REVIEW": "Requires authorised review",
}

# Plain-language names for the evidence classes. The four class names are the
# product's vocabulary, and a reader meeting MARKET_CONVENTION for the first time
# should not have to infer what it means from context.
CLASS_LABEL = {
    "PROVEN": "Proven — every input authored by a third party, authority or insurer",
    "DECLARED": "Declared — at least one input authored by the business itself",
    "MARKET_CONVENTION": "Market convention — inputs evidenced, but the policy does "
                         "not state the arithmetic",
    "UNESTABLISHED": "Unestablished — a required input has no document behind it",
}


def _iter_text(finding: Finding):
    """Every human-facing string a finding renders: the headline and any string in
    its detail (consequence notes, arithmetic labels, resolution path). The never
    check runs over all of them, not just the headline."""
    yield finding.headline
    if finding.resolution_path:
        yield finding.resolution_path
    for v in finding.detail.values():
        if isinstance(v, str):
            yield v
        elif isinstance(v, (list, tuple)):
            for item in v:
                if isinstance(item, str):
                    yield item


def _enforce_never(finding: Finding) -> None:
    if "assert_breach" not in finding.never:
        return
    for text in _iter_text(finding):
        if _BREACH_WORDS.search(text):
            raise ValueError(
                f"Rule {finding.rule_id} carries never:[assert_breach] but a "
                f"rendered field asserts a breach/outcome: {text!r}. Renderer "
                f"refuses to emit it.")


def _finding_view(finding: Finding) -> dict:
    _enforce_never(finding)
    # span_id travels with the view because the consent gate resolves each span to
    # the document that authored it (pipeline.disclosed_brief). Filtering findings by
    # granted document kinds is impossible without it.
    spans = [{
        "span_id": s.id,
        "document_id": s.document_id, "page_no": s.page_no,
        "char_start": s.char_start, "char_end": s.char_end,
        "quote": s.quoted_text,
    } for s in finding.spans]
    return {
        "rule_id": finding.rule_id,
        "state": finding.output_state,
        "state_label": STATE_LABEL.get(finding.output_state, finding.output_state),
        "class": finding.evidence_class,
        "class_label": CLASS_LABEL.get(finding.evidence_class,
                                       finding.evidence_class),
        "headline": finding.headline,
        "spans": spans,
        "detail": finding.detail,
        "resolution_path": finding.resolution_path,
        "never": finding.never,
    }


def build_brief(
    business_name: str,
    carrier: str,
    accounting: Accounting,
    findings: list[Finding],
    searched: list[str],
    not_searched: list[str],
    synthetic: bool = True,
    precedence_note: Optional[str] = None,
    facts: Optional[dict] = None,
) -> dict:
    """Assemble the Review Brief. Findings are sorted PROVEN first, then DECLARED,
    MARKET_CONVENTION, UNESTABLISHED."""
    ordered = sorted(
        findings,
        key=lambda f: (CLASS_SORT_ORDER.get(f.evidence_class, 9), f.rule_id),
    )
    views = [_finding_view(f) for f in ordered]
    by_class: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for v in views:
        by_class[v["class"]] = by_class.get(v["class"], 0) + 1
        by_state[v["state"]] = by_state.get(v["state"], 0) + 1
    return {
        "business": business_name,
        "carrier": carrier,
        "synthetic": synthetic,
        "synthetic_notice": (
            "SYNTHETIC DEMO DATA — fictional business. The only real document in "
            "the pipeline is the Chubb Select+ base wording."
            if synthetic else None),
        "accounting": accounting.as_dict(),
        "precedence_note": precedence_note,
        "searched": searched,
        "not_searched": not_searched,
        "findings": views,
        # Distribution is a first-class output, not a nicety: a Brief that is mostly
        # UNESTABLISHED is honest and not yet useful, and the reader is entitled to
        # see that at a glance rather than infer it by counting cards.
        "summary": {
            "finding_count": len(views),
            "by_class": by_class,
            "by_state": by_state,
            "abstained": sum(by_state.get(s, 0) for s in
                             ("INFORMATION_MISSING", "REQUIRES_AUTHORISED_REVIEW",
                              "NOT_CONFIRMED")),
        },
        # The normalised inputs every finding was computed from, with each one's
        # source and author class. This is what makes an arithmetic result checkable
        # without reading the code.
        "facts": facts or {},
    }
