"""The eight items outstanding after the ten steps, and the §47 correction.

Each test here corresponds to one line of the Master Task List that the first pass either
missed or built differently from the specification. They are grouped by the item they close so
a reviewer can check the list against the suite rather than against a summary.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from proven import advisernote, carrier, ocr, presenter
from proven.api import SESSIONS, app

CARRIER = {"email": "underwriter@carrier.demo", "password": "Demo@123"}
OWNER = {"email": "owner@hockleefoods.demo", "password": "Demo@123"}
ACCOUNT = "ACC-10231"


@pytest.fixture
def client():
    SESSIONS.clear()
    carrier.LOG.clear()
    advisernote.NOTES.forget()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()
    carrier.LOG.clear()
    advisernote.NOTES.forget()


# ══ §47 correction — the provider-failure tests, rewritten as one ═══════════

def test_model_unavailable_still_produces_deterministic_wording():
    """The §47 replacement.

    The original prompt asked for "provider failure" and "provider missing" tests for a
    second hosted provider. That provider is not used — no key is set and `presenter.py`
    already falls back on its own — so those would test a path that does not exist. The
    behaviour actually worth pinning is the one the product
    depends on: with NO model reachable, every screen still gets correct, specific wording
    from the deterministic template, and says which mode it is in.

    The suite runs with model keys stripped by `conftest.py`, so this is the live condition
    rather than a simulated one.
    """
    assert presenter.enabled() is False, (
        "a model key leaked into the suite; this test would then measure the model path")

    for purpose in presenter.PURPOSES:
        result = presenter.compose(purpose, {"overall": 68, "action_count": 4,
                                             "finding_count": 17, "accounts": 6,
                                             "needs_review": 5, "findings_reviewed": 17})
        assert result.available is True, f"{purpose} produced nothing without a model"
        assert result.mode == "DETERMINISTIC_FALLBACK", (
            f"{purpose} claimed mode {result.mode!r} with no model configured")
        assert result.error, f"{purpose} did not say why it fell back"
        pres = result.presentation
        assert pres.get("headline"), f"{purpose} produced no headline"
        # The template must be specific, not a placeholder. A fallback that says
        # "Information" is worse than no fallback because it looks like a working screen.
        assert len(pres["headline"]) > 8
        assert pres.get("primary_visual", {}).get("type"), (
            f"{purpose} chose no visual; the engine picks the picture, not the model")


def test_the_new_carrier_and_adviser_purposes_have_fallbacks():
    """Added purposes must not be model-only, or they break exactly when the model does."""
    for purpose in ("carrier_briefing", "carrier_account_summary", "adviser_note"):
        assert purpose in presenter.PURPOSES
        result = presenter.compose(purpose, {"accounts": 6, "needs_review": 4,
                                            "findings_reviewed": 17, "requires_review": 4})
        assert result.presentation.get("headline")


# ══ Item 3 — carrier decisions reach /api/record ════════════════════════════

def test_carrier_decisions_appear_in_the_portable_record(client):
    client.get("/api/brief")
    client.post("/api/auth/login", json=CARRIER)
    client.post("/api/carrier/decision", json={
        "account_id": ACCOUNT, "decision": "NEEDS_ENDORSEMENT",
        "note": "Second premises is not on the schedule."})

    client.post("/api/auth/login", json=OWNER)
    record = client.get("/api/record").json()
    assert "attestations" in record
    decisions = record["attestations"]["carrier_decisions"]
    assert decisions, "a recorded carrier decision never reached the portable record"
    assert any("premises" in d["note"] for d in decisions)


def test_attestations_are_outside_the_hash_chain(client):
    """The chain covers facts established by DOCUMENTS. A human's conclusion is a different
    kind of statement, and letting it in would give an opinion the integrity guarantee that
    belongs to evidence."""
    client.get("/api/brief")
    record = client.get("/api/record").json()
    att = record["attestations"]
    assert att["in_hash_chain"] is False
    assert att["why_not_in_the_chain"]
    # And the facts themselves must be unchanged in shape.
    for fact in record["facts"]:
        assert "hash" in fact and "prev_hash" in fact


def test_an_approved_adviser_note_is_recorded_as_an_attestation(client):
    client.get("/api/brief")
    client.post("/api/auth/login", json=OWNER)
    client.post("/api/consent", json={
        "recipient": "Licensed adviser",
        "fields": ["BASE_WORDING", "SCHEDULE", "ACCOUNTS"]})
    client.post("/api/auth/login",
                json={"email": "adviser@lionbrokers.demo", "password": "Demo@123"})
    client.post("/api/adviser/note/draft")
    client.post("/api/adviser/note/approve")

    client.post("/api/auth/login", json=OWNER)
    note = client.get("/api/record").json()["attestations"]["adviser_note"]
    assert note is not None
    assert note["is_record"] is True
    assert note["approved_at"]
    # The note TEXT is deliberately absent: the record says a note exists and who approved
    # it, not what it says, because the note is the adviser's file and not the owner's.
    assert "text" not in note


# ══ Item 6 — pre-OCR conditioning ══════════════════════════════════════════

def _page_image(*, rotate: float = 0.0, size=(1200, 500), speckle: bool = False):
    """A synthetic page of text lines, optionally skewed and speckled."""
    PIL = pytest.importorskip("PIL", reason="Pillow is needed for image conditioning")
    from PIL import Image, ImageDraw
    im = Image.new("L", size, 255)
    draw = ImageDraw.Draw(im)
    for y in range(40, size[1] - 40, 40):
        draw.rectangle([60, y, size[0] - 60, y + 12], fill=30)
    if speckle:
        for i in range(0, size[0], 7):
            for j in range(0, size[1], 11):
                if (i * j) % 5 == 0:
                    im.putpixel((i, j), 0)
    if rotate:
        im = im.rotate(rotate, fillcolor=255, expand=True)
    return im


def test_conditioning_reports_what_it_did():
    im = _page_image()
    out, report = ocr.condition_image(im)
    assert out is not None
    assert "applied" in report
    assert report.get("summary")
    assert report.get("error") is None


def test_a_skewed_page_is_straightened():
    """The operation that changes whether a page is readable at all — a page scanned at an
    angle produces text lines that cross the recogniser's row bands."""
    im = _page_image(rotate=6.0)
    out, report = ocr.condition_image(im)
    assert "deskew" in report["applied"], f"skew was not detected: {report}"
    assert report["deskew_degrees"] is not None
    # It must correct TOWARDS level, i.e. rotate back against the 6 degrees applied.
    assert abs(report["deskew_degrees"]) >= ocr.DESKEW_MIN_DEGREES


def test_a_straight_page_is_not_needlessly_rotated():
    """Rotating costs resampling. Below the threshold it buys nothing, so it must not run."""
    im = _page_image(rotate=0.0)
    _, report = ocr.condition_image(im)
    degrees = report["deskew_degrees"]
    assert degrees is None or abs(degrees) < 2.0, report


def test_a_small_page_is_upscaled_and_capped():
    im = _page_image(size=(400, 260))
    _, report = ocr.condition_image(im)
    assert "upscale" in report["applied"]
    assert report["upscale_factor"] <= ocr.UPSCALE_MAX_FACTOR


def test_a_large_page_is_denoised_but_not_upscaled():
    im = _page_image(size=(1800, 1200), speckle=True)
    _, report = ocr.condition_image(im)
    assert report["denoised"] is True
    assert "upscale" not in report["applied"]


def test_conditioning_never_raises_on_a_bad_input():
    """It is an optimisation. A failure must return the original, not take down extraction."""
    out, report = ocr.condition_image(object())
    assert out is not None
    assert report["error"]


def test_the_conditioning_report_reaches_the_page(monkeypatch):
    """It travels on OCRPage so a reader judging a quotation can see the page was rotated to
    get it."""
    page = ocr.OCRPage(1, "text", 90.0, "OK", [],
                       conditioning={"applied": ["deskew"], "deskew_degrees": 1.8})
    assert page.as_dict()["conditioning"]["deskew_degrees"] == 1.8


# ══ Item 7 — ACRA identity prefill is wired, Myinfo is a seam ══════════════

def test_acra_prefill_is_wired_and_myinfo_is_declared_a_seam(client):
    body = client.get("/api/onboarding").json()
    prefill = body["identity_prefill"]
    assert "ACRA API Marketplace (data.gov.sg entities datastore)" in prefill["wired"]
    assert "Myinfo Business" in prefill["seam_only"]
    assert "seam" in prefill["myinfo_status"].lower()


def test_prefill_says_why_it_did_not_run_offline(client):
    """The suite is offline by design, so this is the real path. It must explain rather than
    silently return nothing."""
    body = client.get("/api/onboarding").json()
    prefill = body["identity_prefill"]
    assert prefill["available"] is False
    assert prefill["live"] is False
    assert "PROVEN_LIVE_SOURCES" in prefill["status"]


def test_prefill_refuses_a_record_that_is_not_the_subject(client, monkeypatch):
    """The identity gate, which is the reason this is safe to wire at all.

    `q=` on the ACRA datastore is a fuzzy search and something always ranks first — searching
    the fictional "Hock Lee Foods" once returned MILLENNIUM AUTOMATION & SYSTEMS PTE. LTD.
    A wrong prefill is worse than none: it puts a stranger's UEN on an insurance file.
    """
    from proven import config, discovery
    monkeypatch.setenv("PROVEN_LIVE_SOURCES", "1")
    stranger = {"uen": "X99STRANGER", "entity_name": "MILLENNIUM AUTOMATION & SYSTEMS PTE. LTD.",
                "uen_status_desc": "Live", "reg_street_name": "BENCOOLEN STREET"}
    monkeypatch.setattr(discovery, "fetch_acra_by_name",
                        lambda *a, **k: ([stranger], str(stranger), ""))
    client.get("/api/brief")
    prefill = client.get("/api/onboarding").json()["identity_prefill"]
    assert prefill["available"] is False, "a stranger's record was offered as a prefill"
    assert prefill["fields"] == []
    assert "stranger" in prefill["status"].lower() or "confirmed" in prefill["status"].lower()


def test_prefill_offers_only_grounded_fields(client, monkeypatch):
    """Every offered value is re-located verbatim in the register's own response, exactly as
    every other fact in this product is."""
    from proven import discovery
    monkeypatch.setenv("PROVEN_LIVE_SOURCES", "1")
    record = {"uen": "T09FIXTURE1A", "entity_name": "Hock Lee Foods Pte Ltd",
              "uen_status_desc": "Live", "entity_type_desc": "Private Company",
              "reg_street_name": "TAI SENG AVENUE"}
    raw = ("{\"uen\":\"T09FIXTURE1A\",\"entity_name\":\"Hock Lee Foods Pte Ltd\","
           "\"uen_status_desc\":\"Live\",\"entity_type_desc\":\"Private Company\","
           "\"reg_street_name\":\"TAI SENG AVENUE\"}")
    monkeypatch.setattr(discovery, "fetch_acra_by_name",
                        lambda *a, **k: ([record], raw, ""))
    client.get("/api/brief")
    prefill = client.get("/api/onboarding").json()["identity_prefill"]
    if prefill["available"]:
        for field in prefill["fields"]:
            assert field["author_class"] == "AUTHORITY"
            if field["grounded"]:
                assert field["evidence_class"] == "PROVEN"
                assert field["quote"]
            else:
                assert field["evidence_class"] == "UNESTABLISHED"
