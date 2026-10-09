"""Value of information — ranking the documents that would collapse uncertainty.

The risk this module carries is that "here is what it costs you not to know" slides
into "here is what we think the number is". Most of these tests defend that line.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from proven import voi
from proven.models import Finding, Operand
from proven.voi import BOUND_ASSUMED, BOUND_EVIDENCED, BOUND_ZERO, UNBOUNDED


# ── minimal builders. voi duck-types both arguments, so these are the contract ──

def _operand(name: str, span_id=None) -> Operand:
    return Operand(name=name, value=None, unit="SGD",
                   span_id=span_id, evidence_class="UNESTABLISHED")


def _missing(rule_id: str, *operand_names: str) -> Finding:
    ops = [_operand(n) for n in operand_names]
    return Finding(rule_id=rule_id, rule_version=1,
                   output_state="INFORMATION_MISSING", evidence_class="",
                   headline="x", operands=ops, required_operands=ops)


def _documented(rule_id: str, *operand_names: str) -> Finding:
    ops = [_operand(n, span_id=f"span-{n}") for n in operand_names]
    return Finding(rule_id=rule_id, rule_version=1,
                   output_state="DOCUMENTED", evidence_class="PROVEN",
                   headline="x", operands=ops, required_operands=ops)


class _Facts:
    """Stands in for PolicyFacts. voi only calls .get(name) and reads
    .established / .value, so this is the whole interface it depends on."""

    def __init__(self, **values):
        self._v = values

    def get(self, name):
        if name not in self._v:
            return None
        return type("F", (), {"established": True, "value": self._v[name]})()


# ── the unbounded-first rule. This is the headline regression. ───────────────

def test_an_unbounded_exposure_ranks_first_even_though_its_width_is_nothing():
    """MEASURED DEFECT: buildings had no sum insured, so no interval could be computed,
    so the gap scored 0% of total uncertainty and sorted LAST — beneath gaps worth tens
    of thousands. That told the business to deal with its largest unknown exposure
    after everything else. An exposure nobody has bounded is a gap of unknown size, not
    a small one."""
    findings = [_missing("R1", "building_value"),
                _missing("R2", "stock_value")]
    facts = _Facts(stock_sum_insured=Decimal("140000"))   # no buildings_sum_insured
    out = voi.rank_information_gaps(findings, facts)

    assert out["gaps"][0]["operand"] == "building_value"
    assert out["gaps"][0]["unbounded"] is True
    assert out["gaps"][0]["uncertainty_removed"] == UNBOUNDED
    assert out["unbounded_count"] == 1


def test_an_unbounded_gap_is_reported_without_a_percentage():
    """A share of a total that excludes the unbounded gap is arithmetic on an
    incomplete denominator, so no percentage is shown at all."""
    out = voi.rank_information_gaps(
        [_missing("R1", "building_value"), _missing("R2", "stock_value")],
        _Facts(stock_sum_insured=Decimal("140000")))
    unb = next(g for g in out["gaps"] if g["unbounded"])
    assert unb["share_of_total"] is None


def test_the_recommendation_calls_an_unbounded_gap_unknown_not_small():
    out = voi.rank_information_gaps(
        [_missing("R1", "building_value")],
        _Facts())
    rec = out["recommendation"]
    assert "unknown rather than small" in rec
    assert "Reinstatement cost assessment" in rec


def test_a_bounded_building_value_is_priced_rather_than_unbounded():
    """Once a sum insured exists the exposure IS bounded, and it must stop jumping the
    queue on a technicality."""
    out = voi.rank_information_gaps(
        [_missing("R1", "building_value")],
        _Facts(buildings_sum_insured=Decimal("500000")))
    g = out["gaps"][0]
    assert g["unbounded"] is False
    assert g["interval"]["high"] == "500000"
    assert g["interval"]["high_basis"] == BOUND_EVIDENCED
    assert g["interval"]["low_basis"] == BOUND_ZERO


# ── statutory consequences are not payout intervals ─────────────────────────

@pytest.mark.parametrize("operand", ["contributor_count", "declared_headcount"])
def test_a_statutory_gap_gets_no_interval_and_is_not_called_unbounded(operand):
    """A missing CPF record matters, but its consequence is a penalty and a 21-day
    deadline, not a range of possible payouts. Inventing an interval would be a
    fabricated number; calling it UNBOUNDED would push a statutory item above a real
    monetary exposure. Neither is right, so it is ranked with no interval."""
    out = voi.rank_information_gaps([_missing("R1", operand)], _Facts())
    g = out["gaps"][0]
    assert g["interval"] is None
    assert g["unbounded"] is False
    assert g["uncertainty_removed"] == "0"
    assert "statutory" in g["note"].lower()


def test_a_statutory_gap_never_outranks_a_measured_monetary_one():
    out = voi.rank_information_gaps(
        [_missing("R1", "contributor_count"), _missing("R2", "stock_value")],
        _Facts(stock_sum_insured=Decimal("140000")))
    order = [g["operand"] for g in out["gaps"]]
    assert order.index("stock_value") < order.index("contributor_count")


# ── every bound says where it came from ─────────────────────────────────────

def test_an_assumed_bound_is_labelled_assumed():
    """Stock's upper bound is a plausible multiple of the sum insured, not a reading
    from any document. Presenting it as evidenced would be the exact failure this
    project exists to avoid."""
    out = voi.rank_information_gaps(
        [_missing("R1", "stock_value")], _Facts(stock_sum_insured=Decimal("140000")))
    iv = out["gaps"][0]["interval"]
    assert iv["high_basis"] == BOUND_ASSUMED
    assert iv["high"] == "280000"          # 140000 x the declared factor of 2


def test_every_interval_bound_carries_a_recognised_basis():
    out = voi.rank_information_gaps(
        [_missing("R1", "building_value", "total_plant_value",
                  "stock_value", "endorsement_effect")],
        _Facts(buildings_sum_insured=Decimal("500000"),
               plant_sum_insured=Decimal("180000"),
               stock_sum_insured=Decimal("140000")))
    allowed = {BOUND_EVIDENCED, BOUND_ASSUMED, BOUND_ZERO}
    for g in out["gaps"]:
        if g["interval"]:
            assert g["interval"]["low_basis"] in allowed
            assert g["interval"]["high_basis"] in allowed


def test_the_caveat_states_what_the_labels_mean_and_that_findings_do_not_change():
    out = voi.rank_information_gaps([_missing("R1", "stock_value")],
                                    _Facts(stock_sum_insured=Decimal("140000")))
    caveat = out["caveat"]
    assert BOUND_ASSUMED in caveat
    assert UNBOUNDED in caveat
    assert "remain UNESTABLISHED" in caveat
    assert "not a prediction" in out["method"]


def test_no_gap_ever_states_a_value_for_the_missing_figure():
    """The output is the size of the consequence, never a guess at the operand. A key
    that looks like an estimate must not appear."""
    out = voi.rank_information_gaps(
        [_missing("R1", "building_value", "stock_value")],
        _Facts(stock_sum_insured=Decimal("140000")))
    for g in out["gaps"]:
        assert not any(k in g for k in
                       ("estimate", "estimated_value", "likely_value", "value"))


# ── what does and does not count as a gap ───────────────────────────────────

def test_an_evidenced_finding_contributes_no_gap():
    out = voi.rank_information_gaps(
        [_documented("R1", "building_value")], _Facts())
    assert out["count"] == 0
    assert "every required operand is evidenced" in out["recommendation"]


def test_an_operand_that_already_has_a_span_is_skipped_within_a_missing_finding():
    """A finding can be INFORMATION_MISSING because of one operand while others are
    evidenced. Only the unevidenced ones are gaps."""
    ops = [_operand("building_value", span_id="span-1"), _operand("stock_value")]
    f = Finding(rule_id="R1", rule_version=1, output_state="INFORMATION_MISSING",
                evidence_class="", headline="x", operands=ops, required_operands=ops)
    out = voi.rank_information_gaps([f], _Facts(stock_sum_insured=Decimal("1000")))
    assert [g["operand"] for g in out["gaps"]] == ["stock_value"]


def test_the_same_operand_across_two_findings_is_reported_once():
    out = voi.rank_information_gaps(
        [_missing("R1", "stock_value"), _missing("R2", "stock_value")],
        _Facts(stock_sum_insured=Decimal("140000")))
    assert [g["operand"] for g in out["gaps"]] == ["stock_value"]


def test_an_unknown_operand_is_ignored_rather_than_invented():
    """A new rule can name an operand this module has no source for. It must not
    produce a gap advising a document nobody named."""
    out = voi.rank_information_gaps(
        [_missing("R1", "some_operand_nobody_mapped")], _Facts())
    assert out["count"] == 0


def test_no_findings_at_all_is_handled():
    out = voi.rank_information_gaps([], None)
    assert out["count"] == 0
    assert out["total_uncertainty"] == "0"
    json.dumps(out)


def test_facts_of_none_degrades_to_unbounded_rather_than_crashing():
    out = voi.rank_information_gaps([_missing("R1", "stock_value")], None)
    assert out["gaps"][0]["unbounded"] is True


# ── the arithmetic and the evidence-class promise ───────────────────────────

def test_shares_are_computed_over_the_bounded_total_only():
    out = voi.rank_information_gaps(
        [_missing("R1", "building_value", "stock_value")],
        _Facts(buildings_sum_insured=Decimal("500000"),
               stock_sum_insured=Decimal("140000")))
    assert out["total_uncertainty"] == "780000"          # 500000 + 280000
    priced = [g for g in out["gaps"] if g["interval"]]
    assert sum(int(g["share_of_total"].rstrip("%")) for g in priced) == pytest.approx(
        100, abs=1)


def test_gaps_are_ordered_by_descending_measured_exposure():
    out = voi.rank_information_gaps(
        [_missing("R1", "building_value", "stock_value", "total_plant_value")],
        _Facts(buildings_sum_insured=Decimal("500000"),
               stock_sum_insured=Decimal("140000"),
               plant_sum_insured=Decimal("180000")))
    widths = [Decimal(g["interval"]["width"]) for g in out["gaps"] if g["interval"]]
    assert widths == sorted(widths, reverse=True)


def test_a_self_authored_document_can_only_reach_declared():
    """The source hierarchy is the spine of the whole product: the evidence class
    follows from who authored the document, not from how useful it would be. Stock is
    evidenced by the insured's own accounts, so it can never become PROVEN."""
    out = voi.rank_information_gaps(
        [_missing("R1", "stock_value", "building_value")],
        _Facts(stock_sum_insured=Decimal("140000"),
               buildings_sum_insured=Decimal("500000")))
    by_op = {g["operand"]: g for g in out["gaps"]}
    assert by_op["stock_value"]["author_class"] == "SELF"
    assert by_op["stock_value"]["becomes_class"] == "DECLARED"
    assert by_op["building_value"]["author_class"] == "THIRD_PARTY"
    assert by_op["building_value"]["becomes_class"] == "PROVEN"


def test_every_gap_names_a_document_and_a_kind():
    out = voi.rank_information_gaps(
        [_missing("R1", *voi._OPERAND_SOURCES.keys())],
        _Facts(buildings_sum_insured=Decimal("500000"),
               plant_sum_insured=Decimal("180000"),
               stock_sum_insured=Decimal("140000")))
    assert out["count"] == len(voi._OPERAND_SOURCES)
    for g in out["gaps"]:
        assert g["document_needed"].strip()
        assert g["document_kind"].strip()
        assert g["becomes_class"] in {"PROVEN", "DECLARED"}


def test_ranking_is_deterministic():
    args = ([_missing("R1", "building_value", "stock_value", "total_plant_value")],
            _Facts(stock_sum_insured=Decimal("140000"),
                   plant_sum_insured=Decimal("180000")))
    first = voi.rank_information_gaps(*args)
    for _ in range(3):
        assert voi.rank_information_gaps(*args) == first
