"""Tests for the second-round features: wired resolver, seeded record, quoted
unaccounted provisions, broadened never-enforcement, and the cross-carrier compare."""
import pytest

from proven import brief, pipeline
from proven.models import Finding


def _run():
    return pipeline.run_case()


# ── resolver wired + market-convention precedence note ──────────────────────

def test_precedence_note_surfaced_for_chubb():
    b = _run()["brief"]
    assert b["accounting"]["has_construction_clause"] is False
    assert b["precedence_note"]
    assert "market convention" in b["precedence_note"].lower()


# ── record seeded by the pipeline (demo beat 8) ─────────────────────────────

def test_pipeline_seeds_portable_record():
    r = _run()
    n = pipeline.count_record_facts(r["conn"], r["business_id"])
    assert n >= 4                                  # headcount, ssic, use, carrier...
    rows = r["conn"].execute(
        "SELECT attribute FROM record_fact WHERE business_id=?",
        (r["business_id"],)).fetchall()
    attrs = {row["attribute"] for row in rows}
    assert "employee_headcount" in attrs
    assert "policy_carrier" in attrs


def test_deleting_case_leaves_seeded_record_intact():
    r = _run()
    conn, case_id, business_id = r["conn"], r["case_id"], r["business_id"]
    before = pipeline.count_record_facts(conn, business_id)
    assert before > 0
    pipeline.delete_case(conn, case_id)
    assert pipeline.count_findings(conn, case_id) == 0
    assert pipeline.count_record_facts(conn, business_id) == before


# ── unaccounted provisions are quoted (bounded omission, §5.2) ──────────────

def test_unaccounted_provisions_are_quoted():
    b = _run()["brief"]
    quotes = b["accounting"]["unaccounted_quotes"]
    assert quotes, "expected some unaccounted provisions"
    assert all(q.get("quote") for q in quotes)
    assert b["accounting"]["detection_note"]
    # duplicates are reported, not silently dropped
    assert b["accounting"]["duplicates_collapsed"] >= 0


# ── all four output states are exercised ────────────────────────────────────

def test_all_four_output_states_exercised():
    b = _run()["brief"]
    states = {f["state"] for f in b["findings"]}
    assert {"DOCUMENTED", "NOT_CONFIRMED", "INFORMATION_MISSING",
            "REQUIRES_AUTHORISED_REVIEW"} <= states


def test_new_rules_present_and_graded():
    b = _run()["brief"]
    by_rule = {f["rule_id"]: f for f in b["findings"]}
    assert by_rule["WORKPASS-MEDICAL-BELOW-MINIMUM"]["class"] == "PROVEN"
    assert by_rule["ACTIVITY-SSIC-NOT-DECLARED"]["state"] \
        == "REQUIRES_AUTHORISED_REVIEW"
    assert by_rule["PLANT-ADEQUACY-UNESTABLISHED"]["class"] == "UNESTABLISHED"
    assert by_rule["BUSINESS-INTERRUPTION-NOT-CONFIRMED"]["state"] == "NOT_CONFIRMED"


# ── broadened never:[assert_breach] enforcement ─────────────────────────────

def test_never_enforcement_catches_breach_in_detail():
    """The check runs over detail fields, not only the headline."""
    f = Finding(
        rule_id="X", rule_version=1, output_state="DOCUMENTED",
        evidence_class="PROVEN", headline="A neutral headline.",
        never=["assert_breach"],
        detail={"consequence_note": "The policy is void and the claim will fail."})
    with pytest.raises(ValueError):
        brief._finding_view(f)


def test_never_enforcement_allows_neutral_detail():
    f = Finding(
        rule_id="X", rule_version=1, output_state="REQUIRES_AUTHORISED_REVIEW",
        evidence_class="PROVEN", headline="We locate and quote; we do not conclude.",
        never=["assert_breach"],
        detail={"consequence_note": "Route to a licensed adviser."})
    view = brief._finding_view(f)
    assert view["state"] == "REQUIRES_AUTHORISED_REVIEW"


# ── cross-carrier divergence, live ──────────────────────────────────────────

# Divergence is read out of three published wordings held in the corpus.
@pytest.mark.requires_corpus("sg-chubb-selectplus-wording",
                             "sg-msig-sumo-wording",
                             "sg-liberty-smecare-wording")
def test_cross_carrier_divergence_is_computed():
    c = pipeline.compare_carriers()
    by = {r["carrier"]: r for r in c["carriers"]}
    assert "CHUBB" in by
    # Chubb states a threshold; MSIG/Liberty bite at any shortfall
    assert by["CHUBB"]["threshold_basis"] == "THRESHOLD_STATED"
    if "MSIG" in by:
        assert by["MSIG"]["threshold_basis"] == "NO_THRESHOLD_STATED"
    # the formula class diverges by carrier, decided by the document
    assert by["CHUBB"]["formula_basis_class"] == "MARKET_CONVENTION"
    if "LIBERTY" in by:
        assert by["LIBERTY"]["formula_basis_class"] == "PROVEN"
        assert by["LIBERTY"]["formula_quote"]        # a real anchored span


# ── gate 2 (boundary) genuinely runs in the pipeline ────────────────────────

def test_gate4_fires_on_real_data_precedence_tie():
    """Definition of done item 5: the fourth abstention gate (authority) fires on
    real fixture data, not only in a unit test. Two endorsements amend the same
    sub-limit at the same rank -> the resolver reports the tie and the finding is
    escalated rather than resolved."""
    r = _run()
    assert "LIMIT.SUB_LIMIT" in r["resolution"].unresolved
    by_rule = {f["rule_id"]: f for f in r["brief"]["findings"]}
    conflict = by_rule["SUBLIMIT-CONFLICTING-ENDORSEMENTS"]
    assert conflict["state"] == "REQUIRES_AUTHORISED_REVIEW"
    assert len(conflict["spans"]) == 2       # both endorsements located and quoted


def test_theft_stays_documented_not_escalated_by_boundary_gate():
    """The theft finding's span sits under cl 5.1(c); gate 2 must not escalate it
    even though 5.1(c)(i) is a sub-item ref, and the average finding's 4.2 span
    (p15) must attribute to Average, not the reused 4.2 exclusion (p9)."""
    b = _run()["brief"]
    by_rule = {f["rule_id"]: f for f in b["findings"]}
    assert by_rule["THEFT-FORCIBLE-ENTRY-NOT-WRITTEN-BACK"]["state"] == "DOCUMENTED"
    assert by_rule["AVERAGE-ADEQUACY-BELOW-THRESHOLD"]["state"] == "DOCUMENTED"
