# Evaluation

Run the current local deterministic scoreboard:

```powershell
python -m eval.bench --all
```

The generated files are [../eval/results/scoreboard.json](../eval/results/scoreboard.json) and [../eval/results/scoreboard.md](../eval/results/scoreboard.md). They report the `none` profile separately, including local benchmark/control outcomes, local timing, zero provider cost, a narrow exact-citation check, one objective date-answer check, and one appropriate outcome-question refusal.

The scoreboard deliberately separates its generic local benchmark/control pass rate from canonical-task coverage. Canonical completion contains only cases explicitly mapped to numbered canonical tasks; the red-team and agent-read-plan controls are useful checks but are not denominators for that metric.

## Derivation discipline

- `INDEPENDENT_OBJECTIVE` means a count, date, string, or arithmetic result whose expected value can be checked without legal construction.
- `REVIEWED_BY_EXPERT` requires a human reviewer’s declared credentials and an explicit decision in the reviewer console. The harness will not print a general accuracy claim until such labels exist.
- `ENGINE_OUTPUT` is regression evidence only; it is never a ground-truth accuracy denominator.

The reviewer console is available at `/reviewer` to an authenticated `REVIEWER` role. An owner queues server-derived evidence to a reviewer organisation; the reviewer records agree/disagree, severity, comment, and declared credentials. `GET /api/reviewer/export` emits only agreed records with credentials as candidate expert labels. Credentials are not licence verification; retain human proof outside the repository.

## What the scoreboard is not

It is not a professionally reviewed false-alarm rate, a representative real-policy study, a live-model comparison, a hosted latency claim, proof of real-world claim outcomes, or a claim that every canonical task is complete. Those fields remain null, explicitly not run, or limited to their mapped local checks until a human supplies the required evidence.

## User-study kit

[USER-STUDY-KIT.md](../eval/USER-STUDY-KIT.md) provides the local opt-in task script, SUS instrument, pseudonymous record format, and analysis command. It produces a median paired time-saving and completion rates only from valid opt-in records. No participant result has been entered in this repository.
