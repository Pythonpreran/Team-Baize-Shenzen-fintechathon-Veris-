"""The adviser's file note: the model types, deterministic code checks, a human signs.

WHAT IS ACTUALLY BEING GUARDED.

This is the only place in the product where a model's prose becomes a durable artefact
somebody relies on later. A screen is read now, next to its evidence; a file note is read
months later by somebody who cannot see the evidence at all. So an invented clause
reference here is worse than the same invention on a screen, and the tests are
correspondingly about REJECTION rather than about output quality.

Three properties matter, in this order:
  1. a draft that references anything outside the evidence is rejected, not repaired
  2. nothing is ever sent from here
  3. approval is the only state that means anything, and it cannot be overwritten
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import advisernote
from proven.api import SESSIONS, app

ADVISER = {"email": "adviser@lionbrokers.demo", "password": "Demo@123"}
OWNER = {"email": "owner@hockleefoods.demo", "password": "Demo@123"}
CARRIER = {"email": "underwriter@carrier.demo", "password": "Demo@123"}

ALL_KINDS = ["BASE_WORDING", "SCHEDULE", "ACRA_PROFILE", "ACCOUNTS", "TENANCY",
             "CPF_RECORD", "WIC_SCHEDULE", "WORK_PASS_LIST", "SUPPLIER_INVOICE",
             "ENDORSEMENT"]


@pytest.fixture
def client():
    SESSIONS.clear()
    advisernote.NOTES.forget()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()
    advisernote.NOTES.forget()


def _disclosed(client, fields=None):
    client.get("/api/brief")
    client.post("/api/auth/login", json=OWNER)
    r = client.post("/api/consent", json={"recipient": "Licensed adviser",
                                          "fields": fields or ALL_KINDS})
    assert r.status_code == 200
    client.post("/api/auth/login", json=ADVISER)


# ── the consent boundary ────────────────────────────────────────────────────

def test_only_an_adviser_reaches_the_note(client):
    for who, expect in ((None, 401), (OWNER, 403), (CARRIER, 403)):
        if who:
            client.post("/api/auth/login", json=who)
        assert client.post("/api/adviser/note/draft").status_code == expect


def test_no_disclosure_means_no_note(client):
    """A note about a case the adviser may not read would be a note about nothing."""
    client.get("/api/brief")
    client.post("/api/auth/login", json=ADVISER)
    r = client.post("/api/adviser/note/draft")
    assert r.status_code == 403
    assert "not disclosed" in r.json()["detail"].lower()


def test_a_note_cannot_mention_a_withheld_finding(client):
    """Disclose only the wording. Findings resting on the accounts are withheld from the
    adviser, so they must be absent from the note's own evidence set too."""
    _disclosed(client, fields=["BASE_WORDING"])
    body = client.post("/api/adviser/note/draft").json()
    assert "AVERAGE-ADEQUACY-BELOW-THRESHOLD" not in body["text"], (
        "the note cited a finding the consent gate withheld")


# ── drafting and the deterministic fallback ─────────────────────────────────

def test_a_draft_is_produced_even_with_no_model_configured(client):
    """The suite runs with model keys stripped. A compliance artefact must still exist —
    "the model was unavailable" cannot leave an adviser with an empty box."""
    _disclosed(client)
    body = client.post("/api/adviser/note/draft").json()
    assert body["state"] == advisernote.AI_DRAFT
    assert body["drafted_by"] in ("TEMPLATE", "MODEL")
    assert len(body["text"]) > 200
    assert "FILE NOTE" in body["text"]
    assert "not a coverage opinion" in body["text"]


def test_the_draft_records_the_scope_and_its_limitations(client):
    """The part that makes it defensible: what was reviewed, and what was not available."""
    _disclosed(client)
    text = client.post("/api/adviser/note/draft").json()["text"]
    for heading in ("SCOPE OF REVIEW", "MATTERS REQUIRING AN AUTHORISED OPINION",
                    "BASIS AND LIMITATIONS", "NEXT STEPS"):
        assert heading in text, f"the note omitted {heading}"


def test_the_context_numbers_are_computed_not_supplied(client):
    _disclosed(client)
    body = client.post("/api/adviser/note/draft").json()
    ctx = body["context"]
    findings = client.get("/api/adviser/review").json()["findings"]
    assert ctx["findings_reviewed"] == len(findings)
    assert ctx["requires_review"] == sum(
        1 for f in findings if f["state"] == "REQUIRES_AUTHORISED_REVIEW")


# ── the grounding gate, tested directly ─────────────────────────────────────

def _allowed():
    findings = [{
        "rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD",
        "headline": "Stock adequacy is 66.7% against the 85% threshold.",
        "detail": {"cite": [{"clause": "4.2"}]},
        "spans": [{"page_no": 15, "quoted_text": "sum insured 100,000"}],
    }]
    return advisernote.allowed_facts(findings, {"findings_reviewed": 1})


def test_grounding_accepts_a_draft_that_stays_inside_the_evidence():
    ok, problems = advisernote.verify(
        "Reviewed AVERAGE-ADEQUACY-BELOW-THRESHOLD. cl 4.2 applies, see page 15. "
        "The sum insured is 100,000.", _allowed())
    assert ok, problems


def test_grounding_rejects_an_invented_clause():
    ok, problems = advisernote.verify("The position turns on cl 9.9.", _allowed())
    assert not ok
    assert any("9.9" in p for p in problems)


def test_grounding_rejects_an_invented_page():
    ok, problems = advisernote.verify("See page 88 of the wording.", _allowed())
    assert not ok
    assert any("88" in p for p in problems)


def test_grounding_rejects_an_invented_figure():
    ok, problems = advisernote.verify("The shortfall is 87,500.", _allowed())
    assert not ok
    assert any("87500" in p for p in problems)


def test_grounding_rejects_a_finding_that_was_not_reviewed():
    ok, problems = advisernote.verify(
        "Also considered WICA-HEADCOUNT-UNDERDECLARED.", _allowed())
    assert not ok
    assert any("WICA" in p for p in problems)


@pytest.mark.parametrize("phrase", [
    "you are covered", "not covered", "the claim will fail", "guaranteed", "breach",
])
def test_grounding_rejects_coverage_language_outside_a_quotation(phrase):
    ok, problems = advisernote.verify(f"In my view {phrase}.", _allowed())
    assert not ok, f"accepted a coverage opinion: {phrase!r}"


def test_coverage_language_is_allowed_inside_a_verbatim_quotation():
    """The engine's whole method is to quote the document. A wording that says "not
    covered" must be quotable, or the discipline would forbid the product's own technique."""
    ok, problems = advisernote.verify(
        'The clause reads "loss of electronic data is not covered by this Policy".',
        _allowed())
    assert ok, problems


def test_a_rejected_draft_falls_back_and_says_why(client):
    """Not repaired. Guessing what the model meant is the failure this avoids."""
    _disclosed(client)
    findings = client.get("/api/adviser/review").json()["findings"]
    note = advisernote.NOTES.draft(
        case_id="case-x", business="Test Pte Ltd", adviser="A", organisation="B",
        findings=findings, requests=[],
        model_text="The shortfall is 987654 and cl 99.9 applies at page 400.")
    assert note.drafted_by == "TEMPLATE"
    assert note.grounding["accepted"] is False
    assert note.grounding["problems"]
    assert "not repaired" in note.grounding["reason"].lower() or \
           "guessing" in note.grounding["reason"].lower()


# ── the three states ────────────────────────────────────────────────────────

def test_edit_moves_it_to_edited(client):
    _disclosed(client)
    client.post("/api/adviser/note/draft")
    body = client.post("/api/adviser/note/edit",
                       json={"text": "My own note. Reviewed 3 findings."}).json()
    assert body["state"] == advisernote.ADVISER_EDITED
    assert body["is_record"] is False


def test_only_an_approved_note_is_a_record(client):
    _disclosed(client)
    client.post("/api/adviser/note/draft")
    assert client.get("/api/adviser/note").json()["is_record"] is False
    approved = client.post("/api/adviser/note/approve").json()
    assert approved["state"] == advisernote.ADVISER_APPROVED
    assert approved["is_record"] is True
    assert approved["approved_at"]


def test_an_approved_note_cannot_be_edited(client):
    """Approval is where a human took responsibility. Editing after that would destroy the
    only state in this feature that means anything."""
    _disclosed(client)
    client.post("/api/adviser/note/draft")
    client.post("/api/adviser/note/approve")
    r = client.post("/api/adviser/note/edit", json={"text": "changed my mind"})
    assert r.status_code == 409


def test_redrafting_does_not_overwrite_an_approved_note(client):
    _disclosed(client)
    client.post("/api/adviser/note/draft")
    client.post("/api/adviser/note/approve")
    again = client.post("/api/adviser/note/draft").json()
    assert again["state"] == advisernote.ADVISER_APPROVED


def test_the_history_records_every_transition(client):
    _disclosed(client)
    client.post("/api/adviser/note/draft")
    client.post("/api/adviser/note/edit", json={"text": "edited"})
    body = client.post("/api/adviser/note/approve").json()
    states = [h["state"] for h in body["history"]]
    assert states == [advisernote.AI_DRAFT, advisernote.ADVISER_EDITED,
                      advisernote.ADVISER_APPROVED]


# ── it never sends ──────────────────────────────────────────────────────────

def test_there_is_no_route_that_sends_the_note(client):
    """A draft escaping to a client as if it were advice is the worst outcome this feature
    could produce, so the guarantee is structural: no such route exists."""
    paths = {getattr(r, "path", "") for r in app.routes}
    for path in paths:
        assert "note" not in path or not path.endswith(("/send", "/email")), (
            f"a route exists that could send a draft: {path}")
    assert advisernote.catalogue()["never_sent"]
