"""Language selection primitives for deterministic document handling."""
from collections.abc import Callable

from .english import (
    CJK,
    LATIN,
    OTHER,
    EnglishDateParser,
    EnglishINRDateParser,
    EnglishINRMoneyParser,
    EnglishSGDDateParser,
    EnglishSGDMoneyParser,
    EnglishTokenizer,
    MoneyParser,
    NumberParser,
    ParsedDate,
    ParsedMoney,
    Tokenizer,
    date_parser_for,
    detect_script,
    money_candidates as _english_money_candidates,
    money_parser_for,
    money_parser_from_text,
    tokenizer_for_document,
    tokenizer_for_query,
)

MoneyCandidateProvider = Callable[[str], tuple[ParsedMoney, ...]]

# Providers receive the labelled field text and return spans relative to it. Selecting
# them from the full source document keeps future language packs out of core extraction.
_MONEY_CANDIDATE_PROVIDERS: dict[str, MoneyCandidateProvider] = {
    LATIN: _english_money_candidates,
}


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
    "CJK", "LATIN", "OTHER", "EnglishDateParser", "EnglishINRDateParser",
    "EnglishINRMoneyParser", "EnglishSGDDateParser", "EnglishSGDMoneyParser",
    "EnglishTokenizer", "MoneyCandidateProvider", "MoneyParser", "NumberParser",
    "ParsedDate", "ParsedMoney", "Tokenizer", "date_parser_for", "detect_script",
    "money_candidates_for_document", "money_parser_for", "money_parser_from_text",
    "register_money_candidate_provider", "tokenizer_for_document", "tokenizer_for_query",
]
