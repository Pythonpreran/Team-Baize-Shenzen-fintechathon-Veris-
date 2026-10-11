# Local opt-in user-study kit

This kit is for a human-run study only. It does not collect, transmit, or fabricate participant data. Use random participant codes; do not put names, contact details, policy documents, claims, or medical facts in the study file.

## Before a session

1. Explain that participation is optional and that declining has no effect on product access.
2. Obtain a recorded opt-in, then create a blank local record:
   ```powershell
   python -m eval.user_study --template participant-001.json
   ```
3. Use only the labelled synthetic household and local deterministic profile. Do not use a participant's real policy or a live provider.

## Tasks

For each condition, time completion in seconds and mark completed true/false:

1. Find the cited declared-condition waiting-period date.
2. Identify the neutral “not assessed” cell in the protection map.
3. Prepare a synthetic health-claim checklist without submitting it.
4. Inspect the synthetic settlement check and identify the deduction needing review.

Run each participant once without VERIS and once with VERIS; counterbalance order across participants. Then record all ten SUS answers (1–5) in the same local file.

## Analyse consented records

Combine only opted-in records into a JSON list, then run:

```powershell
python -m eval.user_study --input consented-records.json --output user-study-summary.json
```

The summary reports median paired time saved, completion rates, and median SUS only when valid paired records exist. It is not an expert review, a representative sample, or evidence of real-world claim outcomes.
