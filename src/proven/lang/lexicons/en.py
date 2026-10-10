"""English safety and clause lexicon.

This is data owned by the English language implementation.  Guards import these patterns
instead of embedding English terms in consequential code, so another language can add an
adjacent lexicon without changing the guard contract.
"""
from __future__ import annotations

import re

OUTCOME_PATTERN = re.compile(
    r"\b(is in breach|has breached|breaches the|void(?:ed)?|will not be paid|"
    r"claim will fail|is not covered|not covered by|forfeits? cover|denied|"
    r"you are covered|are covered for|will be paid)\b", re.IGNORECASE)

ADVICE_PATTERN = re.compile(
    r"\b(surrender|switch|cancel|buy|recommend(?:ation|ed|s)?)\b", re.IGNORECASE)

NOTE_BANNED_PHRASES: tuple[str, ...] = (
    "you are covered", "you're covered", "not covered", "no cover",
    "the insurer will pay", "the insurer won't pay", "the claim will fail",
    "the claim would fail", "guaranteed", "breach", "violation",
)

# The first English clause-number pattern preserves the prior adviser-note contract.
CLAUSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcl\.?\s*(\d+(?:\.\d+)*(?:\([a-z0-9]+\))*)", re.IGNORECASE),
)
