"""India jurisdiction pack: sourced boundaries, local formats and calendar arithmetic."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from proven import jurisdiction
from proven.api import SESSIONS, app
from proven.lang import date_parser_for


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as test_client:
        yield test_client
    SESSIONS.clear()


def _pages(*pages: str) -> list[tuple[str, list[str]]]:
    return [("india-schedule.pdf", list(pages))]


def test_india_pack_uses_indian_money_grouping_and_conservative_dates():
    inr = jurisdiction.pack("IN").currency
    assert inr is not None
    assert inr.format(Decimal("500000")) == "₹5,00,000"
    assert inr.format_with_units(Decimal("500000")) == "₹5,00,000 (5 lakh)"
    assert inr.format_with_units(Decimal("15000000")) == "₹1,50,00,000 (1.5 crore)"
    assert jurisdiction.format_money(Decimal("500000"), "INR", with_local_units=True) == \
        "₹5,00,000 (5 lakh)"

    parser = date_parser_for("INR")
    assert parser is not None
    assert parser.parse_date("23-Apr-2026") == date(2026, 4, 23)
    assert parser.parse_date("03/04/2026") is None


def test_indian_schedule_is_detected_from_uin_and_currency_signals():
    detected = jurisdiction.detect_jurisdiction(_pages(
        "Policy schedule\n"
        "Product UIN: IRDAI/HLT/LOTUS/P-H/V.I/001/2026-27\n"
        "Family-floater sum insured: ₹5,00,000"))

    assert (detected.code, detected.confidence) == ("IN", "MEDIUM")
    assert {reason["signal"] for reason in detected.reasons
            if reason.get("direction") == "for"} >= {"names", "currency", "registration_numbers"}
    assert jurisdiction.applicable(detected).pack.code == "IN"


def test_labeled_indian_insurer_registration_signal_is_conservative():
    matched = jurisdiction.detect_jurisdiction(_pages(
        "Insurer registration: IRDAI Regn. No. 151"))
    assert (matched.code, matched.confidence) == ("IN", "MEDIUM")
    registration = next(reason for reason in matched.reasons
                        if reason.get("signal") == "registration_numbers")
    assert registration["matched"] == "IRDAI Regn. No. 151"

    near_miss = jurisdiction.detect_jurisdiction(_pages(
        "Insurer registration: IRDAI Regn. No. 1510"))
    assert (near_miss.code, near_miss.confidence) == (jurisdiction.GENERIC, "LOW")
    assert all(reason.get("signal") != "registration_numbers"
               for reason in near_miss.reasons)


def test_indian_and_singapore_working_days_use_their_selected_holiday_calendars():
    assert jurisdiction.pack("IN").holiday_calendar.wired is True
    assert jurisdiction.pack("SG").holiday_calendar.wired is True
    assert jurisdiction.is_working_day(date(2024, 1, 26), "IN") is False
    assert jurisdiction.add_working_days(date(2024, 1, 25), 1, "IN") == date(2024, 1, 29)
    assert jurisdiction.is_working_day(date(2023, 8, 9), "SG") is False
    assert jurisdiction.add_working_days(date(2023, 8, 8), 1, "SG") == date(2023, 8, 10)


def test_india_candidate_entries_never_become_owner_facing_statements():
    india = jurisdiction.pack("IN")
    pending = india.entry("IN-TL-CASHLESS-AUTHORISATION")
    assert pending.verification.status == jurisdiction.CANDIDATE
    assert pending.owner_facing is False
    assert india.statement("TL-CASHLESS-AUTHORISATION") is None
    assert india.statement("STAT-MVA-146") is not None


def test_india_pack_reference_exposes_the_complaint_route_with_status_and_source(client):
    index = client.get("/api/jurisdictions").json()
    # T27 deliberate (PLAN rule 26): Hong Kong and Malaysia now have packs, so
    # Australia is the one country recognised without a pack. T28 adds the China pack.
    assert {pack["code"] for pack in index["packs"]} == {"CN", "GENERIC", "HK", "IN", "MY",
                                                          "SG"}
    assert {country["code"] for country in index["recognised_without_pack"]} == {"AU"}

    body = client.get("/api/jurisdictions/IN").json()
    assert body["currency"]["grouping"] == "INDIAN"
    ladder = body["complaint_ladder"]
    assert [step["values"]["order"] for step in ladder] == [1, 2, 3, 4]
    for step in ladder:
        verification = step["verification"]
        assert set(verification) >= {"status", "source_url", "retrieved_on", "quote", "verified_by"}
        assert step["owner_facing"] is (verification["status"] != jurisdiction.CANDIDATE)

    bima = next(step for step in ladder if step["id"] == "IN-CL-BIMA-BHAROSA")
    assert bima["verification"]["status"] == jurisdiction.SOURCE_QUOTED
    assert bima["verification"]["source_url"].startswith("https://")
    assert bima["verification"]["quote"]
    assert all(step["values"]["time_limit_days"] is None for step in ladder)


def test_india_document_authorship_mapping_is_pack_derived():
    india = jurisdiction.pack("IN")
    assert india.author_class_for("FIR") == "AUTHORITY"
    assert india.author_class_for("RC") == "AUTHORITY"
    assert india.author_class_for("HOSPITAL_DISCHARGE_SUMMARY") == "THIRD_PARTY"
    assert india.author_class_for("SELF_DECLARATION") == "SELF"
