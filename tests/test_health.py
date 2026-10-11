"""T08 deterministic health-policy mapping and evidence guards."""
from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import date

import pytest
from hypothesis import given, strategies as st

import pdfbuild_household
from proven import align, corpus, detect, extract, health, household, ingest, jurisdiction


IN_WORDING = "in-arogya-sanjeevani-policy-wording"
SG_MEDISHIELD = "sg-medishield-life-information-booklet"
SG_SHIELD = "sg-singlife-shield-standard-plan-wording"
TEMPORAL_SCHEDULE_QUOTE = (
    "Period: 1 April 2026 to 31 March 2027\n"
    "First policy commencement: 1 April 2026.\n"
    "Continuous coverage before this period: 0 months.\n"
    "Portability or migration credit: 0 months.\n"
    "Sum-insured enhancement: none."
)
CONTINUITY_EXCEPTION_SENTENCE = (
    "This exclusion shall not, however, apply if the Insured Person has "
    "Continuous Coverage for more than twelve months."
)


def _india_policy(*, start: str | None = "2026-04-01") -> dict:
    return {
        "country": "IN",
        "line_of_business": "HEALTH",
        "product": "Arogya Sanjeevani Policy (synthetic schedule)",
        "masked_number": "****-HEALTH6",
        "period": {"start": start, "end": "2027-03-31"},
        "coverage_items": [{"mechanism_code": "WAITING.PED"}],
    }


def _india_fixture_context() -> tuple[dict, health.ScheduleContext]:
    spec = next(policy for policy in household.POLICIES
                if policy.masked_number == "****-HEALTH6")
    member_ids = {key: f"member-{key}" for key in spec.member_keys}
    members = {
        member_ids[key]: {
            "id": member_ids[key], "name": household_member.name,
            "date_of_birth": household_member.date_of_birth,
        }
        for key in spec.member_keys
        for household_member in (next(member for member in household.MEMBERS
                                      if member.key == key),)
    }
    policy = {
        "source": "SYNTHETIC_EXAMPLE", "country": spec.country,
        "line_of_business": spec.line_of_business, "product": spec.product,
        "masked_number": spec.masked_number,
        "period": {"start": spec.period_start, "end": spec.period_end},
        "insured_member_ids": [member_ids[key] for key in spec.member_keys],
        "source_document_ids": ["india-schedule-document"],
    }
    context = health.schedule_context_for(policy, members)
    assert context is not None
    return policy, context


def test_health_schedule_detection_and_extraction_are_evidence_bound():
    pages = ingest.extract_pdf_pages(
        pdfbuild_household.india_health_schedule_pdf(), "rao-india-health-schedule.pdf")
    detected = detect.detect_health_schedule(pages)
    assert detected.is_health is True
    assert detected.family_floater is True
    assert detected.members == ("Raghavan", "Lakshmi")
    assert detected.member_ages == (("Raghavan", 67), ("Lakshmi", 63))
    assert detected.declared_ped == "Lakshmi: diabetes"
    assert detected.policy_start == "1 April 2026"
    assert detected.first_policy_start == "1 April 2026"
    assert detected.continuous_coverage_months == 0
    assert detected.portability_migration_credit_months == 0
    assert detected.sum_insured_enhanced is False
    for line in TEMPORAL_SCHEDULE_QUOTE.splitlines():
        assert line in pages[0]

    store = ingest.DocStore()
    store.add(ingest.LoadedDoc("health-schedule", "SCHEDULE", "INSURER", pages=pages))
    facts = extract.extract_facts(store, pack=jurisdiction.pack("IN"))
    assert facts.get("health_family_floater").value is True
    assert facts.get("health_insured_members").value == ["Raghavan", "Lakshmi"]
    assert facts.get("health_member_ages").value == {"Raghavan": 67, "Lakshmi": 63}
    assert facts.get("health_declared_ped").value == "Lakshmi: diabetes"
    assert facts.get("health_policy_start").value == date(2026, 4, 1)
    assert facts.get("health_first_policy_start").value == date(2026, 4, 1)
    assert facts.get("health_continuous_coverage_months").value == 0
    assert facts.get("health_portability_migration_credit_months").value == 0
    assert facts.get("health_sum_insured_enhanced").value is False
    assert facts.get("health_sum_insured_enhanced").established is True
    assert str(facts.get("health_sum_insured").value) == "500000"
    assert facts.get("health_sum_insured").unit == "INR"


@given(st.dates(min_value=date(2000, 1, 1), max_value=date(2090, 12, 31)),
       st.integers(min_value=0, max_value=120))
def test_waiting_period_calendar_arithmetic_never_invents_a_day(start, months):
    result = health.add_months(start, months)
    expected_index = (start.month - 1) + months
    assert (result.year, result.month) == (
        start.year + expected_index // 12, expected_index % 12 + 1)
    assert 1 <= result.day <= 31
    assert result >= start


def test_co_payment_needs_the_policy_condition_not_a_definition():
    definition_only = [(1, "Co-payment means a shared-cost requirement under a health policy.")]
    assert health.match_term_pages("IN", "SHARE.CO_PAYMENT", definition_only) is None


@pytest.mark.requires_corpus(IN_WORDING)
def test_missing_schedule_operands_produce_no_derived_health_value():
    assessment = health.assess_policy(_india_policy())
    by_code = {item["mechanism_code"]: item for item in assessment["items"]}
    for code in ("WAITING.PED", "WAITING.INITIAL", "WAITING.SPECIFIC",
                 "CONDITION.MORATORIUM", "SHARE.CO_PAYMENT"):
        item = by_code[code]
        assert item["state"] == "INFORMATION_MISSING"
        assert item["evidence_class"] == "UNESTABLISHED"
        assert item["value"] is None
        assert item["missing_document_key"] == "health.document.policy_schedule"


@pytest.mark.requires_corpus(IN_WORDING)
def test_current_period_without_continuity_operands_withholds_all_dates():
    """A current policy period alone cannot stand in for coverage history."""
    policy, context = _india_fixture_context()
    citations = {key: dict(value) for key, value in context.citations.items()}
    citations["policy_start"]["quote"] = "Period: 1 April 2026 to 31 March 2027"
    current_period_only = replace(context, citations=citations)

    assessment = health.assess_policy(policy, schedule=current_period_only)
    by_code = {item["mechanism_code"]: item for item in assessment["items"]}
    for code in ("WAITING.PED", "WAITING.INITIAL", "CONDITION.MORATORIUM"):
        item = by_code[code]
        assert item["state"] == "INFORMATION_MISSING"
        assert item["evidence_class"] == "UNESTABLISHED"
        assert item["value"] is None
        assert item["missing_document_key"] == "health.document.policy_schedule"


@pytest.mark.requires_corpus(IN_WORDING)
def test_documented_continuity_exception_has_no_initial_waiting_date():
    """More than twelve documented continuous months is an exception, not a date."""
    policy, context = _india_fixture_context()
    exception_quote = (
        "Period: 1 April 2026 to 31 March 2027\n"
        "First policy commencement: 1 April 2025.\n"
        "Continuous coverage before this period: 13 months.\n"
        "Portability or migration credit: 0 months.\n"
        "Sum-insured enhancement: none."
    )
    citations = {key: dict(value) for key, value in context.citations.items()}
    citations["policy_start"]["quote"] = exception_quote
    assessment = health.assess_policy(policy, schedule=replace(context, citations=citations))
    by_code = {item["mechanism_code"]: item for item in assessment["items"]}

    initial = by_code["WAITING.INITIAL"]
    assert initial["state"] == "DOCUMENTED"
    assert initial["label_key"] == "health.item.initial_waiting_exception"
    assert initial["display"] == "CONDITION"
    assert initial["value"] is None
    assert initial["operand_citations"][0]["quote"] == exception_quote
    assert CONTINUITY_EXCEPTION_SENTENCE in " ".join(initial["citation"]["quote"].split())
    for code in ("WAITING.PED", "CONDITION.MORATORIUM"):
        assert by_code[code]["state"] == "INFORMATION_MISSING"
        assert by_code[code]["value"] is None


@pytest.mark.requires_corpus(IN_WORDING)
def test_health_refuses_changed_original_or_page_text_before_citing(tmp_path, monkeypatch):
    """The mapper reads a verified original and a separately expected text hash."""
    source_original = corpus.original_path(IN_WORDING)
    source_text = corpus.text_path(IN_WORDING)

    text_root = tmp_path / "changed-text"
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(text_root))
    target_original = corpus.original_path(IN_WORDING)
    target_text = corpus.text_path(IN_WORDING)
    target_original.parent.mkdir(parents=True, exist_ok=True)
    target_text.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_original, target_original)
    shutil.copyfile(source_text, target_text)
    assert health._pages(IN_WORDING)
    target_text.write_bytes(b"=== PAGE 1 ===\nchanged after verification\n")
    with pytest.raises(corpus.CorpusDrifted, match="page-marked text"):
        health._pages(IN_WORDING)

    original_root = tmp_path / "changed-original"
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(original_root))
    target_original = corpus.original_path(IN_WORDING)
    target_text = corpus.text_path(IN_WORDING)
    target_original.parent.mkdir(parents=True, exist_ok=True)
    target_text.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_original, target_original)
    shutil.copyfile(source_text, target_text)
    target_original.write_bytes(b"%PDF-1.4 changed after verification\n")
    with pytest.raises(corpus.CorpusDrifted, match="manifest"):
        health._pages(IN_WORDING)


@pytest.mark.requires_corpus(IN_WORDING, SG_MEDISHIELD, SG_SHIELD)
def test_health_terms_are_cited_and_limited_to_allowed_states():
    india_policy, context = _india_fixture_context()
    assessments = [
        health.assess_policy(india_policy, schedule=context),
        health.assess_policy({"country": "SG", "product": "MediShield Life (synthetic schedule)",
                              "period": {"start": "2026-01-01"}, "coverage_items": []}),
        health.assess_policy({"country": "SG", "product": "Singlife Shield Standard Plan (synthetic schedule)",
                              "period": {"start": "2026-01-01"}, "coverage_items": []}),
    ]
    india = assessments[0]
    by_code = {item["mechanism_code"]: item for item in india["items"]}
    assert by_code["WAITING.PED"]["value"] == "2029-04-01"
    assert by_code["WAITING.PED"]["member_scope"] == [{
        "id": "member-lakshmi", "name": "Lakshmi", "age": "63",
    }]
    assert {citation["quote"] for citation in by_code["WAITING.PED"]["operand_citations"]} == {
        TEMPORAL_SCHEDULE_QUOTE, "Lakshmi has declared diabetes.",
    }
    assert by_code["WAITING.INITIAL"]["value"] == "2026-05-01"
    assert {citation["quote"] for citation in by_code["WAITING.INITIAL"]["operand_citations"]} == {
        TEMPORAL_SCHEDULE_QUOTE,
    }
    assert by_code["LIMIT.ROOM_RENT"]["value"] == "5000"
    assert by_code["LIMIT.ICU"]["value"] == "10000"
    assert by_code["SHARE.CO_PAYMENT"]["value"] == "5"
    assert [member["name"] for member in by_code["SHARE.CO_PAYMENT"]["member_scope"]] == [
        "Raghavan", "Lakshmi",
    ]
    assert by_code["CONDITION.MORATORIUM"]["value"] == "2031-04-01"
    assert {citation["quote"] for citation in by_code["CONDITION.MORATORIUM"]["operand_citations"]} == {
        TEMPORAL_SCHEDULE_QUOTE,
    }
    assert by_code["LIMIT.FAMILY_FLOATER_SHARED"]["state"] == "DOCUMENTED"
    assert {item["mechanism_code"] for item in india["items"]} >= {
        "WAITING.PED", "WAITING.INITIAL", "WAITING.SPECIFIC", "LIMIT.ROOM_RENT",
        "LIMIT.ICU", "CONDITION.PROPORTIONATE_DEDUCTION", "COVERED.DAY_CARE",
        "LIMIT.PRE_HOSPITALISATION", "LIMIT.POST_HOSPITALISATION",
        "LIMIT.CUMULATIVE_BONUS", "DEADLINE.CLAIM_INTIMATION",
        "CONDITION.REIMBURSEMENT", "LIMIT.FAMILY_FLOATER_SHARED",
    }

    for assessment in assessments:
        for item in assessment["items"]:
            assert item["state"] in {
                "DOCUMENTED", "NOT_CONFIRMED", "INFORMATION_MISSING",
                "REQUIRES_AUTHORISED_REVIEW",
            }
            assert item["evidence_class"] in {
                "PROVEN", "DECLARED", "MARKET_CONVENTION", "UNESTABLISHED",
            }
            citation = item["citation"]
            assert citation["page"] >= 1
            assert citation["quote"].strip()
            if citation.get("source_url"):
                assert citation["source_url"].startswith("https://")
            # Owner-facing labels encode neither advice nor a coverage outcome.
            assert not any(word in item["label_key"].lower()
                           for word in ("recommend", "not_covered", "will_pay"))


@pytest.mark.requires_corpus(IN_WORDING, SG_SHIELD)
def test_health_alignment_binds_the_full_profile_structures():
    india = align.align_pages(
        [text for _page, text in health._pages(IN_WORDING)], carrier="GO_DIGIT")
    india_mechanisms = {entry.mechanism for entry in india.alignments}
    assert {
        "HEALTH.WAITING.PED", "HEALTH.WAITING.INITIAL", "HEALTH.WAITING.SPECIFIC",
        "HEALTH.LIMIT.ROOM_RENT", "HEALTH.LIMIT.ICU_DAILY",
        "HEALTH.SHARE.CO_PAYMENT", "HEALTH.CLAIM.MORATORIUM",
        "HEALTH.CLAIM.CASHLESS", "HEALTH.CONDITION.PROPORTIONATE_DEDUCTION",
        "HEALTH.CONDITION.DAY_CARE", "HEALTH.LIMIT.PRE_HOSPITALISATION",
        "HEALTH.LIMIT.POST_HOSPITALISATION", "HEALTH.LIMIT.CUMULATIVE_BONUS",
        "HEALTH.DEADLINE.CLAIM_INTIMATION", "HEALTH.CONDITION.REIMBURSEMENT",
    } <= india_mechanisms

    singapore = align.align_pages(
        [text for _page, text in health._pages(SG_SHIELD)], carrier="SINGLIFE")
    singapore_mechanisms = {entry.mechanism for entry in singapore.alignments}
    assert {"HEALTH.SG.PRO_RATION", "HEALTH.SG.WARD_ENTITLEMENT",
            "HEALTH.LIMIT.ICU_DAILY"} <= singapore_mechanisms


def test_only_server_stamped_synthetic_health_policies_are_mapped():
    plain = {
        "policies": [{
            "source": "PORTFOLIO", "line_of_business": "HEALTH", "country": "IN",
            "product": "Arogya Sanjeevani Policy", "coverage_items": [],
        }],
    }
    assert "health_items" not in health.enrich_portfolio(plain)["policies"][0]
