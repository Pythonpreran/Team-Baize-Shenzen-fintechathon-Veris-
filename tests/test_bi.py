"""Business interruption adequacy — the tests that matter.

The valuable assertions here are not "it computes a number". They are the three places
this module could quietly become dishonest:

  1. It must ABSTAIN on the sum insured when gross profit is not on record, rather than
     deriving one from a balance sheet that does not contain it.
  2. It must not re-emit the absence finding the deterministic rule engine already
     reports, because counting one document twice inflates orchestrator priority on false
     corroboration.
  3. The recovery-time benchmark is a DECLARED assumption and must never be used to size a
     sum insured — only to question an indemnity period.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from proven import bi, pipeline
from proven.api import SESSIONS, app


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


# ── test doubles matching the PolicyFacts / Fact shape ───────────────────────

class _Doc:
    def __init__(self, kind):
        self.id = "doc-test"
        self.kind = kind


class _Fact:
    def __init__(self, value, kind="SCHEDULE", page=1, needle="n", established=True):
        self.value = value
        self.established = established
        self.page_no = page
        self.needle = needle
        self.author_class = "INSURER"
        self.doc = _Doc(kind)


class _Facts(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


def _facts(**kw):
    return _Facts(kw)


# ── the pure arithmetic ──────────────────────────────────────────────────────

def test_required_sum_insured_scales_with_the_indemnity_period():
    """The sum insured must cover the WHOLE period, not one year. An 18-month period on
    310k of gross profit needs 465k, and a policy sized at one year's profit is short by
    a third on the day it starts."""
    calc = bi.required_sum_insured(Decimal("310000"), 18)
    assert calc["required_sum_insured"] == "465000.00"
    assert calc["indemnity_months"] == 18


def test_required_sum_insured_applies_forward_growth():
    """Gross profit must be forward-looking. Sizing on last year's figure is the documented
    reason average bites on BI claims however careful the forecast was."""
    calc = bi.required_sum_insured(Decimal("310000"), 18, Decimal("8"))
    assert calc["required_sum_insured"] == "502200.00"


def test_a_twelve_month_period_needs_exactly_one_year_of_gross_profit():
    calc = bi.required_sum_insured(Decimal("250000"), 12)
    assert calc["required_sum_insured"] == "250000.00"


@pytest.mark.parametrize("gp,months", [(Decimal("0"), 12), (Decimal("-1"), 12),
                                       (Decimal("100"), 0), (Decimal("100"), -6)])
def test_the_arithmetic_refuses_impossible_inputs(gp, months):
    """A zero or negative operand is a document we misread, not a finding. Raise rather
    than emit a confident wrong number."""
    with pytest.raises(ValueError):
        bi.required_sum_insured(gp, months)


# ── the recovery benchmark is a declared assumption ──────────────────────────

def test_manufacturing_gets_a_longer_recovery_estimate_than_an_office():
    factory, _ = bi.recovery_benchmark("manufacture of prepared meals and sauces")
    office, _ = bi.recovery_benchmark("management consultancy services")
    assert factory > office


def test_an_unrecognised_activity_falls_back_to_the_declared_default():
    months, rationale = bi.recovery_benchmark("something nobody has classified")
    assert months == bi.DEFAULT_RECOVERY_MONTHS
    assert "default" in rationale.lower()


def test_the_benchmark_is_labelled_as_declared_everywhere_it_appears():
    """A planning assumption presented as a measurement is the failure this system exists
    to refuse. The label has to travel with the number."""
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY"], needle="sections:"),
        business_description=_Fact("manufacture of sauces", needle="desc:"),
        scheduled_premises=_Fact(["Unit 1"], needle="prem:"))}
    result = bi.assess(case)
    assert "DECLARED" in result["recovery_benchmark"]["basis"]
    assert "never" in result["recovery_benchmark"]["basis"].lower()


# ── absence: provable from one document ──────────────────────────────────────

def test_absence_of_bi_cover_on_a_premises_business_is_a_high_finding():
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "PUBLIC_AND_PRODUCTS_LIABILITY"],
                                 needle="sections:"),
        business_description=_Fact("manufacture of prepared meals", needle="desc:"),
        scheduled_premises=_Fact(["Unit 03-11"], needle="prem:"))}
    result = bi.assess(case)
    assert result["cover_present"] is False
    assert result["highest_severity"] == "HIGH"
    finding = next(f for f in result["findings"] if f["key"] == "NO_BI_COVER")
    assert finding["basis"] == "PROVEN_FROM_DOCUMENT"
    assert finding["operands"], "an absence finding must still cite the document"


def test_a_policy_naming_the_section_consequential_loss_is_not_reported_as_absent():
    """The rule engine matches the single literal "INTERRUPTION". A carrier that calls the
    section Consequential Loss would be reported as having no cover at all — which is a
    wrong finding on a screen that leads to money. Matching the family fixes it."""
    for name in ("CONSEQUENTIAL_LOSS", "LOSS_OF_PROFITS", "BUSINESS_INCOME",
                 "TIME_ELEMENT"):
        case = {"facts": _facts(
            operative_sections=_Fact(["PROPERTY", name], needle="sections:"),
            business_description=_Fact("retail shop", needle="desc:"),
            scheduled_premises=_Fact(["Unit 1"], needle="prem:"))}
        assert bi.assess(case)["cover_present"] is True, name


def test_no_property_section_makes_absent_bi_a_low_finding_not_a_high_one():
    """A business with nothing physical to damage may have declined BI deliberately.
    Reporting that as HIGH would be crying wolf."""
    case = {"facts": _facts(
        operative_sections=_Fact(["PROFESSIONAL_INDEMNITY"], needle="sections:"),
        business_description=_Fact("software consultancy", needle="desc:"))}
    result = bi.assess(case)
    assert result["highest_severity"] == "LOW"


def test_it_abstains_entirely_when_the_schedule_is_not_on_record():
    result = bi.assess({"facts": _facts()})
    assert result["abstained"] is True
    assert result["cover_present"] is None
    assert "operative_sections" in result["missing"]


# ── adequacy: abstention is the important behaviour ──────────────────────────

def test_it_abstains_on_the_sum_insured_when_gross_profit_is_absent():
    """THE test in this file. A balance sheet has no gross profit, and the engine must not
    invent one from stock, receivables or cash."""
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "BUSINESS_INTERRUPTION"],
                                 needle="sections:"),
        business_description=_Fact("retail", needle="desc:"),
        bi_sum_insured=_Fact(Decimal("180000"), needle="bi_si: 180000"),
        indemnity_period_months=_Fact(Decimal("12"), needle="indemnity: 12"))}
    result = bi.assess(case)
    keys = {f["key"] for f in result["findings"]}
    assert "BI_SUM_INSURED_SHORT" not in keys
    assert "BI_SUM_INSURED_ADEQUATE" not in keys
    abst = next(a for a in result["abstentions"]
                if "sum insured" in a["question"].lower())
    assert abst["abstained"] is True
    assert "PROFIT_AND_LOSS" in abst["resolved_by"]


def test_turnover_is_not_silently_substituted_for_gross_profit():
    """They are not interchangeable. Using turnover would overstate the requirement, and
    an overstatement is still a wrong number."""
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "BUSINESS_INTERRUPTION"],
                                 needle="sections:"),
        business_description=_Fact("retail", needle="desc:"),
        bi_sum_insured=_Fact(Decimal("180000"), needle="bi_si: 180000"),
        indemnity_period_months=_Fact(Decimal("12"), needle="indemnity: 12"),
        turnover=_Fact(Decimal("900000"), kind="ACCOUNTS", needle="turnover: 900000"))}
    result = bi.assess(case)
    assert not any(f["key"].startswith("BI_SUM_INSURED")
                   for f in result["findings"])
    abst = next(a for a in result["abstentions"]
                if "sum insured" in a["question"].lower())
    assert "not interchangeable" in (abst.get("note_on_turnover") or "")


def test_it_abstains_on_the_sum_insured_when_the_indemnity_period_is_absent():
    """The requirement scales with the period. Assuming twelve months is precisely the
    assumption that leaves real policies short."""
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "BUSINESS_INTERRUPTION"],
                                 needle="sections:"),
        business_description=_Fact("retail", needle="desc:"),
        bi_sum_insured=_Fact(Decimal("180000"), needle="bi_si: 180000"),
        gross_profit=_Fact(Decimal("310000"), kind="ACCOUNTS", needle="gp: 310000"))}
    result = bi.assess(case)
    assert not any(f["key"].startswith("BI_SUM_INSURED")
                   for f in result["findings"])


def test_a_short_sum_insured_is_computed_and_fully_sourced():
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "BUSINESS_INTERRUPTION"],
                                 needle="sections:"),
        business_description=_Fact("retail shop", needle="desc:"),
        scheduled_premises=_Fact(["Unit 1"], needle="prem:"),
        bi_sum_insured=_Fact(Decimal("180000"), needle="bi_si: 180000"),
        indemnity_period_months=_Fact(Decimal("12"), needle="indemnity: 12"),
        gross_profit=_Fact(Decimal("310000"), kind="ACCOUNTS", needle="gp: 310000"))}
    result = bi.assess(case)
    finding = next(f for f in result["findings"] if f["key"] == "BI_SUM_INSURED_SHORT")
    assert finding["basis"] == "COMPUTED"
    assert finding["numbers"]["shortfall"] == "130000.00"
    assert finding["numbers"]["adequacy_pct"] == "58.1"
    assert len(finding["operands"]) == 3, "every operand must be cited"


def test_an_adequate_sum_insured_is_not_reported_as_a_problem():
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "BUSINESS_INTERRUPTION"],
                                 needle="sections:"),
        business_description=_Fact("management consultancy", needle="desc:"),
        bi_sum_insured=_Fact(Decimal("400000"), needle="bi_si: 400000"),
        indemnity_period_months=_Fact(Decimal("12"), needle="indemnity: 12"),
        gross_profit=_Fact(Decimal("310000"), kind="ACCOUNTS", needle="gp: 310000"))}
    result = bi.assess(case)
    keys = {f["key"] for f in result["findings"]}
    assert "BI_SUM_INSURED_SHORT" not in keys
    assert "BI_SUM_INSURED_ADEQUATE" in keys


# ── the indemnity period is its own independent failure ─────────────────────

def test_a_short_indemnity_period_is_flagged_even_when_the_sum_insured_is_fine():
    """The two fail independently, and the period is the one nobody checks."""
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "BUSINESS_INTERRUPTION"],
                                 needle="sections:"),
        business_description=_Fact("manufacture of prepared meals", needle="desc:"),
        bi_sum_insured=_Fact(Decimal("900000"), needle="bi_si: 900000"),
        indemnity_period_months=_Fact(Decimal("6"), needle="indemnity: 6"),
        gross_profit=_Fact(Decimal("310000"), kind="ACCOUNTS", needle="gp: 310000"))}
    result = bi.assess(case)
    finding = next(f for f in result["findings"] if f["key"] == "INDEMNITY_TOO_SHORT")
    assert finding["numbers"]["uninsured_months"] == 12
    assert finding["basis"] == "DECLARED_ASSUMPTION", (
        "a finding resting on a planning assumption must say so")


# ── orchestrator integration: no double counting ─────────────────────────────

def test_absence_is_not_re_emitted_as_a_signal_because_the_rule_engine_owns_it():
    """The engine's BUSINESS-INTERRUPTION-NOT-CONFIRMED rule already raises absence.
    Emitting it again would make one document look like two agreeing sources, which is
    exactly what the orchestrator's overlap logic is meant to detect."""
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY"], needle="sections:"),
        business_description=_Fact("manufacture of sauces", needle="desc:"),
        scheduled_premises=_Fact(["Unit 1"], needle="prem:"))}
    result = bi.assess(case)
    assert any(f["key"] == "NO_BI_COVER" for f in result["findings"])
    assert bi.as_external_signals(result) == []


def test_the_new_findings_are_emitted_with_a_capped_claim_scope():
    """These lean on a declared benchmark and a forward projection. They may raise
    attention; they may never assert that cover has failed."""
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "BUSINESS_INTERRUPTION"],
                                 needle="sections:"),
        business_description=_Fact("manufacture of prepared meals", needle="desc:"),
        bi_sum_insured=_Fact(Decimal("100000"), needle="bi_si: 100000"),
        indemnity_period_months=_Fact(Decimal("6"), needle="indemnity: 6"),
        gross_profit=_Fact(Decimal("310000"), kind="ACCOUNTS", needle="gp: 310000"))}
    signals = bi.as_external_signals(bi.assess(case))
    assert signals
    for s in signals:
        assert s["claim_scope"] == "RISK_SIGNAL"
        # An interest the orchestrator does not recognise is silently dropped, so an
        # unknown one here would be an invisible no-op rather than a loud failure.
        assert s["insured_interests"] == ["business_interruption"]


def test_an_adequate_policy_emits_nothing_to_escalate():
    case = {"facts": _facts(
        operative_sections=_Fact(["PROPERTY", "BUSINESS_INTERRUPTION"],
                                 needle="sections:"),
        business_description=_Fact("management consultancy", needle="desc:"),
        bi_sum_insured=_Fact(Decimal("400000"), needle="bi_si: 400000"),
        indemnity_period_months=_Fact(Decimal("12"), needle="indemnity: 12"),
        gross_profit=_Fact(Decimal("310000"), kind="ACCOUNTS", needle="gp: 310000"))}
    assert bi.as_external_signals(bi.assess(case)) == []


# ── against the real demo case ───────────────────────────────────────────────

def test_the_demo_case_has_no_bi_cover_and_says_so_from_the_schedule():
    """Hock Lee manufactures food from an insured premises and carries no BI section at
    all. That is the ABI finding in miniature: a premises business with no cover for the
    trading it would lose."""
    result = bi.assess(pipeline.run_case())
    assert result["cover_present"] is False
    assert result["highest_severity"] == "HIGH"
    finding = next(f for f in result["findings"] if f["key"] == "NO_BI_COVER")
    assert finding["basis"] == "PROVEN_FROM_DOCUMENT"
    assert result["recovery_benchmark"]["months"] == 18


def test_the_demo_case_abstains_on_adequacy_rather_than_inventing_gross_profit():
    result = bi.assess(pipeline.run_case())
    assert result["abstentions"]
    assert not any(f["key"].startswith("BI_SUM_INSURED")
                   for f in result["findings"])


def test_the_endpoint_serves_the_assessment_and_survives_case_deletion(client):
    body = client.get("/api/bi").json()
    assert body["available"] is True
    assert body["cover_present"] is False
    client.delete("/api/case")
    after = client.get("/api/bi").json()
    assert after["available"] is False
    assert after["deleted"] is True
