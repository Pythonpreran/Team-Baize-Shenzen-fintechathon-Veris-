"""Language selection primitives for deterministic document handling."""
from collections.abc import Callable
from typing import Optional

from . import chinese as _chinese
from . import english as _english
from .chinese import (
    CNY_DATE,
    CNY_MONEY,
    ChineseBigramTokenizer,
    ChineseCNYMoneyParser,
    ChineseDateParser,
    chinese_numeral_value,
    clause_number,
)
from .english import (
    CJK,
    DOLLAR_SIGN_CURRENCIES,
    LATIN,
    OTHER,
    DateParser,
    EnglishDateParser,
    EnglishHKDDateParser,
    EnglishHKDMoneyParser,
    EnglishINRDateParser,
    EnglishINRMoneyParser,
    EnglishMYRDateParser,
    EnglishMYRMoneyParser,
    EnglishSGDDateParser,
    EnglishSGDMoneyParser,
    EnglishTokenizer,
    MoneyParser,
    NumberParser,
    ParsedDate,
    ParsedMoney,
    Tokenizer,
    detect_script,
    money_candidates as _english_money_candidates,
    money_parser_from_text,
)

MoneyCandidateProvider = Callable[[str], tuple[ParsedMoney, ...]]

# Providers receive the labelled field text and return spans relative to it. Selecting
# them from the full source document keeps future language packs out of core extraction.
_MONEY_CANDIDATE_PROVIDERS: dict[str, MoneyCandidateProvider] = {
    LATIN: _english_money_candidates,
    CJK: _chinese.money_candidates,
}

_CHINESE_TOKENIZER = ChineseBigramTokenizer()


def tokenizer_for_document(text: str) -> Tokenizer:
    """Select by the document's own script, never by a caller's assumption.

    Han-script text gets character bigrams (T28); Latin text keeps the English contract
    byte for byte; any other script still receives no tokens rather than a wrong reading.
    """
    if detect_script(text) == CJK:
        return _CHINESE_TOKENIZER
    return _english.tokenizer_for_document(text)


def tokenizer_for_query(text: str) -> Tokenizer:
    """Queries use the same script gate as the documents they are compared against."""
    return tokenizer_for_document(text)


def money_parser_for(currency: Optional[str]) -> Optional[MoneyParser]:
    if (currency or "").upper() == "CNY":
        return CNY_MONEY
    return _english.money_parser_for(currency)


def date_parser_for(currency: Optional[str]) -> Optional[DateParser]:
    if (currency or "").upper() == "CNY":
        return CNY_DATE
    return _english.date_parser_for(currency)


def register_money_candidate_provider(script: str,
                                      provider: MoneyCandidateProvider) -> None:
    """Register deterministic money candidate recognition for one Unicode script."""
    _MONEY_CANDIDATE_PROVIDERS[script] = provider


def money_candidates_for_document(document_text: str,
                                  field_text: str) -> tuple[ParsedMoney, ...]:
    """Enumerate field candidates through the provider selected from document script."""
    provider = _MONEY_CANDIDATE_PROVIDERS.get(detect_script(document_text))
    return () if provider is None else provider(field_text)


__all__ = [
    "CJK", "CNY_DATE", "CNY_MONEY", "ChineseBigramTokenizer", "ChineseCNYMoneyParser",
    "ChineseDateParser", "DOLLAR_SIGN_CURRENCIES", "LATIN", "OTHER", "EnglishDateParser",
    "EnglishHKDDateParser", "EnglishHKDMoneyParser", "EnglishINRDateParser",
    "EnglishINRMoneyParser", "EnglishMYRDateParser", "EnglishMYRMoneyParser",
    "EnglishSGDDateParser", "EnglishSGDMoneyParser",
    "EnglishTokenizer", "MoneyCandidateProvider", "MoneyParser", "NumberParser",
    "ParsedDate", "ParsedMoney", "Tokenizer", "chinese_numeral_value", "clause_number",
    "date_parser_for", "detect_script",
    "money_candidates_for_document", "money_parser_for", "money_parser_from_text",
    "register_money_candidate_provider", "tokenizer_for_document", "tokenizer_for_query",
]
