"""Evaluation harness.

THE PROBLEM THIS FILE HAD. The pitch's differentiator was "a measured error rate on a
labelled set". There was one label file, its expectations were a copy of the engine's
own output, and the harness printed precision and recall over it. That measures
self-consistency and nothing else, and precision/recall printed next to a sample size
of one invites exactly the misreading the product exists to prevent.

WHAT REPLACED IT. Labels now declare how they were derived, and derivation decides
what a number may be used for:

  ENGINE_OUTPUT           A regression baseline. Catches a rule silently changing
                          state or class. Contributes NOTHING to any reported
                          accuracy, ever.
  INDEPENDENT_OBJECTIVE   Expectations derived from the documents by comparison,
                          counting or date arithmetic — no legal construction. This
                          is measurable today without a domain reviewer, and it is
                          reported as a scoped number with its scope stated.
  REVIEWED_BY_EXPERT      A named, insurance-qualified reviewer read the clauses and
                          set the expectations. This is the only derivation that
                          supports an unqualified accuracy claim, and there are
                          currently zero such labels.

`accuracy_claim_permitted` is False until a REVIEWED_BY_EXPERT label exists, and
`--require-expert` exits non-zero so a build script cannot publish a number the
evidence does not support.

Partial labels are supported (`scope: PARTIAL` + `out_of_scope_rules`) because an
objective label can only opine on findings with objective ground truth. Rules a label
declines to opine on are excluded from precision AND recall rather than counted as
errors — the alternative is penalising a label for being honest about its scope.

Run:  python -m eval.harness            (from proven/)
      python -m eval.harness --json
      python -m eval.harness --require-expert
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from proven import pipeline  # noqa: E402

LABELS_DIR = HERE / "labels"
ABSTAINING_STATES = {"INFORMATION_MISSING", "REQUIRES_AUTHORISED_REVIEW",
                     "NOT_CONFIRMED"}
RUNNABLE_BUNDLES = {"hock-lee-foods"}

ENGINE_OUTPUT = "ENGINE_OUTPUT"
INDEPENDENT_OBJECTIVE = "INDEPENDENT_OBJECTIVE"
REVIEWED_BY_EXPERT = "REVIEWED_BY_EXPERT"
DERIVATIONS = (ENGINE_OUTPUT, INDEPENDENT_OBJECTIVE, REVIEWED_BY_EXPERT)

# Only these derivations may contribute to a reported measurement.
MEASURING_DERIVATIONS = (INDEPENDENT_OBJECTIVE, REVIEWED_BY_EXPERT)


def load_labels(labels_dir: Path = LABELS_DIR) -> list[dict]:
    labels = []
    for path in sorted(labels_dir.glob("*.json")):
        try:
            labels.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            labels.append({"label_id": path.name, "_invalid": str(exc)})
    return labels


def derivation_of(label: dict) -> str:
    """A label with no declared derivation is treated as ENGINE_OUTPUT — the
    derivation that can never support a claim. Defaulting the other way would let an
    undeclared label silently become evidence."""
    declared = str(label.get("derivation", "")).strip().upper()
    return declared if declared in DERIVATIONS else ENGINE_OUTPUT


def _score_one(label: dict, brief_cache: dict) -> dict:
    """Run the engine for one label's bundle and score it against the expectations."""
    if label.get("_invalid"):
        return {"label_id": label.get("label_id"), "skipped": True,
                "reason": f"invalid JSON: {label['_invalid']}"}
    bundle = label.get("bundle")
    if bundle not in RUNNABLE_BUNDLES:
        return {"label_id": label.get("label_id"), "skipped": True,
                "reason": f"no runner for bundle {bundle!r}"}

    carrier = label.get("carrier", "CHUBB")
    if carrier not in brief_cache:
        brief_cache[carrier] = pipeline.run_case(carrier=carrier)["brief"]
    brief = brief_cache[carrier]

    actual = {f["rule_id"]: f for f in brief["findings"]}
    expected = label.get("expected_findings", {})
    out_of_scope = set(label.get("out_of_scope_rules", []) or [])

    correct = state_mismatch = missed = 0
    mismatches: list[str] = []
    for rule_id, exp in expected.items():
        got = actual.get(rule_id)
        if got is None:
            missed += 1
            mismatches.append(f"MISSED {rule_id}")
        elif (got["state"] == exp.get("output_state")
              and got["class"] == exp.get("evidence_class")):
            correct += 1
        else:
            state_mismatch += 1
            mismatches.append(
                f"MISMATCH {rule_id}: expected {exp.get('output_state')}/"
                f"{exp.get('evidence_class')} got {got['state']}/{got['class']}")

    # A rule the label declines to opine on is not an error. Counting it as one would
    # punish a label for stating the limits of its own authority.
    unexpected = [rid for rid in actual
                  if rid not in expected and rid not in out_of_scope]

    denom_p = correct + state_mismatch + len(unexpected)
    denom_r = correct + state_mismatch + missed
    abstaining = sum(1 for f in brief["findings"]
                     if f["state"] in ABSTAINING_STATES)

    accounting_check = None
    if label.get("expected_accounting"):
        accounting_check = {
            k: {"expected": v, "actual": brief["accounting"].get(k),
                "match": brief["accounting"].get(k) == v}
            for k, v in label["expected_accounting"].items()
        }

    return {
        "label_id": label.get("label_id"),
        "bundle": bundle,
        "derivation": derivation_of(label),
        "reviewer": label.get("reviewer"),
        "scope": label.get("scope", "FULL"),
        "measuring": derivation_of(label) in MEASURING_DERIVATIONS,
        "correct": correct,
        "state_mismatch": state_mismatch,
        "missed": missed,
        "unexpected": len(unexpected),
        "out_of_scope": len(out_of_scope),
        "precision": round(correct / denom_p, 3) if denom_p else 0.0,
        "recall": round(correct / denom_r, 3) if denom_r else 0.0,
        "abstention_rate": (round(abstaining / len(brief["findings"]), 3)
                            if brief["findings"] else 0.0),
        "accounting": brief["accounting"],
        "accounting_check": accounting_check,
        "mismatches": mismatches,
        "unexpected_rules": unexpected,
    }


def _aggregate(scored: list[dict], only_measuring: bool) -> dict:
    rows = [s for s in scored
            if not s.get("skipped") and (s["measuring"] or not only_measuring)]
    agg = {
        "sample_size": len(rows),
        "correct": sum(s["correct"] for s in rows),
        "state_mismatch": sum(s["state_mismatch"] for s in rows),
        "missed": sum(s["missed"] for s in rows),
        "unexpected": sum(s["unexpected"] for s in rows),
    }
    tp = agg["correct"]
    denom_p = tp + agg["state_mismatch"] + agg["unexpected"]
    denom_r = tp + agg["state_mismatch"] + agg["missed"]
    agg["precision"] = round(tp / denom_p, 3) if denom_p else 0.0
    agg["recall"] = round(tp / denom_r, 3) if denom_r else 0.0
    agg["assertions_scored"] = denom_r
    return agg


def run_eval(labels_dir: Path = LABELS_DIR) -> dict:
    labels = load_labels(labels_dir)
    brief_cache: dict = {}
    scored = [_score_one(label, brief_cache) for label in labels]
    runnable = [s for s in scored if not s.get("skipped")]

    by_derivation: dict[str, int] = {}
    for s in runnable:
        by_derivation[s["derivation"]] = by_derivation.get(s["derivation"], 0) + 1

    expert = [s for s in runnable if s["derivation"] == REVIEWED_BY_EXPERT]
    objective = [s for s in runnable if s["derivation"] == INDEPENDENT_OBJECTIVE]

    return {
        "labels_total": len(labels),
        "skipped": sum(1 for s in scored if s.get("skipped")),
        "by_derivation": by_derivation,
        # Every label, including the engine-output baseline. Diagnostic only.
        "all_labels": _aggregate(scored, only_measuring=False),
        # Only independently derived labels. This is the number that may be reported.
        "measured": _aggregate(scored, only_measuring=True),
        "objective_measurement_available": bool(objective),
        "expert_reviewed_measurement_available": bool(expert),
        "accuracy_claim_permitted": bool(expert),
        "claim_language": _claim_language(objective, expert),
        "blocked_on": ([] if expert else [
            "No label carries derivation REVIEWED_BY_EXPERT. An unqualified accuracy "
            "claim needs a named insurance-qualified reviewer who set the expected "
            "state and class by reading the clauses — in particular deciding whether "
            "a term is a warranty or a condition precedent, which the engine "
            "deliberately refuses to decide.",
        ]),
        "per_label": scored,
    }


def _claim_language(objective: list[dict], expert: list[dict]) -> str:
    """The exact sentence the evidence supports. Written here so nobody has to invent
    one under time pressure on a slide."""
    if expert:
        return ("An expert-reviewed labelled set exists. Report precision and recall "
                "from `measured`, with the sample size and the reviewer named.")
    if objective:
        agg = _aggregate(objective, only_measuring=True)
        return (
            f"SCOPED CLAIM ONLY. On {agg['assertions_scored']} assertions whose "
            f"ground truth is objective — counts, dates and arithmetic read from the "
            f"documents, with no legal construction — the engine agrees with "
            f"independently derived labels on {agg['correct']} "
            f"(precision {agg['precision']}, recall {agg['recall']}, "
            f"n={agg['sample_size']} bundle(s)). This is NOT a general accuracy rate "
            f"and must not be compared with a general one: every finding requiring "
            f"legal construction is deliberately out of scope, and those are the "
            f"findings a general rate would cover.")
    return ("NO MEASUREMENT EXISTS. Do not state an accuracy figure. Lead with the "
            "cross-carrier divergence table, which is real, page-cited and "
            "reproducible.")


def print_report(report: dict) -> None:
    print("=" * 74)
    print("PROVEN evaluation")
    print("=" * 74)
    print(f"Labels: {report['labels_total']} total, {report['skipped']} skipped "
          f"(no runner)")
    print(f"By derivation: {report['by_derivation'] or '{}'}")
    print("-" * 74)

    if not report["accuracy_claim_permitted"]:
        print("!! NO EXPERT-REVIEWED LABELS. An unqualified accuracy claim is NOT")
        print("!! supported by this repository. See `blocked_on` below.")
        print("-" * 74)

    if report["objective_measurement_available"]:
        m = report["measured"]
        print("MEASURED (independently derived labels only)")
        print(f"  Bundles: {m['sample_size']}   Assertions scored: "
              f"{m['assertions_scored']}")
        print(f"  Correct: {m['correct']}   State/class mismatch: "
              f"{m['state_mismatch']}   Missed: {m['missed']}   "
              f"Unexpected: {m['unexpected']}")
        print(f"  Precision: {m['precision']}   Recall: {m['recall']}")
    else:
        print("MEASURED: nothing. No independently derived label exists.")
    print("-" * 74)
    print("CLAIM LANGUAGE THE EVIDENCE SUPPORTS:")
    for line in _wrap(report["claim_language"], 70):
        print(f"  {line}")
    if report["blocked_on"]:
        print("-" * 74)
        print("BLOCKED ON:")
        for item in report["blocked_on"]:
            for line in _wrap(item, 70):
                print(f"  {line}")
    print("-" * 74)
    for s in report["per_label"]:
        if s.get("skipped"):
            print(f"  {s['label_id']}: SKIPPED — {s['reason']}")
            continue
        tag = "MEASURING" if s["measuring"] else "baseline only"
        print(f"  {s['label_id']} [{s['bundle']}] {s['derivation']} ({tag})")
        print(f"     P={s['precision']} R={s['recall']} "
              f"abstention={s['abstention_rate']} scope={s['scope']} "
              f"out_of_scope={s['out_of_scope']}")
        a = s["accounting"]
        print(f"     accounting: {a['detected']} detected / {a['classified']} "
              f"classified ({a['verified_classified']} verified + "
              f"{a['keyword_classified']} keyword) / {a['unaccounted']} unaccounted")
        for m in s["mismatches"]:
            print(f"     {m}")
        if s["unexpected_rules"]:
            print(f"     UNEXPECTED: {', '.join(s['unexpected_rules'])}")
        if s["accounting_check"]:
            bad = {k: v for k, v in s["accounting_check"].items() if not v["match"]}
            if bad:
                print(f"     ACCOUNTING DRIFT: {bad}")
    print("=" * 74)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the PROVEN evaluation harness.")
    parser.add_argument("--json", action="store_true",
                        help="emit the raw report as JSON")
    parser.add_argument("--require-expert", action="store_true",
                        help="exit non-zero unless an expert-reviewed label exists")
    args = parser.parse_args(argv)

    report = run_eval()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)
    if args.require_expert and not report["accuracy_claim_permitted"]:
        print("\nFAILED --require-expert: no REVIEWED_BY_EXPERT label present.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
