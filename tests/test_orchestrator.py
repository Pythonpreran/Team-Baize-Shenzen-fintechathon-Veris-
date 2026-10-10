"""VERIS Sentinel and the Orchestrator: the claim ceiling and the precedence rules.

WHY THIS FILE IS THE IMPORTANT ONE. Adding a second agent doubles the surface for the
single worst failure this product could have: presenting something nobody established as
though it were a fact. Connect says inventory rose to S$150,000. Sentinel says a cost
index rose 8.4%. Both true, neither a finding — and the obvious implementation multiplies
them and renders S$162,600 as a sum insured, a number with no basis in any document.

So these tests do not check that endpoints answer. They check that the system REFUSES:

  * a Sentinel signal cannot be constructed with a claim scope that asserts something
    about a specific business (enforced at __post_init__, not by convention);
  * external data never edits a current value, only `risk_state`;
  * agreement between agents raises priority and never the evidence class;
  * an external signal with nothing business-specific behind it lands on
    POTENTIAL_INCIDENT rather than on an assertion;
  * the decision vocabulary contains no adjudicating word anywhere in the module.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proven import orchestrator, sentinel
from proven.api import SESSIONS, app

SRC = Path(__file__).resolve().parents[1] / "src" / "proven"


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        c.get("/api/brief")
        yield c
    SESSIONS.clear()


# ── the claim ceiling ────────────────────────────────────────────────────────

def test_sentinel_cannot_construct_a_signal_that_asserts_a_business_specific_event():
    """THE STRUCTURAL GUARANTEE. Not a convention, not a code review rule — the
    constructor raises. An external feed has no access to facts about one business, so it
    must be unable to claim one."""
    for forbidden in (sentinel.SME_INCIDENT, sentinel.CONFIRMED_EVIDENCE):
        with pytest.raises(ValueError) as excinfo:
            sentinel.RiskSignal(
                id="X", source=sentinel.MET_SERVICE, signal_type="FLOOD_RISK",
                label="x", detail="x", severity="HIGH",
                horizon=sentinel.HORIZON_CURRENT, observed_on="2026-08-01",
                window="now", regions=("SG",), sectors=("ANY",),
                insured_interests=("stock",), claim_scope=forbidden)
        assert "cannot establish a fact about a specific business" in str(excinfo.value)


def test_every_shipped_signal_stays_within_the_ceiling(client):
    for signal in sentinel._FEED:
        assert signal.claim_scope in sentinel.SENTINEL_MAY_EMIT
    for item in client.get("/api/sentinel").json()["signals"]:
        assert item["claim_scope"] in sentinel.SENTINEL_MAY_EMIT
        assert "does NOT assert" in item["asserts"]


def test_no_signal_is_worded_as_something_that_happened(client):
    """The most common overclaim in this category, caught in the copy rather than only in
    the type. "Flooding expected near the insured location" is fine; "your premises
    flooded" is not, and neither is anything in the past tense about this business."""
    forbidden = ("your premises flooded", "you were robbed", "a theft occurred",
                 "your equipment is now worth", "your business was", "damage occurred",
                 "claim likely", "loss occurred")
    for item in client.get("/api/sentinel").json()["signals"]:
        blob = f"{item['label']} {item['detail']}".lower()
        for phrase in forbidden:
            assert phrase not in blob, f"{item['id']} asserts an event: {phrase!r}"


def test_severity_is_ordinal_and_never_a_multipliable_number(client):
    """A 0-1 risk score invites arithmetic the data cannot support — multiplying a
    forecast confidence by a sum insured produces a figure with no basis. Ordered labels
    can be compared and cannot be multiplied."""
    for item in client.get("/api/sentinel").json()["signals"]:
        assert item["severity"] in sentinel.SEVERITY_ORDER
        assert isinstance(item["severity_rank"], int)


def test_every_source_declares_that_it_is_a_fixture(client):
    """There is no external API key in this build. A mocked feed rendered as "Connected"
    would be the most consequential lie on the least examined screen."""
    body = client.get("/api/sentinel/sources").json()
    assert body["live_count"] == 0
    assert body["demo_count"] == len(body["sources"])
    for source in body["sources"]:
        assert source["mode"] == "DEMO_FIXTURE"
        assert source["is_live"] is False
        assert source["live_equivalent"], "no named live equivalent to swap in"
    assert "no live external API credentials" in body["notice"]
    assert "no code path" in body["boundary"]["enforced_by"]


# ── relevance is computed, not inferred ─────────────────────────────────────

def test_relevance_is_a_deterministic_match_not_a_model_call():
    """"Is this flood warning relevant to this policy?" has a checkable answer. A
    checkable answer beats a persuasive one on a screen that leads to money."""
    source = (SRC / "sentinel.py").read_text(encoding="utf-8")
    assert "import ai" not in source and "from .ai" not in source, \
        "sentinel.py reached the model layer"

    tai_seng = sentinel.relevant_signals(region="SG-TAI-SENG")
    assert tai_seng, "nothing matched the insured district"
    elsewhere = sentinel.relevant_signals(region="SG-WOODLANDS")
    assert len(elsewhere) < len(tai_seng), "region filtering does nothing"


def test_interest_filtering_narrows_to_the_insured_interest():
    stock = sentinel.relevant_signals(insured_interests=("stock",))
    workforce = sentinel.relevant_signals(insured_interests=("workforce",))
    assert stock, "no signal bears on stock"
    assert not workforce, "an external feed claimed to bear on workforce headcount"


def test_the_environment_level_is_the_worst_signal_not_the_average():
    """Averaging four moderate signals with one high one produces a number that hides the
    only signal anybody needs to act on."""
    summary = sentinel.environment_summary(list(sentinel._FEED))
    worst = max(sentinel._FEED, key=lambda s: sentinel.severity_rank(s.severity))
    assert summary["level"] == worst.severity
    assert summary["driver_signal_id"] == worst.id
    assert "rather than an average" in summary["basis"]


# ── the vocabulary ─────────────────────────────────────────────────────────

def test_the_orchestrator_module_contains_no_adjudicating_vocabulary():
    """Greps the module itself. A decision state called BREACH would be a claim this
    product is not entitled to make, and it must not exist as a string, an enum or a
    column."""
    source = (SRC / "orchestrator.py").read_text(encoding="utf-8")
    # Strip the one place the words legitimately appear: the list of what is absent.
    source = re.sub(r'"deliberately_absent":.*?\]', "", source, flags=re.S)
    source = re.sub(r"no BREACH.*?column", "", source, flags=re.S)
    for word in ("CLAIM_APPROVED", "CLAIM_DENIED", "COVERAGE_GUARANTEED"):
        assert word not in source, f"{word} appears in orchestrator.py"
    for decision in orchestrator.DECISIONS:
        assert decision not in {"BREACH", "VIOLATION"}


def test_the_orchestrator_does_not_import_the_model_layer():
    """The agents propose; this decides. Same split as ai.py, one level up — and the
    dependency direction is what enforces it."""
    source = (SRC / "orchestrator.py").read_text(encoding="utf-8")
    assert "import ai" not in source and "from .ai" not in source


# ── PRECEDENCE RULE 1: external data never sets a current value ─────────────

def test_an_external_signal_alone_never_becomes_a_current_position():
    """The rule that stops the category's most common overclaim. A cost index rising is
    not a revaluation of anything anyone owns."""
    external = [s.as_dict() for s in sentinel.relevant_signals(
        insured_interests=("plant",))]
    assert external, "fixture problem: no external signal touches plant"

    out = orchestrator.evaluate(internal_signals=[], external_signals=external,
                                findings=[])
    plant = next(d for d in out["decisions"] if d["interest"] == "plant")

    assert plant["decision"] in {orchestrator.POTENTIAL_INCIDENT, orchestrator.WATCH}
    assert "Nothing on record establishes a current position" in plant["current_state"]
    assert plant["evidence_class"] is None, \
        "an external signal was given an evidence class"
    # The forward risk is present, and it is in its own field.
    assert plant["risk_state"] and "External condition only" in plant["risk_state"]


def test_a_percentage_from_an_index_is_never_applied_to_a_business_figure():
    """The specific arithmetic this module exists to prevent: S$150,000 x 1.084 rendered
    as a sum insured. The index magnitude must not appear anywhere in the current-state
    text."""
    index_signals = [s.as_dict() for s in sentinel._FEED
                     if s.signal_type == "COST_INDEX"]
    assert index_signals and index_signals[0]["magnitude_pct"] == 8.4

    out = orchestrator.evaluate(
        internal_signals=[{"id": "SIG-001", "kind": "ASSET_ACQUIRED",
                           "label": "freezer", "amount_sgd": "62000",
                           "detected_on": "2026-08-01", "asks_for": ["ASSET_REGISTER"]}],
        external_signals=index_signals, findings=[])
    for decision in out["decisions"]:
        assert "162" not in decision["current_state"], \
            "an external percentage was applied to a business figure"
        assert "8.4" not in decision["current_state"], \
            "the index magnitude leaked into the current position"


# ── PRECEDENCE RULE 3: agreement raises priority, not certainty ─────────────

def test_overlap_raises_priority_but_never_the_evidence_class():
    """Two agents agreeing is a reason to look sooner. It is not extra evidence, and an
    evidence class that moved because two systems concurred would be a claim neither of
    them can support."""
    internal = [{"id": "SIG-003", "kind": "INVENTORY", "label": "stock rose",
                 "amount_sgd": "150000", "detected_on": "2026-08-01",
                 "asks_for": ["ACCOUNTS"]}]
    external = [s.as_dict() for s in sentinel.relevant_signals(
        insured_interests=("stock",))]
    findings = [{"rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD",
                 "headline": "stock adequacy below threshold",
                 "state": "DOCUMENTED", "class": "DECLARED", "detail": {}}]

    alone = orchestrator.evaluate(internal_signals=internal, external_signals=[],
                                 findings=findings)
    both = orchestrator.evaluate(internal_signals=internal, external_signals=external,
                                 findings=findings)

    stock_alone = next(d for d in alone["decisions"] if d["interest"] == "stock")
    stock_both = next(d for d in both["decisions"] if d["interest"] == "stock")

    assert stock_both["overlap"] is True and stock_alone["overlap"] is False
    assert (orchestrator.PRIORITY_ORDER.index(stock_both["priority"])
            >= orchestrator.PRIORITY_ORDER.index(stock_alone["priority"]))
    # The class is identical. This is the assertion that matters.
    assert stock_both["evidence_class"] == stock_alone["evidence_class"] == "DECLARED"
    assert "not the evidence class" in stock_both["precedence_reason"]


def test_the_overlap_is_reported_so_the_interface_can_lead_with_it():
    internal = [{"id": "SIG-003", "kind": "INVENTORY", "label": "stock rose",
                 "amount_sgd": "150000", "detected_on": "2026-08-01"}]
    external = [s.as_dict() for s in sentinel.relevant_signals(
        insured_interests=("stock",))]
    out = orchestrator.evaluate(internal_signals=internal, external_signals=external,
                                findings=[])
    assert out["overlap_count"] >= 1
    assert "stock" in out["overlap_interests"]


# ── PRECEDENCE RULE 4: the reason travels with the decision ────────────────

def test_every_decision_explains_its_own_precedence():
    """A conflict resolved without saying why is indistinguishable from a coin toss, and
    an adviser asked to act on it deserves better."""
    out = orchestrator.evaluate(
        internal_signals=[{"id": "SIG-003", "kind": "INVENTORY", "label": "x",
                           "detected_on": "2026-08-01"}],
        external_signals=[s.as_dict() for s in sentinel._FEED],
        findings=[{"rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD", "headline": "x",
                   "state": "DOCUMENTED", "class": "PROVEN", "detail": {}}])
    assert out["decisions"]
    for decision in out["decisions"]:
        assert len(decision["precedence_reason"]) > 40, \
            f"{decision['interest']} gives no real reason"
        assert decision["decision"] in orchestrator.DECISIONS
        assert decision["decision_label"]


def test_documented_findings_win_over_an_external_signal_and_say_so():
    findings = [{"rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD", "headline": "x",
                 "state": "DOCUMENTED", "class": "PROVEN", "detail": {}}]
    out = orchestrator.evaluate(
        internal_signals=[], external_signals=[s.as_dict() for s in sentinel._FEED],
        findings=findings)
    stock = next(d for d in out["decisions"] if d["interest"] == "stock")
    assert stock["decision"] == orchestrator.ACTION_REQUIRED
    assert "documented" in stock["current_state"].lower()


def test_stale_internal_data_is_flagged_without_letting_external_data_win():
    """An old internal record does not become wrong; it becomes less current. Saying so is
    the useful part — and the external signal still does not become evidence."""
    old = (date.today().replace(year=date.today().year - 1)).isoformat()
    out = orchestrator.evaluate(
        internal_signals=[{"id": "SIG-003", "kind": "INVENTORY", "label": "old record",
                           "detected_on": old}],
        external_signals=[s.as_dict() for s in sentinel.relevant_signals(
            insured_interests=("stock",))],
        findings=[])
    stock = next(d for d in out["decisions"] if d["interest"] == "stock")
    assert stock["stale_internal"] is True
    assert "more recent information" in stock["precedence_reason"]
    assert "does not make it evidence" in stock["precedence_reason"]


# ── the seven scenarios ────────────────────────────────────────────────────

def test_scenario_no_inputs_produces_no_invented_monitoring():
    """"We did not look" and "we looked and it is fine" are different statements. An
    interest nobody mentioned is omitted rather than reported as MONITOR."""
    out = orchestrator.evaluate(internal_signals=[], external_signals=[], findings=[])
    assert out["decisions"] == [] and out["count"] == 0


def test_scenario_internal_only_routes_to_review():
    out = orchestrator.evaluate(
        internal_signals=[{"id": "SIG-002", "kind": "HEADCOUNT", "label": "11 to 14",
                           "detected_on": "2026-08-01"}],
        external_signals=[], findings=[])
    workforce = next(d for d in out["decisions"] if d["interest"] == "workforce")
    assert workforce["decision"] == orchestrator.REVIEW
    assert "only a document settles it" in workforce["precedence_reason"]


def test_scenario_missing_evidence_stays_unestablished():
    out = orchestrator.evaluate(
        internal_signals=[], external_signals=[],
        findings=[{"rule_id": "BUILDINGS-VALUE-UNESTABLISHED", "headline": "x",
                   "state": "INFORMATION_MISSING", "class": "UNESTABLISHED",
                   "detail": {"missing_documents": ["VALUATION"]}}])
    buildings = next(d for d in out["decisions"] if d["interest"] == "buildings")
    assert buildings["decision"] == orchestrator.UNESTABLISHED
    assert "will not estimate" in buildings["precedence_reason"]
    documents = [a for a in buildings["actions"] if a["type"] == "PROVIDE_DOCUMENT"]
    assert documents and "VALUATION" in documents[0]["documents"]


def test_no_action_ever_instructs_on_coverage():
    """Actions name documents to obtain and humans to involve. Whether cover responds is a
    licensed adviser's determination."""
    out = orchestrator.evaluate(
        internal_signals=[{"id": "SIG-003", "kind": "INVENTORY", "label": "x",
                           "detected_on": "2026-08-01"}],
        external_signals=[s.as_dict() for s in sentinel._FEED],
        findings=[{"rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD", "headline": "x",
                   "state": "DOCUMENTED", "class": "PROVEN", "detail": {}}])
    for decision in out["decisions"]:
        for action in decision["actions"]:
            blob = f"{action['label']} {action['detail']}".lower()
            for forbidden in ("you are covered", "not covered", "increase your cover to",
                              "buy", "claim will"):
                assert forbidden not in blob, f"action instructs on coverage: {blob}"


# ── the attention matrix ───────────────────────────────────────────────────

def test_the_matrix_axes_are_observations_not_accusations():
    out = orchestrator.evaluate(
        internal_signals=[{"id": "SIG-003", "kind": "INVENTORY", "label": "x",
                           "detected_on": "2026-08-01"}],
        external_signals=[s.as_dict() for s in sentinel._FEED], findings=[])
    matrix = orchestrator.attention_matrix(out["decisions"])
    assert matrix["title"] == "Protection Attention Matrix"
    assert "breach" not in str(matrix).lower() or "no breach" in matrix["notice"].lower()
    assert set(matrix["grid"]) == {"LOW", "MED", "HIGH"}
    assert sum(sum(row.values()) for row in matrix["grid"].values()) == len(
        out["decisions"])


# ── the endpoints ──────────────────────────────────────────────────────────

def test_the_orchestrator_endpoint_reconciles_the_live_case(client):
    body = client.get("/api/orchestrator/evaluate").json()
    assert body["available"] is True
    assert body["method"] == "DETERMINISTIC"
    assert body["decisions"], "nothing was reconciled on the demo case"
    assert body["attention_matrix"]["title"] == "Protection Attention Matrix"
    assert body["environment"]["level"] in sentinel.SEVERITY_ORDER
    for absent in ("BREACH", "VIOLATION", "CLAIM_APPROVED"):
        assert absent in body["vocabulary"]["deliberately_absent"]


def test_the_orchestrator_counts_only_signals_the_owner_applied(client):
    """An unapplied connector row is a row on a screen, not a business change anyone
    acknowledged. Counting it as internal input would let the interface manufacture an
    overlap nobody agreed to."""
    before = client.get("/api/orchestrator/evaluate").json()
    assert before["inputs"]["internal_signals"] == 0

    client.post("/api/signals/apply", json={"signal_ids": ["SIG-003"]})
    after = client.get("/api/orchestrator/evaluate").json()
    assert after["inputs"]["internal_signals"] > 0


def test_the_orchestrator_goes_quiet_when_the_case_is_deleted(client):
    client.delete("/api/case")
    body = client.get("/api/orchestrator/evaluate").json()
    assert body["available"] is False and body["decisions"] == []


def test_the_carrier_portfolio_carries_the_attention_matrix(client):
    from proven import auth

    client.post("/api/auth/login", json={"email": "underwriter@carrier.demo",
                                         "password": auth.DEMO_PASSWORD})
    body = client.get("/api/carrier/portfolio").json()

    # The attention matrix is GONE from this screen, deliberately.
    #
    # It plotted "has the business changed" against "did its environment worsen" over six
    # invented accounts. Both axes were hand-typed per fictional row, so the grid measured
    # nothing but the fixture that fed it — and a 3x3 grid is a strong visual claim to
    # make about companies that do not exist. It returns when there is a book to plot.
    assert "attention_matrix" not in body
    assert body["synthetic"] is False

    # What replaces it is real: a wording portfolio computed over published documents.
    wp = body["wording_portfolio"]
    assert wp["counts"]["wordings"] >= 3
    assert wp["divergence"]["compared"] >= 1
    # And the accounts count reconciles with the honest total rather than a fixture size.
    assert body["totals"]["accounts"] == len(body["accounts"])
