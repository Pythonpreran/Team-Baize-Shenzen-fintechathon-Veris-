"""T27: the Hong Kong and Malaysia packs at standard depth.

What these pin, beyond the deliberate GENERIC-to-pack changes in test_jurisdiction.py:

  * each pack's money, dates and working days are its own country's (HKD/MYR, the HK and
    MY holiday calendars), and an explicit marker always names its own currency;
  * every regulator, statute, compulsory-cover and complaint record that could not be
    machine-checked against its primary text is CANDIDATE and never owner-facing;
  * the only owner-facing statements are the two quotes a fetched primary text supports;
  * HKID numbers are redacted only when their published check character is right, and
    MyKad numbers only when they carry a real birth date and place-of-birth code.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pdfbuild
import pytest

from proven import corpus, ingest, jurisdiction, privacy, redact
from proven.extract import PolicyFacts, _schedule_text, find_money_with_currency
from proven.ingest import LoadedDoc
from proven.lang import date_parser_for, money_parser_for

#: Fictional Hong Kong wording, marked synthetic, carrying the signals a real one carries.
HK_WORDING_LINES = [
    "SYNTHETIC SAMPLE - NOT ISSUED BY ANY INSURER",
    "POLICY WORDING",
    "This Policy is governed by the laws of Hong Kong.",
    "Excess: HK$ 500 each and every claim.",
    "Example Insurance (HK) Limited is authorised by the Insurance Authority.",
]


def test_both_packs_load_with_their_own_currency_timezone_and_calendar():
    hk, my = jurisdiction.pack("HK"), jurisdiction.pack("MY")
    assert (hk.currency.code, hk.timezone) == ("HKD", "Asia/Hong_Kong")
    assert (my.currency.code, my.timezone) == ("MYR", "Asia/Kuala_Lumpur")
    assert jurisdiction.format_money(Decimal("1500"), "HKD") == "HK$1,500"
    assert jurisdiction.format_money(Decimal("1000"), "MYR") == "RM1,000"
    # Establishment Day is a Hong Kong holiday and Merdeka a Malaysian one; neither is
    # Singapore's, so working days really come from each pack's own calendar.
    assert not jurisdiction.is_working_day(date(2026, 7, 1), "HK")
    assert jurisdiction.is_working_day(date(2026, 7, 1), "SG")
    assert not jurisdiction.is_working_day(date(2026, 8, 31), "MY")
    assert jurisdiction.is_working_day(date(2026, 8, 31), "SG")
    nxt = jurisdiction.add_working_days(date(2026, 6, 30), 1, "HK")
    assert nxt == date(2026, 7, 2)


@pytest.mark.parametrize("code", ["HK", "MY"])
def test_unverified_law_is_candidate_and_never_owner_facing(code):
    p = jurisdiction.pack(code)
    owner_facing = {e.id for e in p.entries() if e.owner_facing}
    for entry in p.entries():
        status = entry.verification.status
        if entry.owner_facing:
            assert status in jurisdiction.OWNER_FACING_STATUSES
            if status == "SOURCE_QUOTED":
                assert entry.verification.quote and entry.verification.source_url
                assert entry.verification.retrieved_on
        else:
            assert status == "CANDIDATE"
    # The regulator, every statute and every compulsory cover wait for a primary quote.
    for entry in (*p.regulators, *p.statutes, *p.compulsory_covers):
        assert not entry.owner_facing, f"{entry.id} became owner-facing unquoted"
    expected = {"HK": {"HK-MC-VHIS-STANDARD-PLAN"},
                "MY": {"MY-CL-FINANCIAL-OMBUDSMAN"}}[code]
    regulatory = {e.id for e in (*p.regulators, *p.statutes, *p.compulsory_covers,
                                 *p.regulatory_timelines, *p.complaint_ladder,
                                 *p.market_conventions)}
    assert owner_facing & regulatory == expected


def test_compulsory_covers_name_the_statutes_the_plan_requires():
    hk = {e.values["protection"]: e.values["statute"]
          for e in jurisdiction.pack("HK").compulsory_covers}
    assert hk == {"EMPLOYEES_COMPENSATION": "HK-STAT-ECO-CAP282",
                  "MOTOR_THIRD_PARTY": "HK-STAT-MVITPR-CAP272"}
    my = {e.values["protection"]: e.values["statute"]
          for e in jurisdiction.pack("MY").compulsory_covers}
    assert my == {"MOTOR_THIRD_PARTY": "MY-STAT-RTA-1987"}


def test_withheld_privacy_regimes_give_no_owner_statement():
    notices = {n["country"]: n for n in privacy.notices(["HK", "MY"])}
    for code in ("HK", "MY"):
        assert notices[code]["state"] == "INFORMATION_MISSING"
        assert notices[code]["source"] is None
        assert "withheld" in notices[code]["notice"]


def test_a_synthetic_hong_kong_wording_is_read_under_the_hong_kong_pack():
    pages = ingest.extract_pdf_pages_cached(pdfbuild.build_pdf([HK_WORDING_LINES]),
                                            "hk.pdf")
    applied = jurisdiction.assess([("hk.pdf", pages)])
    assert (applied.detection.code, applied.detection.confidence) == ("HK", "HIGH")
    assert applied.pack.code == "HK" and applied.pack_available
    assert "Singapore" not in (applied.notice or "")


def test_explicit_markers_name_their_own_currency_and_a_bare_dollar_follows_the_pack():
    assert money_parser_for("HKD").parse_money("HK$1,500") == Decimal("1500")
    assert money_parser_for("MYR").parse_money("RM 1,000") == Decimal("1000")
    hit = find_money_with_currency(["Room: HK$1,500 per day"], ("room",), currency=None)
    assert hit is not None and hit[0] == Decimal("1500") and hit[3] == "HKD"
    # The "$" inside "HK$" is never also read as a Singapore dollar.
    hit = find_money_with_currency(["Stock: HK$500,000"], ("stock",), currency="SGD")
    assert hit is not None and hit[3] == "HKD"
    hit = find_money_with_currency(["Excess: RM 1,000 each"], ("excess",), currency=None)
    assert hit is not None and hit[0] == Decimal("1000") and hit[3] == "MYR"
    # A bare "$" is the Hong Kong dollar under the Hong Kong pack, and keeps its
    # Singapore reading everywhere else (behaviour before T27, unchanged).
    assert find_money_with_currency(["Excess: $750"], ("excess",), currency="HKD")[3] == "HKD"
    assert find_money_with_currency(["Excess: $750"], ("excess",), currency="SGD")[3] == "SGD"
    # A lower-case "rm" inside a word is not ringgit.
    assert money_parser_for("MYR").parse_money("confirm 1,000") is None


def test_day_first_dates_refuse_to_guess_under_both_packs():
    for currency in ("HKD", "MYR"):
        parser = date_parser_for(currency)
        assert parser.parse_date("15/07/2026") == date(2026, 7, 15)
        assert parser.parse_date("01/07/2026") is None
        assert parser.parse_date("15 Jul 2026") == date(2026, 7, 15)


def test_a_malaysian_schedule_yields_its_dates_and_ringgit_under_the_pack():
    facts = PolicyFacts()
    doc = LoadedDoc("schedule", "SCHEDULE", "INSURER", pages=[
        "Period of Insurance: from 2026-03-01 to 2027-02-28\n"
        "Premium Received: 2026-05-11\nDeductible: RM 2,500 each and every loss"])
    _schedule_text(facts, doc, currency="MYR")
    assert facts.get("inception").value == date(2026, 3, 1)
    assert facts.get("premium_received").value == date(2026, 5, 11)
    deductible = facts.get("deductible")
    assert (deductible.value, deductible.unit) == (Decimal("2500"), "MYR")


def test_hkid_is_redacted_only_with_its_published_check_character():
    assert redact.valid_hkid("A123456(3)")
    assert redact.valid_hkid("XA123456(8)")
    assert redact.valid_hkid("C1234569")
    assert not redact.valid_hkid("A123456(4)")
    vault = redact.Redactor()
    source = "Claimant HKID A123456(3); form reference A123456(4)."
    out = vault.redact_text(source)
    assert "A123456(3)" not in out
    assert "A123456(4)" in out, "a wrong check character is not an HKID"
    assert vault.token_summary().get("HKID") == 1
    assert vault.rehydrate(out) == source


def test_mykad_is_redacted_only_with_a_real_birth_date_and_place_code():
    assert redact.valid_mykad("800101-14-5678")
    assert not redact.valid_mykad("801301-14-5678")
    assert not redact.valid_mykad("800132-14-5678")
    assert not redact.valid_mykad("800101-00-5678")
    vault = redact.Redactor()
    source = "Insured MyKad 800101-14-5678, Kuala Lumpur."
    out = vault.redact_text(source)
    assert "800101-14-5678" not in out
    assert vault.token_summary().get("MYKAD") == 1
    assert vault.rehydrate(out) == source
    assert redact.Redactor().redact_value({"mykad": "800101-14-5678"}) != \
        {"mykad": "800101-14-5678"}


@pytest.mark.requires_corpus("hk-vhis-standard-plan-template")
def test_the_vhis_quote_is_verbatim_in_the_held_government_template():
    if corpus.missing(["hk-vhis-standard-plan-template"]):
        pytest.skip("corpus document not fetched: hk-vhis-standard-plan-template")
    entry = next(e for e in jurisdiction.pack("HK").market_conventions
                 if e.id == "HK-MC-VHIS-STANDARD-PLAN")
    text = corpus.require_text("hk-vhis-standard-plan-template").read_text(encoding="utf-8")
    assert entry.verification.quote in " ".join(text.split())
    assert entry.owner_facing


@pytest.mark.requires_corpus("hk-vhis-standard-plan-template")
def test_vhis_standard_plan_mechanisms_bind_by_structure_with_their_values():
    from proven import align
    if corpus.missing(["hk-vhis-standard-plan-template"]):
        pytest.skip("corpus document not fetched: hk-vhis-standard-plan-template")
    pages = corpus.split_pages(corpus.require_text(
        "hk-vhis-standard-plan-template").read_text(encoding="utf-8"))
    result = align.align_pages(pages, carrier="hk-vhis-standard-plan-template")
    found = {a.mechanism: a for a in result.alignments}
    assert found["HEALTH.HK.ROOM_AND_BOARD"].values.get("amount") == "750"
    assert found["HEALTH.HK.ROOM_AND_BOARD"].values.get("days") == "180"
    assert found["HEALTH.HK.GUARANTEED_RENEWAL"].values.get("age") == "100"
    assert "HEALTH.HK.NO_LIFETIME_LIMIT" in found
    assert "HEALTH.HK.UNKNOWN_PRE_EXISTING" in found
    for alignment in found.values():
        page = " ".join(pages[alignment.page_no - 1].split())
        assert " ".join(alignment.quote.split()) in page
