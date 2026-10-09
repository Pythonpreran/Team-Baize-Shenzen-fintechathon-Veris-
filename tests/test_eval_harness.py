"""The eval harness is the pitch's evidence base, so its honesty properties are
tested rather than trusted.

The property that matters: a label derived from the engine's own output must never
contribute to a reported number, and no unqualified accuracy claim may be permitted
while no expert-reviewed label exists.
"""
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL.parent) not in sys.path:
    sys.path.insert(0, str(EVAL.parent))

from eval import citation_audit, harness  # noqa: E402


@pytest.fixture(scope="module")
def report():
    return harness.run_eval()


def test_harness_runs_and_reports_sample_sizes(report):
    assert report["labels_total"] >= 2
    assert report["all_labels"]["sample_size"] >= 1
    assert "precision" in report["measured"]
    assert "recall" in report["measured"]


def test_engine_output_labels_are_excluded_from_the_measured_number(report):
    """The core honesty property. A label that copies the engine's output measures
    self-consistency, and self-consistency must not be able to become an accuracy
    figure by accident."""
    assert report["by_derivation"].get("ENGINE_OUTPUT", 0) >= 1
    engine_rows = [s for s in report["per_label"]
                   if not s.get("skipped") and s["derivation"] == "ENGINE_OUTPUT"]
    assert engine_rows
    for row in engine_rows:
        assert row["measuring"] is False
    # The measured aggregate is strictly smaller than the all-labels aggregate.
    assert report["measured"]["sample_size"] < report["all_labels"]["sample_size"]


def test_undeclared_derivation_defaults_to_the_weakest():
    """An undeclared label must not silently become evidence."""
    assert harness.derivation_of({}) == harness.ENGINE_OUTPUT
    assert harness.derivation_of({"derivation": "nonsense"}) == harness.ENGINE_OUTPUT
    assert harness.derivation_of(
        {"derivation": "independent_objective"}) == harness.INDEPENDENT_OBJECTIVE


def test_accuracy_claim_is_not_permitted_without_an_expert(report):
    assert report["expert_reviewed_measurement_available"] is False
    assert report["accuracy_claim_permitted"] is False
    assert report["blocked_on"]
    assert "REVIEWED_BY_EXPERT" in report["blocked_on"][0]


def test_claim_language_states_its_own_scope(report):
    """The report hands over the exact sentence the evidence supports, so nobody has
    to invent one on a slide at 2am."""
    language = report["claim_language"]
    assert "SCOPED CLAIM ONLY" in language
    assert "NOT a general accuracy rate" in language
    assert "n=1" in language


def test_objective_label_scores_and_declares_out_of_scope_rules(report):
    objective = [s for s in report["per_label"]
                 if s.get("derivation") == "INDEPENDENT_OBJECTIVE"]
    assert objective, "the independently derived label is missing"
    row = objective[0]
    assert row["measuring"] is True
    assert row["scope"] == "PARTIAL"
    assert row["out_of_scope"] > 0
    # A rule the label declines to opine on is not counted as an error.
    assert row["unexpected"] == 0, row["unexpected_rules"]


def test_labels_match_current_engine_output(report):
    """Regression guard. If a rule changes state or class, this fails and the labels
    must be re-derived deliberately rather than drifting."""
    for s in report["per_label"]:
        if s.get("skipped"):
            continue
        assert s["state_mismatch"] == 0, s["mismatches"]
        assert s["missed"] == 0, s["mismatches"]
        assert s["unexpected"] == 0, s["unexpected_rules"]


def test_accounting_expectations_in_labels_are_checked(report):
    """A label may pin the accounting counts. Drift in those counts is reported, not
    silently absorbed."""
    checked = [s for s in report["per_label"] if s.get("accounting_check")]
    assert checked, "no label pins the accounting counts"
    for s in checked:
        for key, outcome in s["accounting_check"].items():
            assert outcome["match"], (
                f"{s['label_id']} expected {key}={outcome['expected']} but the "
                f"engine produced {outcome['actual']}")


def test_require_expert_flag_fails_the_build():
    """A build script must not be able to publish a number the evidence does not
    support."""
    assert harness.main(["--require-expert"]) == 2
    assert harness.main([]) == 0


# ── citation fidelity: a measurement that needs no reviewer ─────────────────

@pytest.fixture(scope="module")
def audit():
    return citation_audit.audit()


def test_citation_audit_finds_no_unfaithful_span(audit):
    assert audit["spans_checked"] > 0
    assert audit["span_fidelity_rate"] == 1.0
    assert audit["failures"] == []


def test_every_span_sits_inside_a_cited_provision(audit):
    assert audit["boundary_checked"] > 0
    assert audit["boundary_rate"] == 1.0


def test_every_figure_carries_an_evidence_class(audit):
    assert audit["figures_checked"] > 0
    assert audit["figures_with_a_stated_basis"] == audit["figures_checked"]


def test_audit_states_what_it_does_not_measure(audit):
    """The audit is a narrower claim than accuracy and has to say so itself, or it
    will be quoted as though it were accuracy."""
    assert audit["needs_domain_reviewer"] is False
    assert "legally CORRECT" in audit["what_this_does_not_measure"]
    assert audit["accounting"]["invariant_holds"] is True
