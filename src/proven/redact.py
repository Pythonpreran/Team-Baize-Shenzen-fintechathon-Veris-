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
from datetime import date
from dataclasses import dataclass, field
from typing import Any

_TOKEN_RE = re.compile(r"\[VERIS_[A-Z_]+_\d+\]")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_NRIC_RE = re.compile(r"\b[STFG]\d{7}[A-Z]\b", re.IGNORECASE)
# Hong Kong Identity Card: one or two letters, six digits and a check character, usually
# printed with the check in brackets, e.g. A123456(3). Validated by its mod-11 checksum.
_HKID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,2})(\d{6})(?:\(([0-9A])\)|([0-9A]))(?![A-Za-z0-9])")
# Malaysian MyKad number in its printed form YYMMDD-PB-####. MyKad carries no public
# check digit, so the birth date must be a real date and the place-of-birth code non-zero.
_MYKAD_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})[- ](\d{2})[- ](\d{4})(?!\d)")
# Mainland China resident identity number: area code, YYYYMMDD birth date, sequence and an
# ISO 7064 MOD 11-2 check character (0-9 or X). Bounded by ASCII alphanumerics only,
# because the number is usually written straight after Han characters ("身份证号110…").
_CN_ID_RE = re.compile(r"(?<![0-9A-Za-z])(\d{6})(\d{8})(\d{3})([0-9Xx])(?![0-9A-Za-z])")
# A mainland mobile number, which the general phone pattern misses next to Han characters
# (Python counts a Han character as a word character).
_CN_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)")
# Labelled Chinese names and addresses: "被保险人：张三", "地址：…".
_CN_LABELLED_PERSON_RE = re.compile(
    r"(?:姓名|被保险人|投保人|受益人|联系人|申请人|患者|驾驶人)\s*[:：]\s*([\u4e00-\u9fff·]{2,4})")
_CN_ADDRESS_RE = re.compile(r"(?:地址|住址|联系地址)\s*[:：]\s*([^\n；;。]{4,60})")
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
    "Hong Kong", "Kuala Lumpur", "Insurance Authority", "Bank Negara Malaysia",
})
_SENSITIVE_FIELD = re.compile(
    r"(?:name|phone|email|address|pan|aadhaar|nric|fin|hkid|mykad|identity.?card|"
    r"policy.?number|claim.?number|account|bank|identifier)", re.IGNORECASE)

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


def valid_hkid(value: str) -> bool:
    """Validate the published Hong Kong Identity Card mod-11 check character.

    A single-letter prefix is padded with a space worth 36; letters are worth 10 to 35;
    the eight positions are weighted 9 down to 2; the check character (A = 10) makes the
    weighted sum divisible by 11.
    """
    match = _HKID_RE.fullmatch(str(value or "").upper().replace(" ", ""))
    if match is None:
        return False
    prefix, digits = match.group(1), match.group(2)
    check = match.group(3) or match.group(4)
    padded = prefix.rjust(2)
    values = [36 if ch == " " else ord(ch) - 55 for ch in padded] + [int(d) for d in digits]
    total = sum(value * weight for value, weight in zip(values, range(9, 1, -1)))
    check_value = 10 if check == "A" else int(check)
    return (total + check_value) % 11 == 0


def valid_mykad(value: str) -> bool:
    """A MyKad-shaped number whose first six digits are a real YYMMDD date."""
    match = _MYKAD_RE.fullmatch(str(value or ""))
    if match is None:
        return False
    _yy, mm, dd, place, _serial = match.groups()
    month, day = int(mm), int(dd)
    if not 1 <= month <= 12 or place == "00":
        return False
    # February is allowed 29 days because the century is not part of the number.
    days_in_month = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
    return 1 <= day <= days_in_month


_CN_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CN_ID_CHECK = "10X98765432"


def valid_cn_resident_id(value: str) -> bool:
    """A mainland resident identity number with a real birth date and a valid
    ISO 7064 MOD 11-2 check character."""
    match = _CN_ID_RE.fullmatch(str(value or "").strip())
    if match is None:
        return False
    birth = match.group(2)
    try:
        date(int(birth[:4]), int(birth[4:6]), int(birth[6:]))
    except ValueError:
        return False
    body = "".join(match.groups()[:3])
    total = sum(int(d) * w for d, w in zip(body, _CN_ID_WEIGHTS))
    return _CN_ID_CHECK[total % 11] == match.group(4).upper()


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
        text = _HKID_RE.sub(
            lambda match: self.token("HKID", match.group(0))
            if valid_hkid(match.group(0)) else match.group(0), text)
        text = _MYKAD_RE.sub(
            lambda match: self.token("MYKAD", match.group(0))
            if valid_mykad(match.group(0)) else match.group(0), text)
        text = _CN_ID_RE.sub(
            lambda match: self.token("CN_RESIDENT_ID", match.group(0))
            if valid_cn_resident_id(match.group(0)) else match.group(0), text)
        text = _CN_MOBILE_RE.sub(lambda match: self.token("PHONE", match.group(0)), text)
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
        text = _CN_ADDRESS_RE.sub(
            lambda match: match.group(0).replace(match.group(1), self.token("ADDRESS", match.group(1))),
            text)
        text = _CN_LABELLED_PERSON_RE.sub(
            lambda match: match.group(0).replace(match.group(1), self.token("PERSON", match.group(1))),
            text)

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
