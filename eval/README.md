# PROVEN evaluation harness

Definition-of-done item 9: produce an accuracy table from whatever labelled set
exists, **with the sample size printed**. This is the scaffold where a
domain-reviewed labelled set lands. It is deliberately built before the labels
exist, so the moment a reviewer says yes there is somewhere to put their judgments
and a command that turns them into a table.

## Run it

```powershell
# from proven/, with the venv active
python -m eval.harness
```

Output is an accuracy table: correct / state-mismatch / missed / unexpected,
precision, recall, abstention rate, and the bounded-omission coverage — with the
sample size on top and a loud banner while the set is EXAMPLE-only.

## Status: EXAMPLE only

`labels/example-label.json` mirrors the engine's current output as a smoke baseline.
It is **not ground truth** and its numbers must never appear on a slide as an
accuracy claim. This is the honest state of open item 5 in
`00-current/technical-implementation.md §13`: the measured error rate does not exist
yet because no insurance-qualified reviewer has labelled a set.

## To make it real

1. Name a reviewer who can distinguish a warranty from a condition precedent
   (the binding constraint — open item 5).
2. Have them set `expected_findings` in each label by reading the clauses, not by
   copying the engine. Set `_status` to `REVIEWED by <name>, <date>`.
3. Add more bundles. Only `hock-lee-foods` runs today; a loader for real submitted
   bundles is the next engineering step (see `RUNNABLE_BUNDLES` in `harness.py`).
4. Re-run. The printed sample size is the honest denominator for any claim made
   from the table.

## Layout

```
eval/
  harness.py            the runner and scorer
  labels/
    SCHEMA.md           the label record format
    example-label.json  EXAMPLE — not domain-reviewed
```
