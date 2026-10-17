"""English language primitives used by deterministic retrieval and extraction.

Only English is implemented in this task.  Documents in another Unicode script are
recognised and deliberately receive no English tokenisation; a later language pack can
add its own tokenizer without pretending that English rules understand that document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional, Protocol

LATIN = "LATIN"
CJK = "CJK"
OTHER = "OTHER"


class Tokenizer(Protocol):
    """A deterministic tokenizer for one writing system."""

    script: str

    def tokenize(self, text: str) -> tuple[str, ...]: ...


class NumberParser(Protocol):
    """Parses a written number without silently supplying a value."""

    def parse_number(self, text: str) -> Optional[Decimal]: ...


class MoneyParser(NumberParser, Protocol):
    """Parses a money amount and identifies the currency it actually recognised."""

    currency: str

    def locate_money(self, text: str, *, allow_bare: bool = False) -> Optional["ParsedMoney"]: ...

    def parse_money(self, text: str, *, allow_bare: bool = False) -> Optional[Decimal]: ...


class DateParser(Protocol):
    """Parses dates conservatively; ambiguous numeric dates are never guessed."""

    def locate_date(self, text: str) -> Optional["ParsedDate"]: ...

    def parse_date(self, text: str) -> Optional[date]: ...


@dataclass(frozen=True)
class ParsedMoney:
    amount: Decimal
    currency: str
    start: int
    end: int
    #: True when the only marker was a bare "$". Singapore and Hong Kong both print their
    #: dollar that way, so the label "SGD" is the legacy default and a dollar-currency
    #: pack (HKD) may re-read it; an explicit "S$" or "HK$" is never re-read.
    dollar_sign_only: bool = False


@dataclass(frozen=True)
class ParsedDate:
    value: date
    start: int
    end: int


_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset("""
a an and are as at be by for from in is it its of on or that the this to was were
with will shall may
""".split())


@dataclass(frozen=True)
class EnglishTokenizer:
    """The existing lexical-token contract, preserved byte-for-byte in behaviour."""

    script: str = LATIN

    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(word for word in _WORD.findall((text or "").lower())
                     if word not in _STOPWORDS)


@dataclass(frozen=True)
class UnsupportedTokenizer:
    """Honest fallback until the matching language implementation exists."""

    script: str = OTHER

    def tokenize(self, text: str) -> tuple[str, ...]:
        return ()


_ENGLISH_TOKENIZER = EnglishTokenizer()


def detect_script(text: str) -> str:
    """Return the dominant Unicode script range without a language dependency."""
    latin = sum(1 for char in text or "" if ("A" <= char <= "Z") or ("a" <= char <= "z"))
    cjk = sum(1 for char in text or "" if "\u3400" <= char <= "\u9fff")
    if cjk and cjk >= latin:
        return CJK
    if latin:
        return LATIN
    return OTHER


def tokenizer_for_document(text: str) -> Tokenizer:
    """Select by the document's own script, never by a caller's assumption."""
    script = detect_script(text)
    return _ENGLISH_TOKENIZER if script == LATIN else UnsupportedTokenizer(script=script)


def tokenizer_for_query(text: str) -> Tokenizer:
    """Queries use the same script gate as the documents they are compared against."""
    return tokenizer_for_document(text)


_AMOUNT = r"(?P<amount>\d(?:[\d,]*\d)?(?:\.\d{1,2})?)"


def _decimal(raw: str) -> Optional[Decimal]:
    try:
        return Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


class EnglishSGDMoneyParser:
    currency = "SGD"
    _marked = re.compile(r"(?:S\$|SGD|\$)\s*" + _AMOUNT, re.IGNORECASE)
    _bare = re.compile(_AMOUNT)

    def parse_number(self, text: str) -> Optional[Decimal]:
        match = self._bare.search(text or "")
        return _decimal(match.group("amount")) if match else None

    def locate_money(self, text: str, *, allow_bare: bool = False) -> Optional[ParsedMoney]:
        match = self._marked.search(text or "")
        if match is None and allow_bare:
            match = self._bare.search(text or "")
        if match is None:
            return None
        amount = _decimal(match.group("amount"))
        return None if amount is None else ParsedMoney(amount, self.currency,
                                                       match.start(), match.end())

    def parse_money(self, text: str, *, allow_bare: bool = False) -> Optional[Decimal]:
        hit = self.locate_money(text, allow_bare=allow_bare)
        return hit.amount if hit else None


_IN_SCALE = {
    "lakh": Decimal("100000"), "lakhs": Decimal("100000"),
    "crore": Decimal("10000000"), "crores": Decimal("10000000"),
}
_WORD_VALUES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}


class EnglishINRMoneyParser:
    currency = "INR"
    _numeric = re.compile(
        r"(?:(?P<marker>₹|INR|Rs\.?|Rupees)\s*)?" + _AMOUNT
        + r"(?:\s*(?P<scale>lakh|lakhs|crore|crores))?", re.IGNORECASE)
    _words = re.compile(r"\b(?:rupees|rs\.?)\s+(?P<words>[A-Za-z\s-]{1,80}?)(?:\s+only\b|$)",
                        re.IGNORECASE)

    def parse_number(self, text: str) -> Optional[Decimal]:
        match = self._numeric.search(text or "")
        return _decimal(match.group("amount")) if match else None

    @staticmethod
    def _words_amount(raw: str) -> Optional[Decimal]:
        total = 0
        current = 0
        seen = False
        for token in re.findall(r"[a-z]+", (raw or "").lower()):
            if token == "and":
                continue
            if token in _WORD_VALUES:
                current += _WORD_VALUES[token]
                seen = True
            elif token == "hundred":
                current = max(current, 1) * 100
                seen = True
            elif token in _IN_SCALE:
                total += current * int(_IN_SCALE[token])
                current = 0
                seen = True
            else:
                return None
        return Decimal(total + current) if seen else None

    def locate_money(self, text: str, *, allow_bare: bool = False) -> Optional[ParsedMoney]:
        for match in self._numeric.finditer(text or ""):
            marker, scale = match.group("marker"), match.group("scale")
            if not marker and not scale and not allow_bare:
                continue
            amount = _decimal(match.group("amount"))
            if amount is None:
                continue
            if scale:
                amount *= _IN_SCALE[scale.lower()]
            return ParsedMoney(amount, self.currency, match.start(), match.end())
        for match in self._words.finditer(text or ""):
            amount = self._words_amount(match.group("words"))
            if amount is not None:
                return ParsedMoney(amount, self.currency, match.start(), match.end())
        return None

    def parse_money(self, text: str, *, allow_bare: bool = False) -> Optional[Decimal]:
        hit = self.locate_money(text, allow_bare=allow_bare)
        return hit.amount if hit else None


class EnglishHKDMoneyParser:
    """Hong Kong dollars. "HK$" and "HKD" are explicit; under the Hong Kong pack a bare
    "$" is also the Hong Kong dollar — the government's own Standard Plan template prints
    its limits as "$750 per day"."""

    currency = "HKD"
    _marked = re.compile(r"(?:HK\$|HKD)\s*" + _AMOUNT, re.IGNORECASE)
    _dollar = re.compile(r"(?<![A-Za-z$])\$\s*" + _AMOUNT)
    _bare = re.compile(_AMOUNT)

    def parse_number(self, text: str) -> Optional[Decimal]:
        match = self._bare.search(text or "")
        return _decimal(match.group("amount")) if match else None

    def locate_money(self, text: str, *, allow_bare: bool = False) -> Optional[ParsedMoney]:
        body = text or ""
        match = self._marked.search(body) or self._dollar.search(body)
        if match is None and allow_bare:
            match = self._bare.search(body)
        if match is None:
            return None
        amount = _decimal(match.group("amount"))
        return None if amount is None else ParsedMoney(amount, self.currency,
                                                       match.start(), match.end())

    def parse_money(self, text: str, *, allow_bare: bool = False) -> Optional[Decimal]:
        hit = self.locate_money(text, allow_bare=allow_bare)
        return hit.amount if hit else None


class EnglishMYRMoneyParser:
    """Malaysian ringgit, printed "RM 1,000" or "MYR 1,000". Case-sensitive on purpose:
    "rm" inside an English word is not a currency marker."""

    currency = "MYR"
    _marked = re.compile(r"(?<![A-Za-z])(?:RM|MYR)\s*" + _AMOUNT)
    _bare = re.compile(_AMOUNT)

    def parse_number(self, text: str) -> Optional[Decimal]:
        match = self._bare.search(text or "")
        return _decimal(match.group("amount")) if match else None

    def locate_money(self, text: str, *, allow_bare: bool = False) -> Optional[ParsedMoney]:
        match = self._marked.search(text or "")
        if match is None and allow_bare:
            match = self._bare.search(text or "")
        if match is None:
            return None
        amount = _decimal(match.group("amount"))
        return None if amount is None else ParsedMoney(amount, self.currency,
                                                       match.start(), match.end())

    def parse_money(self, text: str, *, allow_bare: bool = False) -> Optional[Decimal]:
        hit = self.locate_money(text, allow_bare=allow_bare)
        return hit.amount if hit else None


SGD_MONEY = EnglishSGDMoneyParser()
INR_MONEY = EnglishINRMoneyParser()
HKD_MONEY = EnglishHKDMoneyParser()
MYR_MONEY = EnglishMYRMoneyParser()

#: Currencies whose documents print a bare "$" for their own dollar.
DOLLAR_SIGN_CURRENCIES = frozenset({"SGD", "HKD"})


def money_parser_for(currency: Optional[str]) -> Optional[MoneyParser]:
    code = (currency or "").upper()
    if code == "SGD":
        return SGD_MONEY
    if code == "INR":
        return INR_MONEY
    if code == "HKD":
        return HKD_MONEY
    if code == "MYR":
        return MYR_MONEY
    return None


def money_candidates(text: str) -> tuple[ParsedMoney, ...]:
    """Return supported money expressions and bare numbers in document order.

    A non-empty ``currency`` belongs to that exact expression. A bare number carries
    an empty currency so callers can apply a jurisdiction pack only to that number,
    never to a different marked value later in the same text.
    """
    body = text or ""
    explicit: list[ParsedMoney] = []

    # Hong Kong and Malaysian markers first: the "$" inside "HK$" must not also be read
    # as a Singapore dollar.
    for parser in (HKD_MONEY, MYR_MONEY):
        for match in parser._marked.finditer(body):
            amount = _decimal(match.group("amount"))
            if amount is not None:
                explicit.append(ParsedMoney(amount, parser.currency,
                                            match.start(), match.end()))
    foreign = tuple(explicit)

    for match in SGD_MONEY._marked.finditer(body):
        if any(match.start() < hit.end and hit.start < match.end() for hit in foreign):
            continue
        amount = _decimal(match.group("amount"))
        if amount is not None:
            marker = match.group(0)[:match.start("amount") - match.start()].strip()
            # "A$" or "US$" is some other dollar: it keeps the legacy label and is never
            # re-read as a pack's own dollar.
            lettered = match.start() > 0 and body[match.start() - 1].isalpha()
            explicit.append(ParsedMoney(amount, SGD_MONEY.currency,
                                        match.start(), match.end(),
                                        dollar_sign_only=marker == "$" and not lettered))

    for match in INR_MONEY._numeric.finditer(body):
        marker, scale = match.group("marker"), match.group("scale")
        if not marker and not scale:
            continue
        amount = _decimal(match.group("amount"))
        if amount is None:
            continue
        if scale:
            amount *= _IN_SCALE[scale.lower()]
        explicit.append(ParsedMoney(amount, INR_MONEY.currency,
                                    match.start(), match.end()))

    for match in INR_MONEY._words.finditer(body):
        amount = INR_MONEY._words_amount(match.group("words"))
        if amount is not None:
            explicit.append(ParsedMoney(amount, INR_MONEY.currency,
                                        match.start(), match.end()))

    def overlaps_explicit(start: int, end: int) -> bool:
        return any(start < hit.end and hit.start < end for hit in explicit)

    bare: list[ParsedMoney] = []
    for match in SGD_MONEY._bare.finditer(body):
        if overlaps_explicit(match.start(), match.end()):
            continue
        amount = _decimal(match.group("amount"))
        if amount is not None:
            bare.append(ParsedMoney(amount, "", match.start(), match.end()))

    return tuple(sorted(explicit + bare,
                        key=lambda hit: (hit.start, hit.end, hit.currency)))


def money_parser_from_text(text: str) -> Optional[MoneyParser]:
    """Resolve one unambiguous, explicitly marked document money expression."""
    candidates = money_candidates(text)
    if len(candidates) != 1 or not candidates[0].currency:
        return None
    return money_parser_for(candidates[0].currency)


_MONTHS = {month: number for number, month in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
    start=1)}
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
_WORD_DATE = re.compile(r"\b(\d{1,2})(?:\s+|-)([A-Za-z]{3,9})(?:\s+|-)(\d{4})\b")


class EnglishDateParser:
    """English day-first parser used by both supported English country formats."""

    def _make(self, year: int, month: int, day: int) -> Optional[date]:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def locate_date(self, text: str) -> Optional[ParsedDate]:
        body = text or ""
        for match in _ISO_DATE.finditer(body):
            value = self._make(*(int(match.group(i)) for i in (1, 2, 3)))
            if value is not None:
                return ParsedDate(value, match.start(), match.end())
        for match in _NUMERIC_DATE.finditer(body):
            first, second, year = (int(match.group(i)) for i in (1, 2, 3))
            # 03/04 could mean either date. A country default is not evidence of which
            # convention the author used, so this is deliberately not parsed.
            if first <= 12 and second <= 12:
                continue
            value = self._make(year, second, first)
            if value is not None:
                return ParsedDate(value, match.start(), match.end())
        for match in _WORD_DATE.finditer(body):
            month = _MONTHS.get(match.group(2)[:3].lower(), 0)
            value = self._make(int(match.group(3)), month, int(match.group(1)))
            if value is not None:
                return ParsedDate(value, match.start(), match.end())
        return None

    def parse_date(self, text: str) -> Optional[date]:
        hit = self.locate_date(text)
        return hit.value if hit else None


class EnglishSGDDateParser(EnglishDateParser):
    pass


class EnglishINRDateParser(EnglishDateParser):
    pass


class EnglishHKDDateParser(EnglishDateParser):
    """Hong Kong English documents print day-first dates, as Singapore's do."""


class EnglishMYRDateParser(EnglishDateParser):
    """Malaysian English documents print day-first dates, as Singapore's do."""


SGD_DATE = EnglishSGDDateParser()
INR_DATE = EnglishINRDateParser()
HKD_DATE = EnglishHKDDateParser()
MYR_DATE = EnglishMYRDateParser()


def date_parser_for(currency: Optional[str]) -> Optional[DateParser]:
    code = (currency or "").upper()
    if code == "SGD":
        return SGD_DATE
    if code == "INR":
        return INR_DATE
    if code == "HKD":
        return HKD_DATE
    if code == "MYR":
        return MYR_DATE
    return None
