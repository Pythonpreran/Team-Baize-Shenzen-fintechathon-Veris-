"""The action loop — the half of the product that changes something.

These tests exist because every guarantee in `actions.py` is the kind that holds in the
place a reader looks and quietly fails elsewhere. The four that matter most:

  * no premium, anywhere, ever
  * an owner cannot mark their own request BOUND
  * exposure counts once per insured interest, not once per action
  * deleting a case reaches the action list
"""
from __future__ import annotations

import inspect
import re
import sqlite3
from decimal import Decimal

import pytest

from proven import actions, pipeline
from proven.db import connect, init_db


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    init_db(c)
    actions.init(c)
    return c


FACTS = {
    "stock_sum_insured": {"value": "100000", "unit": "SGD", "source": "STRUCTURED",
                          "document_kind": "SCHEDULE", "author_class": "INSURER"},
    "stock_value": {"value": "150000", "unit": "SGD", "source": "STRUCTURED",
                    "document_kind": "ACCOUNTS", "author_class": "SELF"},
    "declared_headcount": {"value": 11, "unit": None, "source": "STRUCTURED",
                           "document_kind": "WIC_SCHEDULE", "author_class": "INSURER"},
    "contributor_count": {"value": 14, "unit": None, "source": "STRUCTURED",
                          "document_kind": "CPF_RECORD", "author_class": "AUTHORITY"},
}

AVERAGE_FINDING = {
    "rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD",
    "headline": "Stock sum insured is below the value at risk",
    "class": "DECLARED",
    "spans": [{"clause_ref": "4.2", "page_no": 15, "document": "BASE_WORDING",
               "quote": "proportionately reduced"}],
    "detail": {},
}


def _average(conn, case_id="case-1"):
    return actions.raise_from_finding(conn, case_id, AVERAGE_FINDING, facts=FACTS)


# ── the money rule ───────────────────────────────────────────────────────────

def test_premium_is_never_quoted(conn):
    a = _average(conn)
    assert a.premium_basis == actions.NOT_QUOTED
    assert a.as_dict()["premium_basis"] == actions.NOT_QUOTED


def test_premium_basis_cannot_be_changed(conn):
    a = _average(conn)
    a.premium_basis = "INDICATIVE_2400_SGD"
    with pytest.raises(ValueError, match="NOT_QUOTED"):
        a.assert_invariant()


def test_no_premium_shaped_column_exists():
    """Structural version of the promise: there is nothing to roll up."""
    banned = ("premium", "price", "rate", "quote_amount", "quoted_amount", "cost")
    schema = actions.SCHEMA.lower()
    cols = re.findall(r"^\s{2}(\w+)", schema, re.M)
    for col in cols:
        assert not any(b in col for b in banned), f"{col!r} is a price-shaped column"


def test_no_field_ever_carries_a_numeric_premium(conn):
    """Not a literal search — the serialised shape must hold no price-like number.

    The docstrings in `actions.py` discuss indicative premiums in order to refuse them, so
    grepping the source for the word is the wrong test. What matters is the output.
    """
    a = _average(conn)
    d = a.as_dict()
    price_keys = [k for k in d
                  if any(w in k.lower()
                         for w in ("premium", "price", "rate", "cost", "quote"))]
    assert price_keys == ["premium_basis", "premium_notice"], price_keys
    assert d["premium_basis"] == actions.NOT_QUOTED
    assert not re.search(r"\d", d["premium_basis"])


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_exposure_is_the_documented_shortfall(conn):
    a = _average(conn)
    assert a.exposure_sgd == "50000"
    assert Decimal(a.observed_value) - Decimal(a.documented_value) == Decimal(50000)


def test_exposure_basis_names_both_provenances(conn):
    a = _average(conn)
    assert "SCHEDULE" in a.exposure_basis and "INSURER" in a.exposure_basis
    assert "ACCOUNTS" in a.exposure_basis


def test_exposure_figure_requires_a_basis(conn):
    a = _average(conn)
    a.exposure_basis = ""
    with pytest.raises(ValueError, match="basis"):
        a.assert_invariant()


def test_no_figure_when_the_current_value_is_not_established(conn):
    """The plant interest has a documented limit and nothing that totals current plant."""
    f = dict(AVERAGE_FINDING, rule_id="PLANT-ADEQUACY-UNESTABLISHED")
    a = actions.raise_from_finding(conn, "case-1", f, facts={
        "plant_sum_insured": {"value": "180000", "document_kind": "SCHEDULE",
                              "author_class": "INSURER"}})
    assert a.exposure_sgd is None
    assert "not established" in a.exposure_basis
    assert "ASSET_REGISTER" in a.documents_requested


def test_a_count_interest_gets_no_money_figure(conn):
    f = dict(AVERAGE_FINDING, rule_id="WICA-HEADCOUNT-UNDERDECLARED")
    a = actions.raise_from_finding(conn, "case-1", f, facts=FACTS)
    assert a.exposure_sgd is None
    assert a.count_documented == 11 and a.count_observed == 14
    assert "measured in people" in a.exposure_basis
    assert "not a sum insured" in a.count_basis


def test_no_shortfall_when_value_is_within_the_limit(conn):
    facts = dict(FACTS, stock_value={"value": "80000", "document_kind": "ACCOUNTS",
                                     "author_class": "SELF"})
    a = actions.raise_from_finding(conn, "case-1", AVERAGE_FINDING, facts=facts)
    assert a.exposure_sgd == "0"
    assert "does not exceed" in a.exposure_basis


def test_negative_exposure_is_refused(conn):
    a = _average(conn)
    a.exposure_sgd = "-1"
    with pytest.raises(ValueError, match="negative"):
        a.assert_invariant()


# ── evidence ─────────────────────────────────────────────────────────────────

def test_an_action_cannot_exist_without_evidence(conn):
    a = _average(conn)
    a.policy_refs, a.signal_refs, a.documents_requested = [], [], []
    with pytest.raises(ValueError, match="must carry evidence"):
        a.assert_invariant()


def test_unmapped_rule_raises_nothing(conn):
    f = dict(AVERAGE_FINDING, rule_id="SOME-RULE-NOBODY-MAPPED")
    assert actions.raise_from_finding(conn, "case-1", f, facts=FACTS) is None


def test_policy_refs_carry_page_and_quote(conn):
    a = _average(conn)
    assert a.policy_refs[0]["page_no"] == 15
    assert a.policy_refs[0]["clause_ref"] == "4.2"
    assert a.policy_refs[0]["quote"]


# ── the state machine ────────────────────────────────────────────────────────

def test_starts_drafted_and_not_sent(conn):
    a = _average(conn)
    assert a.state == actions.DRAFTED
    assert a.history[0]["by"] == "engine"
    assert "Not sent" in a.history[0]["note"]


def test_owner_cannot_bind_their_own_request(conn):
    a = _average(conn)
    actions.advance(conn, a.id, actions.SENT_TO_ADVISER, by="owner")
    actions.advance(conn, a.id, actions.ADVISER_REVIEWING, by="adviser")
    actions.advance(conn, a.id, actions.QUOTE_RECEIVED, by="adviser")
    with pytest.raises(actions.TransitionRefused) as exc:
        actions.advance(conn, a.id, actions.BOUND, by="owner")
    assert exc.value.code == "WRONG_ACTOR"


def test_engine_appears_exactly_once_in_the_transition_table():
    """Only DRAFTED is machine-made. Every other move is a person's."""
    actors = {actor for moves in actions.TRANSITIONS.values() for _, actor in moves}
    assert actors == {"owner", "adviser"}


def test_illegal_jump_is_refused_and_names_what_is_permitted(conn):
    a = _average(conn)
    with pytest.raises(actions.TransitionRefused) as exc:
        actions.advance(conn, a.id, actions.BOUND, by="adviser")
    assert "Permitted" in str(exc.value)


def test_terminal_states_have_no_moves():
    for state in actions.TERMINAL:
        assert actions.TRANSITIONS[state] == ()


def test_full_path_to_bound(conn):
    a = _average(conn)
    for to, by in [(actions.SENT_TO_ADVISER, "owner"),
                   (actions.ADVISER_REVIEWING, "adviser"),
                   (actions.QUOTE_RECEIVED, "adviser"),
                   (actions.BOUND, "adviser")]:
        a = actions.advance(conn, a.id, to, by=by)
    assert a.state == actions.BOUND
    assert a.is_terminal and a.closes_exposure
    assert [h["to"] for h in a.history][-1] == actions.BOUND


def test_information_requested_can_return_to_review(conn):
    a = _average(conn)
    actions.advance(conn, a.id, actions.SENT_TO_ADVISER, by="owner")
    actions.advance(conn, a.id, actions.ADVISER_REVIEWING, by="adviser")
    actions.advance(conn, a.id, actions.INFORMATION_REQUESTED, by="adviser")
    a = actions.advance(conn, a.id, actions.ADVISER_REVIEWING, by="adviser")
    assert a.state == actions.ADVISER_REVIEWING


# ── the outcome metric ───────────────────────────────────────────────────────

def test_only_bound_closes_exposure(conn):
    assert actions.CLOSES_EXPOSURE == frozenset({actions.BOUND})
    for state in (actions.DECLINED, actions.NOT_REQUIRED, actions.WITHDRAWN):
        assert state not in actions.CLOSES_EXPOSURE


def test_declined_does_not_count_as_closed(conn):
    a = _average(conn)
    actions.advance(conn, a.id, actions.SENT_TO_ADVISER, by="owner")
    actions.advance(conn, a.id, actions.ADVISER_REVIEWING, by="adviser")
    actions.advance(conn, a.id, actions.DECLINED, by="adviser")
    out = actions.outcomes(conn, "case-1")
    assert out["exposure_closed_sgd"] == "0"
    assert out["actions_resolved"] == 1


def test_exposure_counts_once_per_interest_not_once_per_action(conn):
    """The measured defect. Two corrections, same shortfall, one figure."""
    actions.raise_from_finding(conn, "case-1", AVERAGE_FINDING, facts=FACTS)
    actions.raise_from_finding(
        conn, "case-1",
        dict(AVERAGE_FINDING, rule_id="SUBLIMIT-CONFLICTING-ENDORSEMENTS"),
        facts=FACTS)
    out = actions.outcomes(conn, "case-1")
    assert out["actions_raised"] == 2
    assert out["exposure_identified_sgd"] == "50000", "summing would give 100000"
    assert len(out["exposure_by_interest"]["stock"]["actions"]) == 2
    assert any("more than one requested correction" in n for n in out["notes"])


def test_drafted_is_prepared_not_pending(conn):
    """A prepared request has asked nobody anything, so it cannot count as momentum."""
    _average(conn)
    out = actions.outcomes(conn, "case-1")
    assert out["exposure_identified_sgd"] == "50000"
    assert out["exposure_pending_sgd"] == "0"
    assert out["actions_drafted"] == 1
    assert out["actions_in_flight"] == 0
    assert out["exposure_by_interest"]["stock"]["status"] == "NOT_PROGRESSED"


def test_pending_and_closed_are_reported_separately(conn):
    a = _average(conn)
    actions.advance(conn, a.id, actions.SENT_TO_ADVISER, by="owner")
    out = actions.outcomes(conn, "case-1")
    assert out["exposure_pending_sgd"] == "50000"
    assert out["exposure_closed_sgd"] == "0"
    assert out["actions_in_flight"] == 1

    actions.advance(conn, a.id, actions.ADVISER_REVIEWING, by="adviser")
    actions.advance(conn, a.id, actions.QUOTE_RECEIVED, by="adviser")
    actions.advance(conn, a.id, actions.BOUND, by="adviser")
    out = actions.outcomes(conn, "case-1")
    assert out["exposure_closed_sgd"] == "50000"
    assert out["exposure_pending_sgd"] == "0"


def test_outcomes_states_its_counting_basis(conn):
    _average(conn)
    out = actions.outcomes(conn, "case-1")
    assert "once per insured interest" in out["counting_basis"]
    assert out["premium_basis"] == actions.NOT_QUOTED


# ── idempotence and deletion ─────────────────────────────────────────────────

def test_rerunning_does_not_duplicate_a_request(conn):
    findings = [AVERAGE_FINDING]
    first = actions.raise_for_case(conn, "case-1", findings, facts=FACTS)
    second = actions.raise_for_case(conn, "case-1", findings, facts=FACTS)
    assert len(first) == 1 and second == []
    assert len(actions.for_case(conn, "case-1")) == 1


def test_deletion_removes_every_action(conn):
    actions.raise_for_case(conn, "case-1", [AVERAGE_FINDING], facts=FACTS)
    assert actions.for_case(conn, "case-1")
    actions.delete_for_case(conn, "case-1")
    assert actions.for_case(conn, "case-1") == []


def test_actions_are_scoped_to_their_case(conn):
    actions.raise_from_finding(conn, "case-A", AVERAGE_FINDING, facts=FACTS)
    actions.raise_from_finding(conn, "case-B", AVERAGE_FINDING, facts=FACTS)
    actions.delete_for_case(conn, "case-A")
    assert actions.for_case(conn, "case-A") == []
    assert len(actions.for_case(conn, "case-B")) == 1


# ── vocabulary hygiene ───────────────────────────────────────────────────────

def test_no_adjudicating_vocabulary_in_this_module():
    """Same guard the rest of the engine carries. We request; we do not accuse."""
    src = inspect.getsource(actions).lower()
    for word in ("breach", "violation", "void the policy", "claim will be denied"):
        assert word not in src, f"adjudicating word {word!r} present"


def test_every_action_type_has_a_label_and_an_ask():
    for t in actions.ACTION_TYPES:
        assert t in actions.ACTION_LABELS
        assert t in actions.ACTION_ASKS


def test_every_state_has_a_label_and_a_transition_entry():
    for s in actions.STATES:
        assert s in actions.STATE_LABELS
        assert s in actions.TRANSITIONS


# ── the real pipeline ────────────────────────────────────────────────────────

def test_pipeline_prepares_actions_from_the_demo_case():
    st = pipeline.run_case()
    assert st["brief"]["actions_prepared"] >= 1
    items = actions.for_case(st["conn"], st["case_id"])
    assert items, "the demo case should raise at least one correction"
    assert all(a.state == actions.DRAFTED for a in items), "nothing is sent automatically"


def test_demo_case_quantifies_the_stock_shortfall():
    st = pipeline.run_case()
    out = actions.outcomes(st["conn"], st["case_id"])
    assert out["exposure_identified_sgd"] == "50000"
    assert out["exposure_closed_sgd"] == "0", "nothing is closed without a human"
