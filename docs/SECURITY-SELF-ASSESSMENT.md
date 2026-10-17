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
| Mandate-gated scheduler payment (sandbox invoice only) | `renewals.py`, `agent/runtime.py` | `tests/test_renewals.py`, `tests/test_renewals_api.py` |
| Consent artefacts, adult health-data consent, scoped time-limited delegate, step-up crypto-shred | `privacy.py`, `persistence.py`, `auth.py`, `api.py` | `tests/test_privacy.py`, `tests/test_delegate_claims.py`, `tests/test_auth_roles.py` |
| Service worker caches public shell files only; cleared on sign-out | `web/sw.js`, `web/app.js`, `api.py` | `tests/test_pwa.py`, `tools/smoke_frontend.mjs` |
| Camera capture through the existing size, signature, and parser limits; no image reaches a model | `web/app.js`, `claims.py`, `ingest.py` | `tests/test_pwa.py` |
| Demo reset: owner-only, CSRF in hosted mode, synthetic records only, uploads kept | `api.py`, `persistence.py` | `tests/test_demo_reset.py` |
| Chinese outcome/advice lexicons and Chinese-numeral grounding on every guard | `lang/lexicons/zh.py`, `llm.py`, `ai.py`, `qa.py` | `tests/test_chinese.py` |
| HKID, MyKad, and mainland resident-ID redaction with checksums | `redact.py` | `tests/test_hk_my_packs.py`, `tests/test_chinese.py` |
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
| India motor section 196 | Deferred, human-blocked | Two public primary-source attempts could not machine-verify current operative text; penalty output stays withheld until a human records it and the pack verifier passes. |
| Hong Kong, Malaysia, and China legal entries | Mostly `CANDIDATE` | Regulator sites refused automated fetches, legislation pages rendered by script, and the China legislature's texts are served only over plain HTTP. Those entries never reach an owner; only machine-checked HTTPS quotes do. |
| Translation quality | Human review required | The zh-CN owner screens and the Chinese system prompt are team drafts; a fluent reviewer should check them. Adviser, insurer, and reviewer screens stay English by design. |
| Installed shell on a shared device | Mitigated | The worker stores only public shell files and never an API response; sign-out clears its caches. Device-level browser storage outside VERIS remains the device owner's responsibility. |
| Delegate identity in hosted mode | Human operating task | The software scopes and time-limits a delegate; verifying that person's identity and authority is an onboarding procedure outside the repository. |
| Mainland working days | Known limitation | Public holidays come from the pinned calendar; weekend make-up working days are not represented, and no owner-facing China timeline uses working days. |
| Local mode | Deliberately unsafe for hosting | Use only offline synthetic demos; hosted mode is mandatory for judges over a network. |

## Security commands

```powershell
python scripts/scan_repo.py
pip-audit --progress-spinner off
bandit -q -r src -ll
python -m security.redteam
python scripts/verify_evidence.py
```
