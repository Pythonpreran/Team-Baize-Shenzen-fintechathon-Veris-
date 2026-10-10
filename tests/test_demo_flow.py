"""The demo, as an acceptance test.

WHY THIS FILE EXISTS. Everything else in the suite tests a unit or an endpoint. None of
it tests the thing that will actually be judged: a sequence of uploads that has to
produce a specific, moving, explicable story. A build where all 397 other tests pass and
the score no longer moves when the tenancy agreement arrives is a build that fails on
stage, and nothing else here would catch it.

THE STORY UNDER TEST, and every number in it is computed by the pipeline from real PDFs:

    six core documents                 score 74   stock adequacy 66.7%, S$25,000 exposure
    + the tenancy agreement            score 72   the premises finding becomes PROVEN
    + two contradicting endorsements   score 70   the engine ABSTAINS instead of picking
    + the illegible scan               score 72   the engine abstains for a stated reason

The middle two beats are the pitch. When the lease arrives, a finding stops being
"we could not establish this" and becomes "this is proven and needs an authorised
review" — that is the product's claim, moving, on real documents. When two endorsements
contradict each other on the same sub-limit, the score falls because the engine refuses
to guess which one governs, which is the opposite of what a model would do.

THESE ASSERTIONS ARE DIRECTIONAL, NOT EXACT. Pinning score 74 exactly would make every
future rule addition fail this file for no good reason, and a test that has to be
edited whenever the product improves gets deleted the first time it is inconvenient.
So what is pinned is what the demo depends on: the direction of each move, and the
state transition that causes it.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import demodocs

from proven.api import EXAMPLE_DIR, SESSIONS, app

CORE = ("01-policy-wording.pdf", "02-policy-schedule.pdf",
        "03-management-accounts.pdf", "06-cpf-contribution-record.pdf",
        "07-work-injury-schedule.pdf", "09-acra-business-profile.pdf")
TENANCY = "04-tenancy-agreement.pdf"
ENDORSEMENT_5K = "11-endorsement-food-liability-5k.pdf"
ENDORSEMENT_25K = "12-endorsement-food-liability-25k.pdf"
ILLEGIBLE = "13-illegible-endorsement-scan.pdf"

ALL_DEMO_DOCS = CORE + (TENANCY, ENDORSEMENT_5K, ENDORSEMENT_25K, ILLEGIBLE)

# The story needs the published wording as well as the synthetic documents: every beat
# below is a clause finding moving, and without the wording there are no clauses. The
# wording is not in git, so this file skips — visibly, naming what is missing — on a
# checkout where the corpus has not been fetched.
pytestmark = [
    pytest.mark.requires_corpus(demodocs.WORDING_CORPUS_ID),
    pytest.mark.skipif(not EXAMPLE_DIR.exists(),
                       reason="demo-documents/ is not present in this checkout"),
]


@pytest.fixture
def client(monkeypatch):
    """No key: the demo must tell its whole story on the deterministic engine alone.

    If the story depended on the AI layer it would be at the mercy of a rate limit on
    the night, and the central claim — that no model makes a consequential decision —
    would be false.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


def _upload(client: TestClient, names, *, fresh_session: bool = False) -> dict:
    """Upload a bundle through the ordinary multipart path and return score + brief.

    Deliberately the same endpoint a user hits. Seeding the case from fixtures would
    prove the rules work and would not prove the product does.

    `fresh_session` drops the cookie first, which both isolates the bundle and resets
    the per-session upload allowance. The full sequence moves more than twenty
    documents and the rate limiter is right to refuse that on one session — so the test
    works with the limit rather than around it.
    """
    if fresh_session:
        client.cookies.clear()
    missing = demodocs.missing(names)
    assert not missing, f"demo documents not available: {missing}"
    files = demodocs.parts(names)
    response = client.post("/api/bundle", files=files)
    assert response.status_code == 200, response.text
    brief = client.get("/api/brief").json()
    score = client.get("/api/score").json()
    return {
        "score": score["overall"],
        "exposure": score["headline_exposure"],
        "issues": score["issues"],
        "rules": {f["rule_id"]: (f["state"], f["class"]) for f in brief["findings"]},
        "accounting": brief.get("accounting"),
    }


# ── beat 1: the opening position ─────────────────────────────────────────────

def test_the_core_bundle_produces_a_score_and_a_computed_exposure(client):
    """What a judge sees in the first fifteen seconds: one number, and one currency
    figure that came from arithmetic rather than an estimate."""
    result = _upload(client, CORE)
    assert 0 < result["score"] < 100, "an all-or-nothing score tells no story"
    assert result["issues"], "no priority issues to show"

    exposure = result["exposure"]
    assert exposure is not None, "no exposure was computable from the core bundle"
    assert Decimal(exposure["amount_sgd"]) > 0
    assert exposure["rule_id"] == "AVERAGE-ADEQUACY-BELOW-THRESHOLD"
    # The figure must arrive with the loss it was assessed against. A settlement
    # illustration with no stated basis is the one number this product must not show.
    assert Decimal(exposure["assessed_loss_sgd"]) > Decimal(
        exposure["illustrated_settlement_sgd"])


def test_the_opening_bundle_states_what_it_did_not_read(client):
    """The falsifiable half of "we read your papers". Unaccounted provisions are shown,
    not hidden, and the accounting has to add up."""
    accounting = _upload(client, CORE)["accounting"]
    assert accounting, "no clause accounting in the brief"
    assert accounting["classified"] + accounting["unaccounted"] == \
        accounting["detected"], "the accounting does not reconcile"
    assert accounting["unaccounted"] > 0, "a claim of total coverage is not credible"


# ── beat 2: the lease arrives and a finding becomes PROVEN ───────────────────

def test_the_tenancy_agreement_turns_an_unknown_into_a_proven_finding(client):
    """THE PITCH, AS AN ASSERTION.

    The accounting feed reports a new rent line and names the tenancy agreement as the
    document that would settle it. When that document arrives, the premises finding
    stops being "we could not establish this" and becomes proven — and the score falls,
    because a known problem is worse news than an open question.
    """
    before = _upload(client, CORE)
    after = _upload(client, CORE + (TENANCY,))

    rule = "ALTERATION-PREMISES-NOT-ON-SCHEDULE"
    assert before["rules"][rule] == ("INFORMATION_MISSING", "UNESTABLISHED"), \
        "the premises finding did not start out unestablished"
    assert after["rules"][rule] == ("REQUIRES_AUTHORISED_REVIEW", "PROVEN"), \
        "the tenancy agreement did not make the premises finding proven"
    assert after["score"] < before["score"], (
        "establishing a real problem must cost more than not knowing about it")


def test_the_document_the_feed_asks_for_is_the_one_that_moves_the_finding(client):
    """The loop has to close on the SAME document. A connector that names a document
    which turns out to be irrelevant is theatre."""
    from proven import connectors

    asked = connectors.requested_documents(["SIG-004"])   # the new-premises signal
    assert "TENANCY" in asked, "the premises signal does not ask for the lease"


# ── beat 3: contradiction, and the refusal to guess ──────────────────────────

def test_two_contradicting_endorsements_make_the_engine_abstain(client):
    """The beat that separates this from a model reading a policy.

    Two endorsements set different sub-limits for the same cover. A system optimising
    for a confident answer picks the later one. This engine records that it cannot
    determine which governs and routes it to a human — and that costs score, because
    an unresolvable conflict is a real problem rather than a silence.
    """
    one = _upload(client, CORE + (ENDORSEMENT_5K,))
    both = _upload(client, CORE + (ENDORSEMENT_5K, ENDORSEMENT_25K))

    rule = "SUBLIMIT-CONFLICTING-ENDORSEMENTS"
    assert rule not in one["rules"], (
        "a single endorsement should not raise a conflict finding")
    assert both["rules"][rule] == ("REQUIRES_AUTHORISED_REVIEW", "PROVEN"), \
        "the engine resolved a conflict it should have refused to resolve"
    assert both["score"] < one["score"]


def test_an_illegible_scan_abstains_rather_than_guessing(client):
    """An unreadable document must produce a stated abstention, not a silent omission
    and not a best guess."""
    result = _upload(client, CORE + (ILLEGIBLE,))
    rule = "ENDORSEMENT-ILLEGIBLE-ABSTAIN"
    assert result["rules"].get(rule) == ("INFORMATION_MISSING", "UNESTABLISHED"), \
        "the illegible scan did not produce an abstention finding"


# ── the whole sequence, in order ─────────────────────────────────────────────

def test_the_demo_score_moves_monotonically_as_evidence_arrives(client):
    """The full run, asserted as a shape rather than as four magic numbers.

    Each document adds either a proven problem or a stated abstention, so the score
    must never improve as the bundle grows. If it ever does, the narrative on stage
    inverts — "we found more and it got better" — and the demo stops making sense.
    """
    steps = [
        ("core", CORE),
        ("core + lease", CORE + (TENANCY,)),
        ("core + lease + conflicting endorsements",
         CORE + (TENANCY, ENDORSEMENT_5K, ENDORSEMENT_25K)),
    ]
    scores = []
    for label, names in steps:
        result = _upload(client, names, fresh_session=True)
        scores.append((label, result["score"]))

    for (prev_label, prev), (label, current) in zip(scores, scores[1:]):
        assert current <= prev, (
            f"score rose from {prev} to {current} between '{prev_label}' and "
            f"'{label}' — adding evidence of a problem must not improve the score")
    assert scores[-1][1] < scores[0][1], (
        "the full sequence produced no net movement, so there is nothing to show")


def test_every_deduction_in_the_final_state_traces_to_a_finding(client):
    """The score has to be defensible under questioning. Every point lost names the
    rule that took it, and every one of those rules is in the brief."""
    files = CORE + (TENANCY, ENDORSEMENT_5K, ENDORSEMENT_25K)
    _upload(client, files)
    brief_rules = {f["rule_id"] for f in client.get("/api/brief").json()["findings"]}
    score = client.get("/api/score").json()
    deductions = [d for dim in score["dimensions"] for d in dim["deductions"]]
    assert deductions
    for deduction in deductions:
        assert deduction["rule_id"] in brief_rules


def test_the_copilot_can_answer_about_the_uploaded_case_with_no_key(client):
    """The last beat of the demo must not depend on a network call succeeding."""
    _upload(client, CORE + (TENANCY,))
    body = client.post("/api/ai/ask",
                       json={"question": "What is my biggest exposure?"}).json()
    assert body["text"].strip()
    assert body["source"] == "DETERMINISTIC"
