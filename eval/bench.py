"""Evaluation v2 benchmark and honest scoreboard generator.

The runner reports what it measured under the deterministic ``none`` profile and keeps
unmeasured claims as ``null`` with a reason. It never substitutes model, expert, or
human-study numbers that do not exist.

Run ``python -m eval.bench --all`` from the repository root.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from proven import persistence, qa  # noqa: E402
from proven.agent import AgentRuntime, AgentStore  # noqa: E402
from security.redteam import runner as redteam  # noqa: E402

DEFAULT_SEED = 20261012
DEFAULT_OUTPUT_DIR = HERE / "results"

# Only these fixture checks map directly to numbered canonical tasks in the plan. The
# agent read-plan and red-team checks remain useful benchmark controls, but are not
# silently counted as canonical-task completion.
_CANONICAL_TASK_CASES: dict[str, int] = {
    "canonical.cited_answer": 1,
    "canonical.honest_abstention": 2,
}


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _sample_portfolio() -> dict:
    return {
        "members": [{
            "id": "member-mum", "name": "Lakshmi Rao", "relationship": "parent",
            "health_facts": "declared diabetes",
        }],
        "policies": [{
            "id": "policy-health", "line_of_business": "HEALTH",
            "insured_member_ids": ["member-mum"],
            "health_items": [{
                "mechanism_code": "WAITING.PED", "state": "DOCUMENTED",
                "evidence_class": "PROVEN", "value": "2028-01-01",
                "member_scope": [{"id": "member-mum", "name": "Lakshmi Rao"}],
                "citation": {"document": "Synthetic health schedule", "document_id": "doc-1",
                             "page": 1, "quote": "Declared diabetes waiting period ends on 1 January 2028."},
            }],
        }],
    }


def _case(name: str, action: Callable[[], bool], *, measure: bool) -> dict:
    started = perf_counter()
    try:
        passed = bool(action())
        error = None
    except Exception as exc:  # evidence must state a local failure, not terminate silently
        passed, error = False, type(exc).__name__
    elapsed = (perf_counter() - started) * 1000 if measure else None
    return {"id": name, "passed": passed, "error": error,
            "latency_ms": round(elapsed, 3) if elapsed is not None else None}


def _qa_cited_answer() -> bool:
    answer = qa.answer("When does Mum's diabetes waiting period end?", _sample_portfolio())
    return (answer.output_state == "DOCUMENTED" and answer.evidence_class == "PROVEN"
            and answer.citations and "2028" in answer.text)


def _qa_abstention() -> bool:
    answer = qa.answer("Will they pay for surgery?", _sample_portfolio())
    return answer.output_state == "REQUIRES_AUTHORISED_REVIEW" and not answer.citations


def _agent_read_plan() -> bool:
    with tempfile.TemporaryDirectory(prefix="veris-bench-") as directory:
        runtime = AgentRuntime(AgentStore(Path(directory) / "agent.sqlite", b"b" * 32))
        scope = persistence.TenantScope("benchmark", "owner", "Owner", "owner@example.test", "SME")
        runtime.store.grant_consent(scope.tenant_id, ["agent:read"])
        plan = runtime.plan(scope, intent="Read my policy", origin="USER")
        return not plan["refused"] and plan["steps"] and len(plan["steps"]) <= plan["budgets"]["max_steps"]


def _red_team_clean() -> bool:
    return redteam.run(seed=DEFAULT_SEED)["summary"]["failed"] == 0


def _objective_metrics() -> dict:
    # These two deterministic questions have known answers derived from the fixture,
    # rather than copied from a model output or an expert judgement.
    answer = qa.answer("When does Mum's diabetes waiting period end?", _sample_portfolio())
    abstention = qa.answer("Will they pay for surgery?", _sample_portfolio())
    citation_ok = bool(answer.citations and answer.citations[0]["quote"]
                       == "Declared diabetes waiting period ends on 1 January 2028.")
    return {
        "objective_qa_accuracy": {
            "value": ratio(int(answer.output_state == "DOCUMENTED"), 1),
            "numerator": int(answer.output_state == "DOCUMENTED"), "denominator": 1,
            "derivation": "INDEPENDENT_OBJECTIVE", "scope": "one synthetic date fact",
        },
        "citation_fidelity": {
            "value": ratio(int(citation_ok), 1), "numerator": int(citation_ok), "denominator": 1,
            "derivation": "INDEPENDENT_OBJECTIVE", "scope": "one exact synthetic citation",
        },
        "unsupported_answer_rate": {
            "value": ratio(int(abstention.output_state != "REQUIRES_AUTHORISED_REVIEW"), 1),
            "numerator": int(abstention.output_state != "REQUIRES_AUTHORISED_REVIEW"), "denominator": 1,
            "derivation": "INDEPENDENT_OBJECTIVE", "scope": "one outcome-question refusal",
        },
        "unnecessary_refusal_rate": {
            "value": 0.0 if answer.output_state == "DOCUMENTED" else 1.0,
            "numerator": int(answer.output_state != "DOCUMENTED"), "denominator": 1,
            "derivation": "INDEPENDENT_OBJECTIVE", "scope": "one documented date fact",
        },
        "false_alarm_rate": {
            "value": None, "reason": "No professionally reviewed gap-finding labels are present.",
        },
        "general_accuracy": {
            "value": None, "reason": "No REVIEWED_BY_EXPERT benchmark labels are present.",
        },
    }


def run_all(*, seed: int = DEFAULT_SEED, measure: bool = True) -> dict:
    """Run deterministic benchmark cases; fixed seed stabilises outcomes, not wall time."""
    previous_profile = os.environ.get("VERIS_LLM_PROFILE")
    os.environ["VERIS_LLM_PROFILE"] = "none"
    try:
        cases = [
            _case("canonical.cited_answer", _qa_cited_answer, measure=measure),
            _case("canonical.honest_abstention", _qa_abstention, measure=measure),
            _case("control.agent_read_plan", _agent_read_plan, measure=measure),
            _case("security.redteam", _red_team_clean, measure=measure),
        ]
    finally:
        if previous_profile is None:
            os.environ.pop("VERIS_LLM_PROFILE", None)
        else:
            os.environ["VERIS_LLM_PROFILE"] = previous_profile
    benchmark_completed = sum(int(case["passed"]) for case in cases)
    canonical_cases = [case for case in cases if case["id"] in _CANONICAL_TASK_CASES]
    canonical_completed = sum(int(case["passed"]) for case in canonical_cases)
    latencies = [case["latency_ms"] for case in cases if case["latency_ms"] is not None]
    median_latency = sorted(latencies)[len(latencies) // 2] if latencies else None
    return {
        "schema_version": 3,
        "seed": seed,
        "profile": "none",
        "execution": "LOCAL_DETERMINISTIC",
        "network_access": False,
        "cases": cases,
        "metrics": _objective_metrics(),
        "benchmark_completion": {
            "completed": benchmark_completed, "executed": len(cases),
            "rate": ratio(benchmark_completed, len(cases)),
            "scope": "local deterministic benchmark/control set, including non-canonical controls.",
        },
        "canonical_task_completion": {
            "completed": canonical_completed, "executed": len(canonical_cases),
            "rate": ratio(canonical_completed, len(canonical_cases)),
            "task_ids": sorted(_CANONICAL_TASK_CASES.values()),
            "case_to_task": dict(_CANONICAL_TASK_CASES),
            "scope": "only explicitly mapped canonical-task checks in this narrow local subset; not a claim that every canonical task is externally complete.",
            "not_measured": [
                "completed real-world claims, deployments, registrations, submissions and videos",
                "professional-review false-alarm rate", "live-provider profile comparison",
            ],
        },
        "efficiency": {
            "median_latency_ms": median_latency,
            "steps_per_task": {case["id"]: (2 if case["id"] == "control.agent_read_plan" else 1)
                               for case in cases},
            "estimated_cost_usd": 0.0,
            "cost_basis": "profile none; no model transport was attempted.",
        },
        "profiles_not_run": {
            "openai": "No live provider run was requested or performed.",
            "deepseek": "No live provider run was requested or performed.",
            "qwen": "No live provider run was requested or performed.",
            "ollama": "No local model runtime was configured for this benchmark.",
        },
        "derivation_limits": [
            "Objective fixture checks are narrow and do not establish general insurance accuracy.",
            "No human reviewer credentials or expert labels are embedded in this repository.",
            "Latency is local-process timing, not hosted or user-study timing.",
        ],
    }


def markdown(report: dict) -> str:
    benchmark = report["benchmark_completion"]
    completion = report["canonical_task_completion"]
    metrics = report["metrics"]
    lines = [
        "# VERIS evaluation v2 scoreboard", "",
        f"- Profile: `{report['profile']}`", f"- Execution: `{report['execution']}`",
        f"- Network access: `{report['network_access']}`", f"- Seed: `{report['seed']}`", "",
        "## Local deterministic benchmark and control checks", "",
        f"{benchmark['completed']} / {benchmark['executed']} executed benchmark/control cases passed "
        f"(rate `{benchmark['rate']}`). {benchmark['scope']}", "",
        "| Case | Result | Local latency (ms) |", "| --- | --- | ---: |",
    ]
    for case in report["cases"]:
        lines.append(f"| {case['id']} | {'PASS' if case['passed'] else 'FAIL'} | {case['latency_ms'] if case['latency_ms'] is not None else 'not measured'} |")
    lines.extend([
        "", "## Explicitly mapped canonical task coverage", "",
        f"{completion['completed']} / {completion['executed']} explicitly mapped canonical-task checks passed "
        f"(rate `{completion['rate']}`) for task ids `{completion['task_ids']}`. {completion['scope']}",
        "", "| Canonical task | Benchmark case |", "| ---: | --- |",
    ])
    for case_id, task_id in completion["case_to_task"].items():
        lines.append(f"| {task_id} | {case_id} |")
    lines.extend(["", "## Metrics and derivations", ""])
    for name, value in metrics.items():
        if value.get("value") is None:
            lines.append(f"- **{name}**: not reported — {value.get('reason')}")
        else:
            lines.append(f"- **{name}**: `{value['value']}` ({value.get('numerator')}/{value.get('denominator')}); {value.get('derivation')}, {value.get('scope')}")
    lines.extend(["", "## What these numbers are not", ""])
    lines.extend(f"- {item}" for item in report["derivation_limits"])
    lines.extend(["", "## Not run", ""])
    lines.extend(f"- **{profile}**: {reason}" for profile, reason in report["profiles_not_run"].items())
    return "\n".join(lines) + "\n"


def write(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output_dir / "scoreboard.json", output_dir / "scoreboard.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    markdown_path.write_text(markdown(report), encoding="utf-8", newline="\n")
    return json_path, markdown_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic VERIS evaluation v2 benchmark.")
    parser.add_argument("--all", action="store_true", help="run every local deterministic benchmark case")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    report = run_all(seed=args.seed, measure=True)
    json_path, markdown_path = write(report, args.output_dir)
    print(f"Wrote {json_path} and {markdown_path}.")
    return 0 if all(case["passed"] for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
