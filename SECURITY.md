# Security policy

## Reporting a vulnerability

Please report security issues **privately**, through the repository host's private
vulnerability reporting ("Report a vulnerability" on the repository's Security tab).
Do not open a public issue for anything that could expose a user's documents, a
credential, or a way past the consent and role boundaries.

Include what you did, what you expected, what happened, and the commit you tested.
There is no bug bounty.

## Supported versions

Only the latest commit on the default branch is supported. This is a pre-release
research build, not a product with maintained release lines.

## The posture of this build — read before deploying anything

These are true of the code as it stands, and each is a reason not to put this build on a
network with real documents yet:

- **No authentication in local mode.** A session cookie isolates one browser's case from
  another's; it does not authenticate a person. Seeded demo accounts with a printed
  password exist so the role boundaries can be demonstrated, not to protect anything.
  `GET /api/health` reports `authenticated: false` so the gap cannot be missed. Hosted
  mode with mandatory login, CSRF tokens, lockout and the rest of the hardening is
  planned work, not present work.
- **Uploads are untrusted input.** They are size-, page- and type-checked before parsing,
  rate-limited per session, held in memory only and never written to disk. PDF parsing
  still runs in-process; a parser exploit or decompression bomb is a live risk until
  parsing moves into a sandboxed subprocess with time and memory limits.
- **Model calls are optional and off by default.** With a key set, document text is sent
  to a third-party model provider. No model output reaches a figure, an evidence class or
  a cover decision, and every model output passes an outcome gate, but redaction of
  personal data before a model call is not yet implemented.
- **Secrets come from the environment.** `.env` is gitignored, `.env.example` carries
  names only, and `scripts/scan_repo.py` fails the build on a key-shaped literal, a
  committed database or a third-party PDF.

## Known risks and documented gaps

| Risk or gap | Status | Mitigation in place |
|---|---|---|
| No authentication or CSRF protection in local mode | Open | Local demo surface only; reported by `/api/health`. |
| PDF parsing in the server process | Open | Byte, page and type caps; per-session rate limit; in-memory only. |
| Prompt injection through document text | Mitigated, not solved | Model output never reaches figures or decisions; outcome gate on every model answer; deterministic fallback. |
| Personal data in model calls | Open | Model calls are off unless a key is configured. |
| Third-party documents | Mitigated | Kept out of git (scanner-enforced); hash-verified at fetch and at read; shown as a cited paragraph plus a publisher link, never as page images. |
| **Accounting integration** | **Documented seam, not a built feature** | The accounting feed is labelled simulated on every row and screen. A live connector to one vendor's API existed in an earlier build and was removed rather than shipped half-supported; `connectors.ACCOUNTING_SEAM` specifies what a real one must supply and must never change. |
| Premium arithmetic left in the carrier workspace | Mitigated | VERIS never computes or states a premium. The carrier's premium-at-risk route refuses and names the missing operands, guarded by `test_no_carrier_route_carries_a_premium_figure`; the old computation module (`premium_at_risk.py`) is still in the tree, unrouted, with its own unit tests, and is due to be removed with the agent permission tiers. |
| No formal penetration test | Open | Not yet commissioned. |

A fuller security self-assessment — permission tiers, where each is enforced in code, the
test that proves it, and red-team results — is part of the documentation set still to
come.
