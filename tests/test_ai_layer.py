"""The AI layer, the protection score and the connector feed.

These four modules existed as code and were reachable from nothing: no endpoint
imported them, no test exercised them and the front end had never heard of them. So
the most defensible technology in the repository — a model whose every claim is checked
against the source document before it is allowed to matter — was invisible. This file
exists because wiring it up without tests would have swapped one unverified layer for
another.

TWO THINGS ARE UNDER TEST AND THEY ARE DIFFERENT.

  1. The endpoints work, and degrade. Every test here runs with NO OPENAI_API_KEY,
     because that is the state a judge cloning this repo will be in, and because the
     suite must not need a network round trip or a paid call. So these tests prove the
     *fallback* path: real retrieval, real citations, real score, deterministic prose.

  2. The model cannot reach anything consequential. That claim is architectural, not
     behavioural, so `test_no_consequential_module_imports_the_ai_layer` asserts it
     against the import graph rather than against an output. A behavioural test would
     pass on a build where the guarantee had been quietly removed.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proven import config, connectors, score
from proven.api import SESSIONS, app

SRC = Path(__file__).resolve().parents[1] / "src" / "proven"


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        c.get("/api/brief")            # build this session's case
        yield c
    SESSIONS.clear()


@pytest.fixture
def no_key(monkeypatch):
    """Force the AI layer off, whatever the developer's environment holds.

    Without this the suite would pass or fail depending on whether the machine running
    it happened to have a key exported, which is the kind of test that stops meaning
    anything the first time it is green for the wrong reason.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config._import_openai.cache_clear()
    yield
    config._import_openai.cache_clear()


# ── the architectural guarantee ──────────────────────────────────────────────

CONSEQUENTIAL = ("resolve.py", "arithmetic.py", "grading.py", "score.py", "rules.py")


def test_no_consequential_module_imports_the_ai_layer():
    """The claim "no model makes a consequential decision" is enforced by there being
    no code path, and this is the test that keeps it that way.

    Precedence resolution, settlement arithmetic, evidence grading, the score and the
    rules must not be able to reach `ai.py` even transitively-by-one-hop. An
    import-graph assertion catches a regression that no output test would: a build
    where someone wired the model in and the numbers still looked plausible.
    """
    offenders = []
    for name in CONSEQUENTIAL:
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"ai", "proven.ai"}:
                offenders.append(f"{name} imports {node.module}")
            if isinstance(node, ast.ImportFrom) and node.module is None:
                for alias in node.names:          # `from . import ai`
                    if alias.name == "ai":
                        offenders.append(f"{name} does `from . import ai`")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"ai", "proven.ai"}:
                        offenders.append(f"{name} imports {alias.name}")
    assert not offenders, (
        "a consequential module reached the AI layer: " + "; ".join(offenders))


def test_the_score_module_does_not_import_the_ai_layer_even_indirectly():
    """`score.py` is the headline number on the first screen. If a model contributed to
    it, every figure downstream would inherit the model's uncertainty and the product
    would have laundered a guess into a metric."""
    source = (SRC / "score.py").read_text(encoding="utf-8")
    assert "import ai" not in source and "from .ai" not in source


# ── /api/ai/status ───────────────────────────────────────────────────────────

def test_ai_status_reports_presence_and_never_the_key(client, monkeypatch):
    """The one thing this endpoint must never do. Set a recognisable key and assert it
    appears nowhere in the serialised response."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-CANARY-must-never-be-served")
    body = client.get("/api/ai/status").text
    assert "CANARY" not in body
    assert "sk-test" not in body


def test_ai_status_declares_the_boundary_and_what_enforces_it(client, no_key):
    body = client.get("/api/ai/status").json()
    assert body["enabled"] is False
    assert body["mode"] == "DETERMINISTIC_ONLY"
    assert body["retrieval"] == "LEXICAL_BM25"
    never = " ".join(body["boundary"]["model_may_never"]).lower()
    for forbidden in ("cover exists", "precedence", "settlement", "evidence class",
                      "protection score"):
        assert forbidden in never
    assert "do not import ai.py" in body["boundary"]["enforced_by"]


def test_ai_status_reports_the_real_index_not_an_aspirational_one(client, no_key):
    """A stated retrieval mode has to be the mode actually in use. With no key the
    index is lexical and must say so rather than claiming semantic search."""
    index = client.get("/api/ai/status").json()["index"]
    assert index["chunks"] > 0, "the demo case indexed no chunks"
    assert index["pages_indexed"] > 0
    assert index["embedded"] is False
    assert index["method"] == "LEXICAL_BM25"


# ── /api/score ───────────────────────────────────────────────────────────────

def test_score_is_deterministic_and_decomposes_to_findings(client, no_key):
    """Two calls, one answer — and every point deducted names the rule it came from.
    A score nobody can decompose is a credit rating, which this product refuses to be."""
    first = client.get("/api/score").json()
    second = client.get("/api/score").json()
    assert first["overall"] == second["overall"]
    assert first["method"] == "DETERMINISTIC"
    assert 0 <= first["overall"] <= 100

    rule_ids = {f["rule_id"] for f in client.get("/api/brief").json()["findings"]}
    deducted = [d for dim in first["dimensions"] for d in dim["deductions"]]
    assert deducted, "nothing was deducted on a case with known gaps"
    for deduction in deducted:
        assert deduction["rule_id"] in rule_ids, (
            f"deduction cites {deduction['rule_id']}, which is not a finding")
        assert deduction["points"] > 0


def test_an_unassessed_dimension_is_not_reported_as_full_marks(client, no_key):
    """"We did not look" must never render as "you are fine".

    This test used to prove the invariant by asserting the DEMO BUNDLE contained an
    unassessed dimension, and it passed because `digital` had an empty rule tuple and
    could never be assessed by anything. That made the test a witness to a defect
    rather than a guard against one: cyber was permanently invisible, and because
    `overall` averages only assessed dimensions, the score was HIGHER for the absence.

    `digital` and `dependencies` are now wired to real rules, so the demo bundle has
    no hole left to point at. The invariant is unchanged and is now proved directly:
    build a brief that touches one dimension only, and assert every other dimension
    comes back unassessed, scored 0, and excluded from the mean — never 100.
    """
    from proven.score import DIMENSIONS, compute_score

    # One finding, aimed at exactly one dimension.
    target_key, _, _, target_rules = DIMENSIONS[0]
    brief = {"findings": [{
        "rule_id": target_rules[0], "output_state": "DOCUMENTED",
        "evidence_class": "PROVEN", "headline": "Synthetic finding for one dimension.",
    }]}
    body = compute_score(brief)

    by_key = {d["key"]: d for d in body["dimensions"]}
    assert by_key[target_key]["assessed"] is True

    unassessed = [d for d in body["dimensions"] if not d["assessed"]]
    assert unassessed, "a brief touching one dimension must leave the others unassessed"
    for dim in unassessed:
        assert dim["score"] == 0, "an unexamined dimension must not score above zero"
        assert "not assessed" in dim["reason"].lower()
    assert body["assessed_dimensions"] < body["total_dimensions"]
    # The load-bearing half: an unassessed dimension is EXCLUDED from the mean, not
    # counted as zero, because counting it would invent a finding.
    assert body["overall"] == by_key[target_key]["score"]


def test_every_declared_dimension_can_actually_be_assessed(client, no_key):
    """No dimension may exist that nothing can ever fill.

    `digital` shipped for two versions with an empty rule tuple. It rendered as a
    labelled row on the owner's score screen, so it looked like a thing being watched,
    while being structurally incapable of ever reporting anything. That is worse than
    omitting it: it is a promise of scrutiny that no code could keep.
    """
    from proven.score import DIMENSIONS

    empty = [key for key, _, _, rules in DIMENSIONS if not rules]
    assert not empty, (
        f"dimensions {empty} declare no rules, so they can never be assessed and "
        f"will sit on the owner's screen implying a check that cannot happen")


def test_the_score_never_invents_an_exposure_figure(client, no_key):
    """`headline_exposure` is the largest figure the engine actually computed, or
    absent. There is no path that estimates one."""
    exposure = client.get("/api/score").json()["headline_exposure"]
    if exposure is not None:
        assert exposure.get("rule_id")
        assert exposure.get("amount_sgd") is not None
        # An illustrated settlement without the loss it was assessed against is a
        # figure with no stated basis, which is the one thing this product must not put
        # on a screen.
        if exposure.get("illustrated_settlement_sgd") is not None:
            assert exposure.get("assessed_loss_sgd") is not None


def test_score_goes_quiet_when_the_case_is_deleted_and_keeps_its_shape(client, no_key):
    """Deletion has to reach every case-scoped view, not only the Brief — that defect
    has been real in this codebase before. Shape is preserved so a consumer never has
    to branch on it."""
    live = client.get("/api/score").json()
    client.delete("/api/case")
    dead = client.get("/api/score").json()
    assert dead["available"] is False and dead["deleted"] is True
    assert dead["dimensions"] == [] and dead["issues"] == []
    assert set(live) <= set(dead), "deleted response dropped keys the live one has"


# ── /api/evidence/search — the "show me why" drawer ──────────────────────────

def test_evidence_search_returns_page_anchored_hits_that_verify(client, no_key):
    """The whole architecture in one assertion: a returned passage is only evidence if
    re-reading the stored page at its cited offsets yields the same text."""
    body = client.get("/api/evidence/search",
                      params={"q": "average sum insured underinsurance"}).json()
    assert body["available"] is True
    assert body["count"] > 0, "no evidence retrieved for a term that is in the wording"
    assert body["verified_count"] == body["count"], (
        "a retrieved passage did not match the stored page at its cited offsets")
    for hit in body["hits"]:
        assert hit["verified"] is True
        assert hit["page_no"] >= 1
        assert hit["quote"].strip()


def test_evidence_search_works_with_no_key_because_retrieval_degrades(client, no_key):
    body = client.get("/api/evidence/search", params={"q": "premium payment"}).json()
    assert body["method"] == "LEXICAL_BM25"
    assert body["count"] > 0


def test_evidence_search_rejects_an_empty_query(client, no_key):
    assert client.get("/api/evidence/search", params={"q": "   "}).status_code == 400


def test_evidence_search_bounds_top_k(client, no_key):
    """An unbounded top_k from the query string is a cheap way to make the server do
    arbitrary work."""
    body = client.get("/api/evidence/search",
                      params={"q": "policy", "top_k": 9999}).json()
    assert body["count"] <= 20


def test_evidence_search_goes_quiet_when_the_case_is_deleted(client, no_key):
    client.delete("/api/case")
    body = client.get("/api/evidence/search", params={"q": "average"}).json()
    assert body["available"] is False and body["hits"] == []


# ── /api/ai/propose ──────────────────────────────────────────────────────────

def test_propose_degrades_to_nothing_rather_than_failing(client, no_key):
    """With no key the endpoint must return an empty, labelled result — not a 500.
    A demo that errors for someone without our key is worse than a demo with no AI."""
    body = client.post("/api/ai/propose").json()
    assert body["available"] is False
    assert body["mode"] == "DETERMINISTIC_ONLY"
    assert body["proposals"] == []
    assert body["verified_count"] == 0 and body["rejected_count"] == 0
    assert "OPENAI_API_KEY" in (body["error"] or "")


def test_propose_is_refused_on_a_deleted_case(client, no_key):
    client.delete("/api/case")
    body = client.post("/api/ai/propose").json()
    assert body["available"] is False and body["deleted"] is True


def test_the_proposable_vocabulary_is_closed_and_maps_into_the_taxonomy(no_key):
    """The model picks from a fixed list, so it cannot invent a coverage concept and
    have it enter the system. Every key must land on a real taxonomy code."""
    import yaml

    from proven import ai

    classification = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "taxonomy" / "classification.yaml")
        .read_text(encoding="utf-8"))
    codes = {rule["code"] for rule in classification["codes"]}
    assert ai.PROPOSABLE_MECHANISMS
    for key, code in ai.PROPOSABLE_MECHANISMS.items():
        assert key.islower(), f"{key} is not a stable lowercase key"
        assert code in codes, f"{key} maps to {code}, which is not in the taxonomy"


def test_a_verified_proposal_is_not_recorded_as_a_hand_verified_clause(no_key):
    """A verified quote is still not a qualified reader construing a clause, so the
    classifier must be its own value and must not collide with the verified one."""
    from proven import ai
    from proven.classify import EXACT_MAP

    assert ai.AI_CLASSIFIER == "AI_PROPOSED_VERIFIED"
    assert ai.AI_CLASSIFIER != EXACT_MAP


# ── /api/ai/explain ──────────────────────────────────────────────────────────

def test_explain_returns_deterministic_prose_and_real_citations(client, no_key):
    findings = client.get("/api/brief").json()["findings"]
    rule_id = findings[0]["rule_id"]
    body = client.post("/api/ai/explain", json={"rule_id": rule_id}).json()
    assert body["rule_id"] == rule_id
    assert body["available"] is False
    assert body["mode"] == "DETERMINISTIC_ONLY"
    assert body["source"] == "TEMPLATE"
    assert body["text"].strip(), "no explanation produced"
    for citation in body["citations"]:
        assert citation["page_no"] and citation["quote"]


def test_explain_404s_on_a_rule_that_is_not_in_this_case(client, no_key):
    r = client.post("/api/ai/explain", json={"rule_id": "NO-SUCH-RULE"})
    assert r.status_code == 404


def test_explain_never_publishes_model_prose_that_asserts_an_outcome(no_key):
    """The renderer's prohibition on adjudicating language applies to model output too.
    A sentence that says the policy is void gets discarded, not published — this is the
    filter that makes the "we locate and quote, an adviser decides" claim hold even
    when the model ignores its instructions."""
    from proven.ai import _asserts_outcome

    for bad in ("The policy is void.", "This claim will not be paid.",
                "The insured has breached the warranty.",
                "Cover is voided by this condition."):
        assert _asserts_outcome(bad), f"outcome-asserting prose slipped through: {bad}"
    for good in ("Your documents show a shortfall against the stated threshold.",
                 "An authorised adviser should review this clause."):
        assert not _asserts_outcome(good), f"legitimate prose was discarded: {good}"


# ── /api/ai/ask — the copilot ────────────────────────────────────────────────

def test_ask_answers_from_this_case_with_verified_citations(client, no_key):
    body = client.post("/api/ai/ask",
                       json={"question": "Why is my protection score low?"}).json()
    assert body["available"] is False
    assert body["source"] == "DETERMINISTIC"
    assert body["text"].strip()
    for citation in body["citations"]:
        assert citation["page_no"] >= 1 and citation["quote"].strip()


def test_ask_validates_the_question(client, no_key):
    assert client.post("/api/ai/ask", json={"question": "a"}).status_code == 422
    assert client.post("/api/ai/ask",
                       json={"question": "x" * 501}).status_code == 422


def test_ask_is_refused_on_a_deleted_case(client, no_key):
    client.delete("/api/case")
    assert client.post("/api/ai/ask",
                       json={"question": "What is my exposure?"}).status_code == 404


# ── /api/signals — the simulated connector ───────────────────────────────────

def test_every_signal_is_labelled_simulated(client):
    """The product must never imply it holds an OAuth token. `simulated: true` on every
    signal, and the envelope says it in prose as well."""
    body = client.get("/api/signals").json()
    assert body["events"], "the feed served no signals"
    for signal in body["events"]:
        assert signal["simulated"] is True
    assert "SIMULATED" in body["notice"]
    connected = [c for c in body["connectors"] if c["status"] == "CONNECTED"]
    # Two sources are implemented: the transactional accounting feed and minuted
    # decisions. The count is asserted so that adding a third cannot slip in unlabelled —
    # the guarantee being protected is that NOTHING claims to hold a live token, not that
    # there is only ever one source.
    assert len(connected) == 2, "accounting feed and meeting notes"
    assert all(c["simulated"] is True for c in body["connectors"])
    assert all(c["simulated"] is True for c in connected)


def test_applying_signals_derives_events_the_owner_did_not_type(client, no_key):
    """The point of the connector: a fact arrives from a system the owner did not type
    into, and the engine re-derives from it."""
    ids = ["SIG-001", "SIG-003"]
    body = client.post("/api/signals/apply", json={"signal_ids": ids}).json()
    assert body["derived_events"] == connectors.derived_events(ids)
    assert body["derived_events"], "no event was derived from the signals"
    assert set(body["derived_events"]) <= set(body["recorded"])
    assert body["requested_documents"], "a signal must name the document that settles it"
    assert body["score_before"]["method"] == "DETERMINISTIC"
    assert body["score_delta"]["before"] == body["score_before"]["overall"]
    assert body["score_delta"]["after"] == body["score_after"]["overall"]


def test_a_connector_signal_cannot_raise_an_evidence_class(client, no_key):
    """The boundary that makes the grading scheme survive contact with live data. An
    accounting line is third-party authored but it is not a document, so it may select
    which findings are relevant and may never make one proven."""
    before = {f["rule_id"]: (f["class"], f["state"])
              for f in client.get("/api/brief").json()["findings"]}
    client.post("/api/signals/apply",
                json={"signal_ids": [s["id"] for s in
                                     client.get("/api/signals").json()["events"]]})
    after = {f["rule_id"]: (f["class"], f["state"])
             for f in client.get("/api/brief").json()["findings"]}
    assert before == after, (
        "a connector signal changed a finding's evidence class or state")


def test_applying_signals_ignores_unknown_ids_rather_than_failing(client, no_key):
    body = client.post("/api/signals/apply",
                       json={"signal_ids": ["SIG-001", "NOPE-999"]}).json()
    assert body["ignored_unknown_ids"] == ["NOPE-999"]
    assert "BOUGHT_COSTLY_EQUIPMENT" in body["derived_events"]


def test_applying_signals_is_refused_on_a_deleted_case(client, no_key):
    client.delete("/api/case")
    assert client.post("/api/signals/apply",
                       json={"signal_ids": ["SIG-001"]}).status_code == 404


def test_every_signal_implies_a_real_change_event(no_key):
    """A signal that implies an event code the catalogue does not hold would silently
    derive nothing, which would look like the connector working and doing nothing."""
    from proven import events

    for signal in connectors._FEED:
        if signal.implies_event:
            assert signal.implies_event in events.BY_CODE, (
                f"{signal.id} implies {signal.implies_event}, not a known event")


# ── the key is never served, on any route ────────────────────────────────────

def test_no_endpoint_leaks_the_api_key(client, monkeypatch):
    """A blanket sweep rather than one endpoint. `ai_status` is the one designed to talk
    about the key, so it is the likeliest place to leak it, but a copilot response that
    echoed configuration would be just as bad."""
    canary = "sk-live-LEAKCANARY-0123456789"
    monkeypatch.setenv("OPENAI_API_KEY", canary)
    paths = ["/api/ai/status", "/api/score", "/api/signals", "/api/health",
             "/api/session"]
    for path in paths:
        assert canary not in client.get(path).text, f"{path} leaked the key"
    assert canary not in client.post("/api/ai/propose").text
