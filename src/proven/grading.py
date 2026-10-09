"""Layer 3 — evidence-class grading. DERIVED, never set.

The grading rule is AUTHORSHIP, not format. A document is not evidence because it
is a PDF. It is evidence according to who wrote it and whether they had an
independent reason to get it right.

  PROVEN            every operand span is authored by a third party or authority
  DECLARED          at least one operand comes from a document the business authored
  MARKET_CONVENTION every operand is a span, but the computation is not in the wording
  UNESTABLISHED     a required operand has no span in any document

There is deliberately NO code path that accepts an evidence_class argument from a
caller (test_evidence_class_cannot_be_set_by_caller). That is the guarantee it
cannot be overstated under time pressure.
"""
from __future__ import annotations

from typing import Optional

from .models import Operand

AUTHOR_TO_CLASS = {
    "INSURER": "PROVEN",
    "AUTHORITY": "PROVEN",
    "THIRD_PARTY": "PROVEN",
    "SELF": "DECLARED",
}


def grade_operand(span_id: Optional[str], author_class: str) -> str:
    """A single operand's class. No span anywhere -> UNESTABLISHED."""
    if span_id is None:
        return "UNESTABLISHED"
    return AUTHOR_TO_CLASS[author_class]


def grade_finding(
    operands: list[Operand],
    computation_span: Optional[str],
    has_computation: bool = False,
) -> str:
    """Weakest link wins, over two independent axes:

      1. the OPERANDS    — do we have a span for every input?
      2. the COMPUTATION — does the wording state how to combine them?

    Axis 2 exists because of a measured fact. Liberty SMECare states the average
    arithmetic verbatim; Chubb cl 4.2 and MSIG GC12 say "proportionately reduced"
    and stop. So a finding can have perfect insurer-authored operands and still not
    have insurer-authored arithmetic.

    The computation axis ONLY applies to findings that actually perform a
    computation (has_computation=True). A theft or headcount finding has no
    arithmetic to combine, so a null computation_span means nothing there — it must
    not silently demote a clean PROVEN finding to MARKET_CONVENTION.

    Ordering is deliberate:
      UNESTABLISHED     outranks MARKET_CONVENTION — a missing input is worse than
                        unstated arithmetic; with no value at risk there is nothing
                        to compute either way.
      MARKET_CONVENTION outranks DECLARED — unstated arithmetic taints the whole
                        computation; a self-declared input taints one operand.
    """
    if any(o.evidence_class == "UNESTABLISHED" for o in operands):
        return "UNESTABLISHED"

    if has_computation and computation_span is None:
        return "MARKET_CONVENTION"

    if any(o.evidence_class == "DECLARED" for o in operands):
        return "DECLARED"

    return "PROVEN"


# Sort order for the Review Brief: PROVEN first, UNESTABLISHED last.
CLASS_SORT_ORDER = {
    "PROVEN": 0,
    "DECLARED": 1,
    "MARKET_CONVENTION": 2,
    "UNESTABLISHED": 3,
}
