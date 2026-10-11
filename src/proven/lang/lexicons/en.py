"""English safety and clause lexicon.

This is data owned by the English language implementation.  Guards import these patterns
instead of embedding English terms in consequential code, so another language can add an
adjacent lexicon without changing the guard contract.
"""
from __future__ import annotations

import re

OUTCOME_PATTERN = re.compile(
    r"\b(?:"
    r"(?:is|are|was|were)\s+(?:not\s+)?covered(?:\s+(?:under|by))?|"
    r"(?:this|the|your|a)\s+(?:policy|plan|cover(?:age)?)\s+"
    r"(?:covers?|provides?|includes?|will\s+cover|does(?:\s+not|n't)\s+cover)|"
    r"(?:no\s+cover|coverage\s+(?:exists|applies|does\s+not\s+apply|is\s+available))|"
    r"(?:the\s+)?(?:insurer|carrier)\s+(?:will|would|won't|will\s+not)\s+pay|"
    r"(?:the\s+)?(?:claim|payment)\s+(?:"
    r"(?:will|would|won't|will\s+not)\s+(?:be\s+)?(?:paid|payable|approved)|"
    r"(?:is|are|was|were|has\s+been|have\s+been)\s+"
    r"(?:paid|payable|approved)(?:\s+for\s+payment)?"
    r")|"
    r"(?:will|would|won't|will\s+not)\s+be\s+(?:paid|payable|approved)|"
    r"(?:is|are|was|were|has\s+been|have\s+been)\s+breached|"
    r"(?:is|are|was|were)\s+in\s+breach|"
    r"(?:you|the\s+insured|the\s+policyholder)\s+(?:have|has)\s+breached|"
    r"(?:has|have)\s+breached\s+(?:the|a)\s+(?:term|condition|warranty)|"
    r"void(?:ed)?|claim\s+will\s+fail|forfeits?\s+cover|denied"
    r")\b", re.IGNORECASE)

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
