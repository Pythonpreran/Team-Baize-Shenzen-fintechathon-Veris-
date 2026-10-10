"""T04 language plumbing: English stays stable while additions remain data-led."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from proven import advisernote, ai, retrieval
from proven.extract import PolicyFacts, _schedule_text, find_money_with_currency
from proven.ingest import LoadedDoc
from proven.lang import (
    CJK,
    LATIN,
    EnglishINRMoneyParser,
    EnglishSGDMoneyParser,
    EnglishTokenizer,
    date_parser_for,
    detect_script,
    tokenizer_for_document,
)

ROOT = Path(__file__).resolve().parents[1]


def _i18n_checker():
    path = ROOT / "scripts" / "check_i18n.py"
    spec = importlib.util.spec_from_file_location("_check_i18n", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_english_tokenization_keeps_the_existing_bm25_contract():
    assert EnglishTokenizer().tokenize("The policy is not for the owner") == (
        "policy", "not", "owner")
    chunk = retrieval.chunk_page(
        "The policy covers an owner only when the named condition is met. " * 2,
        page_no=1, document_id="d", document_kind="SCHEDULE", filename="schedule.pdf",
        start_id=1,
    )[0]
    assert "policy" in chunk.tokens and "condition" in chunk.tokens


def test_retrieval_selects_a_tokenizer_from_the_documents_unicode_script():
    assert detect_script("Policy wording") == LATIN
    assert detect_script("保單條款") == CJK
    assert isinstance(tokenizer_for_document("Policy wording"), EnglishTokenizer)
    assert tokenizer_for_document("保單條款").tokenize("保單條款") == ()


def test_english_money_parsers_preserve_sgd_and_support_indian_notation():
    sgd = EnglishSGDMoneyParser()
    inr = EnglishINRMoneyParser()
    assert sgd.parse_money("S$1,234.50") == Decimal("1234.50")
    assert inr.parse_money("₹5,00,000") == Decimal("500000")
    assert inr.parse_money("Rs. 5 lakh") == Decimal("500000")
    assert inr.parse_money("1.5 crore") == Decimal("15000000")
    assert inr.parse_money("Rupees Five Lakh only") == Decimal("500000")


def test_money_extraction_names_only_a_currency_the_pack_or_document_establishes():
    pages = ["Sum insured: 500,000"]
    assert find_money_with_currency(pages, ("sum insured",), currency=None) is None
    hit = find_money_with_currency(["Sum insured: ₹5,00,000"], ("sum insured",),
                                  currency=None)
    assert hit is not None and hit[0] == Decimal("500000") and hit[3] == "INR"


@pytest.mark.parametrize(
    ("pack_currency", "marked_amount", "expected_currency"),
    (("SGD", "₹5,00,000", "INR"), ("INR", "S$500,000", "SGD")),
)
def test_explicit_currency_marker_overrides_the_pack_bare_amount_fallback(
        pack_currency, marked_amount, expected_currency):
    facts = PolicyFacts()
    doc = LoadedDoc("schedule", "SCHEDULE", "INSURER",
                    pages=[f"Stock: {marked_amount}"])
    _schedule_text(facts, doc, currency=pack_currency)
    fact = facts.get("stock_sum_insured")
    assert fact.value == Decimal("500000")
    assert fact.unit == expected_currency


@pytest.mark.parametrize(
    ("pack_currency", "target", "reference", "expected_currency"),
    (
        ("INR", "S$500,000", "₹30,000,000", "SGD"),
        ("SGD", "500,000", "₹30,000,000", "SGD"),
    ),
)
def test_labelled_money_binds_currency_to_its_own_candidate(
        pack_currency, target, reference, expected_currency):
    facts = PolicyFacts()
    doc = LoadedDoc("schedule", "SCHEDULE", "INSURER",
                    pages=[f"Stock: {target}; reference value: {reference}"])
    _schedule_text(facts, doc, currency=pack_currency)
    fact = facts.get("stock_sum_insured")
    assert fact.value == Decimal("500000")
    assert fact.unit == expected_currency
    assert fact.needle == f"Stock: {target}"


def test_labelled_money_abstains_when_multiple_values_share_one_field():
    facts = PolicyFacts()
    doc = LoadedDoc("schedule", "SCHEDULE", "INSURER",
                    pages=["Stock: S$500,000 / ₹30,000,000"])
    _schedule_text(facts, doc, currency="SGD")
    assert not facts.get("stock_sum_insured").established


def test_english_date_parsers_refuse_an_ambiguous_numeric_date():
    parser = date_parser_for("INR")
    assert parser is not None
    assert parser.parse_date("03/04/2026") is None
    assert parser.parse_date("23/04/2026") == date(2026, 4, 23)
    assert parser.parse_date("23 Apr 2026") == date(2026, 4, 23)


def test_english_lexicon_preserves_outcome_and_clause_guards():
    assert ai._asserts_outcome("This claim will not be paid.")
    assert not ai._asserts_outcome("An authorised adviser should review this clause.")
    assert ai._asserts_advice("Should I surrender this policy?")
    assert advisernote._CLAUSE.search("cl 4.2 applies").group(1) == "4.2"


def test_translation_checker_accepts_the_repository_catalogue():
    assert _i18n_checker().check(ROOT).ok


def test_translation_checker_reports_missing_and_unused_keys(tmp_path):
    (tmp_path / "web" / "i18n").mkdir(parents=True)
    (tmp_path / "web" / "i18n" / "en.json").write_text(
        json.dumps({"shell": {"used": "Used", "unused": "Unused"}}), encoding="utf-8")
    (tmp_path / "web" / "app.js").write_text('t("shell.used"); t("shell.missing");',
                                                 encoding="utf-8")
    result = _i18n_checker().check(tmp_path)
    assert result.missing == ("shell.missing",)
    assert result.unused == ("shell.unused",)
