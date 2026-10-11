"""T21 local submission-package artefact checks."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts import check_docs, generate_compliance_matrix, verify_evidence

ROOT = Path(__file__).resolve().parents[1]


def test_required_submission_documents_exist_and_link_cleanly():
    required = {
        "TECHNICAL.md", "SECURITY-SELF-ASSESSMENT.md", "COMPLIANCE.md", "EVALUATION.md",
        "README.md", "DEPLOYMENT.md", "PRIOR-WORK.md", "DECK-OUTLINE.md", "VIDEO-SCRIPT.md",
        "DEMO-RUNBOOK.md", "SUBMISSION-CHECKLIST.md",
    }
    present = {path.name for path in (ROOT / "docs").glob("*.md")}
    assert required <= present
    assert check_docs.check(ROOT) == []


def test_compliance_matrix_and_evidence_bundle_verify():
    assert generate_compliance_matrix.render() == (ROOT / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")
    assert verify_evidence.verify(ROOT / "evidence") == []


def test_deployment_assets_are_hosted_mode_ready_without_claiming_a_live_service():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    hosted = (ROOT / "docker-compose.hosted.yml").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "python:3.12-slim" in dockerfile
    assert "tesseract-ocr-eng" in dockerfile and "tesseract-ocr-chi-sim" in dockerfile
    assert "VERIS_MODE: hosted" in hosted
    assert "does **not** provision" in deployment
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "evidence/**/*.json -text" in attributes and "evidence/**/*.jsonl -text" in attributes


def test_documented_smoke_bootstraps_the_src_layout_in_an_isolated_interpreter():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "pip install -r requirements.txt -e ." in readme
    completed = subprocess.run(
        [sys.executable, "-I", "scripts/smoke_deployment.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "first TOTP enrollment" in completed.stdout
