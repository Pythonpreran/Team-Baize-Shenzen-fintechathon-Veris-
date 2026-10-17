"""Build the local synthetic evidence bundle without external requests.

The generated sandbox log uses public fixture signing material solely so a clone can
verify the local test artifact. It is never a deployment credential and does not prove
an external insurer, bank, payment, video, deployment, or submission occurred.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from eval import bench
from security.redteam import runner as redteam
from proven import sandbox

OUT = ROOT / "evidence"
PUBLIC_FIXTURE_MATERIAL = b"VERIS local synthetic evidence fixture v1; not a secret"
FIXTURE_TENANT = "evidence-fixture"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sandbox_fixture(output: Path) -> dict:
    key = hashlib.sha256(PUBLIC_FIXTURE_MATERIAL).digest()
    with tempfile.TemporaryDirectory(prefix="veris-evidence-sandbox-") as directory:
        base = Path(directory)
        temp_log = base / "sandbox-operations.jsonl"
        service = sandbox.SandboxService(base / "sandbox.sqlite", signing_key=key, log_path=temp_log)
        claim = service.submit_claim({
            "tenant_id": FIXTURE_TENANT, "policy_id": "synthetic-health-policy",
            "packet_sha256": "a" * 64,
        }, idempotency_key="evidence-claim-submit")
        invoice = service.issue_invoice({
            "tenant_id": FIXTURE_TENANT, "payee": "Lotus General (Sandbox)", "amount": "10.00",
        }, idempotency_key="evidence-invoice")
        records = service.operation_log(FIXTURE_TENANT)
        verification = service.verify_operation_log(FIXTURE_TENANT)
        output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(temp_log, output / "sandbox-operations.jsonl")
    return {
        "synthetic": True,
        "fixture_key_derivation": "SHA-256 of documented public fixture material; not a deployment secret.",
        "tenant_id": FIXTURE_TENANT,
        "claim_id": claim["claim_id"], "invoice_id": invoice["invoice_id"],
        "verification_at_build": verification, "record_count": len(records),
    }


def _agent_traces() -> list[dict]:
    # A trace descriptor distinguishes local exercised contracts from human-only or deferred
    # work. It avoids representing a test fixture as a real-world operation.
    return [
        {"task": 1, "name": "Cited policy answer", "status": "LOCAL_DETERMINISTIC", "evidence": "eval/bench.py canonical.cited_answer"},
        {"task": 2, "name": "Honest abstention", "status": "LOCAL_DETERMINISTIC", "evidence": "eval/bench.py canonical.honest_abstention"},
        {"task": 3, "name": "Protection map", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_protection_map_api.py"},
        {"task": 4, "name": "Health what-if", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_health_whatif_api.py"},
        {"task": 5, "name": "Start health claim", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_claims_api.py"},
        {"task": 6, "name": "Answer insurer query", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_demo_spine_e2e.py"},
        {"task": 7, "name": "Settlement correction", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_demo_spine_e2e.py"},
        {"task": 8, "name": "Escalate overdue claim", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_claims.py"},
        # T22 and T24 were built after submission v1. Each descriptor names exactly what
        # the local contract exercises, so nothing beyond the synthetic sandbox is implied.
        {"task": 9, "name": "Renew compulsory motor cover", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_renewals.py (renewal calendar with the compulsory motor alert; the scheduler's sandbox invoice payment needs a matching live mandate and keeps exact-payload confirmation plus step-up); no real insurer or payment."},
        {"task": 10, "name": "Renewal diff", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_renewal.py; tests/test_renewals.py (owner-held prior/current document diff, no coverage verdict); tests/test_renewals_api.py (tenant-scoped routes)"},
        {"task": 11, "name": "Share and revoke claim file", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_delegate_claims.py and tests/test_privacy.py (owner-granted, member-consented, scoped and time-limited delegate access to one claim with no submission or decision authority; consent revocation exercised); hosted delegate identity remains a human action."},
        {"task": 12, "name": "SME adviser escalation", "status": "LOCAL_TEST_CONTRACT", "evidence": "tests/test_adviser_actions.py"},
    ]


def build(output: Path = OUT) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    red = redteam.run()
    redteam.write(red, output / "redteam")
    scoreboard = bench.run_all(measure=False)
    bench.write(scoreboard, output / "evaluation")
    sandbox_meta = _sandbox_fixture(output)
    _write_json(output / "sandbox-operation-verification.json", sandbox_meta)
    _write_json(output / "agent-traces" / "canonical-task-traces.json", _agent_traces())
    (output / "screenshots").mkdir(parents=True, exist_ok=True)
    (output / "screenshots" / "README.md").write_text(
        "# Screenshot status\n\nNo screenshot is represented as captured by automation. A human must capture the running local or hosted demo after reviewing the displayed data; see human-actions.md.\n",
        encoding="utf-8", newline="\n")
    (output / "README.md").write_text(
        "# Local evidence bundle\n\nThis bundle contains only deterministic local synthetic evidence. It does not prove a real insurer, bank, payment, hosted deployment, video, registration, or submission. `scripts/verify_evidence.py` verifies its hashes and the public-fixture sandbox chain.\n",
        encoding="utf-8", newline="\n")
    files = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "manifest.json")
    manifest = {
        "schema_version": 1,
        "scope": "LOCAL_SYNTHETIC_EVIDENCE_ONLY",
        "files": [{"path": path.relative_to(output).as_posix(), "sha256": _sha256(path)} for path in files],
        "limitations": [
            "No external counterparty, deployment, video, registration or submission is claimed.",
            "Screenshot capture remains a human action.",
            "Sandbox signatures use public fixture material for reproducible local verification, not a deployment credential.",
        ],
    }
    _write_json(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    manifest = build()
    print(f"Wrote local evidence bundle with {len(manifest['files'])} hashed files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
