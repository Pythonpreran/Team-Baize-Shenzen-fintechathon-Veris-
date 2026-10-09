"""Clause-level market map computed from published wordings.

The map is the most quotable artefact in the product and the easiest to overclaim
with, so most of these tests defend a restraint rather than a capability.
"""
from __future__ import annotations

import json
import pytest

from proven import marketmap
from proven.marketmap import NOT_ALIGNED

AVERAGE = "ADEQ.PROPORTIONATE_REDUCTION"
UNOCC = "COND.UNOCCUPANCY"
PPW = "COND.PREMIUM_PAYMENT_WARRANTY"

# The map is computed from the seven published wordings in the corpus, which is not in
# git. Named explicitly so the skip reason says which document is missing.
pytestmark = pytest.mark.requires_corpus(
    *[spec["corpus_id"] for spec in marketmap.CORPUS_WORDINGS])


@pytest.fixture(scope="module")
def mm():
    return marketmap.build_market_map()


def _cell(mm, mechanism: str, carrier: str) -> dict:
    row = next(r for r in mm.rows if r["mechanism"] == mechanism)
    return next(c for c in row["cells"] if c["carrier"] == carrier)


# ── the map is built from real documents ────────────────────────────────────

def test_map_covers_the_corpus_and_shows_its_rejections(mm):
    assert len(mm.carriers) >= 3
    assert len(mm.rows) == len(marketmap.MAP_MECHANISMS)
    assert mm.proposals > 0
    assert mm.rejected > 0, (
        "a map with no rejections cannot be told apart from one whose verifier "
        "accepts everything")


def test_every_populated_cell_is_checkable_against_a_page(mm):
    """The claim that separates this from price benchmarking is that a reader can
    verify any cell from the source PDF. That requires a page and a verbatim quote on
    every detail entry, with no exceptions."""
    for row in mm.rows:
        for cell in row["cells"]:
            if cell["state"] == NOT_ALIGNED:
                assert cell["detail"] == []
                continue
            assert cell["detail"], f"{row['mechanism']}/{cell['carrier']} ALIGNED but empty"
            for d in cell["detail"]:
                assert isinstance(d["page_no"], int) and d["page_no"] >= 1
                assert d["quote"].strip()


def test_as_dict_is_json_safe(mm):
    json.dumps(mm.as_dict())


# ── absence of an alignment is not evidence of absence ──────────────────────

def test_not_aligned_is_defined_on_the_response_itself(mm):
    """A three-word cell label will be read by someone who never sees the docs. The
    meaning has to travel with the payload, not live in a README."""
    d = mm.as_dict()
    meaning = d["not_aligned_meaning"]
    assert "does NOT mean" in meaning
    assert "lacks" in meaning


def test_no_cell_anywhere_claims_a_mechanism_is_absent(mm):
    """'absent' is a claim about the document. NOT_ALIGNED is a claim about the
    engine. Only the second one is ours to make."""
    for row in mm.rows:
        for cell in row["cells"]:
            assert "absent" not in cell["summary"].lower()
            assert "does not have" not in cell["summary"].lower()


# ── jurisdictions must not be totalled together ─────────────────────────────

def test_malaysian_wordings_are_carried_but_flagged(mm):
    my = [c for c in mm.carriers if c["jurisdiction"] == "MY"]
    assert my, "the generalisation evidence is missing from the map"
    for c in my:
        assert c["hand_read"] is False
    assert "NOT Singapore market positions" in mm.as_dict()["jurisdiction_warning"]


def test_singapore_ordering_never_mentions_a_malaysian_carrier(mm):
    """The ordering note is the sentence most likely to be quoted in a pitch. A
    Malaysian threshold inside a Singapore ordering would be a false market claim."""
    row = next(r for r in mm.rows if r["mechanism"] == AVERAGE)
    note = row.get("ordering_note", "")
    assert note, "no ordering note was produced for the under-insurance row"
    for c in mm.carriers:
        if c["jurisdiction"] == "MY":
            assert c["carrier"] not in note


def test_ordering_is_computed_only_over_hand_read_wordings(mm):
    row = next(r for r in mm.rows if r["mechanism"] == AVERAGE)
    note = row.get("ordering_note", "")
    for c in mm.carriers:
        if not c["hand_read"]:
            assert c["carrier"] not in note


def test_jurisdiction_filter_excludes_everything_else():
    sg = marketmap.build_market_map(only_jurisdiction="SG")
    assert sg.carriers
    assert {c["jurisdiction"] for c in sg.carriers} == {"SG"}
    for row in sg.rows:
        assert {c["jurisdiction"] for c in row["cells"]} == {"SG"}


# ── the carrier facts themselves ────────────────────────────────────────────

def test_chubb_and_msig_are_reported_differently_on_under_insurance():
    """The single most important cell pair in the map. Chubb states an 85% cushion;
    MSIG states none, so exposure begins at the first dollar short. Collapsing these
    to one market number is wrong in one direction or the other."""
    mm = marketmap.build_market_map(only_jurisdiction="SG")
    chubb = _cell(mm, AVERAGE, "Chubb")
    msig = _cell(mm, AVERAGE, "MSIG")
    assert "85" in chubb["summary"]
    assert "first dollar" in msig["summary"]
    assert not any(d["threshold_pct"] for d in msig["detail"]), \
        "a threshold was invented for MSIG"


def test_liberty_is_shown_as_holding_two_provisions_not_one():
    mm = marketmap.build_market_map(only_jurisdiction="SG")
    liberty = _cell(mm, AVERAGE, "Liberty")
    assert len(liberty["detail"]) >= 2
    assert "provisions" in liberty["summary"]
    thresholds = {d["threshold_pct"] for d in liberty["detail"]}
    assert "85" in thresholds and None in thresholds


def test_the_carrier_on_both_sides_of_the_ordering_is_named_as_such():
    """MEASURED DEFECT: Liberty appeared in the first-dollar group AND the cushioned
    group, and the note listed it in both without explanation, which reads as a
    contradiction. The overlap is the most consequential fact in the row — which
    provision governs depends on how the claim is valued — so it is stated."""
    mm = marketmap.build_market_map(only_jurisdiction="SG")
    row = next(r for r in mm.rows if r["mechanism"] == AVERAGE)
    note = row["ordering_note"]
    assert "does both" in note
    assert "bases of settlement" in note
    # and it must not be silently double-listed instead
    first_half, _, rest = note.partition("does both")
    assert first_half.count("Liberty") <= 1


def test_unoccupancy_variation_within_one_wording_is_surfaced():
    """Liberty states 30 days in most sections and 7 in another. A single number here
    would be wrong for whichever section the claim arises under."""
    mm = marketmap.build_market_map(only_jurisdiction="SG")
    liberty = _cell(mm, UNOCC, "Liberty")
    assert "BY SECTION" in liberty["summary"]
    days = {d["days"] for d in liberty["detail"]}
    assert {"7", "30"} <= days


def test_premium_warranty_is_the_one_uniform_row_in_singapore():
    """GIA standardised this clause, so all three hand-read Singapore wordings should
    align. If this row ever goes patchy, the aligner regressed rather than the market
    having changed."""
    mm = marketmap.build_market_map(only_jurisdiction="SG")
    row = next(r for r in mm.rows if r["mechanism"] == PPW)
    states = {c["carrier"]: c["state"] for c in row["cells"]}
    assert states.get("Chubb") == "ALIGNED"
    assert states.get("MSIG") == "ALIGNED"
    assert states.get("Liberty") == "ALIGNED"


# ── position_for, which is what a Review Brief embeds ───────────────────────

def test_position_for_compares_only_within_a_jurisdiction(mm):
    pos = marketmap.position_for("CHUBB", AVERAGE, mm)
    assert pos["available"] is True
    assert pos["own"]["carrier"] == "Chubb"
    assert pos["peers"], "no peers to compare against"
    for p in pos["peers"]:
        assert p["jurisdiction"] == pos["own"]["jurisdiction"]


def test_position_for_excludes_the_carrier_from_its_own_peers(mm):
    pos = marketmap.position_for("MSIG", AVERAGE, mm)
    assert all(p is not pos["own"] for p in pos["peers"])
    assert sum(1 for p in pos["peers"] if p["carrier"] == "MSIG") == 0


def test_position_for_an_uncorpused_carrier_says_so_rather_than_guessing(mm):
    """The sandbox insurer the carrier workspace speaks for is NOT in the corpus.
    Returning a neighbour's position here would be the worst possible failure of this
    feature. (Was asserted against the previous seeded carrier's key; that organisation
    was replaced by the fictional sandbox insurer, and the property is the same.)"""
    pos = marketmap.position_for("LOTUS_GENERAL", AVERAGE, mm)
    assert pos["available"] is False
    assert "corpus" in pos.get("reason", "")


def test_position_for_an_unknown_mechanism_is_unavailable_not_empty(mm):
    pos = marketmap.position_for("CHUBB", "NOT.A.MECHANISM", mm)
    assert pos["available"] is False


# ── the method statement must keep the price-benchmarking line clear ────────

def test_method_statement_disclaims_premium_benchmarking(mm):
    method = mm.as_dict()["method"]
    assert "not premiums" in method or "not premium" in method
    assert "private dataset" in method
    assert "verbatim quote" in method


def test_missing_corpus_directory_yields_an_empty_map_not_a_crash(tmp_path):
    """A corpus that has not been extracted yet is a degraded mode, not an error: the
    rest of the engine works without it."""
    mm = marketmap.build_market_map(corpus_dir=tmp_path)
    assert mm.carriers == []
    for row in mm.rows:
        assert row["cells"] == []
    json.dumps(mm.as_dict())


# ── memoisation ─────────────────────────────────────────────────────────────

def test_the_map_is_memoised_per_jurisdiction():
    """It aligns seven wordings from disk and cost ~0.65s per call, which was the
    slowest thing on the page and was being paid again on every jurisdiction switch.
    The corpus is immutable published documents, so a second call within one process
    can only produce the same answer."""
    first = marketmap.build_market_map()
    second = marketmap.build_market_map()
    assert first is second, "the map was rebuilt instead of served from cache"

    sg = marketmap.build_market_map(only_jurisdiction="SG")
    assert sg is not first, "the SG filter was served the unfiltered map"
    assert sg is marketmap.build_market_map(only_jurisdiction="SG")


def test_a_different_corpus_is_never_served_another_corpus_answer(tmp_path):
    """The cache key includes the directory. Without that, the degraded-mode test
    below would be handed the real corpus, and a deployment pointed at a different
    corpus would silently serve the wrong market."""
    marketmap.build_market_map()                      # populate with the real corpus
    empty = marketmap.build_market_map(corpus_dir=tmp_path)
    assert empty.carriers == []
    assert empty is not marketmap.build_market_map()
