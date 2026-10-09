"""Coverage-mechanism alignment: Propose, Verify, Bind.

Every test here defends a defect that was actually found while building the module, or
a property the honesty rules depend on. The comments say which.
"""
from __future__ import annotations

import re
import pytest

from proven import align, corpus

# Wordings are addressed by CORPUS ID, not by filename. They are third-party published
# documents and live outside git; `_has()` is what lets a test on a checkout with no
# corpus skip rather than assert nothing.


def _pages(doc_id: str) -> list[str]:
    raw = corpus.text_path(doc_id).read_text(encoding="utf-8", errors="replace")
    return corpus.split_pages(raw)


def _has(doc_id: str) -> bool:
    return corpus.text_path(doc_id).exists()


CHUBB = "sg-chubb-selectplus-lite-wording"
MSIG = "sg-msig-sumo-wording"
LIBERTY = "sg-liberty-smecare-wording"
QBE_IAR = "my-qbe-industrial-all-risk-wording"

AVERAGE = "ADEQ.PROPORTIONATE_REDUCTION"
PPW = "COND.PREMIUM_PAYMENT_WARRANTY"
UNOCC = "COND.UNOCCUPANCY"
DECL = "COND.DECLARATION_CLAUSE_GENERAL"


# ── the mechanism definitions load and are well formed ──────────────────────

def test_mechanisms_load_with_mandatory_slots():
    mechs = align.mechanisms()
    assert mechs, "mechanisms.yaml did not load"
    for key, mech in mechs.items():
        assert mech.mandatory, f"{key} has no mandatory slots, so it verifies nothing"
        assert mech.taxonomy_code, f"{key} has no taxonomy code to bind to"


def test_every_slot_pattern_compiles():
    """A malformed pattern must fail here rather than silently never matching."""
    for key, mech in align.mechanisms().items():
        for group in (mech.mandatory, mech.negative, mech.optional):
            for slot in group:
                for pat in slot.any_of:
                    re.compile(pat.replace(" ", r"\s+"), re.IGNORECASE)
                if slot.capture:
                    re.compile(slot.capture.replace(" ", r"\s+"), re.IGNORECASE)
                for _label, pat in slot.value_map:
                    re.compile(pat.replace(" ", r"\s+"), re.IGNORECASE)


# ── the CONTRIBUTION false positive. This is the headline regression. ───────

CONTRIBUTION_CLAUSE = """
14. CONTRIBUTION
If at the time of any loss or damage there shall be any other insurance covering the
same property, the Company shall not be liable to pay or contribute more than its
rateable proportion of such loss or damage.
"""

REAL_AVERAGE_CLAUSE = """
4. Average
If the property hereby insured shall at the happening of any fire be collectively of
greater value than the sum insured thereon, then the Insured shall be considered as
being his own insurer for the difference and shall bear a rateable proportion of the
loss accordingly.
"""


def test_contribution_clause_is_rejected_not_verified_as_average():
    """Contribution apportions between INSURERS; average apportions between insurer
    and INSURED against a value at risk. They share the phrase 'rateable proportion'.
    An earlier build verified contribution as average — this is that regression."""
    mech = align.mechanisms()[AVERAGE]
    ok, reason, failed, _ = align.verify(mech, CONTRIBUTION_CLAUSE)
    assert ok is False
    assert reason == "NEGATIVE_SLOT"
    assert failed == "other_insurance_apportionment"


def test_a_real_average_clause_still_verifies():
    """The negative slot must not be so broad that it kills true positives."""
    mech = align.mechanisms()[AVERAGE]
    ok, reason, _, matched = align.verify(mech, REAL_AVERAGE_CLAUSE)
    assert ok is True
    assert reason == "VERIFIED"
    assert set(matched) == {"comparison_to_value_at_risk", "reduction_consequence"}


def test_negative_slots_are_tested_before_mandatory_ones():
    """Order matters: a contribution clause satisfies the average slots on its shared
    vocabulary, so it must be rejected before they are consulted."""
    mech = align.mechanisms()[AVERAGE]
    _ok, reason, _failed, matched = align.verify(mech, CONTRIBUTION_CLAUSE)
    assert reason == "NEGATIVE_SLOT"
    assert matched == {}, "mandatory slots were evaluated despite a negative match"


# ── verification is what decides, not the proposal ─────────────────────────

def test_missing_mandatory_slot_rejects_and_names_the_slot():
    mech = align.mechanisms()[AVERAGE]
    # Reduction language with no comparison against a value at risk.
    text = "The Insured shall bear a rateable proportion of the loss accordingly."
    ok, reason, failed, _ = align.verify(mech, text)
    assert ok is False
    assert reason == "MISSING_SLOT"
    assert failed == "comparison_to_value_at_risk"


def test_a_mechanism_with_no_matching_text_binds_nothing():
    res = align.align_pages(["This page is about nothing in particular."], "TEST")
    assert res.verified == 0


def test_rejections_are_returned_not_swallowed():
    """A verifier whose refusals are hidden cannot be told apart from one that accepts
    everything, so rejections are part of the result."""
    if not _has(CHUBB):
        pytest.skip("corpus wording not present")
    res = align.align_pages(_pages(CHUBB), "CHUBB")
    assert res.rejected > 0
    for r in res.rejections:
        assert r.reason in {"MISSING_SLOT", "NEGATIVE_SLOT"}
        assert r.failed_slot
        assert r.quote


@pytest.mark.parametrize("name", [CHUBB, MSIG, LIBERTY, QBE_IAR])
def test_the_alignment_accounting_closes_on_every_wording(name):
    """MEASURED DEFECT: proposals did not equal verified + rejected. MSIG offered 23,
    rejected 13 and showed 4 — six verified duplicates disappeared silently between
    the count and the list, and the displayed rejection rate could not be checked by
    arithmetic. Every proposal must now land in exactly one of three buckets."""
    if not _has(name):
        pytest.skip("corpus wording not present")
    res = align.align_pages(_pages(name), name)
    res.assert_invariant()
    assert res.proposals == res.verified + res.rejected + res.collapsed


def test_a_broken_accounting_is_refused_rather_than_reported():
    """The invariant must actually bite, or it is decoration."""
    res = align.AlignmentResult(carrier="TEST", proposals=9)
    with pytest.raises(AssertionError):
        res.as_dict()


# ── per-carrier facts must never collapse to a market default ──────────────

@pytest.mark.skipif(not _has(CHUBB), reason="corpus wording not present")
def test_chubb_average_is_85_percent_on_an_actual_value_basis():
    """MEASURED DEFECT: settlement_basis was first read from the whole candidate
    window, which reached the next clause, and Chubb cl 4.2 was reported as
    REINSTATEMENT when its own text says 'the actual value'. Slot scoping fixed it."""
    res = align.align_pages(_pages(CHUBB), "CHUBB")
    avg = res.by_mechanism(AVERAGE)
    assert avg, "Chubb's average condition was not found"
    assert any(a.values.get("threshold_pct") == "85" for a in avg)
    assert any(a.values.get("settlement_basis") == "ACTUAL_VALUE" for a in avg)
    assert not any(a.values.get("settlement_basis") == "REINSTATEMENT" for a in avg)


@pytest.mark.skipif(not _has(MSIG), reason="corpus wording not present")
def test_msig_average_states_no_threshold():
    """MSIG GC12 has no cushion. Inventing 85% here would under-report every MSIG
    insured whose adequacy sits between 85% and 100% — wrong in the direction that
    costs the business money at claim time."""
    res = align.align_pages(_pages(MSIG), "MSIG")
    avg = res.by_mechanism(AVERAGE)
    assert avg
    assert any(a.values.get("threshold_pct") is None for a in avg)


@pytest.mark.skipif(not _has(LIBERTY), reason="corpus wording not present")
def test_liberty_yields_both_average_provisions():
    """Liberty carries an average on a value basis with no threshold AND an 85%
    'Average Relief' on a reinstatement basis. Losing either is a wrong answer in one
    direction or the other. This is the regression for the whole
    per-clause-instance design."""
    res = align.align_pages(_pages(LIBERTY), "LIBERTY")
    avg = res.by_mechanism(AVERAGE)
    thresholds = {a.values.get("threshold_pct") for a in avg}
    assert "85" in thresholds, "the 85% Average Relief provision was lost"
    assert None in thresholds, "the no-threshold value-basis provision was lost"


@pytest.mark.skipif(not _has(LIBERTY), reason="corpus wording not present")
def test_liberty_unoccupancy_keeps_both_seven_and_thirty_days():
    """Same wording, two different day counts in different sections. A carrier-level
    default cannot represent this."""
    res = align.align_pages(_pages(LIBERTY), "LIBERTY")
    days = {a.values.get("days") for a in res.by_mechanism(UNOCC)}
    assert "7" in days
    assert "30" in days


@pytest.mark.skipif(not _has(CHUBB), reason="corpus wording not present")
def test_chubb_unoccupancy_is_fourteen_days():
    res = align.align_pages(_pages(CHUBB), "CHUBB")
    days = {a.values.get("days") for a in res.by_mechanism(UNOCC)}
    assert "14" in days


# ── clauses that run across a page break ───────────────────────────────────

@pytest.mark.skipif(not _has(CHUBB), reason="corpus wording not present")
def test_premium_warranty_found_although_the_clause_spans_a_page_break():
    """MEASURED DEFECT: 'within 60 days' ends one page and 'automatically terminated'
    opens the next, behind a footer. Proposing per page meant no candidate held both
    mandatory slots and the highest-confidence mechanism in the corpus was missed
    entirely. Page bleed fixed it."""
    res = align.align_pages(_pages(CHUBB), "CHUBB")
    assert res.by_mechanism(PPW), "premium payment warranty was not aligned"


def test_page_bleed_never_pushes_a_span_past_its_own_page():
    """The bleed exists to VERIFY across a break. Binding an offset into the next page
    would make the quote unverifiable at the recorded position, so spans are clamped
    to the anchor's own page and the overflow is flagged instead."""
    page_one = ("Premium must be paid within 60 days of inception, then:"
                + " filler." * 30)
    page_two = "the cover is automatically terminated immediately after that period."
    pages = [page_one, page_two]
    res = align.align_pages(pages, "TEST")
    assert res.by_mechanism(PPW), "the split clause did not verify across the bleed"
    for a in res.alignments:
        page = pages[a.page_no - 1]
        assert 0 <= a.char_start < a.char_end <= len(page)
        assert a.continues_next_page is True


def test_bound_offsets_reproduce_the_quote_from_the_stored_page_text():
    """The fidelity property the whole 'every number is quotable' claim rests on: the
    recorded offsets must reproduce the recorded quote from the page text on disk. If
    the page bleed leaked into an offset this fails."""
    for name in (CHUBB, MSIG, LIBERTY, QBE_IAR):
        if not _has(name):
            continue
        pages = _pages(name)
        for prop in (align.LexicalProposer(), align.TfidfProposer()):
            res = align.align_pages(pages, name, proposer=prop)
            for a in res.alignments:
                got = pages[a.page_no - 1][a.char_start:a.char_end]
                assert re.sub(r"\s+", " ", got).strip() == a.quote, (
                    f"{name}/{prop.name}: {a.mechanism} p{a.page_no} "
                    f"offsets do not reproduce the quote")


# ── absence is not evidence of absence ─────────────────────────────────────

@pytest.mark.skipif(not _has(CHUBB), reason="corpus wording not present")
def test_chubb_has_no_general_declaration_clause():
    """Verified absent by hand across all 28 pages. A bound alignment here would be a
    false positive on the most consequential mechanism in the corpus."""
    res = align.align_pages(_pages(CHUBB), "CHUBB")
    assert res.by_mechanism(DECL) == []


@pytest.mark.skipif(not _has(MSIG), reason="corpus wording not present")
def test_msig_has_a_sweeping_declaration_clause():
    res = align.align_pages(_pages(MSIG), "MSIG")
    assert res.by_mechanism(DECL), "MSIG's declaration clause was not found"


# ── generalisation to a wording nobody has read ────────────────────────────

@pytest.mark.skipif(not _has(QBE_IAR), reason="corpus wording not present")
def test_mechanisms_are_found_in_a_never_hand_mapped_wording():
    """The whole point of structural alignment: no clause map exists for this carrier,
    so anything found was found from structure alone."""
    res = align.align_pages(_pages(QBE_IAR), "MY_QBE_IAR")
    assert res.by_mechanism(AVERAGE), "no average condition found in an unseen wording"


@pytest.mark.skipif(not _has(QBE_IAR), reason="corpus wording not present")
def test_no_bound_average_span_contains_other_insurance_language():
    """Across every corpus wording, a bound average must not be a contribution clause.
    The QBE industrial wording is where this first went wrong."""
    for name in (CHUBB, MSIG, LIBERTY, QBE_IAR):
        if not _has(name):
            continue
        res = align.align_pages(_pages(name), name)
        for a in res.by_mechanism(AVERAGE):
            assert not re.search(r"any other insurance|other insurances?\b",
                                 a.quote, re.IGNORECASE), \
                f"contribution language inside a bound average span in {name}"


# ── proposer is swappable and cannot change what binds ─────────────────────

@pytest.mark.skipif(not _has(CHUBB), reason="corpus wording not present")
def test_swapping_the_proposer_cannot_change_a_carrier_fact():
    """A proposer may widen or narrow recall. It must never change the VALUE that
    binds — that is what keeps 'no machine-learning model makes a consequential
    decision' true once a model participates in reading. TF-IDF here stands in for any
    learned proposer.

    Chubb's average is 85% on an actual-value basis. Whichever proposer offered the
    candidate, that is what the wording says, so that is what must bind."""
    pages = _pages(CHUBB)
    for prop in (align.LexicalProposer(), align.TfidfProposer()):
        res = align.align_pages(pages, "CHUBB", proposer=prop)
        for a in res.by_mechanism(AVERAGE):
            assert a.values.get("threshold_pct") in {"85", None}, \
                f"{prop.name} bound a threshold Chubb's wording does not state"
            assert a.values.get("settlement_basis") != "REINSTATEMENT", \
                f"{prop.name} bound the neighbouring clause's basis"
        for a in res.by_mechanism(UNOCC):
            assert a.values.get("days") == "14", \
                f"{prop.name} bound a day count Chubb's wording does not state"


@pytest.mark.skipif(not _has(CHUBB), reason="corpus wording not present")
def test_a_proposer_that_offers_nothing_binds_nothing_rather_than_guessing():
    """Degraded proposal must degrade recall, never fabricate a fact."""

    class Silent:
        name = "silent"

        def propose(self, mech, pages):
            return iter(())

    res = align.align_pages(_pages(CHUBB), "CHUBB", proposer=Silent())
    assert res.proposals == 0
    assert res.verified == 0
    assert res.alignments == []


def test_only_filter_restricts_which_mechanisms_run():
    text = ["The premium must be paid within 60 days or cover is "
            "automatically terminated."]
    res = align.align_pages(text, "TEST", only=[PPW])
    assert all(a.mechanism == PPW for a in res.alignments)
    assert all(r.mechanism == PPW for r in res.rejections)


# ── the result contract the API depends on ─────────────────────────────────

@pytest.mark.skipif(not _has(MSIG), reason="corpus wording not present")
def test_as_dict_is_json_safe_and_reports_the_rejection_rate():
    import json

    res = align.align_pages(_pages(MSIG), "MSIG")
    d = res.as_dict()
    json.dumps(d)                                   # must not raise
    assert d["proposals"] == d["verified"] + d["rejected"] + d["collapsed"]
    assert 0.0 <= d["rejection_rate"] <= 1.0
    for a in d["alignments"]:
        assert a["classifier"] == align.MECHANISM_ALIGNED
        assert a["quote"]
        assert a["char_end"] > a["char_start"]
