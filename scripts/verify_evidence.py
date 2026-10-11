"""Verify the generated local evidence manifest and public-fixture sandbox chain."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
PUBLIC_FIXTURE_MATERIAL = b"VERIS local synthetic evidence fixture v1; not a secret"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _verify_sandbox(path: Path) -> list[str]:
    errors, previous, expected_tenant_seq = [], "", 1
    key = hashlib.sha256(PUBLIC_FIXTURE_MATERIAL).digest()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            entry = json.loads(line)
            record = {name: entry[name] for name in (
                "seq", "tenant_id", "tenant_seq", "at", "operation", "payload_sha256", "synthetic")}
        except (KeyError, TypeError, json.JSONDecodeError):
            errors.append(f"sandbox line {line_no} is malformed")
            continue
        expected = hmac.new(key, _canonical({"record": record, "previous": previous}).encode("utf-8"), hashlib.sha256).hexdigest()
        if entry.get("prev_hmac") != previous or entry.get("hmac") != expected:
            errors.append(f"sandbox line {line_no} has an invalid HMAC chain")
        if int(record["tenant_seq"]) != expected_tenant_seq:
            errors.append(f"sandbox line {line_no} has a tenant sequence gap")
        previous, expected_tenant_seq = str(entry.get("hmac") or ""), expected_tenant_seq + 1
    if not previous:
        errors.append("sandbox log is empty")
    return errors


def verify(root: Path = EVIDENCE) -> list[str]:
    root = Path(root)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return ["evidence manifest is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["evidence manifest is invalid JSON"]
    errors = []
    for entry in manifest.get("files") or []:
        path = root / str(entry.get("path") or "")
        if not path.is_file():
            errors.append(f"missing evidence file: {entry.get('path')}")
        elif _sha256(path) != entry.get("sha256"):
            errors.append(f"hash mismatch: {entry.get('path')}")
    sandbox_log = root / "sandbox-operations.jsonl"
    if sandbox_log.is_file():
        errors.extend(_verify_sandbox(sandbox_log))
    else:
        errors.append("sandbox operation log is missing")
    traces = root / "agent-traces" / "canonical-task-traces.json"
    try:
        if len(json.loads(traces.read_text(encoding="utf-8"))) != 12:
            errors.append("canonical task trace descriptor does not contain 12 tasks")
    except (OSError, json.JSONDecodeError):
        errors.append("canonical task trace descriptor is invalid")
    return errors


def main() -> int:
    errors = verify()
    if errors:
        print("Evidence verification failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Evidence bundle verified (local synthetic scope).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
