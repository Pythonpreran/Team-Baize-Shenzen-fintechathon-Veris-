"""Presentation intelligence — the tests are almost entirely about the guardrails.

That a model can write a nicer sentence is not interesting and does not need a test. What
needs a test is every way a model could put something on an insurance screen that must not
be there. Each gate below was written because the model actually did the thing.

The suite never calls a model. `_call_model` is the only networked function and it is not
exercised here; everything else is pure. That keeps this file fast and offline like the
rest of the suite.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import presenter
from proven.api import SESSIONS, app


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


@pytest.fixture(autouse=True)
def _offline_and_clean(monkeypatch):
    """No test in this file may reach a model.

    `conftest.py` already forces PROVEN_LIVE_SOURCES=0 so the suite cannot call a
    government API. The same reasoning applies to a model key: a suite that quietly makes
    paid network calls is slow, flaky, and fails on a train. The model path is verified by
    checking `validate()` and `_merge()` directly, which is where the risk actually lives —
    not by proving that OpenAI answers the phone.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    presenter.clear_cache()
    yield
    presenter.clear_cache()


# ── the number gate: the one that matters most ───────────────────────────────

def test_a_number_absent_from_the_data_is_rejected():
    """MEASURED FAILURE. Given only {cover_present: false, recovery_months: 18},
    gpt-4o-mini wrote "You risk losing S$238,300 in income without cover". Fluent,
    plausible, entirely invented. A wrong number is the one output this product cannot
    ship, so every numeral is checked against the input."""
    ok, why = presenter.numbers_are_grounded(
        "You risk losing S$238,300 in income without cover", allowed={"18"})
    assert ok is False
    assert "238,300" in why


def test_a_number_present_in_the_data_is_allowed():
    ok, _ = presenter.numbers_are_grounded(
        "You would pay S$238,300 yourself", allowed={"238300"})
    assert ok is True


def test_comma_formatting_does_not_defeat_the_check():
    """The engine stores 238300; the model writes S$238,300. Same number."""
    ok, _ = presenter.numbers_are_grounded("S$238,300", allowed={"238300"})
    assert ok is True


def test_decimal_values_license_their_whole_part():
    """The engine emits 238300.00. A model writing 238,300 is restating, not inventing."""
    allowed = presenter._numbers_in({"owner_shortfall": "238300.00"})
    ok, _ = presenter.numbers_are_grounded("You would pay S$238,300", allowed)
    assert ok is True


def test_structural_numbers_must_be_explicitly_present():
    """A model may repeat a deterministic scale only when that scale is supplied."""
    ok, _ = presenter.numbers_are_grounded(
        "Your protection score is 62 out of 100", allowed={"62"})
    assert ok is False
    ok, _ = presenter.numbers_are_grounded(
        "Your protection score is 62 out of 100", allowed={"62", "100"})
    assert ok is True


def test_numbers_are_found_through_nested_structures():
    found = presenter._numbers_in(
        {"counts": {"BREACHED": 2}, "items": [{"gap": 130000}], "note": "18 months"})
    assert {"2", "130000", "18"} <= found


def test_prose_with_no_numbers_always_passes():
    ok, _ = presenter.numbers_are_grounded("Worth a look when you have a moment", set())
    assert ok is True


# ── the coverage-outcome gate ────────────────────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "Your claim will be denied",
    "This would not be paid",
    "Your policy is invalid",
    "You are not insured for this",
    "Cover is guaranteed",
    "This is a breach of your policy",
])
def test_language_that_predicts_a_claim_outcome_is_rejected(phrase):
    """Whether a claim pays is a licensed adviser's determination and, ultimately, a
    lawyer's. A presentation layer that can drift into it is not a presentation layer."""
    ok, why = presenter.validate(
        {"headline": phrase, "primary_visual": {"type": "waterfall"}})
    assert ok is False
    assert "forbidden" in why


def test_the_gate_also_reads_the_body_not_just_the_headline():
    ok, why = presenter.validate({
        "headline": "Worth a look",
        "primary_visual": {"type": "waterfall"},
        "why_it_matters": "This is a breach of warranty.",
    })
    assert ok is False


def test_stating_that_cover_is_absent_is_allowed():
    """The narrowing that matters. An earlier list banned "no cover", which also blocked
    "you have no cover for lost income" — a fact readable straight off the schedule. The
    line is not "never mention absent cover", it is "never predict a claim outcome"."""
    ok, why = presenter.validate({
        "headline": "You have no cover for lost income",
        "primary_visual": {"type": "evidence_gap"},
    })
    assert ok is True, why


# ── the filler gate ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("filler", [
    "Understanding your coverage helps protect your assets.",
    "Review your policy for any gaps.",
    "This can protect your business from losses.",
])
def test_sentences_true_of_any_business_are_rejected(filler):
    """A generic sentence is a worse outcome than a written template, because it occupies
    the space where a specific fact should be. Rejecting it falls back to copy that says
    something."""
    ok, why = presenter.validate({
        "headline": "Worth a look", "primary_visual": {"type": "status_stack"},
        "why_it_matters": filler})
    assert ok is False
    assert "filler" in why


# ── the domain gate ──────────────────────────────────────────────────────────

def test_personal_lines_language_is_rejected():
    """gpt-4o-mini opened with "Significant gaps in your home insurance coverage" for a
    commercial policy. A model that has lost the domain cannot be trusted on the rest of
    the same screen."""
    ok, why = presenter.validate({
        "headline": "Gaps in your home insurance coverage",
        "primary_visual": {"type": "progress_ring"}})
    assert ok is False
    assert "domain" in why


# ── schema gates ─────────────────────────────────────────────────────────────

def test_a_visual_the_interface_cannot_draw_is_rejected():
    ok, why = presenter.validate(
        {"headline": "x", "primary_visual": {"type": "pie_of_doom"}})
    assert ok is False
    assert "visual" in why


def test_an_unknown_severity_is_rejected():
    ok, why = presenter.validate({
        "headline": "x", "primary_visual": {"type": "waterfall"},
        "visual_emphasis": {"severity": "APOCALYPTIC"}})
    assert ok is False


def test_a_missing_headline_is_rejected():
    ok, why = presenter.validate({"primary_visual": {"type": "waterfall"}})
    assert ok is False
    assert "headline" in why


def test_an_unreadably_long_headline_is_rejected():
    ok, why = presenter.validate({
        "headline": "x" * 200, "primary_visual": {"type": "waterfall"}})
    assert ok is False


# ── the merge: engine owns the chart, model owns the words ───────────────────

def test_the_model_cannot_change_the_chart():
    """Asked to present a score, an obligation register and a renewal diff, the model chose
    risk_meter for all three — one right, two wrong. Chart choice is a deterministic
    mapping from the kind of finding, so it stays with the engine."""
    base = presenter.build_fallback("protection_overview", {"overall": 62},
                                    "protection_status")
    merged = presenter._merge(base, {
        "headline": "Nicer words here",
        "primary_visual": {"type": "risk_meter", "title": "hijacked"}})
    assert merged["primary_visual"] == base["primary_visual"]
    assert merged["headline"] == "Nicer words here"


def test_the_model_cannot_change_the_severity():
    base = presenter.build_fallback("scenario_explanation",
                                     {"owner_shortfall": 238300, "loss": 300000},
                                     "claim_scenario")
    merged = presenter._merge(base, {
        "headline": "ok",
        "visual_emphasis": {"severity": "LOW", "metric": "S$238,300"}})
    assert merged["visual_emphasis"]["severity"] == \
        base["visual_emphasis"]["severity"]
    assert merged["visual_emphasis"]["metric"] == "S$238,300"


def test_a_field_the_model_left_blank_keeps_the_template_wording():
    base = presenter.build_fallback("home_overview", {"overall": 62, "action_count": 4},
                                     "protection_status")
    merged = presenter._merge(base, {"headline": "  ", "what_next": None})
    assert merged["headline"] == base["headline"]
    assert merged["what_next"] == base["what_next"]


# ── the templates are the product when no model runs ─────────────────────────

def test_every_purpose_has_a_template():
    """The screen must never depend on a model being reachable."""
    for purpose in presenter.PURPOSES:
        out = presenter.build_fallback(purpose, {}, "protection_status")
        assert out.get("headline"), purpose
        assert (out.get("primary_visual") or {}).get("type") in presenter.VALID_VISUALS


def test_every_template_passes_its_own_guardrails():
    """A template may supply deterministic facts to the model-output gate itself."""
    samples = {
        "home_overview": {"overall": 62, "action_count": 4, "finding_count": 15},
        "protection_overview": {"overall": 62, "assessed_dimensions": 4,
                                "total_dimensions": 5},
        "scenario_explanation": {"loss": 300000, "owner_shortfall": 238300,
                                 "final_illustration": 61700},
        "warranty_overview": {"counts": {"BREACHED": 2, "UNVERIFIED": 2},
                              "obligations_in_policy": 4},
        "bi_overview": {"cover_present": False, "recovery_months": 18},
        "renewal_summary": {"material_count": 1, "change_count": 1},
        "connect_summary": {"event_count": 8},
        "sentinel_summary": {"environment_level": "ELEVATED", "signal_count": 4},
        "discovery_summary": {"verified_count": 2, "sources_checked": 4},
        "drift_summary": {"crossed_on": None},
        "gaps_summary": {"gap_count": 3, "top_document": "Reinstatement cost assessment"},
    }
    for purpose, ctx in samples.items():
        out = presenter.build_fallback(purpose, ctx,
                                        presenter.detect_semantic_category(purpose, ctx))
        # `_call_model` permits the model to repeat deterministic template wording, so
        # exercise the same bound rather than treating the template as untrusted prose.
        allowed = (presenter._numbers_in(ctx)
                   | presenter._numbers_in({field: out.get(field)
                                            for field in presenter._MODEL_OWNS}))
        ok, why = presenter.validate(out, allowed_numbers=allowed)
        assert ok, f"{purpose}: {why}"


def test_templates_avoid_the_jargon_the_product_exists_to_translate():
    """The whole point is that a shop owner reads this. These words are the ones that make
    an insurance screen unreadable."""
    banned = ("sum insured", "indemnity period", "condition precedent",
              "average clause", "quantum", "pursuant", "aforementioned")
    for purpose in presenter.PURPOSES:
        out = presenter.build_fallback(purpose, {"cover_present": False}, "obligation")
        prose = " ".join(str(out.get(k, "")) for k in
                         ("headline", "subheadline", "why_it_matters", "what_next")).lower()
        for word in banned:
            assert word not in prose, f"{purpose} used jargon: {word}"


# ── semantic routing ─────────────────────────────────────────────────────────

def test_a_claim_scenario_routes_to_a_waterfall():
    cat = presenter.detect_semantic_category(
        "scenario_explanation", {"loss": 1, "deductible": 2})
    assert presenter.SEMANTIC_CATEGORIES[cat]["primary_visual"] == "waterfall"


def test_an_unrecognised_purpose_still_routes_somewhere_drawable():
    cat = presenter.detect_semantic_category("something_new", {})
    assert presenter.SEMANTIC_CATEGORIES[cat]["primary_visual"] in presenter.VALID_VISUALS


# ── compose contract ─────────────────────────────────────────────────────────

def test_compose_always_returns_something_drawable():
    """With no key configured there is no network call and the template answers."""
    r = presenter.compose("scenario_explanation",
                          {"loss": 300000, "owner_shortfall": 238300})
    assert r.available is True
    assert r.mode == "DETERMINISTIC_FALLBACK"
    assert "238,300" in r.presentation["headline"]


def test_a_placeholder_key_counts_as_no_key(monkeypatch):
    """`.env.example` ships OPENAI_API_KEY=sk-your-key-here. Treating that as configured
    would send every screen down a path that 401s."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-your-key-here")
    assert presenter.enabled() is False


def test_only_the_supported_provider_key_selects_a_provider(monkeypatch):
    """REPLACES the earlier test that a second hosted provider was preferred when both keys
    were set.

    That provider used to win whenever its key was present. It was removed deliberately;
    the behaviour now pinned is that this module reads exactly one key, and a key for any
    other service neither selects a provider nor displaces the supported one.
    """
    monkeypatch.setenv("OTHER_PROVIDER_API_KEY", "op-real-looking-value-0123456789")
    assert presenter.provider() is None, "an unsupported provider's key selected a provider"
    assert presenter.enabled() is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-looking-value")
    assert presenter.provider() == "openai"
    assert presenter.model_name() == presenter.OPENAI_DEFAULT_MODEL


def test_the_result_carries_the_boundary_statement():
    """The claim about what the model did travels with the payload, so an interface cannot
    quietly present model wording as engine output."""
    r = presenter.compose("home_overview", {"overall": 62})
    assert "deterministic engine" in r.as_dict()["boundary"]


# ── endpoints ────────────────────────────────────────────────────────────────

def test_status_reports_the_guardrails_and_never_the_key(client):
    body = client.get("/api/presentation/status").json()
    assert body["mode"] in ("MODEL", "DETERMINISTIC_FALLBACK")
    assert body["guardrails"]["forbidden_phrases"] > 0
    assert body["guardrails"]["falls_back"] is True
    assert "key" not in json_lower(body)


def json_lower(obj) -> str:
    import json
    return json.dumps(obj).lower().replace("api_key", "").replace("no model key", "")


def test_compose_endpoint_returns_a_drawable_payload(client):
    body = client.post("/api/presentation/compose", json={
        "purpose": "scenario_explanation",
        "verified_context": {"loss": 300000, "owner_shortfall": 238300,
                             "final_illustration": 61700}}).json()
    assert body["available"] is True
    pres = body["presentation"]
    assert pres["headline"]
    assert pres["primary_visual"]["type"] in presenter.VALID_VISUALS


def test_compose_endpoint_rejects_a_junk_purpose_gracefully(client):
    body = client.post("/api/presentation/compose", json={
        "purpose": "not_a_real_purpose", "verified_context": {}}).json()
    assert body["available"] is True
    assert body["presentation"]["headline"]
