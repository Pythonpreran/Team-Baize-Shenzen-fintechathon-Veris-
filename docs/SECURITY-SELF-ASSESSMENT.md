# Security self-assessment

## Scope and evidence

This is a local engineering self-assessment, not a formal penetration test or certification. Current deterministic red-team evidence is in [../security/redteam/security-results.md](../security/redteam/security-results.md). The local evidence bundle is checked by `python scripts/verify_evidence.py`.

## Permission tiers

| Tier | Permitted purpose | Enforcement | Evidence |
| --- | --- | --- | --- |
| 0 — Observe | Read, cite, and deterministic computation | `src/proven/agent/runtime.py` registry plus tenant scope and consent checks | `tests/test_agent_runtime.py` role/consent matrix |
| 1 — Prepare | Reversible drafts in VERIS | Registry side-effect type `DRAFT`; no external connector call | `tests/test_agent_runtime.py` draft assertions |
| 2 — Act | Synthetic external effect | Exact canonical payload hash, single-use confirmation, password or TOTP step-up, and mandate checks where applicable | `tests/test_agent_runtime.py`, `tests/test_demo_spine_e2e.py`, `tests/test_redteam.py` |
| 3 — Never | Claim outcomes, premiums, product recommendations, signing declarations, non-insurer payments | No registry entry exists for this tier | `tests/test_agent_runtime.py::test_tier_three_has_no_tool_and_deterministic_budgets_hold` |

## Hosted platform controls

| Control | Code location | Test evidence |
| --- | --- | --- |
| Hosted login wall and invite-only signup | `src/proven/api.py`, `src/proven/auth.py` | `tests/test_platform_hardening.py` |
| PBKDF2 work factor, lockout, account-and-trusted-client login rate limits | `src/proven/auth.py`, `Dockerfile`, `deploy/nginx.conf` | `tests/test_platform_hardening.py`, `tests/test_sandbox.py` |
| Hosted password re-entry for first TOTP enrollment | `src/proven/auth.py`, `src/proven/api.py` | `tests/test_platform_hardening.py` |
| CSRF double-submit and secure cookies | `src/proven/api.py`, `web/app.js` | `tests/test_platform_hardening.py` |
| HSTS, CSP, no default CORS grant, hidden docs | `src/proven/api.py` | `tests/test_platform_hardening.py` |
| Request and parser limits | `src/proven/api.py`, `src/proven/ingest.py` | `tests/test_platform_hardening.py` |
| Outbound allowlist | `src/proven/security.py`, `discovery.py`, `sentinel.py`, `llm.py` | `tests/test_platform_hardening.py` |
| Private synthetic-sandbox topology and confirmed action smoke | `docker-compose.hosted.yml`, `scripts/smoke_deployment.py` | `tests/test_sandbox.py`, `tests/test_delivery_artifacts.py` |
| Tenant-scoped portfolio, notifications, outbox, reviewer queue | `persistence.py`, `api.py`, `reviewer.py` | `tests/test_portfolio.py`, `tests/test_notifications_api.py`, `tests/test_reviewer_api.py` |
| Portable-record HMAC | `security.py`, `pipeline.py`, `api.py` | `tests/test_platform_hardening.py` |
| Dependency/static audits | CI workflow | `pip-audit --progress-spinner off`; `bandit -q -r src -ll` |

## Red-team result

The reproducible local runner exercises document-origin policy controls, role confusion, cross-tenant identifiers, confirmation mutation/replay, mandate overspend, consent bypass, a pre-transport PII-redaction control, hosted SSRF, and malformed upload input. The five document-origin rows and the PII row are explicitly **control-level checks**: they do not construct or ingest named PDF/OCR/metadata media or capture a provider-bound prompt. Its current result is stated in the generated report: **0 tier-2 attack attempts triggered an action**. This is not evidence that all future prompt injection or provider behaviours are solved.

## Known risks

| Risk | Status | Current mitigation / required action |
| --- | --- | --- |
| Formal penetration test | Open | Commission an independent test before public hosting. |
| Prompt injection | Mitigated, not solved | Untrusted-data boundary, redaction, grounding, explicit intent, fixed registry, confirmation, and deterministic fallback. Continue adversarial testing for each provider. |
| Third-party model processing | Optional residual risk | Owner consent and health-data consent gate provider transport; run `none` when no provider approval exists. |
| Rule-domain sign-off | Open | Candidate rules remain non-owner-facing; obtain insurance-qualified review for every required rule set. |
| Sandbox realism | Deliberately limited | All counterparty activity is synthetic and private. Do not present its logs as real insurer, bank, or payment evidence. |
| Hosted backup erasure | Open operating task | Define and test backup, snapshot, replica, and key-destruction procedures before hosting. |
| Reviewer credentials | Human validation required | Export records declared credentials but does not verify a licence; retain human evidence outside the repository. |
| India motor section 196 | Deferred | Penalty output remains withheld until a human verifies current primary-source wording. |
| Local mode | Deliberately unsafe for hosting | Use only offline synthetic demos; hosted mode is mandatory for judges over a network. |

## Security commands

```powershell
python scripts/scan_repo.py
pip-audit --progress-spinner off
bandit -q -r src -ll
python -m security.redteam
python scripts/verify_evidence.py
```
