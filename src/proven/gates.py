"""Layer 3 — the abstention gate. Four deterministic checks. No token
probabilities anywhere. Every gate is a string or integer comparison.

Escalation is a correct answer; a guess is not. The asymmetry matters: a tool that
wrongly says "you are fine" costs the business its claim; a tool that wrongly says
"you are in breach" costs an unnecessary adviser conversation. The gate is tuned to
locate, quote, escalate — and never resolve.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .models import Finding, Span

_WS = re.compile(r"\s+")


def norm_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _drop(finding: Finding, reason: str) -> Finding:
    finding.drop_reason = reason
    finding.output_state = "DROPPED"
    return finding


def _escalate(finding: Finding, state: str) -> Finding:
    finding.output_state = state
    return finding


def gate(
    finding: Finding,
    page_text: Callable[[str, int], str],
    within_provision: Optional[Callable[[Finding], bool]] = None,
) -> Finding:
    """Run the four gates in order. `page_text(document_id, page_no)` returns the
    stored immutable page text so span fidelity can be checked byte-for-byte."""

    # 1. Span fidelity — quoted text must exist at the recorded offsets.
    for s in finding.spans:
        source = page_text(s.document_id, s.page_no)[s.char_start:s.char_end]
        if norm_ws(source) != norm_ws(s.quoted_text):
            return _drop(finding, reason="SPAN_FIDELITY_FAIL")   # logged, counted

    # 2. Boundary — span must sit inside the provision it is attributed to.
    if within_provision is not None and not within_provision(finding):
        return _escalate(finding, "REQUIRES_AUTHORISED_REVIEW")

    # 3. Operand completeness — never substitute a default.
    if any(o.span_id is None for o in finding.required_operands):
        return finding.with_state("INFORMATION_MISSING")

    # 4. Authority — unresolved conflict escalates, never picks.
    if finding.precedence_unresolved:
        return _escalate(finding, "REQUIRES_AUTHORISED_REVIEW")

    return finding
