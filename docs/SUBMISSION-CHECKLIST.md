# Submission package checklist

| Required item | Local artefact | Status |
| --- | --- | --- |
| Technical documentation | [TECHNICAL.md](TECHNICAL.md) | Present |
| Security self-assessment | [SECURITY-SELF-ASSESSMENT.md](SECURITY-SELF-ASSESSMENT.md) | Present; human review still needed |
| Compliance matrix | [COMPLIANCE.md](COMPLIANCE.md) | Generated from packs |
| Evaluation | [EVALUATION.md](EVALUATION.md) and scoreboard | Present; expert metrics intentionally absent |
| Source, README, deployment and tests | [../README.md](../README.md), [DEPLOYMENT.md](DEPLOYMENT.md) | Present |
| Presentation outline | [DECK-OUTLINE.md](DECK-OUTLINE.md) | Present; not recorded |
| Video script | [VIDEO-SCRIPT.md](VIDEO-SCRIPT.md) | Present; not recorded |
| Execution evidence | [../evidence/README.md](../evidence/README.md) | Local synthetic only |
| Human submission | See `07-fintechathon/human-actions.md` | Not performed by this repository |

Before a human upload, run `python scripts/check_docs.py`, `python scripts/verify_evidence.py`, the full test/security commands in the root README, and inspect the generated artefacts.
