"""T19 deterministic red-team evidence checks."""
from __future__ import annotations

import json
from pathlib import Path

from security.redteam import runner


def test_redteam_runner_blocks_every_local_attack_and_is_reproducible(tmp_path):
    first = runner.run(seed=4242)
    second = runner.run(seed=4242)
    assert first == second
    assert first["summary"]["failed"] == 0
    assert first["summary"]["checks"] == len(first["cases"])
    assert first["summary"]["tier2_actions_triggered_by_attack_content"] == 0
    pii = next(case for case in first["cases"] if case["id"] == "RT-PII-EXFIL")
    document = next(case for case in first["cases"] if case["id"] == "RT-DOC-PDF-METADATA")
    assert pii["blocked"] and "Control-only" in pii["vector"]
    assert "No provider-bound prompt" in pii["evidence"]
    assert "Control-only" in document["vector"]
    assert "No PDF, metadata, image, or OCR media" in document["evidence"]

    json_path, markdown_path = runner.write(first, tmp_path)
    persisted = json.loads(json_path.read_text(encoding="utf-8"))
    assert persisted == first
    content = markdown_path.read_text(encoding="utf-8")
    assert "0 of" in content and "OWASP" in content and "local checks" in content


def test_redteam_cli_writes_the_submission_evidence(tmp_path):
    assert runner.main(["--seed", "77", "--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / runner.RESULTS_JSON).is_file()
    assert (tmp_path / runner.RESULTS_MARKDOWN).is_file()
