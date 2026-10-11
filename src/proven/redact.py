"""Reversible server-side tokenisation for model-bound text.

The provider boundary may receive useful insurance wording, but it must not receive a
person's identifiers.  This module replaces recognised personal values with stable,
per-call tokens.  The mapping stays in process and is used only when a guarded model
answer is rendered back to the person who supplied the value; it is never written to a
prompt log or sent to a provider.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_TOKEN_RE = re.compile(r"\[VERIS_[A-Z_]+_\d+\]")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_NRIC_RE = re.compile(r"\b[STFG]\d{7}[A-Z]\b", re.IGNORECASE)
_AADHAAR_RE = re.compile(r"(?<!\d)(?:\d[ -]?){11}\d(?!\d)")
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[ -]?)?(?:\d[ -]?){7,14}\d(?!\w)")
_BANK_RE = re.compile(
    r"\b(?:bank\s+(?:account|a/c)|account\s*(?:number|no\.?|#)|iban)\s*"
    r"(?:is|:|#)?\s*([A-Z0-9][A-Z0-9 -]{5,33})", re.IGNORECASE)
_POLICY_RE = re.compile(
    r"\b(?:policy|claim)\s*(?:(?:number|no\.?|id)\s*(?:[:#-]\s*|\s+)|[:#]\s*)"
    r"([A-Z0-9][A-Z0-9/_-]{4,})", re.IGNORECASE)
_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+[A-Za-z][A-Za-z .'-]{2,80}\b"
    r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|lane|ln\.?|drive|dr\.?|"
    r"close|crescent|marg|nagar|layout)\b(?:,?\s*[A-Za-z0-9 .'-]{0,80})?",
    re.IGNORECASE)
_LABELLED_PERSON_RE = re.compile(
    r"\b(?:name|insured|patient|member|policyholder)\s*:\s*"
    r"([A-Za-z][A-Za-z' -]{1,100})", re.IGNORECASE)
_PERSON_RE = re.compile(
    r"\b(?:Mr\.?|Mrs\.?|Ms\.?|Dr\.?)?\s*"
    r"((?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,})){1,3})\b")
# A possessive single name in an identity-bearing context is a common way someone asks
# about their own policy (for example, "Maya's policy").  The surrounding context makes
# this safe for lowercase input too, without mistaking ordinary sentence openings such as
# "What's" for an identity.
_POSSESSIVE_PERSON_RE = re.compile(
    r"\b([A-Za-z][A-Za-z]{1,50})(?='s\s+(?:policy|plan|claim|cover(?:age)?|"
    r"schedule|account|case|address)\b)")

# Headings and organisation labels are often title-cased in wording.  Redacting them is
# safe but needlessly degrades retrieval, so keep this intentionally small exception list.
_NOT_A_PERSON = frozenset({
    "Policy Schedule", "Claim Form", "General Insurance", "Health Policy",
    "Medical Expenses", "Sum Insured", "Waiting Period", "India", "Singapore",
})
_SENSITIVE_FIELD = re.compile(
    r"(?:name|phone|email|address|pan|aadhaar|nric|fin|policy.?number|claim.?number|"
    r"account|bank|identifier)", re.IGNORECASE)

# Verhoeff multiplication, permutation and inverse tables.  Aadhaar uses the Verhoeff
# check digit; accepting only a valid value avoids treating every 12-digit policy amount
# as an identifier.
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_NRIC_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)
_NRIC_CHECK = {
    "S": "JZIHGFEDCBA", "T": "JZIHGFEDCBA",
    "F": "XWUTRQPNMLK", "G": "XWUTRQPNMLK",
}


def valid_aadhaar(value: str) -> bool:
    """Return true only for a 12-digit value carrying a valid Verhoeff check digit."""
    digits = re.sub(r"[ -]", "", str(value or ""))
    if len(digits) != 12 or not digits.isdigit() or len(set(digits)) == 1:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[index % 8][int(digit)]]
    return checksum == 0


def valid_nric_fin(value: str) -> bool:
    """Validate the published checksum shape used by Singapore NRIC and FIN values."""
    token = str(value or "").upper()
    if not _NRIC_RE.fullmatch(token):
        return False
    prefix, digits, suffix = token[0], token[1:8], token[8]
    total = sum(int(digit) * weight for digit, weight in zip(digits, _NRIC_WEIGHTS))
    return _NRIC_CHECK[prefix][total % 11] == suffix


@dataclass
class Redactor:
    """A request-scoped reversible token vault.

    A token appears wherever the original text appeared, so the provider can preserve a
    reference without receiving the underlying value.  Rehydration is deliberately an
    explicit final display operation rather than a property of the outbound request.
    """

    _values: dict[str, str] = field(default_factory=dict)
    _tokens_by_value: dict[tuple[str, str], str] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def token(self, kind: str, value: object) -> str:
        raw = str(value)
        category = re.sub(r"[^A-Z_]", "_", str(kind).upper()) or "VALUE"
        with self._lock:
            existing = self._tokens_by_value.get((category, raw))
            if existing is not None:
                return existing
            number = self._counts.get(category, 0) + 1
            self._counts[category] = number
            token = f"[VERIS_{category}_{number}]"
            self._tokens_by_value[(category, raw)] = token
            self._values[token] = raw
            return token

    def redact_text(self, value: object) -> str:
        """Replace recognisable PII while retaining non-sensitive policy language."""
        text = str(value or "")
        if not text:
            return text

        text = _EMAIL_RE.sub(lambda match: self.token("EMAIL", match.group(0)), text)
        text = _PAN_RE.sub(lambda match: self.token("PAN", match.group(0)), text)
        text = _NRIC_RE.sub(
            lambda match: self.token("NRIC_FIN", match.group(0))
            if valid_nric_fin(match.group(0)) else match.group(0), text)
        text = _AADHAAR_RE.sub(
            lambda match: self.token("AADHAAR", match.group(0))
            if valid_aadhaar(match.group(0)) else match.group(0), text)
        text = _BANK_RE.sub(
            lambda match: match.group(0).replace(match.group(1), self.token("BANK", match.group(1))),
            text)
        text = _POLICY_RE.sub(
            lambda match: match.group(0).replace(match.group(1), self.token("POLICY_CLAIM", match.group(1))),
            text)
        text = _ADDRESS_RE.sub(lambda match: self.token("ADDRESS", match.group(0)), text)
        text = _LABELLED_PERSON_RE.sub(
            lambda match: match.group(0).replace(match.group(1), self.token("PERSON", match.group(1))),
            text)
        text = _POSSESSIVE_PERSON_RE.sub(
            lambda match: self.token("PERSON", match.group(1)), text)

        def redact_phone(match: re.Match[str]) -> str:
            candidate = match.group(0)
            digits = re.sub(r"\D", "", candidate)
            # Years, dates and monetary values are shorter or contain punctuation that
            # cannot meet this all-digit telephone shape.
            return self.token("PHONE", candidate) if 8 <= len(digits) <= 15 else candidate

        text = _PHONE_RE.sub(redact_phone, text)

        def redact_person(match: re.Match[str]) -> str:
            candidate = match.group(1).strip()
            if candidate in _NOT_A_PERSON or _TOKEN_RE.fullmatch(candidate):
                return match.group(0)
            return match.group(0).replace(candidate, self.token("PERSON", candidate))

        return _PERSON_RE.sub(redact_person, text)

    def redact_value(self, value: Any, *, field_name: str = "") -> Any:
        """Recursively redact a JSON-like value without changing its shape."""
        if isinstance(value, Mapping):
            return {str(key): self.redact_value(item, field_name=str(key))
                    for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [self.redact_value(item, field_name=field_name) for item in value]
        if value is None:
            return None
        if _SENSITIVE_FIELD.search(field_name):
            # JSON identifiers are commonly numbers.  Treat the field classification,
            # not Python's scalar type, as the privacy boundary.
            return self.token("SENSITIVE_FIELD", value)
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def rehydrate(self, value: object) -> str:
        """Restore tokens only for the local display boundary."""
        text = str(value or "")
        with self._lock:
            return _TOKEN_RE.sub(lambda match: self._values.get(match.group(0), match.group(0)), text)

    def token_summary(self) -> dict[str, int]:
        """Safe audit metadata: categories and counts, never original values."""
        with self._lock:
            return dict(sorted(self._counts.items()))


def redact_text(value: object) -> tuple[str, Redactor]:
    """Convenience entry point for a one-off request-scoped redaction."""
    vault = Redactor()
    return vault.redact_text(value), vault
