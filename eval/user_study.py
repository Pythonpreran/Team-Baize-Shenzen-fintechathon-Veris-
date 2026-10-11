"""Local opt-in user-study record validation and analysis.

Participants are represented by random study codes, never names, email addresses or
policy documents. This helper turns consented before/after task timing plus a SUS
questionnaire into an honest aggregate; it reports no result until paired records exist.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

SUS_QUESTIONS = 10
TASKS = ("cited_answer", "protection_map", "claim_preparation", "settlement_check")


def template() -> dict:
    return {
        "schema_version": 1,
        "consent": {"opted_in": False, "recorded_at": None},
        "participant_code": "",
        "tasks": [{"task": task, "condition": "without_veris", "seconds": None, "completed": None}
                  for task in TASKS] +
                 [{"task": task, "condition": "with_veris", "seconds": None, "completed": None}
                  for task in TASKS],
        "sus": [None] * SUS_QUESTIONS,
    }


def _validate_record(record: dict) -> dict:
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("Each study record must use schema_version 1.")
    if record.get("consent", {}).get("opted_in") is not True:
        raise ValueError("A record without explicit opt-in cannot be analysed.")
    code = "".join(str(record.get("participant_code") or "").split())
    if not 3 <= len(code) <= 80:
        raise ValueError("Use a non-identifying participant code of 3–80 characters.")
    tasks = record.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Study tasks must be a list.")
    sus = record.get("sus")
    if not isinstance(sus, list) or len(sus) != SUS_QUESTIONS:
        raise ValueError("SUS requires exactly 10 answers.")
    if any(not isinstance(answer, int) or not 1 <= answer <= 5 for answer in sus):
        raise ValueError("Each SUS answer must be an integer from 1 to 5.")
    return record


def sus_score(answers: list[int]) -> float:
    score = sum((answer - 1) if index % 2 == 0 else (5 - answer)
                for index, answer in enumerate(answers)) * 2.5
    return round(score, 1)


def analyse(records: list[dict]) -> dict:
    validated = [_validate_record(record) for record in records]
    paired: dict[str, dict[str, dict]] = {}
    for record in validated:
        for task in record["tasks"]:
            if not isinstance(task, dict) or task.get("task") not in TASKS:
                continue
            condition = task.get("condition")
            seconds = task.get("seconds")
            completed = task.get("completed")
            if condition not in {"with_veris", "without_veris"} or not isinstance(seconds, (int, float)):
                continue
            if seconds < 0 or not isinstance(completed, bool):
                continue
            paired.setdefault(record["participant_code"], {}).setdefault(task["task"], {})[condition] = task
    savings, completions = [], []
    for participant in paired.values():
        for task in participant.values():
            before, after = task.get("without_veris"), task.get("with_veris")
            if before is None or after is None or before["seconds"] <= 0:
                continue
            savings.append((before["seconds"] - after["seconds"]) / before["seconds"])
            completions.append({"without_veris": before["completed"], "with_veris": after["completed"]})
    scores = [sus_score(record["sus"]) for record in validated]
    return {
        "participants_opted_in": len(validated),
        "paired_task_observations": len(savings),
        "median_time_saved_fraction": round(statistics.median(savings), 4) if savings else None,
        "completion_rate_without_veris": (round(sum(item["without_veris"] for item in completions) / len(completions), 4)
                                            if completions else None),
        "completion_rate_with_veris": (round(sum(item["with_veris"] for item in completions) / len(completions), 4)
                                         if completions else None),
        "median_sus": round(statistics.median(scores), 1) if scores else None,
        "limits": ("No result is reported without paired opt-in task records. This is a local "
                   "study aggregate, not a claim about a population or clinical outcome."),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or analyse opt-in VERIS user-study records.")
    parser.add_argument("--template", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.template:
        args.template.write_text(json.dumps(template(), indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {args.template}")
        return 0
    if not args.input or not args.output:
        parser.error("use --template, or both --input and --output")
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else [raw]
    result = analyse(records)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
