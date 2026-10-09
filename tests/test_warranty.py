"""Warranties and conditions precedent — the tests that keep this honest.

This module makes a claim that could do real harm if it were wrong: "your policy contains
this obligation and nothing shows you are meeting it". Two failure modes matter more than
anything else here.

  1. A FALSE CLEAN BILL OF HEALTH. If UNVERIFIED ever drifts to COMPLIANT, the product
     manufactures confidence exactly where the exposure lives. Silence is not compliance,
     and these tests hold that line.
  2. AN INVENTED OBLIGATION. Keyword matching on a policy wording finds the word
     "sprinkler" in a grant of cover and "waste" in a definition. Reporting either as an
     obligation the customer might be breaching is a fabricated finding. The obligation
     guard exists for this, and it is tested against the real wording.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import pipeline, warranty
from proven.api import SESSIONS, app


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


class _Doc:
    def __init__(self, kind):
        self.id = "doc-test"
        self.kind = kind


class _Fact:
    def __init__(self, value, kind="SCHEDULE", page=1, needle="n"):
        self.value = value
        self.established = True
        self.page_no = page
        self.needle = needle
        self.author_class = "INSURER"
        self.doc = _Doc(kind)


class _Facts(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


# ── the obligation guard ─────────────────────────────────────────────────────

def test_duty_language_admits_a_clause_as_an_obligation():
    ok, reason = warranty._reads_as_obligation(
        "3.1 Alteration The Insured shall give notice to Chubb of any material "
        "alteration or change in circumstances affecting the risk covered.")
    assert ok is True
    assert "shall" in reason


def test_a_definition_is_not_an_obligation():
    """Measured against the real wording: "waste" appears in the Pollutants DEFINITION.
    Calling that an obligation would be an invented finding."""
    ok, reason = warranty._reads_as_obligation(
        "2.24 Pollutants means any solid, liquid, gaseous or thermal irritant or "
        "contaminant, including smoke, vapours, soot, fumes, acids, alkalis, chemicals "
        "and waste. Waste includes material to be recycled or reclaimed.")
    assert ok is False
    assert "definition" in reason.lower() or "duty language" in reason.lower()


def test_an_exclusion_list_is_not_an_obligation():
    """"electrical" appears in "mechanical or electrical breakdown" inside an exclusion.
    That is not an inspection warranty."""
    ok, _ = warranty._reads_as_obligation(
        "(iii) cracking, fracturing, collapse or overheating of boilers; (iv) mechanical "
        "or electrical breakdown or derangement of machinery or equipment.")
    assert ok is False


def test_a_grant_of_cover_is_not_an_obligation():
    """A sprinkler leakage EXTENSION grants cover. It imposes no duty to certify."""
    ok, _ = warranty._reads_as_obligation(
        "2.4 Sprinkler Leakage for Damage to Property Insured caused by discharge or "
        "leakage from any sprinkler, other than Damage caused by the order of the "
        "government.")
    assert ok is False


def test_missing_context_cannot_admit_an_obligation():
    ok, reason = warranty._reads_as_obligation(None)
    assert ok is False
    assert reason


# ── the premium payment condition precedent ──────────────────────────────────

def test_premium_paid_late_is_a_breach_with_both_dates_cited():
    state, detail, evidence = warranty._check_premium_payment(
        _Facts({"inception": _Fact("2026-03-01", needle="inception: 2026-03-01"),
                "premium_received": _Fact("2026-05-11",
                                          needle="received_date: 2026-05-11")}), None)
    assert state == warranty.BREACHED
    assert "day 71" in detail
    assert len(evidence) == 2, "a breach must cite every date it rests on"


def test_premium_paid_inside_the_period_is_compliant():
    state, _, _ = warranty._check_premium_payment(
        _Facts({"inception": _Fact("2026-03-01"),
                "premium_received": _Fact("2026-03-20")}), None)
    assert state == warranty.COMPLIANT


def test_a_missing_receipt_date_is_unverified_and_never_compliant():
    """THE test. No receipt date means we do not know. It does not mean paid."""
    state, detail, _ = warranty._check_premium_payment(
        _Facts({"inception": _Fact("2026-03-01")}), None)
    assert state == warranty.UNVERIFIED
    assert state != warranty.COMPLIANT
    assert "cannot say" in detail.lower()


def test_an_unparseable_date_abstains_rather_than_assuming_compliance():
    state, _, _ = warranty._check_premium_payment(
        _Facts({"inception": _Fact("not-a-date"),
                "premium_received": _Fact("also-not-a-date")}), None)
    assert state == warranty.UNVERIFIED


def test_exactly_on_the_threshold_is_not_a_breach():
    """Day 60 of a 60-day condition is compliant. An off-by-one here alleges a breach
    that does not exist."""
    state, _, _ = warranty._check_premium_payment(
        _Facts({"inception": _Fact("2026-03-01"),
                "premium_received": _Fact("2026-04-30")}), None)
    assert state == warranty.COMPLIANT


# ── the premises condition ───────────────────────────────────────────────────

def test_an_undeclared_occupied_premises_is_a_breach():
    state, detail, _ = warranty._check_premises_declared(
        _Facts({"scheduled_premises": _Fact(["Unit 03-11, 21 Tai Seng Avenue"]),
                "demised_premises": _Fact(["Unit 03-11, 21 Tai Seng Avenue",
                                           "Unit 03-12, 21 Tai Seng Avenue"])}), None)
    assert state == warranty.BREACHED
    assert "03-12" in detail


def test_all_premises_declared_is_compliant():
    state, _, _ = warranty._check_premises_declared(
        _Facts({"scheduled_premises": _Fact(["Unit 03-11, 21 Tai Seng Avenue"]),
                "demised_premises": _Fact(["Unit 03-11, 21 Tai Seng Avenue"])}), None)
    assert state == warranty.COMPLIANT


def test_a_missing_tenancy_is_unverified_not_compliant():
    state, detail, _ = warranty._check_premises_declared(
        _Facts({"scheduled_premises": _Fact(["Unit 03-11"])}), None)
    assert state == warranty.UNVERIFIED
    assert "tenancy" in detail.lower()


def test_list_coercion_handles_both_native_and_stringified_lists():
    assert warranty._as_list(["a", "b"]) == ["a", "b"]
    assert warranty._as_list("['a', 'b']") == ["a", "b"]
    assert warranty._as_list("plain") == ["plain"]
    assert warranty._as_list("") == []


# ── the register against the real wording ────────────────────────────────────

def test_the_register_finds_the_real_obligations_in_the_real_wording():
    """Run against the actual Chubb Select+ wording, not a fixture of one. Four
    obligations are genuinely there, each with duty language."""
    reg = warranty.register(pipeline.run_case())
    admitted = {e["id"] for e in reg["register"] if e["clause_located"]}
    assert {"PREMIUM_PAYMENT", "PREMISES_DECLARED", "CLAIM_NOTIFICATION",
            "REASONABLE_PRECAUTIONS"} <= admitted


def test_every_admitted_obligation_quotes_the_real_surrounding_text():
    """The quote must be the sentence, not an echo of the search phrase. A register that
    quotes its own query back proves only that it searched."""
    reg = warranty.register(pipeline.run_case())
    for entry in reg["register"]:
        if not entry["clause_located"]:
            continue
        quote = entry["clause_quote"] or ""
        assert len(quote) > len(entry["matched_phrase"] or ""), entry["id"]
        assert entry["clause_page"], entry["id"]


def test_the_false_positives_are_set_aside_with_a_published_reason():
    """sprinkler, waste and electrical all appear in the wording and none of them is an
    obligation. Each must be set aside WITH its excerpt so a reader can overrule us."""
    reg = warranty.register(pipeline.run_case())
    by_id = {e["id"]: e for e in reg["register"]}
    for oid in ("SPRINKLER_MAINTAINED", "WASTE_REMOVAL", "ELECTRICAL_INSPECTION"):
        entry = by_id[oid]
        assert entry["state"] == warranty.NOT_IN_POLICY, oid
        assert entry["phrases_set_aside"], f"{oid} must publish why it was set aside"
        aside = entry["phrases_set_aside"][0]
        assert aside["reason"] and aside["excerpt"], oid


def test_obligations_genuinely_absent_report_what_was_searched():
    reg = warranty.register(pipeline.run_case())
    by_id = {e["id"]: e for e in reg["register"]}
    entry = by_id["HOT_WORKS"]
    assert entry["state"] == warranty.NOT_IN_POLICY
    assert entry["needles_searched"], "say what was looked for, not just what was found"
    assert not entry["phrases_set_aside"]


def test_the_demo_case_surfaces_two_breaches_one_of_them_a_condition_precedent():
    reg = warranty.register(pipeline.run_case())
    assert reg["counts"][warranty.BREACHED] == 2
    assert reg["conditions_precedent_breached"] == 1
    assert reg["highest_severity"] == "CRITICAL"


def test_breaches_sort_above_unverified_and_unverified_above_absent():
    """Order is the interface. A breach buried under ten absent obligations is a breach
    nobody reads."""
    reg = warranty.register(pipeline.run_case())
    seen = [e["state"] for e in reg["register"]]
    rank = {warranty.BREACHED: 0, warranty.UNVERIFIED: 1,
            warranty.COMPLIANT: 2, warranty.NOT_IN_POLICY: 3}
    assert seen == sorted(seen, key=lambda s: rank[s])


def test_no_obligation_is_ever_reported_compliant_without_evidence():
    """The load-bearing invariant of the whole module."""
    reg = warranty.register(pipeline.run_case())
    for entry in reg["register"]:
        if entry["state"] == warranty.COMPLIANT:
            assert entry["evidence"], (
                f"{entry['id']} claims COMPLIANT with no evidence attached")


def test_every_in_policy_entry_states_the_consequence_of_breaching_it():
    reg = warranty.register(pipeline.run_case())
    for entry in reg["register"]:
        if entry["state"] != warranty.NOT_IN_POLICY:
            assert entry["consequence"], entry["id"]
            assert entry["why_it_matters"], entry["id"]


def test_the_register_publishes_its_own_discipline_and_the_legal_basis():
    """The reasoning has to travel with the result. A register whose standard of proof is
    invisible cannot be audited."""
    reg = warranty.register(pipeline.run_case())
    assert "UNVERIFIED is the default" in reg["discipline"]
    assert "silence is not compliance" in reg["discipline"].lower()
    assert len(reg["why_this_matters"]["citations"]) >= 2


def test_a_register_with_no_wording_reports_everything_as_not_in_policy():
    """No wording means nothing can be located. It must not become "no obligations", which
    reads as a clean policy."""
    reg = warranty.register({"facts": _Facts({}), "conn": None, "base_doc_id": None})
    assert reg["obligations_in_policy"] == 0
    assert all(e["state"] == warranty.NOT_IN_POLICY for e in reg["register"])
    assert "No warranties" in reg["headline"]


# ── orchestrator integration ─────────────────────────────────────────────────

def test_breaches_are_escalated_but_never_as_confirmed_evidence():
    """A failed date arithmetic is documented fact. The CONSEQUENCE — that cover is void —
    is a legal determination this engine is not entitled to make."""
    signals = warranty.as_external_signals(warranty.register(pipeline.run_case()))
    assert signals
    for s in signals:
        assert s["claim_scope"] == "RISK_SIGNAL"
        assert "does NOT assert that cover is void" in s["asserts"]


def test_unverified_continuing_conditions_are_not_escalated_as_noise():
    """Escalating every unverified housekeeping condition would drown the register and
    train the reader to ignore it."""
    reg = warranty.register(pipeline.run_case())
    signals = warranty.as_external_signals(reg)
    ids = {s["id"] for s in signals}
    for entry in reg["register"]:
        if (entry["state"] == warranty.UNVERIFIED
                and entry["kind"] != warranty.CONDITION_PRECEDENT):
            sid = "warranty-" + entry["id"].lower().replace("_", "-")
            assert sid not in ids, entry["id"]


def test_signals_target_interests_the_orchestrator_actually_recognises():
    for s in warranty.as_external_signals(warranty.register(pipeline.run_case())):
        assert s["insured_interests"], s["id"]
        assert set(s["insured_interests"]) <= {"stock", "plant", "buildings", "premises",
                                               "workforce", "business_interruption"}


# ── the endpoint ─────────────────────────────────────────────────────────────

def test_the_endpoint_serves_the_register_and_reconciles_it(client):
    body = client.get("/api/warranty").json()
    assert body["available"] is True
    assert body["counts"][warranty.BREACHED] == 2
    assert body["orchestration"]["decisions"]


def test_the_endpoint_stops_serving_analysis_after_the_case_is_deleted(client):
    assert client.get("/api/warranty").json()["available"] is True
    client.delete("/api/case")
    after = client.get("/api/warranty").json()
    assert after["available"] is False
    assert after["deleted"] is True
