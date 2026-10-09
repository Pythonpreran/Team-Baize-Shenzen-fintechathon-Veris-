# Label file schema

One JSON file per document bundle. The harness (`eval/harness.py`) scores the
engine's output against these expectations.

```jsonc
{
  "label_id": "string, unique",              // e.g. "reviewer-acme-002"
  "_status": "string",                       // MUST begin with "REVIEWED by <name>, <date>"
                                             // once a domain reviewer has set it.
                                             // Anything starting "EXAMPLE" is treated as
                                             // a non-authoritative smoke baseline.
  "bundle": "string",                        // which document bundle. Only
                                             // "hock-lee-foods" is runnable today.
  "carrier": "CHUBB | MSIG | LIBERTY",       // which wording the bundle pairs with
  "expected_accounting": {
    "classified": 9                          // optional sanity expectations
  },
  "expected_findings": {
    "<RULE_ID>": {
      "output_state": "DOCUMENTED | NOT_CONFIRMED | INFORMATION_MISSING | REQUIRES_AUTHORISED_REVIEW",
      "evidence_class": "PROVEN | DECLARED | MARKET_CONVENTION | UNESTABLISHED"
    }
    // ... one entry per finding the reviewer expects
  }
}
```

## Scoring

For each expected rule the harness records:

| Outcome | Meaning |
|---|---|
| **correct** | emitted with the expected state AND class |
| **state_mismatch** | emitted but state or class differs |
| **missed** | expected but not emitted |
| **unexpected** | emitted but not in the label |

precision = correct / (correct + state_mismatch + unexpected)
recall    = correct / (correct + state_mismatch + missed)

It also reports the **abstention rate** (share of emitted findings that escalate or
ask for information) and the **bounded-omission coverage** (detected / classified /
unaccounted).

## The rule that matters most

The engine's output is NOT the label. An `EXAMPLE`-status file that copies engine
output measures nothing but self-consistency. A real label is a reviewer reading the
clauses and deciding, independently, what each finding's state and class should be —
in particular whether a term is a warranty or a condition precedent, which the engine
deliberately refuses to decide. Sample size is printed on every report so a small
labelled set is never mistaken for a large one.
