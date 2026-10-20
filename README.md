# VERIS

**FinTechathon submission by Team Baize**
**2026 Shenzhen International FinTech Competition · International Track · Topic D — Insurance Assistant**

VERIS is an evidence-bound insurance workflow for policyholders, households, and small businesses. It helps people work from the policy material they hold: understand what the evidence says, identify documented gaps or overlaps, see what evidence is missing, and take the next safe action without presenting an unsupported coverage or payment outcome as fact.

## The problem

Policyholders and SMEs often face fragmented policy evidence, unclear coverage boundaries, incomplete claim or renewal paperwork, and uncertainty about what to do next. A useful assistant must make the evidence and uncertainty visible rather than turn incomplete documents into confident conclusions.

## The solution

VERIS combines natural-language interaction with deterministic authority for policy evidence, rules, calculations, and action permissions. It returns page-cited answers when support exists; names missing information or requires authorised review when it does not; and prepares controlled synthetic actions only within explicit consent and confirmation boundaries. Every eligible action is bound to a canonical payload, a confirmation step, and an auditable receipt so that the workflow remains inspectable.

## Evaluation alignment

| Evaluation theme | What evaluators can inspect in VERIS |
| --- | --- |
| **Task Completion** | Cited policy questions, documented gap and overlap mapping, missing-evidence states, synthetic claim-work preparation, and an owner-scoped evidence export support an end-to-end review workflow. |
| **Security & Compliance** | Deterministic evidence and calculation paths, tenant-scoped encrypted storage, consent, exact-payload confirmation, step-up controls, idempotency, and an HMAC-chained receipt ledger make authority and action boundaries visible. |
| **Innovation & Interaction** | Natural-language assistance is separated from consequential decisions. The guided local flow, source drawer, abstention states, action receipt, and evaluator evidence export make both capability and uncertainty easy to inspect. |

These are evaluation-alignment points, not performance claims or outcome metrics. The local evidence and validation materials show the recorded behaviour of this build under its documented conditions.

## Current capabilities

| Capability | Current behaviour |
| --- | --- |
| Evidence-bound review | Validates supplied or generated local material, retains cited source spans, and presents documented, missing-information, or authorised-review states. |
| Portfolio understanding | Maintains a tenant-scoped encrypted household or small-business portfolio and maps documented protection gaps and overlaps. |
| Deterministic calculations | Uses deterministic rules and `Decimal` arithmetic for supported illustrations; missing operands remain missing rather than being substituted. |
| Controlled action preparation | Prepares typed synthetic claim, query, settlement-check, renewal, and escalation work under a fixed permission model. |
| Consent and confirmation | Requires active consent, a canonical payload, and step-up confirmation for eligible synthetic actions; mandate checks apply where required. |
| Auditability | Records plans, confirmations, tool results, and verification state in an HMAC-chained ledger and exposes an owner-scoped Agent Action Receipt. |

## Compact architecture

```text
Owner-held policy material and supplied facts
                  |
                  v
Validated intake → retained page-cited evidence
                  |
                  v
Deterministic evidence grading, policy rules, and calculations
                  |
                  v
Encrypted tenant-scoped portfolio
        |                                  |
        v                                  v
Cited answers, gap maps, and abstention   Permissioned action runtime
                                           |
                                           v
                         Consent → canonical payload → confirmation
                                           |
                                           v
                    Private local synthetic sandbox + receipt ledger
```

Optional language assistance can propose or present wording, but it is outside the decision path. VERIS keeps evidence states, calculations, coverage boundaries, and action permissions deterministic.

## Run the safe local demo

Use PowerShell from the repository root with Python 3.11 or newer:

```powershell
.\scripts\start-local-demo.ps1 -Install -SeedCorpus
```

This is the reproducible safe-default path. It creates or uses a project-local virtual environment, stores the portfolio database outside the repository, selects local deterministic operation, disables live sources, and binds the server only to `127.0.0.1`. Corpus seeding checks locally available material by hash; unavailable or mismatched wording is surfaced as absent rather than inferred.

Open `http://127.0.0.1:8000` and sign in with the listed local demo owner account. Keep local mode loopback-only: do not bind it to a LAN address, tunnel it, or expose it through a public endpoint. For setup recovery and operational detail, read the [local demo runbook](docs/DEMO-RUNBOOK.md).

### Manual local setup

For a developer-managed local environment, use the same local-only defaults explicitly:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -e .
$env:VERIS_MODE = "local"
$env:VERIS_PORTFOLIO_DB_PATH = "$env:LOCALAPPDATA\veris-demo\portfolio.sqlite"
$env:VERIS_LLM_PROFILE = "none"
$env:PROVEN_LIVE_SOURCES = "0"
python -m uvicorn proven.api:app --host 127.0.0.1 --port 8000
```

## 4–5 minute judge demo

| Time | Demonstrate | What it establishes |
| --- | --- | --- |
| 0:00–0:25 | In **Portfolio**, open **Guided Judge Demo** and run **Check local prerequisites**. | The local configuration, fixture or corpus presence, role/session state, and offline switches are visible. |
| 0:25–1:10 | Start the labelled synthetic household, open **Ask VERIS**, ask a supported question, and open its source drawer. | The answer is tied to an evidence state and page citation. |
| 1:10–1:40 | Ask an outcome question that the available evidence cannot support. | VERIS names the missing input or requires authorised review instead of inventing a coverage or payment decision. |
| 1:40–2:35 | Open **Claims** and prepare the synthetic packet or checklist. | The checklist distinguishes cited material from missing evidence and keeps preparation separate from a real submission. |
| 2:35–3:20 | Show the canonical payload and step-up confirmation boundary. | The user-facing confirmation is exact and deliberate; stop before confirmation unless a private local synthetic rehearsal was deliberately configured. |
| 3:20–5:00 | Open **Agent Action Receipt** and download evaluator evidence. | Recorded consent, plan, payload hash, ledger state, citations, rule details, missing inputs, and action status can be inspected. |

The [judge-readiness package](docs/JUDGE-READINESS.md) and [demo runbook](docs/DEMO-RUNBOOK.md) provide the complete walkthrough and recovery guidance.

## Validation and release gates

Run these commands from the repository root after using the local setup above:

| Purpose | Command | What the output establishes |
| --- | --- | --- |
| Check Markdown links | `.\.venv\Scripts\python.exe scripts\check_docs.py` | `Documentation links are valid` confirms that repository-local Markdown targets resolve. |
| Run the Python suite | `.\.venv\Scripts\python.exe -m pytest -q -p no:warnings -ra` | Reports the current offline test results and explicitly preserves corpus-dependent skips; a skip is not a pass. |
| Run the release gate | `.\scripts\run-release-gate.ps1` | Runs corpus status, tests, deployment smoke checks, JavaScript syntax checks, repository and i18n checks, dependency and static-security scans, deterministic red-team checks, the evaluation benchmark, compliance, documentation, and evidence verification. It writes command logs outside the repository and reports their location. |

A successful command establishes only the checks it actually ran in the local environment. It does not convert unavailable corpus material, human review, or external validation into a passing claim.

## Repository guide

| Path | Purpose |
| --- | --- |
| `src/proven/` | FastAPI application, evidence pipeline, deterministic rules, portfolio storage, and permissioned agent runtime. |
| `web/` | Static owner-facing application, accessibility behaviour, and localisation resources. |
| `scripts/` | Local startup, release-gate, corpus, documentation, and evidence-validation tools. |
| `tests/` | Offline unit, integration, security, and delivery-artifact tests. |
| `docs/` | Technical, security, integration, evaluation, deployment, and demo documentation. |
| `corpus/` | Manifest and hashes for third-party material stored outside the repository. |
| `evidence/` and `security/` | Deterministic local evidence and red-team materials. |
| `fixtures/` and `demo-documents/` | Labelled synthetic demonstration inputs. |

## Documentation

| Need | Start here |
| --- | --- |
| Documentation index | [docs/README.md](docs/README.md) |
| Architecture and algorithms | [Technical documentation](docs/TECHNICAL.md) |
| Local setup and walkthrough | [Local demo runbook](docs/DEMO-RUNBOOK.md) |
| Judge-facing controls and evidence | [Judge-readiness package](docs/JUDGE-READINESS.md) |
| Local synthetic adapter boundary | [Integration readiness](docs/INTEGRATION-READINESS.md) |
| Validation and local evidence | [Evaluation](docs/EVALUATION.md) and [evidence bundle](evidence/README.md) |
| Security and current scope | [Security policy](SECURITY.md), [security self-assessment](docs/SECURITY-SELF-ASSESSMENT.md), and [scope and implementation conditions](docs/disadvantages.md) |

## Scope boundary

VERIS is a controlled local and synthetic demonstration, not a live insurance, banking, TPA, payment, claims, settlement, or production service. Its private sandbox represents no external counterparty and creates no real transaction, claim submission, approval, settlement, or money movement. VERIS does not decide coverage, underwriting, claim outcomes, premiums, product choice, legal matters, or medical matters; it makes evidence and review boundaries visible for human or authorised review.

Published third-party policy material remains outside the repository and is tracked through manifest and hash metadata. Synthetic inputs are labelled as such. Read the [current scope and implementation conditions](docs/disadvantages.md) before treating this demonstration as a hosting, integration, or professional-decision solution.

## Team

**Baize**

Preran S Gowda · Shamanth M S · Shreya Belavatha Jayaram · Shashank S

## Contributing and license

Contributions should preserve the evidence, deterministic-authority, consent, and local/synthetic boundaries; include appropriate tests and documentation; and pass the release gate before review.

VERIS is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) and [open-source component notices](OPEN-SOURCE.md) for attribution and third-party-material conditions.
