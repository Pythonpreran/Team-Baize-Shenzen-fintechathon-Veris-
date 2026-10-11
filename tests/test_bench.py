"""T20 benchmark, reviewer-store, and opt-in study contracts."""
from __future__ import annotations

import base64
import json
import os

import pytest

from eval import bench, user_study
from proven import reviewer


def test_benchmark_fixed_seed_has_identical_scoring_and_refuses_expert_claims(tmp_path):
    first = bench.run_all(seed=99, measure=False)
    second = bench.run_all(seed=99, measure=False)
    assert first == second
    assert all(case["passed"] for case in first["cases"])
    assert first["metrics"]["general_accuracy"]["value"] is None
    assert first["metrics"]["false_alarm_rate"]["value"] is None
    assert first["benchmark_completion"] == {"completed": 4, "executed": 4, "rate": 1.0,
                                               "scope": "local deterministic benchmark/control set, including non-canonical controls."}
    canonical = first["canonical_task_completion"]
    assert canonical["completed"] == canonical["executed"] == 2
    assert canonical["task_ids"] == [1, 2]
    assert set(canonical["case_to_task"]) == {"canonical.cited_answer", "canonical.honest_abstention"}
    assert "security.redteam" not in canonical["case_to_task"]
    json_path, markdown_path = bench.write(first, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["profile"] == "none"
    content = markdown_path.read_text(encoding="utf-8")
    assert "What these numbers are not" in content
    assert "Explicitly mapped canonical task coverage" in content


def test_reviewer_store_exports_only_agreed_credential_bearing_human_labels(tmp_path):
    store = reviewer.ReviewerStore(tmp_path / "reviews.sqlite", b"r" * 32)
    finding = store.enqueue(tenant_id="tenant-a", reviewer_org_id="review-org", finding={
        "rule_id": "RULE-1", "output_state": "DOCUMENTED", "evidence_class": "PROVEN",
        "headline": "A finding", "evidence": [{"quote": "Exact wording", "page": 1}],
    })
    assert store.export_labels("review-org")["exportable_count"] == 0
    review = store.record_review(
        reviewer_org_id="review-org", reviewer_id="reviewer-1", reviewer_name="Reviewer",
        credentials="licensed reviewer", finding_id=finding["id"], decision="AGREE",
        severity="HIGH", comment="Checked against the cited wording.")
    assert review["decision"] == "AGREE"
    exported = store.export_labels("review-org")
    assert exported["exportable_count"] == 1
    assert exported["labels"][0]["derivation"] == "REVIEWED_BY_EXPERT"
    assert exported["labels"][0]["reviewer"]["credentials"] == "licensed reviewer"


def test_user_study_requires_opt_in_and_calculates_paired_metrics():
    record = user_study.template()
    record["consent"] = {"opted_in": True, "recorded_at": "2026-10-12T00:00:00Z"}
    record["participant_code"] = "P-001"
    record["sus"] = [5, 1, 5, 1, 5, 1, 5, 1, 5, 1]
    for task in record["tasks"]:
        task["seconds"] = 100 if task["condition"] == "without_veris" else 50
        task["completed"] = True
    result = user_study.analyse([record])
    assert result["median_time_saved_fraction"] == 0.5
    assert result["completion_rate_with_veris"] == 1.0
    record["consent"]["opted_in"] = False
    with pytest.raises(ValueError, match="opt-in"):
        user_study.analyse([record])
