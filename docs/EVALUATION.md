# Evaluation

## What can be measured locally

Run the current deterministic scoreboard:

```powershell
python -m eval.bench --all
```

By default it writes `eval/results/scoreboard.json` and `eval/results/scoreboard.md`. `scripts/run-release-gate.ps1` instead writes its generated benchmark output outside the repository with the other actual command logs.

The scoreboard reports the `none` profile separately: local benchmark/control outcomes, local timing, zero provider cost, a narrow exact-citation check, one objective date-answer check, and one appropriate outcome-question refusal. It deliberately separates generic local benchmark/control pass rate from canonical-task coverage. Canonical completion contains only cases explicitly mapped to numbered canonical tasks; red-team and agent-read-plan controls are useful checks but are not denominators for that metric.

## Inspecting a demonstrated record

The owner can download `/api/evaluation/evidence-export` from the **Agent Action Receipt**. It is tenant/role scoped and projects only existing local records:

- session finding output state, evidence class, citation spans, stored rule citations, missing inputs, and deterministic detail where present;
- seeded portfolio evidence item/value/source fields and available generated-schedule quotations;
- claim checklist, packet manifest, deadline, and deterministic settlement-check state; and
- consent, pending canonical confirmation previews, ledger records, and ledger verification.

The export does not rerun a rule, contact any counterparty, reveal secrets, or create a new claim/decision. It is local synthetic evidence, not clinical or insurance-professional validation, ground truth, an accuracy report, a customer result, a real action, or evidence of hosted interoperability.

## Derivation discipline

- `INDEPENDENT_OBJECTIVE` means a count, date, string, or arithmetic result whose expected value can be checked without legal construction.
- `REVIEWED_BY_EXPERT` requires a human reviewer’s declared credentials and an explicit decision in the reviewer console. The harness will not print a general accuracy claim until such labels exist.
- `ENGINE_OUTPUT` is regression evidence only; it is never a ground-truth accuracy denominator.

The reviewer console is available at `/reviewer` to an authenticated `REVIEWER` role. An owner queues server-derived evidence to a reviewer organisation; the reviewer records agree/disagree, severity, comment, and declared credentials. `GET /api/reviewer/export` emits only agreed records with credentials as candidate expert labels. Credentials are not licence verification; retain human proof outside the repository.

## What the scoreboard is not

It is not a professionally reviewed false-alarm rate, a representative real-policy study, a live-model comparison, a hosted latency claim, proof of real-world claim outcomes, proof of a completed coverage correction, buyer validation, or a claim that every canonical task is complete. Those fields remain null, explicitly not run, or limited to their mapped local checks until a human supplies the required evidence.

## Structural alignment after submission v1

`python -m eval.alignment_metrics` measures whether each bound span contains the structural elements its mechanism requires, against team-read labels in `eval/alignment_labels.yaml`. After the home, travel, Hong Kong, and GENERIC-showcase wordings were added it scores 14 documents: 66 correct alignments, no missed or wrong values, no false positives (precision 1.0, recall 1.0), with 507 of 631 proposals rejected by the verifier. The labels are team-read, not expert-reviewed, and this is not an accuracy rate for findings.

Canonical tasks 9 (renewal payment under a mandate), 10 (renewal diff), and 11 (scoped, time-limited sharing of a claim) have local test contracts listed in `evidence/agent-traces/canonical-task-traces.json`. They are not scoreboard benchmark cases, so the scoreboard's canonical-task coverage still counts tasks 1 and 2 only.

## User-study kit

[USER-STUDY-KIT.md](../eval/USER-STUDY-KIT.md) provides the local opt-in task script, SUS instrument, pseudonymous record format, and analysis command. It produces a median paired time-saving and completion rates only from valid opt-in records. No participant result has been entered in this repository.
