# Security policy

## Reporting a vulnerability

Report security issues privately through the repository host’s private vulnerability-reporting channel. Do not publish documents, credentials, or a path around the consent, tenant, or permission boundaries. Include the observed behaviour, expected behaviour, and revision tested. There is no bug bounty.

## Supported version

Only the current default-branch revision is supported. VERIS is a pre-release research build, not a production insurance service.

## Deployment posture

- `VERIS_MODE=local` is an offline synthetic demonstration mode. It intentionally keeps seeded accounts and cannot be exposed safely to a network.
- `VERIS_MODE=hosted` requires invite-only accounts, 600000-round PBKDF2-SHA256, account lockout, login rate limiting, double-submit CSRF, secure cookies, HSTS, disabled interactive API documentation, request limits, isolated PDF parsing, outbound hostname allowlisting, and environment-only secrets.
- Uploaded PDFs are bounded before parsing. Hosted parsing occurs in a child process with a wall-clock limit and a Linux memory limit; malformed input fails with a typed response.
- Optional provider transport is consent-gated, redacted, grounded, and off by default. No model decides coverage, settlement, evidence class, premium, or product choice.

## Known risks and operational gaps

| Risk | Status | Mitigation / action |
| --- | --- | --- |
| Formal external penetration test | Open | Obtain one before public hosting. |
| Prompt injection | Mitigated, not solved | Explicit user intent, untrusted-data handling, grounding, redaction, fixed tools, confirmation, and deterministic fallback; continue provider-specific testing. |
| Third-party model processing | Optional residual risk | Owner/health consent gates, spend budgets, and `none` profile. |
| Domain-qualified review | Open | Candidate regulatory rules remain non-owner-facing; obtain qualified review. |
| Synthetic counterparty | Deliberately limited | Never describe local sandbox records as real insurer, bank, payment, or settlement evidence. |
| Accounting integration | Documented seam, not a built feature | The accounting feed remains labelled simulated; a real connector requires separate review and onboarding. |
| Backup/key destruction | Open operating task | Define and test the backup/snapshot lifecycle before hosting. |
| Reviewer credentials | Human validation required | The console records declared credentials but does not verify licences. |
| India motor source gap | Deferred | Penalty output remains withheld pending human primary-source verification. |

The detailed code/test matrix and red-team evidence are in [docs/SECURITY-SELF-ASSESSMENT.md](docs/SECURITY-SELF-ASSESSMENT.md).
