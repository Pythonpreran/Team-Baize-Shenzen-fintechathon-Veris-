"""The exposure surfaces named in the problem statement.

Cyber, supply chain, growth stage, compounding over time, and the blind-spot register.

WHAT THESE TESTS ARE GUARDING, AND WHY IT IS NOT THE USUAL THING.

Every other surface in this product reports a SHORTFALL: two documented figures, one
smaller than the other. These five report an ABSENCE, and an absence has a failure mode
a shortfall does not — it can be concluded from silence. "Your schedule has no cyber
section" and "we could not read your schedule" produce the same empty set, and only one
of them is a finding. Most of what follows exists to pin that distinction.

The second thing being guarded is tone. These are the only screens that name a
protection the owner does not hold, which makes them the only screens that can shade
into selling. So there are explicit tests that a market convention is never phrased as
an obligation, that an unknown never acquires a severity, and that no module predicts a
breach or a penalty.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import blindspots, compounding, cyber, stages, supplychain
from proven.api import SESSIONS, app


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


# ── /api/cyber ──────────────────────────────────────────────────────────────

def test_cyber_finds_the_carriers_own_exclusion_and_quotes_it(client):
    """The strongest evidence in the system: the insurer, in its own document.

    The demo wording is a real Chubb Select+ policy and it carries LMA5401, the Lloyd's
    Market Association Property Cyber and Data Exclusion. Locating it and quoting it
    verbatim at a page number is what turns "you probably aren't covered" into a fact
    there is nothing to argue about.
    """
    body = client.get("/api/cyber").json()
    assert body["available"] is True
    assert body["cover_state"] == "EXCLUDED"
    assert body["gap_open"] is True

    exclusion = body["exclusion"]
    assert exclusion is not None, "the wording carries an exclusion; it must be quoted"
    assert exclusion["page_no"] >= 1
    assert exclusion["author_class"] == "INSURER"
    assert exclusion["evidence_class"] == "PROVEN"
    assert len(exclusion["quote"]) >= 20


def test_cyber_requires_both_halves_before_it_reports_a_gap(client):
    """Data held AND no cover. Either alone is not a gap and must not be shown as one."""
    body = client.get("/api/cyber").json()
    assert body["holdings"], "a gap was reported with no evidenced data holding"
    for holding in body["holdings"]:
        # Every holding traces to a document, and none of them is self-declared:
        # the whole point is that this is established without asking the owner.
        assert holding["document_kind"]
        assert holding["evidence_class"] in {"PROVEN", "DECLARED"}
        assert holding["author_class"] in {"AUTHORITY", "THIRD_PARTY", "INSURER"}


def test_cyber_never_predicts_a_breach_or_prices_a_penalty(client):
    """The statutory ceiling is statute. It is not an expected loss, and the payload has
    to say so in the same breath it states the figure."""
    body = client.get("/api/cyber").json()
    statutory = body["statutory"]
    assert statutory["penalty_ceiling_sgd"] == "1000000"
    disclaimer = statutory["not_a_prediction"].lower()
    assert "do not predict" in disclaimer
    assert "not an expected cost" in disclaimer
    assert statutory["authority"]

    blob = str(body).lower()
    for forbidden in ("likely to be breached", "will be attacked",
                      "probability of a breach", "expected penalty",
                      "you will be fined"):
        assert forbidden not in blob, f"cyber payload predicts: {forbidden!r}"


def test_cyber_cost_heads_separate_nothing_from_unknown(client):
    """"Nothing responds" is a finding. "We could not tell" is an abstention. Collapsing
    them would turn an unanswered question into a confident negative."""
    body = client.get("/api/cyber").json()
    assert body["cost_heads"]
    for head in body["cost_heads"]:
        assert head["responds"] in {"NOTHING", "UNKNOWN", "REVIEW", "CYBER",
                                    "BUSINESS_INTERRUPTION"}
        assert head["why"], "a cost head with no reason is an assertion"
    # This wording has no BI and no cyber section, so most heads must be NOTHING.
    assert body["uncovered_count"] >= 3


def test_cyber_abstains_when_no_data_holding_is_established():
    """A business we cannot show holds personal data must not be told it has a cyber gap."""
    result = cyber.assess({"facts": None, "store": None})
    assert result["available"] is False
    assert "not going to tell you" in result["reason"].lower()


def test_cyber_abstention_survives_an_exclusion_being_found():
    """REGRESSION. The abstention path must be reachable when cover IS excluded.

    The test above passes `store: None`, so `_cover_state()` never resolves past
    INDETERMINATE and the gap-intersection guard is never evaluated. That left the real
    combination untested: a wording that DOES carry a cyber exclusion, uploaded without
    any document establishing a data holding.

    That combination is not exotic. Chubb Select+ carries LMA5401 at cl 4.3, so it
    arose the first time anyone uploaded the demo wording on its own — cover_state
    EXCLUDED, holdings empty, guard fires, AssertionError, HTTP 500 on /api/cyber and
    on /api/gap-summary which composes over it. 1013 tests were green at the time.

    So this asserts the contract directly on the dataclass: the guard must still bite
    when a gap is being rendered, and must not bite when the caller has already decided
    to abstain.
    """
    a = cyber.CyberAssessment()
    a.cover_state = cyber.COVER_EXCLUDED
    a.holdings = []

    # The abstention path declares it is rendering no gap, so the rule does not apply.
    a.assert_invariant(gap_reported=False)

    # But a caller claiming to render a gap with no holding behind it is still a bug.
    with pytest.raises(AssertionError, match="no data holding"):
        a.assert_invariant()


def test_wording_only_upload_does_not_break_any_exposure_screen(client):
    """REGRESSION, at the level the user actually hit it.

    Uploading a base wording and nothing else is the most likely first action a judge
    or a new user takes. Every exposure surface has to degrade to an honest "we cannot
    say" rather than 500, because a crashed screen teaches the viewer that the
    abstention discipline is decoration.
    """
    from proven import pipeline
    path = pipeline.CHUBB_PDF
    if not path.exists():                       # pragma: no cover - corpus not fetched
        pytest.skip(f"published wording not in the corpus at {path}")

    with path.open("rb") as fh:
        r = client.post("/api/bundle",
                        files={"files": (path.name, fh, "application/pdf")},
                        data={"kinds": '["BASE_WORDING"]'})
    assert r.status_code == 200, r.text

    cy = client.get("/api/cyber")
    assert cy.status_code == 200, cy.text
    assert cy.json()["available"] is False

    for route in ("/api/gap-summary", "/api/blindspots", "/api/score",
                  "/api/supply-chain", "/api/stages", "/api/compounding"):
        assert client.get(route).status_code < 500, f"{route} -> {client.get(route).text[:200]}"


# ── /api/supply-chain ───────────────────────────────────────────────────────

def test_supply_chain_states_the_structural_impossibility(client):
    """No BI section means no extension can attach to one. That is structure, not risk."""
    body = client.get("/api/supply-chain").json()
    assert body["available"] is True
    assert body["cbi_state"] == "IMPOSSIBLE_NO_BI"
    assert body["gap_open"] is True
    assert body["counts"]["dependencies"] >= 1
    assert body["counts"]["uncovered_paths"] >= 1


def test_supply_chain_finds_the_cold_storage_dependency(client):
    """The one a questionnaire would never surface.

    The tenancy's permitted use says cold storage. That is THIRD_PARTY-authored, so the
    dependency is established even though the owner never mentioned it — which is the
    blind-spot case in its purest form: stock degrades the moment the power stops and
    the building is never damaged, so no property section has anything to attach to.
    """
    body = client.get("/api/supply-chain").json()
    utilities = [d for d in body["dependencies"] if d["kind"] == "UTILITY"]
    assert utilities, "cold storage in the permitted use must raise a utility dependency"
    assert utilities[0]["evidence_class"] == "PROVEN"
    assert "UTILITY_FAILURE" in [m["mode"] for m in utilities[0]["failure_modes"]]


def test_supply_chain_always_admits_the_map_is_partial(client):
    """A partial map shown as complete converts an unknown unknown into a false known,
    which is worse than showing nothing. So visibility is a field, not a footnote."""
    body = client.get("/api/supply-chain").json()
    visibility = body["visibility"]
    assert visibility["is_exhaustive"] is False
    assert "NOT a complete list" in visibility["statement"]
    assert visibility["what_would_complete_it"]


def test_supply_chain_labels_its_market_convention_premise(client):
    """How CBI is conventionally written is market practice, not this policy's terms."""
    body = client.get("/api/supply-chain").json()
    assert body["how_cbi_works"]["basis_class"] == "MARKET_CONVENTION"
    assert body["how_cbi_works"]["basis_caveat"]


# ── /api/stages ─────────────────────────────────────────────────────────────

def test_stage_is_derived_from_documents_not_from_what_the_owner_said(client):
    body = client.get("/api/stages").json()
    assert body["available"] is True
    assert body["stage"]["key"] == "SCALING"
    established = [e for e in body["evidence"] if e["established"]]
    assert established, "a stage was assigned with no established evidence"
    for item in established:
        assert item["source"], "evidence with no source document"


def test_stages_never_phrase_a_market_convention_as_an_obligation(client):
    """The single most dangerous output in the product. Enforced, not reviewed.

    A habit described with the force of law is a lie told in the owner's favour, and it
    is still a lie. `stages.assert_invariant()` refuses to serialise one; this test
    checks the wire format too, because that is what a reader actually sees.
    """
    body = client.get("/api/stages").json()
    forbidden = ("required", "must hold", "mandatory", "compulsory",
                 "non-compliant", "in breach", "illegal", "you have to")
    for expectation in body["expectations"]:
        assert expectation["basis"] in {"STATUTORY", "MARKET_CONVENTION"}
        if expectation["basis"] == "MARKET_CONVENTION":
            because = expectation["because"].lower()
            for word in forbidden:
                assert word not in because, (
                    f"{expectation['protection']} is a convention but says {word!r}")
            assert expectation["authority"] is None
        else:
            assert expectation["authority"], "a statutory item must name its instrument"


def test_stages_report_indeterminate_rather_than_guessing_absence(client):
    """Work injury is a separate statutory policy. A property schedule cannot evidence
    it either way, so it must come back indeterminate rather than absent."""
    body = client.get("/api/stages").json()
    assert "WORK_INJURY" in body["indeterminate"]
    assert "WORK_INJURY" not in body["absent"]


def test_stages_abstain_when_nothing_places_the_business():
    result = stages.assess({"facts": None})
    assert result["available"] is False
    assert result["unestablished"]


def test_stage_ladder_is_reachable_without_a_case(client):
    body = client.get("/api/stages/ladder").json()
    assert len(body["ladder"]) >= 4
    assert body["protections"]
    assert {b["basis"] for b in body["bases"]} == {"STATUTORY", "MARKET_CONVENTION"}


# ── /api/compounding ────────────────────────────────────────────────────────

def test_compounding_measures_days_and_never_invents_a_running_cost(client):
    """Exposure crystallises on loss; it does not accrue daily. A dollar figure here
    would be fabricated, so the measure is the distance between two document dates."""
    body = client.get("/api/compounding").json()
    assert body["available"] is True
    assert body["counts"]["business_changes"] >= 1
    assert body["longest_open_days"] >= 1
    assert "days" in body["why_days_not_dollars"].lower()
    for loop in body["open_loops"]:
        assert loop["days_open"] is not None
        assert loop["what_would_close_it"], "an open loop with no remedy is an alarm"


def test_compounding_credits_the_change_the_business_did_act_on(client):
    """The demo business endorsed its plant cover 7 days after buying a freezer. A
    timeline that only showed failures would be a sales pitch; showing the one they
    closed is what makes the three they did not credible."""
    body = client.get("/api/compounding").json()
    closed = [c for c in body["timeline"] if c["status"] == "CLOSED"]
    assert closed, "the equipment purchase was endorsed and must show as closed"
    assert closed[0]["closed_by"]
    assert closed[0]["days_open"] is not None


def test_compounding_never_adjudicates(client):
    body = client.get("/api/compounding").json()
    assert body["innocent_explanations"], "an open loop must offer benign readings"
    assert body["not_an_adjudication"]
    blob = " ".join(f"{c['label']} {c['detail']}" for c in body["timeline"]).lower()
    for word in ("breach", "lapsed", "void", "invalid"):
        assert word not in blob, f"timeline adjudicates with {word!r}"


def test_compounding_clock_is_pinned_to_documents_not_today(client):
    """Two calls must agree, and a demo must read the same tomorrow as today."""
    first = client.get("/api/compounding").json()
    second = client.get("/api/compounding").json()
    assert first["as_at"] == second["as_at"]
    assert first["longest_open_days"] == second["longest_open_days"]
    assert "not today" in first["as_at_basis"].lower()


# ── /api/blindspots ─────────────────────────────────────────────────────────

def test_blindspots_reports_the_boundary_of_the_assessment(client):
    body = client.get("/api/blindspots").json()
    assert body["coverage"]["areas_total"] == len(blindspots.AREAS)
    assert body["coverage"]["statement"]
    for area in body["areas"]:
        assert area["state"] in {"ASSESSED", "PARTIAL", "NOT_ASSESSED"}
        assert area["unlocking_document"], "every area names the document that opens it"


def test_blindspots_explains_what_the_score_leaves_out(client):
    """The load-bearing sentence. `overall` is the mean of ASSESSED dimensions, so an
    area with nothing to read is absent from the number rather than lowering it — right
    arithmetic, wrong impression, corrected here."""
    body = client.get("/api/blindspots").json()
    caveat = body["score_caveat"]
    assert caveat["applies"] is True
    assert caveat["statement"]


def test_blindspots_never_gives_an_unknown_a_severity(client):
    """Sorting unknowns by how frightening they could be makes a better demo and a worse
    instrument. Blocked in code; checked here on the wire."""
    body = client.get("/api/blindspots").json()
    for gap in body["silent_gaps"]:
        assert "severity" not in gap
        assert gap["resolved_by"], "a silent gap with no remedy is fear, not information"
        assert gap["why_quiet_is_not_safe"]


def test_dependencies_area_is_never_reported_as_fully_assessed(client):
    """The dependency map is built from documents that happen to name a counterparty, so
    it cannot be exhaustive. Claiming ASSESSED would be the false-known failure."""
    body = client.get("/api/blindspots").json()
    deps = next(a for a in body["areas"] if a["key"] == "dependencies")
    assert deps["state"] in {"PARTIAL", "NOT_ASSESSED"}
    assert deps["could_not_check"]


def test_blindspot_register_is_reachable_without_a_case(client):
    body = client.get("/api/blindspots/register").json()
    assert len(body["areas"]) == len(blindspots.AREAS)
    assert {s["state"] for s in body["states"]} == {"ASSESSED", "PARTIAL",
                                                   "NOT_ASSESSED"}


def test_digital_area_does_not_fall_back_to_the_empty_score_dimension():
    """`digital` shipped for two versions with an empty rule tuple, so it always
    reported assessed:false and was excluded from the mean — the score went UP for the
    absence of the check. If cyber abstains, this area must say NOT_ASSESSED rather than
    borrowing that silence and calling it an answer."""
    report = blindspots.assess(
        {"findings": []},
        score={"overall": 70, "dimensions": [
            {"key": "digital", "assessed": False, "score": 0,
             "reason": "Not assessed", "deductions": []}]},
        cyber={"available": False, "reason": "No data holding established.",
               "unestablished": ["No ACRA profile was read."]},
        dependency=None)
    digital = next(a for a in report["areas"] if a["key"] == "digital")
    assert digital["state"] == "NOT_ASSESSED"
    assert digital["could_not_check"]


# ── all five surfaces go quiet together on deletion ─────────────────────────

@pytest.mark.parametrize("path", [
    "/api/cyber", "/api/supply-chain", "/api/stages", "/api/compounding",
    "/api/blindspots",
])
def test_new_surfaces_go_quiet_when_the_case_is_deleted(client, path):
    """Deletion reaching some views and not others has been a real defect in this
    codebase. Every new case-scoped view is pinned the same way as the old ones."""
    assert client.get(path).status_code == 200
    client.delete("/api/case")
    body = client.get(path).json()
    assert body["available"] is False
    assert body.get("deleted") is True
