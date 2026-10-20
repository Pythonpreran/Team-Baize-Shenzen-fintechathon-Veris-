# Local five-minute demo runbook

## Scope before starting

VERIS demonstrates evidence-bound, permissioned insurance workflows using controlled local/synthetic data and auditable action controls. Keep the in-app local/synthetic badges and evidence/authorised-review states visible. The runbook presents a local workflow, not a live counterparty or production service; detailed policy, language, deployment, and validation conditions are in [disadvantages.md](disadvantages.md).

## Start from a safe local state

```powershell
.\scripts\start-local-demo.ps1 -Install -SeedCorpus
```

The script sets portfolio storage outside the repository, sets deterministic/offline switches, and binds only `127.0.0.1`. It does not start a public server or read/write tracked secrets. Open `http://127.0.0.1:8000` and sign in as the listed owner demo account.

If the separate local sandbox composition is intentionally used, review [INTEGRATION-READINESS.md](INTEGRATION-READINESS.md) first. It provides the controlled local adapter used by the demonstration.

## Timed path (4:30–5:00)

| Time | Screen/action | Say/show |
| --- | --- | --- |
| 0:00–0:25 | Portfolio → Guided Judge Demo → **Check local prerequisites** | The preflight checks local DB configuration, fixture/corpus presence, role/session state, and offline switches. |
| 0:25–1:10 | Start labelled synthetic household → Ask VERIS | Show a supported cited question, evidence/output state, source drawer, and page/quote. |
| 1:10–1:40 | Ask an outcome question outside the available evidence | Show abstention/authorised-review state and missing document rather than a decision. |
| 1:40–2:35 | Claims | Create/prepare the synthetic packet and show checklist source/missing evidence. |
| 2:35–3:20 | Exact confirmation | Show canonical payload and step-up requirement. Stop before confirmation unless a private local sandbox rehearsal is deliberately configured. |
| 3:20–4:10 | Agent Action Receipt | Show actual consent, plan, payload-hash, sandbox/ledger state. Pending/not-established is the correct state when no action was recorded. |
| 4:10–4:35 | Evaluator evidence export | Download JSON and explain it contains recorded citations, rule details, missing inputs, and action status. |
| 4:35–5:00 | Close | Identify the workflow as a controlled local demonstration and point to [disadvantages.md](disadvantages.md) for the current scope. |

## Recovery without improvisation

1. **Wrong synthetic state:** Use **Reset synthetic example** and enter the displayed confirmation. It removes only server-stamped synthetic records; uploads, ledger history, mandates, and synthetic sandbox-operation logs remain. Privacy erase is separate.
2. **No policy/example loaded:** Reopen Portfolio and use **Start labelled path**. Do not seed a record by editing the database.
3. **Missing wording:** Use the preflight/corpus message and demonstrate the honest missing-wording path. Do not fetch from a publisher during judging or claim corpus-dependent assertions passed.
4. **Browser/session issue:** Close the tab, reopen `http://127.0.0.1:8000`, sign in, and reload the labelled household.
5. **Startup issue:** Stop with `Ctrl+C` and rerun `scripts/start-local-demo.ps1`. Do not bind `0.0.0.0`, use a tunnel, or move the portfolio DB into the checkout.

## Evidence to retain after a rehearsal

- local preflight result;
- cited answer and source drawer;
- abstention/missing-input state;
- packet checklist and confirmation hash;
- Agent Action Receipt and evaluator-evidence JSON;
- if configured, the labelled local synthetic acknowledgement and ledger verification; and
- release-gate logs from `%LOCALAPPDATA%\veris-demo\release-gate`.

For technical controls and the risk/control matrix, use [JUDGE-READINESS.md](JUDGE-READINESS.md). For detailed current scope and implementation conditions, use [disadvantages.md](disadvantages.md).
