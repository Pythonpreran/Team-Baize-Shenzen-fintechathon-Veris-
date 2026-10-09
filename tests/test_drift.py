"""The drift clock — adequacy decaying while nothing about the policy changes.

Two things can go wrong here and both are serious: presenting sample numbers as a
published series, and applying one carrier's threshold to another carrier's policy.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from proven import drift
from proven.drift import INDEX_PUBLISHED, INDEX_SAMPLE


class _Facts:
    """Stands in for PolicyFacts; drift only calls .get() and reads
    .established / .value."""

    def __init__(self, **values):
        self._v = values

    def get(self, name):
        if name not in self._v:
            return None
        return type("F", (), {"established": True, "value": self._v[name]})()


SI = Decimal("180000")


# ── the sample series must never be mistaken for a published one ────────────

def test_every_shipped_index_declares_itself_as_sample_data():
    for key, index in drift.INDICES.items():
        assert index.basis == INDEX_SAMPLE, f"{key} is not labelled as sample data"
        assert "SAMPLE" in index.source_note.upper()


def test_the_basis_travels_on_every_computed_result():
    """A crossing date is the most quotable output in the product. The label has to be
    in the payload, because whoever screenshots it will not read the module docstring."""
    res = drift.compute_drift("Plant", SI, SI, Decimal("0.85"),
                              drift.SAMPLE_CONSTRUCTION_INDEX)
    d = res.as_dict()
    assert d["index_basis"] == INDEX_SAMPLE
    assert "DECLARED SAMPLE DATA" in d["caveat"]
    assert "SAMPLE" in d["source_note"].upper()


def test_the_caveat_changes_when_the_series_is_published():
    """The interface a real series would populate: swapping the basis must change what
    the caveat claims, or the label is decorative."""
    published = drift.CostIndex(
        name="real", unit="index", basis=INDEX_PUBLISHED, source_note="published",
        points=drift.SAMPLE_CONSTRUCTION_INDEX.points)
    res = drift.compute_drift("Plant", SI, SI, Decimal("0.85"), published)
    assert "DECLARED SAMPLE DATA" not in res.caveat
    assert "a published series" in res.caveat


# ── the threshold is the carrier's own, never a market default ──────────────

def test_no_stated_threshold_crosses_earlier_than_an_eighty_five_percent_cushion():
    """The core reason this cannot use one market number. MSIG's clause states no
    threshold, so any shortfall engages it; Chubb's states 85%. Defaulting MSIG to
    Chubb's figure would tell an MSIG insured they were safe for three more quarters
    than their own wording allows."""
    index = drift.SAMPLE_CONSTRUCTION_INDEX
    no_threshold = drift.compute_drift("Plant", SI, SI, None, index)
    cushioned = drift.compute_drift("Plant", SI, SI, Decimal("0.85"), index)

    assert no_threshold.crossed_on is not None
    assert cushioned.crossed_on is not None
    assert no_threshold.crossed_on < cushioned.crossed_on
    assert no_threshold.threshold_basis == "NO_THRESHOLD_STATED"
    assert cushioned.threshold_basis == "THRESHOLD_STATED"
    assert no_threshold.threshold_pct is None


def test_a_no_threshold_result_never_prints_a_percentage_it_does_not_have():
    res = drift.compute_drift("Plant", SI, SI, None,
                              drift.SAMPLE_CONSTRUCTION_INDEX)
    d = res.as_dict()
    assert d["threshold_pct"] is None
    assert "85" not in d["headline"]
    assert "any shortfall at all" in d["headline"]


def test_a_stated_threshold_is_named_in_the_headline():
    res = drift.compute_drift("Plant", SI, SI, Decimal("0.85"),
                              drift.SAMPLE_CONSTRUCTION_INDEX)
    assert "85% threshold" in res.headline


# ── the arithmetic ──────────────────────────────────────────────────────────

def test_adequacy_is_whole_at_inception_and_not_engaged():
    """The drift argument is about MOVEMENT. If it fired at inception it would be
    reporting a pre-existing shortfall, which is a different finding."""
    for threshold in (None, Decimal("0.85")):
        res = drift.compute_drift("Plant", SI, SI, threshold,
                                  drift.SAMPLE_CONSTRUCTION_INDEX)
        first = res.observations[0]
        assert first.adequacy_ratio == "100.0%"
        assert first.engaged is False


def test_adequacy_falls_monotonically_as_the_index_rises():
    res = drift.compute_drift("Plant", SI, SI, Decimal("0.85"),
                              drift.SAMPLE_CONSTRUCTION_INDEX)
    ratios = [float(o.adequacy_ratio.rstrip("%")) for o in res.observations]
    assert ratios == sorted(ratios, reverse=True)
    assert ratios[-1] < ratios[0]


def test_the_sum_insured_is_held_constant_across_the_whole_series():
    """The entire point: the policy did not change. If the sum insured moved, the
    finding would not be about drift."""
    res = drift.compute_drift("Plant", SI, SI, Decimal("0.85"),
                              drift.SAMPLE_CONSTRUCTION_INDEX)
    assert res.sum_insured == str(SI)
    implied = [Decimal(o.implied_value_at_risk) for o in res.observations]
    assert implied[0] == SI
    assert all(b >= a for a, b in zip(implied, implied[1:]))


def test_the_crossing_date_is_the_first_engaged_observation():
    res = drift.compute_drift("Plant", SI, SI, Decimal("0.85"),
                              drift.SAMPLE_CONSTRUCTION_INDEX)
    engaged = [o for o in res.observations if o.engaged]
    assert engaged, "nothing engaged, so the crossing test proves nothing"
    assert res.crossed_on == engaged[0].as_at
    # and once engaged it stays engaged on a monotone series
    assert all(o.engaged for o in res.observations
               if o.as_at >= res.crossed_on)


def test_nothing_beyond_the_last_observation_is_projected():
    """A forecast would be a prediction dressed as arithmetic."""
    index = drift.SAMPLE_CONSTRUCTION_INDEX
    res = drift.compute_drift("Plant", SI, SI, Decimal("0.85"), index)
    assert len(res.observations) == len(index.points)
    assert res.observations[-1].as_at == index.last.as_at.isoformat()
    assert "No value beyond the last observation is projected" in res.caveat


def test_a_generous_sum_insured_produces_no_crossing_and_says_so():
    """A clean result must be stated as a clean result, not left blank."""
    res = drift.compute_drift("Plant", Decimal("400000"), Decimal("180000"),
                              Decimal("0.85"), drift.SAMPLE_CONSTRUCTION_INDEX)
    assert res.crossed_on is None
    assert "No drift crossing" in res.headline
    assert not any(o.engaged for o in res.observations)


def test_the_equipment_series_moves_faster_than_the_construction_series():
    """They are shaped from different real-world movements; identical behaviour would
    mean the index argument is not actually being used."""
    plant_c = drift.compute_drift("Plant", SI, SI, Decimal("0.85"),
                                  drift.SAMPLE_CONSTRUCTION_INDEX)
    plant_e = drift.compute_drift("Plant", SI, SI, Decimal("0.85"),
                                  drift.SAMPLE_EQUIPMENT_INDEX)
    assert plant_e.crossed_on < plant_c.crossed_on


def test_value_at_returns_the_last_observation_on_or_before_a_date():
    index = drift.SAMPLE_CONSTRUCTION_INDEX
    assert index.value_at(date(2026, 3, 1)) == Decimal("100.0")
    assert index.value_at(date(2026, 4, 15)) == Decimal("100.0")   # no interpolation
    assert index.value_at(date(2026, 6, 1)) == Decimal("104.2")
    assert index.value_at(date(2025, 1, 1)) is None                # before the series


def test_as_dict_is_json_safe():
    res = drift.compute_drift("Plant", SI, SI, None,
                             drift.SAMPLE_CONSTRUCTION_INDEX)
    json.dumps(res.as_dict())


# ── the case-level entry point ──────────────────────────────────────────────

def test_no_sum_insured_means_unavailable_rather_than_a_fabricated_view():
    """A demo-shaped default here would be a number with no document behind it."""
    out = drift.drift_for_case(facts=_Facts(), carrier="CHUBB")
    assert out["available"] is False
    assert "policy schedule" in out["reason"]


def test_facts_of_none_is_also_unavailable():
    out = drift.drift_for_case(facts=None, carrier="CHUBB")
    assert out["available"] is False


def test_plant_is_preferred_over_stock_and_the_item_is_named():
    out = drift.drift_for_case(
        facts=_Facts(plant_sum_insured=Decimal("180000"),
                     stock_sum_insured=Decimal("140000")),
        carrier="CHUBB")
    assert out["available"] is True
    assert out["item"] == "Plant, machinery and equipment"
    assert out["sum_insured"] == "180000"


def test_stock_is_used_when_no_plant_is_established():
    out = drift.drift_for_case(facts=_Facts(stock_sum_insured=Decimal("140000")),
                               carrier="CHUBB")
    assert out["available"] is True
    assert out["item"] == "Stock in trade"


def test_the_case_view_states_its_full_insurance_assumption():
    """Assuming full insurance at inception is what isolates drift from a pre-existing
    shortfall. Unstated, it would look like a claim that the sum insured was correct."""
    out = drift.drift_for_case(facts=_Facts(plant_sum_insured=SI), carrier="CHUBB")
    assert "fully insured at inception" in out["assumption"]
    assert "reported separately" in out["assumption"]


def test_the_case_view_carries_the_carrier_and_the_sample_label():
    out = drift.drift_for_case(facts=_Facts(plant_sum_insured=SI), carrier="CHUBB")
    assert out["carrier"] == "CHUBB"
    assert out["index_basis"] == INDEX_SAMPLE


@pytest.mark.parametrize("index_key", ["construction", "equipment"])
def test_both_indices_are_reachable_by_key(index_key):
    out = drift.drift_for_case(facts=_Facts(plant_sum_insured=SI),
                               carrier="CHUBB", index_key=index_key)
    assert out["available"] is True
    assert out["index_name"] == drift.INDICES[index_key].name


def test_an_unknown_index_key_falls_back_to_construction_rather_than_crashing():
    out = drift.drift_for_case(facts=_Facts(plant_sum_insured=SI),
                               carrier="CHUBB", index_key="not-an-index")
    assert out["available"] is True
    assert out["index_name"] == drift.SAMPLE_CONSTRUCTION_INDEX.name


def test_two_carriers_on_the_same_facts_can_cross_on_different_dates():
    """End-to-end proof that the threshold is read per carrier. If these ever match,
    a market default has crept back in."""
    facts = _Facts(plant_sum_insured=SI)
    chubb = drift.drift_for_case(facts=facts, carrier="CHUBB",
                                 index_key="construction")
    msig = drift.drift_for_case(facts=facts, carrier="MSIG",
                                index_key="construction")
    assert chubb["threshold_pct"] != msig["threshold_pct"]
    assert msig["crossed_on"] < chubb["crossed_on"]
