"""Layer 2b — the extraction layer that was missing.

Before this existed, every rule reached into fixture JSON by key, so Layer 3 worked
on exactly one hand-built bundle and raised KeyError on anything else. These tests
pin the contract that replaced it: `get()` always returns a Fact, an absent value is
ABSENT rather than a default, and both provenances normalise to the same shape.
"""
import pdfbuild

from proven import ingest, pipeline
from proven.extract import ABSENT, PDF_TEXT, STRUCTURED, PolicyFacts, extract_facts
from proven.db import connect, init_db


def _structured_facts() -> PolicyFacts:
    conn = connect(":memory:")
    init_db(conn)
    store = ingest.ingest_fixtures(conn, "b")
    return extract_facts(store)


def _text_facts() -> dict:
    """The serialised facts as they reach the Brief, extracted from uploaded PDFs."""
    return pipeline.run_bundle([
        pipeline.UploadedFile("schedule.pdf", pdfbuild.schedule_pdf()),
        pipeline.UploadedFile("tenancy.pdf", pdfbuild.tenancy_pdf()),
        pipeline.UploadedFile("accounts.pdf", pdfbuild.accounts_pdf()),
    ])["brief"]["facts"]


# ── the contract ────────────────────────────────────────────────────────────

def test_get_never_raises_and_never_defaults():
    facts = PolicyFacts()
    fact = facts.get("a_field_that_does_not_exist")
    assert fact.value is None
    assert fact.source == ABSENT
    assert fact.established is False
    # An unestablished fact grades as owner-authored at best — never upward.
    assert fact.author_class == "SELF"


def test_missing_lists_exactly_what_is_absent():
    facts = _structured_facts()
    assert facts.missing("stock_sum_insured") == []
    assert facts.missing("a", "b") == ["a", "b"]


def test_established_requires_value_document_and_needle():
    facts = _structured_facts()
    fact = facts.get("stock_sum_insured")
    assert fact.established is True
    assert fact.doc is not None
    assert fact.needle
    assert fact.unit == "SGD"


def test_an_established_fact_is_not_overwritten_by_a_later_absent_probe():
    facts = PolicyFacts()
    from proven.extract import Fact
    from proven.ingest import LoadedDoc
    doc = LoadedDoc("d1", "SCHEDULE", "INSURER", pages=["stock 100"])
    facts.put(Fact("x", 100, doc, "stock 100", 1, "SGD", STRUCTURED))
    facts.put(Fact("x", None, None, None, 1, "SGD", ABSENT))
    assert facts.get("x").value == 100


# ── structured provenance (fixtures) ────────────────────────────────────────

def test_structured_extraction_reads_the_fixture_bundle():
    facts = _structured_facts()
    assert str(facts.get("stock_sum_insured").value) == "100000"
    assert str(facts.get("stock_value").value) == "150000"
    assert str(facts.get("deductible").value) == "5000"
    assert facts.get("contributor_count").value == 14
    assert facts.get("declared_headcount").value == 11
    assert facts.get("theft_full_elected").value == "not applicable"
    assert facts.get("inception").value.isoformat() == "2026-03-01"
    assert facts.get("premium_received").value.isoformat() == "2026-05-11"
    assert len(facts.get("demised_premises").value) == 2
    assert facts.get("workpass_below_minimum_count").value == 1
    for name in ("stock_sum_insured", "contributor_count"):
        assert facts.get(name).source == STRUCTURED


def test_buildings_value_stays_absent_when_the_schedule_states_null():
    """The schedule says the buildings sum insured is null because the premises are
    leasehold. That must remain UNESTABLISHED, never a computed figure."""
    facts = _structured_facts()
    assert facts.get("buildings_sum_insured").established is False


def test_author_class_travels_with_each_fact():
    facts = _structured_facts()
    assert facts.get("contributor_count").author_class == "AUTHORITY"
    assert facts.get("stock_value").author_class == "SELF"       # -> DECLARED
    assert facts.get("demised_premises").author_class == "THIRD_PARTY"
    assert facts.get("stock_sum_insured").author_class == "INSURER"


# ── text provenance (uploaded PDFs) ─────────────────────────────────────────

def test_text_extraction_reads_the_same_fields_from_pdfs():
    facts = _text_facts()
    assert facts["stock_sum_insured"]["value"] == "100000"
    assert facts["stock_value"]["value"] == "150000"
    assert facts["deductible"]["value"] == "5000"
    assert facts["plant_sum_insured"]["value"] == "180000"
    assert facts["theft_full_elected"]["value"] == "not applicable"
    assert facts["inception"]["value"] == "2026-03-01"
    assert facts["premium_received"]["value"] == "2026-05-11"
    assert facts["stock_sum_insured"]["source"] == PDF_TEXT


def test_permitted_use_is_captured_whole():
    """A greedy filler in the pattern used to eat the first characters of the value,
    turning 'Light food manufacturing' into 'od manufacturing'."""
    facts = _text_facts()
    assert facts["permitted_use"]["value"].startswith("Light food manufacturing")


def test_theft_election_is_not_inverted_by_regex_ordering():
    """With 'applicable' before 'not applicable' in the alternation, the regex matches
    the tail of 'not applicable' and reports the opposite of the truth."""
    facts = _text_facts()
    assert facts["theft_full_elected"]["value"] == "not applicable"


def test_both_provenances_produce_the_same_field_names():
    """One shape, two provenances. If the names diverge, rules start branching on
    where a document came from, which is how the fixture coupling got in."""
    structured = set(_structured_facts().as_dict())
    text = set(_text_facts().keys())
    shared = {"stock_sum_insured", "stock_value", "deductible", "inception",
              "premium_received", "theft_full_elected", "scheduled_premises",
              "demised_premises", "permitted_use", "business_description"}
    assert shared <= structured
    assert shared <= text


def test_malformed_document_degrades_to_absent_not_an_exception():
    """One unreadable document must not take down the review of the others."""
    conn = connect(":memory:")
    init_db(conn)
    from proven.ingest import DocStore, LoadedDoc
    store = DocStore()
    store.add(LoadedDoc("d1", "SCHEDULE", "INSURER",
                        data={"property_section": {"items": "not a list"}},
                        pages=["x"]))
    facts = extract_facts(store)
    assert facts.get("stock_sum_insured").established is False
    assert facts.notes                       # and it says so, rather than silently
