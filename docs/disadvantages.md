# Current scope and implementation conditions

This page is the detailed source for VERIS's current scope, operational conditions, known risks, and features not represented by the repository demonstration. It describes the current build rather than a product roadmap or assurance statement. Security control evidence remains in [SECURITY.md](../SECURITY.md) and [SECURITY-SELF-ASSESSMENT.md](SECURITY-SELF-ASSESSMENT.md); technical implementation detail remains in [TECHNICAL.md](TECHNICAL.md).

## Implemented local behavior

VERIS provides an evidence-bound workflow for household and small-business policy material. The local demonstration retains cited source spans, applies deterministic rules and calculations, stores tenant-scoped encrypted records, returns documented or review-required states, and records permissioned synthetic actions with consent, exact-payload confirmation, and an audit ledger. The local sandbox exercises the adapter, receipt, reconciliation, and action-control paths without leaving the controlled environment.

## Current scope and representations

| Area | Current implementation | Not represented by the current build |
| --- | --- | --- |
| Runtime and counterparties | `VERIS_MODE=local` uses loopback-only local data, generated fixtures, and a private synthetic sandbox. Action receipts and sandbox records are local records. | A live insurer, broker, TPA, bank, payment rail, policy-administration, accounting, claims, or public API integration; money movement; live claim submission, approval, or settlement. |
| Human and authorised review | Cited evidence, deterministic calculations, named missing inputs, and `REQUIRES_AUTHORISED_REVIEW` states keep decisions inspectable. Optional model wording is outside the decision path. | An automated coverage, underwriting, claim-outcome, settlement, premium, product-choice, legal, medical, or professional insurance determination. Human or authorised review remains required where the workflow identifies that state. |
| Policy, profile, and jurisdiction | The workflow covers supplied household and small-business material, selected public wordings, and jurisdiction packs for Singapore, India, Hong Kong, Malaysia, and mainland China. A recognised country without a pack is handled as `GENERIC` with no imported legal conclusion. | A determination for every policy form, customer profile, product line, jurisdiction, or regulatory rule. |
| India health | The India-health path is implemented only for the named mapped synthetic wording used by the demonstration. | Interpretation of arbitrary Indian health policies or a general India-health catalogue. |
| Languages | English is available throughout. Simplified Chinese is a partial owner-facing interface; adviser, insurer, and reviewer screens remain English. | Hindi, Tamil, Kannada, or a complete multilingual catalogue. |
| Home and travel | Structural home and travel mechanisms can be bound to supported public wordings and used in cited, evidence-bound illustrations. | Full home or travel claim automation, arbitrary policy interpretation, or an outcome decision. |
| Deployment and operations | The repository includes local operation and hosted-mode configuration guidance, with controls documented in [DEPLOYMENT.md](DEPLOYMENT.md). | A production hosted deployment, service-level commitment, operational recovery record, or completed institutional rollout. |
| Corpus, OCR, and optional providers | Third-party wordings are managed outside the repository through a manifest and hash checks; missing or drifted material is surfaced as absent. OCR output depends on the installed engine and source document. Optional provider transport is off by default and is subject to the documented consent, redaction, budget, and allowlist controls when configured. | Guaranteed corpus availability, publisher-live retrieval during the demo, universal OCR/language extraction, or an enabled external-provider result unless separately configured and reviewed. |
| External validation and commercial evidence | Local tests, deterministic benchmarks, evidence exports, and review workflows record engineering behavior in the controlled environment. | A completed qualified insurance review, independent accuracy or false-alarm study, customer or pilot outcome, buyer commitment, commercial validation, paid deployment, or production customer result. |

## Security and operating conditions

- Local mode is for offline controlled demonstrations and must remain loopback-only. It must not be exposed through a LAN bind, tunnel, or public endpoint.
- Hosting requires the controls and operator procedures in [DEPLOYMENT.md](DEPLOYMENT.md), including secrets, certificates, persistent storage, corpus mounting, access provisioning, and backup/key-lifecycle decisions.
- Formal external penetration testing, provider-specific adversarial testing, hosted backup and recovery practice, reviewer credential verification, and source-rule review are separate operating activities. The current control and risk matrix is maintained in [SECURITY-SELF-ASSESSMENT.md](SECURITY-SELF-ASSESSMENT.md).
- Corpus source availability, OCR capability, translation review, and jurisdiction-rule verification are conditional on their documented inputs and review status; VERIS labels unavailable or unverified material rather than substituting a result.

## How to interpret local evidence

The local evidence export, benchmark, and sandbox receipts demonstrate recorded behavior of this build. They should be read alongside [EVALUATION.md](EVALUATION.md), [INTEGRATION-READINESS.md](INTEGRATION-READINESS.md), and the security documentation; they are not substitutes for external professional, operational, pilot, or commercial validation.
