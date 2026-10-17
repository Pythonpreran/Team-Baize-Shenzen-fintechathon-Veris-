"""Simplified-Chinese language primitives (T28): tokenizer, numerals, money, dates, clauses.

No dependency is added. Word segmentation (jieba and similar) was considered and rejected
for the deterministic core: a segmenter is a statistical model whose output changes with
its dictionary, and retrieval here must be reproducible byte for byte. Overlapping
character bigrams are the standard dictionary-free baseline for Chinese retrieval and are
exactly reproducible.

Every parser refuses rather than guesses: a numeral it cannot read yields None, a money
expression with no currency marker is reported as a bare number, and a date is only read
in a year-first form, which is the only form Chinese documents use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from .english import CJK, ParsedDate, ParsedMoney
from .english import money_candidates as _english_money_candidates

_CJK_CHAR = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_SEGMENT = re.compile(rf"[{_CJK_CHAR}]+|[a-z0-9]+(?:\.[0-9]+)?")


@dataclass(frozen=True)
class ChineseBigramTokenizer:
    """Overlapping character bigrams for Han text; Latin words and numbers kept whole.

    A one-character run is kept as a unigram so a single-character term still matches.
    Punctuation, including full-width punctuation, separates runs and is dropped.
    """

    script: str = CJK

    def tokenize(self, text: str) -> tuple[str, ...]:
        out: list[str] = []
        for segment in _SEGMENT.findall((text or "").lower()):
            if "\u3400" <= segment[0] <= "\ufaff":
                if len(segment) == 1:
                    out.append(segment)
                else:
                    out.extend(segment[i:i + 2] for i in range(len(segment) - 1))
            else:
                out.append(segment)
        return tuple(out)


# ── numerals ────────────────────────────────────────────────────────────────

_DIGIT = {
    "零": 0, "〇": 0, "○": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "貳": 2, "两": 2,
    "兩": 2, "三": 3, "叁": 3, "參": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6,
    "陆": 6, "陸": 6, "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_SMALL_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_WAN = {"万": 10 ** 4, "萬": 10 ** 4}
_YI = {"亿": 10 ** 8, "億": 10 ** 8}
NUMERAL_CHARS = "".join((*_DIGIT, *_SMALL_UNIT, *_WAN, *_YI))


def chinese_numeral_value(text: str) -> Optional[int]:
    """The integer a Chinese numeral writes (小写 or 大写), or None if it is not one.

    `十一` is 11, `一千零五` 1,005, `伍拾万` 500,000, `三亿五千万` 350,000,000. A string
    of bare digits such as `二〇二六` is read digit by digit, the way years are written.
    """
    raw = (text or "").strip()
    if not raw or any(ch not in NUMERAL_CHARS for ch in raw):
        return None
    # "万元" is a unit heading ("amounts in ten-thousands of yuan"), not the number zero.
    if not any(ch in _DIGIT or ch in _SMALL_UNIT for ch in raw):
        return None
    if all(ch in _DIGIT for ch in raw):
        return int("".join(str(_DIGIT[ch]) for ch in raw)) if len(raw) > 1 else _DIGIT[raw]
    total = section = number = 0
    for ch in raw:
        if ch in _DIGIT:
            number = _DIGIT[ch]
        elif ch in _SMALL_UNIT:
            section += (number or 1) * _SMALL_UNIT[ch]
            number = 0
        elif ch in _WAN:
            total += (section + number) * _WAN[ch]
            section = number = 0
        else:
            total = (total + section + number) * _YI[ch]
            section = number = 0
    return total + section + number


def _number(raw: str) -> Optional[Decimal]:
    """An Arabic or Chinese number, with full-width digits and separators normalised."""
    text = (raw or "").strip().translate(_FULLWIDTH).replace(",", "")
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    value = chinese_numeral_value(text)
    return None if value is None else Decimal(value)


_FULLWIDTH = str.maketrans("０１２３４５６７８９，．", "0123456789,.")


# ── money ───────────────────────────────────────────────────────────────────

_SCALE = {"万": Decimal(10 ** 4), "萬": Decimal(10 ** 4), "亿": Decimal(10 ** 8),
          "億": Decimal(10 ** 8), "千": Decimal(1000), "仟": Decimal(1000)}
_PREFIX = r"(?P<prefix>人民币|RMB|CNY|¥|￥)"
# Arabic figures, optionally scaled by 万/亿, optionally followed by 元: "50万元", "180,000元".
_ARABIC = re.compile(
    _PREFIX + r"?\s*(?P<num>[0-9０-９][0-9０-９,，]*(?:[.．][0-9０-９]+)?)\s*"
    r"(?P<scale>万|萬|亿|億|千|仟)?\s*(?P<unit>元|圆|块)?")
# A written amount must end in 元 or 圆 to be money: "伍拾万元整", "一千二百元".
_WRITTEN = re.compile(
    _PREFIX + r"?\s*(?P<yuan>[" + NUMERAL_CHARS + r"]+)\s*(?:元|圆)"
    r"(?:\s*(?P<jiao>[" + "".join(_DIGIT) + r"])角)?(?:\s*(?P<fen>[" + "".join(_DIGIT)
    + r"])分)?(?:\s*[整正])?")


@dataclass(frozen=True)
class ChineseCNYMoneyParser:
    """Renminbi written in Chinese documents.

    Explicit when it carries a prefix (人民币, RMB, CNY, ¥) or the unit 元: the currency is
    then established by the expression itself. A figure with neither is a bare number.
    """

    currency: str = "CNY"

    def parse_number(self, text: str) -> Optional[Decimal]:
        hit = self.locate_money(text, allow_bare=True)
        return hit.amount if hit else None

    def locate_money(self, text: str, *, allow_bare: bool = False) -> Optional[ParsedMoney]:
        for hit in money_candidates(text):
            if hit.currency == self.currency or (allow_bare and not hit.currency):
                return ParsedMoney(hit.amount, self.currency, hit.start, hit.end)
        return None

    def parse_money(self, text: str, *, allow_bare: bool = False) -> Optional[Decimal]:
        hit = self.locate_money(text, allow_bare=allow_bare)
        return hit.amount if hit else None


def _written_amount(match: re.Match) -> Optional[Decimal]:
    yuan = chinese_numeral_value(match.group("yuan"))
    if yuan is None:
        return None
    amount = Decimal(yuan)
    if match.group("jiao"):
        amount += Decimal(_DIGIT[match.group("jiao")]) / 10
    if match.group("fen"):
        amount += Decimal(_DIGIT[match.group("fen")]) / 100
    return amount


def _cny_candidates(body: str) -> list[ParsedMoney]:
    found: list[ParsedMoney] = []
    for match in _WRITTEN.finditer(body):
        amount = _written_amount(match)
        if amount is not None:
            found.append(ParsedMoney(amount, "CNY", match.start(), match.end()))

    def overlaps(start: int, end: int) -> bool:
        return any(start < hit.end and hit.start < end for hit in found)

    for match in _ARABIC.finditer(body):
        if overlaps(match.start(), match.end()):
            continue
        amount = _number(match.group("num"))
        if amount is None:
            continue
        if match.group("scale"):
            amount *= _SCALE[match.group("scale")]
        explicit = bool(match.group("prefix") or match.group("unit"))
        found.append(ParsedMoney(amount, "CNY" if explicit else "",
                                 match.start(), match.end()))
    return found


def _drop_restatement(hits: list[ParsedMoney], body: str) -> list[ParsedMoney]:
    """Chinese contracts state an amount twice: "50万元（伍拾万元整）". The bracketed
    restatement of the SAME amount is one value, not two; a different amount is kept, so
    the field stays ambiguous and extraction abstains."""
    out: list[ParsedMoney] = []
    for hit in hits:
        prev = out[-1] if out else None
        if (prev is not None and prev.currency == hit.currency == "CNY"
                and prev.amount == hit.amount
                and re.fullmatch(r"\s*[（(]\s*", body[prev.end:hit.start] or "")
                and re.match(r"\s*[）)]", body[hit.end:])):
            continue
        out.append(hit)
    return out


def money_candidates(text: str) -> tuple[ParsedMoney, ...]:
    """Money in a Chinese document, in order: renminbi first, then any other explicit
    currency the English layer recognises (an `HK$` figure in a Chinese schedule names
    its own currency), with bare figures last so a pack can claim only those."""
    body = text or ""

    def overlaps(a: ParsedMoney, b: ParsedMoney) -> bool:
        return a.start < b.end and b.start < a.end

    found = _cny_candidates(body)
    explicit_cny = [hit for hit in found if hit.currency]
    explicit_other = [hit for hit in _english_money_candidates(body)
                      if hit.currency and not any(overlaps(hit, c) for c in explicit_cny)]
    # A bare figure inside an explicit expression ("500" in "HK$500") is that expression.
    bare = [hit for hit in found if not hit.currency
            and not any(overlaps(hit, other) for other in explicit_other)]
    ordered = sorted(explicit_cny + explicit_other + bare,
                     key=lambda hit: (hit.start, hit.end))
    return tuple(_drop_restatement(ordered, body))


CNY_MONEY = ChineseCNYMoneyParser()


# ── dates ───────────────────────────────────────────────────────────────────

_DATE_NUM = rf"[0-9０-９{''.join(_DIGIT)}十拾]+"
_YMD_CJK = re.compile(rf"(?P<y>{_DATE_NUM})\s*年\s*(?P<m>{_DATE_NUM})\s*月\s*(?P<d>{_DATE_NUM})\s*[日号]")
_YMD_NUMERIC = re.compile(r"(?<!\d)(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})(?!\d)")


def _date_part(raw: str) -> Optional[int]:
    value = _number(raw)
    return None if value is None or value != value.to_integral_value() else int(value)


class ChineseDateParser:
    """Year-first dates only: 2026年10月11日, 二〇二六年十月十一日, 2026-10-11, 2026/10/11.

    A day-first or month-first numeric date is not a Chinese form and is refused rather
    than guessed, matching the English parsers' refusal of ambiguous dates.
    """

    def locate_date(self, text: str) -> Optional[ParsedDate]:
        body = text or ""
        hits: list[tuple[int, int, date]] = []
        for pattern in (_YMD_CJK, _YMD_NUMERIC):
            for match in pattern.finditer(body):
                parts = [_date_part(match.group(k)) for k in ("y", "m", "d")]
                if None in parts:
                    continue
                try:
                    value = date(*parts)
                except ValueError:
                    continue
                hits.append((match.start(), match.end(), value))
        if not hits:
            return None
        start, end, value = min(hits)
        return ParsedDate(value, start, end)

    def parse_date(self, text: str) -> Optional[date]:
        hit = self.locate_date(text)
        return hit.value if hit else None


CNY_DATE = ChineseDateParser()


# ── clause numbering ────────────────────────────────────────────────────────

_CLAUSE_NUM = rf"[0-9{''.join(_DIGIT)}十拾百佰千]+"
CLAUSE_PATTERN = re.compile(
    rf"第\s*(?P<article>{_CLAUSE_NUM})\s*条(?:\s*第\s*(?P<paragraph>{_CLAUSE_NUM})\s*款)?"
    rf"(?:\s*第\s*(?P<item>{_CLAUSE_NUM})\s*项)?")


def clause_number(text: str) -> Optional[str]:
    """`第二十三条第二款` -> `23(2)`; `第5条` -> `5`. None if no article is cited."""
    match = CLAUSE_PATTERN.search(text or "")
    if match is None:
        return None
    parts = []
    for name in ("article", "paragraph", "item"):
        raw = match.group(name)
        if raw is None:
            break
        value = _date_part(raw)
        if value is None:
            return None
        parts.append(value)
    return str(parts[0]) + "".join(f"({p})" for p in parts[1:])
