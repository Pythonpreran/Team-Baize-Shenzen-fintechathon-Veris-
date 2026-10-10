"""T08 deterministic health-policy mapping and evidence guards."""
from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given, strategies as st

import pdfbuild_household
from proven import align, detect, extract, health, ingest, jurisdiction


IN_WORDING = "in-arogya-sanjeevani-policy-wording"
SG_MEDISHIELD = "sg-medishield-life-information-booklet"
SG_SHIELD = "sg-singlife-shield-standard-plan-wording"


def _india_policy(*, start: str | None = "2026-04-01") -> dict:
    return {
        "country": "IN",
        "product": "Arogya Sanjeevani Policy (synthetic schedule)",
        "period": {"start": start},
        "coverage_items": [{"mechanism_code": "WAITING.PED"}],
    }


def test_health_schedule_detection_and_extraction_are_evidence_bound():
    pages = ingest.extract_pdf_pages(
        pdfbuild_household.india_health_schedule_pdf(), "rao-india-health-schedule.pdf")
    detected = detect.detect_health_schedule(pages)
    assert detected.is_health is True
    assert detected.family_floater is True
    assert detected.members == ("Raghavan", "Lakshmi")
    assert detected.declared_ped == "Lakshmi: diabetes"
    assert detected.policy_start == "1 April 2026"

    store = ingest.DocStore()
    store.add(ingest.LoadedDoc("health-schedule", "SCHEDULE", "INSURER", pages=pages))
    facts = extract.extract_facts(store, pack=jurisdiction.pack("IN"))
    assert facts.get("health_family_floater").value is True
    assert facts.get("health_insured_members").value == ["Raghavan", "Lakshmi"]
    assert facts.get("health_declared_ped").value == "Lakshmi: diabetes"
    assert facts.get("health_policy_start").value == date(2026, 4, 1)
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
def test_missing_policy_start_produces_no_waiting_period_end_date():
    assessment = health.assess_policy(_india_policy(start=None))
    waiting = next(item for item in assessment["items"]
                   if item["mechanism_code"] == "WAITING.PED")
    assert waiting["state"] == "INFORMATION_MISSING"
    assert waiting["evidence_class"] == "UNESTABLISHED"
    assert waiting["value"] is None
    assert waiting["missing_document_key"] == "health.document.policy_schedule"


@pytest.mark.requires_corpus(IN_WORDING, SG_MEDISHIELD, SG_SHIELD)
def test_health_terms_are_cited_and_limited_to_allowed_states():
    policies = [
        _india_policy(),
        {"country": "SG", "product": "MediShield Life (synthetic schedule)",
         "period": {"start": "2026-01-01"}, "coverage_items": []},
        {"country": "SG", "product": "Singlife Shield Standard Plan (synthetic schedule)",
         "period": {"start": "2026-01-01"}, "coverage_items": []},
    ]
    assessments = [health.assess_policy(policy) for policy in policies]
    india = assessments[0]
    assert india["mapped"] is True
    by_code = {item["mechanism_code"]: item for item in india["items"]}
    assert by_code["WAITING.PED"]["value"] == "2029-04-01"
    assert by_code["LIMIT.ROOM_RENT"]["value"] == "5000"
    assert by_code["LIMIT.ROOM_RENT"]["unit"] == "INR"
    assert by_code["SHARE.CO_PAYMENT"]["value"] == "5"
    assert by_code["CONDITION.MORATORIUM"]["value"] == "60"

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
            assert citation["source_url"].startswith("https://")
            # Owner-facing labels encode neither advice nor a coverage outcome.
            assert not any(word in item["label_key"].lower()
                           for word in ("recommend", "not_covered", "will_pay"))


@pytest.mark.requires_corpus(IN_WORDING, SG_SHIELD)
def test_health_alignment_binds_the_expected_structures():
    india = align.align_pages(
        [text for _page, text in health._pages(IN_WORDING)], carrier="GO_DIGIT")
    india_mechanisms = {entry.mechanism for entry in india.alignments}
    assert {
        "HEALTH.WAITING.PED", "HEALTH.LIMIT.ROOM_RENT", "HEALTH.SHARE.CO_PAYMENT",
        "HEALTH.CLAIM.MORATORIUM", "HEALTH.CLAIM.CASHLESS",
    } <= india_mechanisms

    singapore = align.align_pages(
        [text for _page, text in health._pages(SG_SHIELD)], carrier="SINGLIFE")
    assert "HEALTH.SG.PRO_RATION" in {entry.mechanism for entry in singapore.alignments}


def test_only_server_stamped_synthetic_health_policies_are_mapped():
    plain = {
        "policies": [{
            "source": "PORTFOLIO", "line_of_business": "HEALTH", "country": "IN",
            "product": "Arogya Sanjeevani Policy", "coverage_items": [],
        }],
    }
    assert "health_items" not in health.enrich_portfolio(plain)["policies"][0]
