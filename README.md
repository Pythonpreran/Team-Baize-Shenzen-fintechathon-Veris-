# VERIS

VERIS is an **evidence-bound insurance agent** for households and small businesses. It reads policies already held, answers with page-cited evidence, maps documented gaps and overlaps, prepares local synthetic claim work, and stops for confirmation before a tier-2 synthetic action. A deterministic core computes every number; no model decides coverage, settlement, evidence class, premium, or product choice.

> This repository contains local synthetic demonstrations and evidence only. It does not claim a real insurer/bank integration, payment, hosted deployment, video, registration, or submission.

## Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -e .
python scripts/fetch_corpus.py --seed-from ../06-evidence
python -m uvicorn proven.api:app --port 8000
```

Open `http://localhost:8000`. The default local mode supports the offline synthetic demo. Do not expose it to a network; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for hosted mode.

To run the local synthetic claim-to-correction composition, follow the generated-local-credential instructions in [docs/DEMO-RUNBOOK.md](docs/DEMO-RUNBOOK.md), then run:

```powershell
docker compose up --build
```

The sandbox stays private to the compose network and makes no real external call.

## Verify

```powershell
python -m pytest -q -p no:warnings
python scripts/smoke_deployment.py
node --check web/app.js
node --check web/reviewer.js
python scripts/scan_repo.py
python scripts/check_i18n.py
pip-audit --progress-spinner off
bandit -q -r src -ll
python -m security.redteam
python -m eval.bench --all
python scripts/generate_compliance_matrix.py --check
python scripts/check_docs.py
python scripts/verify_evidence.py
```

## Canonical test cases

1. Cited declared-condition waiting-period answer.
2. Honest abstention naming the missing document.
3. Protection map with documented states and a sourced compulsory alert where evidence permits.
4. Evidence-bound health illustration.
5. Synthetic health-claim intake, checklist, packet, and confirmation boundary.
6. Synthetic insurer-query reply preparation.
7. Synthetic settlement check, dispute preparation, correction receipt, and reconciliation.
8. Synthetic overdue-claim escalation gate.
9. Renewal/payment under mandate — not implemented in this delivery scope.
10. Renewal comparison — local test contract.
11. Adviser file sharing/revocation — not implemented in this delivery scope.
12. Small-business adviser action workflow — local test contract.

## Documentation and evidence

Start at [docs/README.md](docs/README.md). The generated local evidence bundle is in [evidence/README.md](evidence/README.md); verify it with `python scripts/verify_evidence.py`.

Published third-party policy documents are not in the repository. [corpus/manifest.yaml](corpus/manifest.yaml) records their source and hashes; `scripts/fetch_corpus.py` retrieves them into a separate corpus directory. The viewer displays cited excerpts and publisher links rather than full corpus pages.

## Security posture

`VERIS_MODE=local` is an offline demo posture. `VERIS_MODE=hosted` requires invite-only login, CSRF, secure cookies, HSTS, parser limits, outbound allowlisting, and environment-only secrets. Read [docs/SECURITY-SELF-ASSESSMENT.md](docs/SECURITY-SELF-ASSESSMENT.md) before hosting.

## Licence

[Apache License 2.0](LICENSE) — see [NOTICE](NOTICE), [OPEN-SOURCE.md](OPEN-SOURCE.md), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and [SECURITY.md](SECURITY.md).

## Team

Preran S Gowda, Shamanth M S, Shreya Belavatha Jayaram and Shashank S.
