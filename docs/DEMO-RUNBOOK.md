# Local demo runbook

## Prepare

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/fetch_corpus.py --seed-from ../06-evidence
python -m uvicorn proven.api:app --port 8000
```

Open `http://localhost:8000`. Keep `VERIS_LLM_PROFILE=none` for a deterministic demonstration. The synthetic insurer/bank composition is optional and local only; see the root README.

## Demonstrate

1. Load the labelled synthetic household through the ordinary upload flow.
2. Ask the declared-condition waiting-period question and open the citation.
3. Show an outcome-question refusal and the exact document needed.
4. Open the protection map and a health illustration.
5. Prepare a synthetic claim; show tier-2 confirmation before any synthetic send.
6. Show the settlement correction and local ledger/sandbox verification if the local composition was prepared.
7. Run `python -m security.redteam`, `python -m eval.bench --all`, and `python scripts/verify_evidence.py`.

## Do not claim

Do not describe this runbook as a hosted demo, a real insurer or bank integration, a real payment, a video, registration, or a submitted entry. Those require human action.
