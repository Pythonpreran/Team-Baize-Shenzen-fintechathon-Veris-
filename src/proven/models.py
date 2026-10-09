"""Shared value types. Plain dataclasses so the engine stays deterministic and
easy to test without a database round-trip."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass
class Span:
    id: str
    document_id: str
    page_no: int
    char_start: int
    char_end: int
    quoted_text: str
    sha256: str = ""


@dataclass
class Operand:
    name: str
    value: Optional[str]
    unit: Optional[str]
    span_id: Optional[str]
    evidence_class: str          # DERIVED in grading.grade_operand — never hand-set
    author_class: Optional[str] = None


@dataclass
class Provision:
    id: str
    document_id: str
    span_id: str
    clause_ref: Optional[str]
    taxonomy_code: Optional[str]
    status: str                  # CLASSIFIED | UNACCOUNTED
    authority_rank: int
    carrier: Optional[str] = None
    quoted_text: str = ""


@dataclass
class Settlement:
    adequacy_ratio: Decimal
    average_triggered: bool
    illustration: Decimal
    formula_basis: str           # STATED_IN_WORDING | MARKET_CONVENTION | NOT_TRIGGERED
    formula_basis_class: str     # PROVEN | MARKET_CONVENTION
    threshold_basis: str         # THRESHOLD_STATED | NO_THRESHOLD_STATED
    order_applied: str


@dataclass
class Finding:
    rule_id: str
    rule_version: int
    output_state: str            # DOCUMENTED|NOT_CONFIRMED|INFORMATION_MISSING|
                                 # REQUIRES_AUTHORISED_REVIEW
    evidence_class: str          # weakest operand wins
    headline: str
    operands: list[Operand] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    required_operands: list[Operand] = field(default_factory=list)
    resolution_path: Optional[str] = None
    precedence_unresolved: bool = False
    never: list[str] = field(default_factory=list)   # e.g. assert_breach
    detail: dict = field(default_factory=dict)
    drop_reason: Optional[str] = None

    def with_state(self, state: str) -> "Finding":
        self.output_state = state
        return self


@dataclass
class Resolution:
    governing: dict                        # taxonomy_code -> Provision
    unresolved: list[str] = field(default_factory=list)
    note: Optional[str] = None
    has_construction_clause: bool = False
