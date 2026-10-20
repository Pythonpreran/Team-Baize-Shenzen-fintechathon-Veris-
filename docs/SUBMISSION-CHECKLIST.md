# Submission package checklist

Use [disadvantages.md](disadvantages.md) as the detailed statement of what the current local artefacts represent.

| Required item | Local artefact | Readiness status |
| --- | --- | --- |
| Technical architecture and feature boundary | [TECHNICAL.md](TECHNICAL.md), [JUDGE-READINESS.md](JUDGE-READINESS.md) | Present; controlled local workflow documented |
| Five-minute demo/recovery script | [DEMO-RUNBOOK.md](DEMO-RUNBOOK.md), [VIDEO-SCRIPT.md](VIDEO-SCRIPT.md) | Present; ready for human recording |
| Security self-assessment and risk/control matrix | [SECURITY-SELF-ASSESSMENT.md](SECURITY-SELF-ASSESSMENT.md), [JUDGE-READINESS.md](JUDGE-READINESS.md) | Present; engineering self-assessment and controls documented |
| Local synthetic adapter contract | [INTEGRATION-READINESS.md](INTEGRATION-READINESS.md) | Present; controlled local adapter documented |
| Evaluation and evidence export | [EVALUATION.md](EVALUATION.md), owner-scoped `/api/evaluation/evidence-export` | Present; local evidence workflow documented |
| Compliance matrix | [COMPLIANCE.md](COMPLIANCE.md) | Generated from packs; candidate entries remain non-owner-facing |
| Source, setup, deployment and tests | [../README.md](../README.md), [DEPLOYMENT.md](DEPLOYMENT.md) | Present; setup and deployment guidance documented |
| Presentation outline | [DECK-OUTLINE.md](DECK-OUTLINE.md) | Present; ready for human presentation preparation |
| Execution evidence | [../evidence/README.md](../evidence/README.md), release-gate logs outside Git | Run and retain current logs before upload |
| Human submission | Organiser portal and human checks | Complete before upload |

Before a human upload, run:

```powershell
.\scripts\run-release-gate.ps1
```

Retain the actual timestamped logs under `%LOCALAPPDATA%\veris-demo\release-gate`. Inspect pytest skip output, protect local evidence exports as portfolio data, and verify every screenshot, deck, and video statement matches the same current build and [disadvantages.md](disadvantages.md). Complete organiser eligibility, legal/attribution approval, secret review, visual rehearsal, required external review, backup preparation, and the final upload through the applicable human process.
