# VERIS V4

VERIS is an evidence-bound insurance workflow demonstration for households and small businesses. It turns supplied or generated local policy material into page-cited answers, documented gaps and overlaps, deterministic illustrations, and controlled synthetic action preparation.

**Decision boundary:** citations, evidence states, calculations, rule outcomes, and action permissions are deterministic. Optional, consented language assistance may propose or present wording, but it cannot decide coverage, settlement, evidence class, premium, or product choice.

## What VERIS does

| Capability | Current implementation |
| --- | --- |
| Evidence-grounded review | Validates local policy inputs, retains page-cited evidence, and returns documented, missing-information, or authorised-review states instead of inventing an answer. |
| Portfolio understanding | Maintains an encrypted, tenant-scoped household or small-business portfolio and maps documented protection gaps and overlaps. |
| Deterministic workflows | Prepares synthetic claim, query, settlement-check, renewal, and escalation work from evidence and deterministic rules. |
| Permissioned actions | Uses consent, canonical payloads, step-up confirmation, idempotency, and an HMAC-chained receipt ledger before a tier-2 synthetic action. |
| Evaluation evidence | Provides an owner-scoped Agent Action Receipt and local evaluator-evidence export containing recorded citations, rule details, missing inputs, and action state. |

## Architecture

```text
Local policy material or generated synthetic documents
                    |
                    v
      Validated intake and page-cited evidence
                    |
                    v
     Deterministic rules and calculations
                    |
                    v
       Encrypted tenant-scoped portfolio
            |                         |
            v                         v
 Cited answers, maps, drafts   Permissioned agent
                                      |
                                      v
                  Private local synthetic sandbox + receipt ledger
```

Optional language assistance is constrained to source-backed proposal or presentation work. Deterministic evidence, rules, calculations, and action controls remain authoritative.

## Five-minute local demo

Start the local demo using the Windows setup below, then follow this 4–5 minute path:

| Time | Action | Show |
| --- | --- | --- |
| 0:00–0:25 | Open `http://127.0.0.1:8000`, sign in as the listed local demo owner, then open **Portfolio → Guided Judge Demo → Check local prerequisites**. | Local configuration, fixture/corpus presence, role/session state, and offline switches. |
| 0:25–1:40 | Start the labelled synthetic household and open **Ask VERIS**. Ask a supported policy question, then open its cited source. | Evidence state, page citation, and the source drawer. |
| 1:40–2:35 | Ask an outcome question outside the available evidence. | A named missing input or authorised-review state rather than a coverage or payment decision. |
| 2:35–3:20 | Open **Claims** and prepare the synthetic packet/checklist. | Cited checklist items and the exact confirmation boundary. |
| 3:20–5:00 | Open **Agent Action Receipt** and download evaluator evidence. | Recorded consent, plan, payload hash, ledger state, and the owner-scoped JSON export. Stop before confirmation unless a private local sandbox rehearsal has been deliberately configured. |

For recovery steps and the complete walkthrough, see the [local demo runbook](docs/DEMO-RUNBOOK.md).

## Windows setup

### Recommended safe start

From PowerShell in this repository:

```powershell
.\scripts\start-local-demo.ps1 -Install -SeedCorpus
```

The script uses a project-local virtual environment, keeps the portfolio database outside Git, enables local deterministic/offline operation, and binds only to `127.0.0.1`. With `-SeedCorpus`, it verifies locally available corpus material by hash; unavailable or mismatched wording remains visibly absent rather than inferred.

### Manual start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -e .
$env:VERIS_PORTFOLIO_DB_PATH = "$env:LOCALAPPDATA\veris-demo\portfolio.sqlite"
$env:VERIS_LLM_PROFILE = "none"
$env:PROVEN_LIVE_SOURCES = "0"
python -m uvicorn proven.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Keep local mode loopback-only: do not expose it through a LAN bind, tunnel, or public endpoint.

## Test and release gate

| Purpose | Command |
| --- | --- |
| Full Python test suite | `python -m pytest -q -p no:warnings` |
| Release gate | `.\scripts\run-release-gate.ps1` |

The release gate runs corpus status, tests, deployment smoke checks, JavaScript syntax checks, repository and i18n checks, dependency and static-security scans, deterministic red-team checks, the evaluation benchmark, compliance, documentation, and evidence verification. It writes logs outside the repository and preserves corpus-dependent skips as skips.

## Repository structure

| Path | Purpose |
| --- | --- |
| `src/proven/` | FastAPI application, evidence pipeline, deterministic rules, portfolio storage, and permissioned agent runtime. |
| `web/` | Static owner-facing application and localisation resources. |
| `scripts/` | Local startup, release-gate, smoke, corpus, documentation, and evidence checks. |
| `tests/` | Offline unit, integration, security, and delivery-artifact tests. |
| `docs/` | Technical, security, integration, evaluation, deployment, and demo documentation. |
| `corpus/` | Manifest and hashes for third-party material stored outside the repository. |
| `evidence/` and `security/` | Deterministic local evidence bundle and red-team material. |
| `fixtures/` and `demo-documents/` | Labelled synthetic demonstration inputs. |

## Documentation

| Need | Start here |
| --- | --- |
| Documentation index | [docs/README.md](docs/README.md) |
| Architecture and algorithms | [Technical documentation](docs/TECHNICAL.md) |
| Demo walkthrough and recovery | [Local demo runbook](docs/DEMO-RUNBOOK.md) |
| Judge-facing controls and evidence | [Judge-readiness package](docs/JUDGE-READINESS.md) |
| Local synthetic adapter contract | [Integration readiness](docs/INTEGRATION-READINESS.md) |
| Security controls and reporting | [Security policy](SECURITY.md) and [security self-assessment](docs/SECURITY-SELF-ASSESSMENT.md) |
| Local benchmarks and evidence export | [Evaluation](docs/EVALUATION.md) and [evidence bundle](evidence/README.md) |
| Current implementation boundary | [Current scope and implementation conditions](docs/disadvantages.md) |

## Scope and security

VERIS V4 is a controlled local/synthetic demonstration, not a live counterparty or production operation. The private sandbox represents no insurer, bank, TPA, payment rail, claim decision, settlement, or money movement. Third-party policy material remains outside the repository and is tracked through a manifest with source and hash metadata.

Use cited output, missing-information states, and authorised review as the product's decision boundary. Read the [current scope](docs/disadvantages.md), [security policy](SECURITY.md), and [integration-readiness contract](docs/INTEGRATION-READINESS.md) before hosting or considering any external connection.

## Contributing

Contributions should be focused, preserve the evidence and local/synthetic boundaries, include appropriate tests and documentation, and pass the release gate before review.

## License and credits

VERIS is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) and [open-source component notices](OPEN-SOURCE.md) for attribution and third-party-material conditions.

Created by Preran S Gowda, Shamanth M S, Shreya Belavatha Jayaram, and Shashank S.
