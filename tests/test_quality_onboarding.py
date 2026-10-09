"""Page quality, onboarding, and concentration risk — the last three gaps.

Three separate features, one shared discipline: each takes a place where the product was
weaker than its story and makes the code true rather than softening the copy.

  PAGE QUALITY      the corpus is cleaner than reality, so name the pages we could not read
                    and refuse to grade a finding PROVEN on them.
  ONBOARDING        a real SME does not have fourteen documents to hand, so be useful on two
                    and say honestly how much of the picture two buys.
  CONCENTRATION     "supply chain" invites a network diagram we cannot evidence, so report
                    the concentration we CAN see and name the sub-tier we cannot.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import pagequality, supplychain
from proven.api import MANDATORY_KINDS, SESSIONS, app


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


# ══ page quality ═══════════════════════════════════════════════════════════

def test_a_low_confidence_page_cannot_support_a_proven_finding():
    """The rule that makes this a control rather than a caveat.

    The confidence of the EXTRACTION constrains the class of the EVIDENCE, exactly as
    authorship already does. A page we could barely read is UNESTABLISHED however
    authoritative its author.
    """
    page = pagequality.assess_page(page_no=9, confidence=41.0, text_length=800)
    assert page.verdict == pagequality.UNCERTAIN
    assert page.class_ceiling == "UNESTABLISHED"
    assert pagequality.cap_evidence_class("PROVEN", page.class_ceiling) == "UNESTABLISHED"


def test_a_clean_page_never_promotes_a_weaker_class():
    """One-directional on purpose. A good scan is not evidence OF anything — it only means we
    read the words correctly, so it can never turn a DECLARED figure into a PROVEN one."""
    assert pagequality.cap_evidence_class("DECLARED", "PROVEN") == "DECLARED"
    assert pagequality.cap_evidence_class("UNESTABLISHED", "PROVEN") == "UNESTABLISHED"


def test_an_unreadable_page_is_reported_not_silently_dropped():
    page = pagequality.assess_page(page_no=3, confidence=None, text_length=0,
                                   status="EMPTY")
    assert page.verdict == pagequality.UNREADABLE
    assert "Nothing could be extracted" in page.reason


def test_a_sparse_page_with_good_confidence_is_still_suspect():
    """A near-empty page with high confidence usually means the layout defeated the parser,
    not that the page was blank."""
    page = pagequality.assess_page(page_no=5, confidence=95.0, text_length=6)
    assert page.verdict == pagequality.UNCERTAIN
    assert "layout" in page.reason.lower()


def test_a_digital_text_layer_is_distinguished_from_a_good_scan():
    """Different facts. An auditor should be able to tell "never needed OCR" from "OCR went
    well", and lumping them together would hide which pages were recognised at all."""
    page = pagequality.assess_page(page_no=1, confidence=None, text_length=900,
                                   had_text_layer=True)
    assert page.verdict == pagequality.NOT_SCANNED
    assert page.class_ceiling == "PROVEN"


def test_the_report_names_the_specific_pages():
    """Page level, not document level. "91% overall" cannot be acted on."""
    report = pagequality.report([
        {"page_no": 1, "text_length": 900, "confidence": 96.0},
        {"page_no": 9, "text_length": 700, "confidence": 38.0},
        {"page_no": 10, "text_length": 0, "confidence": None, "status": "EMPTY"},
    ], document_kind="BASE_WORDING", filename="wording.pdf")
    assert report["pages_total"] == 3
    assert report["pages_uncertain"] == 1
    assert report["pages_unreadable"] == 1
    flagged = {p["page_no"] for p in report["pages_needing_attention"]}
    assert flagged == {9, 10}
    assert "page" in report["why_page_level"].lower()


def test_a_finding_takes_the_weakest_page_it_rests_on():
    """A finding quoting a clean page 3 and an uncertain page 9 is an uncertain finding."""
    pages = pagequality.report([
        {"page_no": 3, "text_length": 900, "confidence": 97.0},
        {"page_no": 9, "text_length": 700, "confidence": 35.0},
    ])["pages"]
    assert pagequality.ceiling_for_pages(pages, [3]) == "PROVEN"
    assert pagequality.ceiling_for_pages(pages, [3, 9]) == "UNESTABLISHED"


def test_the_page_quality_endpoint_reports_the_demo_wording(client):
    body = client.get("/api/page-quality").json()
    assert body["available"] is True
    assert body["pages_total"] > 0
    assert body["documents"]
    assert body["class_ceiling_rule"]
    for doc in body["documents"]:
        assert doc["summary"]


def test_page_quality_goes_quiet_on_deletion(client):
    assert client.get("/api/page-quality").status_code == 200
    client.delete("/api/case")
    assert client.get("/api/page-quality").json()["available"] is False


# ══ onboarding ═════════════════════════════════════════════════════════════

def test_only_two_documents_are_mandatory():
    """The irreducible minimum. Nothing else can establish what cover exists at all."""
    assert MANDATORY_KINDS == ("BASE_WORDING", "SCHEDULE")


def test_the_app_is_useful_on_the_mandatory_two(client):
    """"Upload fourteen documents before you see anything" is where a pilot dies."""
    body = client.get("/api/onboarding").json()
    assert body["available"] is True
    assert body["ready_to_assess"] is True
    assert body["missing_mandatory"] == []
    assert body["completeness"]["areas_assessable"] >= 1


def test_the_completeness_meter_is_not_computed_twice(client):
    """Two sources for one figure is how a product starts contradicting itself."""
    onboarding = client.get("/api/onboarding").json()
    blind = client.get("/api/blindspots").json()
    assert (onboarding["completeness"]["areas_assessable"]
            == blind["coverage"]["areas_we_can_speak_to"])
    assert (onboarding["completeness"]["areas_total"]
            == blind["coverage"]["areas_total"])
    assert onboarding["completeness"]["computed_from"] == "/api/blindspots"


def test_each_next_document_says_what_it_unlocks(client):
    """Progressive disclosure: one document at a time, ranked by what it settles — which
    voi.py already computes, so this surfaces it rather than re-ranking it."""
    body = client.get("/api/onboarding").json()
    for item in body["next_documents"]:
        assert item["document"]
        assert item["unlocks"], "a requested document with no stated purpose is a chore"


def test_identity_prefill_separates_what_is_wired_from_what_is_a_seam(client):
    """Two sources, two honesty levels, and the distinction is the point.

    This test originally asserted the whole prefill was a declared seam, which was true of the
    first pass and is no longer. ACRA is now WIRED — `discovery.fetch_acra_by_name()` already
    called the data.gov.sg datastore, so reusing it for onboarding was a few lines. Myinfo
    Business remains a seam because it needs a real onboarding agreement with GovTech.

    Collapsing the two would be the overclaim: "we integrate with government identity
    services" reads the same whether one, both or neither is real.
    """
    body = client.get("/api/onboarding").json()
    prefill = body["identity_prefill"]
    assert any("ACRA" in s for s in prefill["wired"]), (
        "ACRA is wired and must be reported as wired")
    assert "Myinfo Business" in prefill["seam_only"]
    assert "seam" in prefill["myinfo_status"].lower()
    # Offline in the suite, so nothing is offered — and it must say why rather than look empty.
    assert prefill["available"] is False
    assert prefill["status"]


# ══ concentration risk ═════════════════════════════════════════════════════

def test_concentration_is_reported_from_documents_already_read(client):
    body = client.get("/api/supply-chain").json()
    assert "concentrations" in body
    for c in body["concentrations"]:
        assert c["inferred"] is False, "a concentration was inferred rather than evidenced"
        assert c["evidence_class"] in ("PROVEN", "DECLARED")
        assert c["reading"]
        assert c["why_it_bites"]


def test_the_single_supplier_concentration_cites_the_schedule_listing_none(client):
    """The sharper, honest sentence: one cold-storage supply, no alternate named, and CBI
    conventionally responds only to suppliers the schedule lists — yours lists none."""
    body = client.get("/api/supply-chain").json()
    single = [c for c in body["concentrations"]
              if c["kind"] == supplychain.SINGLE_SOURCE]
    if single:
        assert "schedule" in single[0]["why_it_bites"].lower()


def test_the_utility_single_point_of_failure_is_reported(client):
    """Cold storage read off the tenancy's permitted use — THIRD_PARTY authored, so it is
    established even though the owner never mentioned it."""
    body = client.get("/api/supply-chain").json()
    kinds = {c["kind"] for c in body["concentrations"]}
    assert supplychain.UTILITY_SPOF in kinds


def test_geographic_concentration_uses_the_postal_district_not_a_guessed_radius(client):
    """Coarse on purpose. Claiming a tighter radius from an address string would be
    precision we have not earned."""
    body = client.get("/api/supply-chain").json()
    geo = [c for c in body["concentrations"]
           if c["kind"] == supplychain.GEOGRAPHIC]
    for c in geo:
        assert "district" in c["subject"].lower()


def test_the_sub_tier_boundary_is_stated_as_the_feature(client):
    """Three suppliers who all depend on one upstream plant are one supplier. We have no
    document that evidences it, so we name it rather than model it."""
    body = client.get("/api/supply-chain").json()
    sub = body["sub_tier"]
    assert sub["visible"] is False
    assert "suppliers' suppliers" in sub["statement"]
    assert sub["why_we_do_not_estimate_it"]
    assert sub["what_would_reveal_it"]


def test_no_supplier_is_invented(client):
    """Every named subject must trace to a document the engine read."""
    body = client.get("/api/supply-chain").json()
    known = {d["name"] for d in body["dependencies"]}
    for c in body["concentrations"]:
        if c["kind"] in (supplychain.UTILITY_SPOF, supplychain.LANDLORD_SPOF,
                         supplychain.SINGLE_SOURCE):
            assert c["subject"] in known, (
                f"{c['subject']!r} is not a dependency any document named")
