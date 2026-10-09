"""Authentication and roles.

WHAT THIS IS, STATED PLAINLY. Three seeded demo accounts with salted PBKDF2 password
hashes, bound to the existing session. It is a real credential check and a real
server-side role boundary. It is NOT a user management system: there is no signup, no
password reset, no rotation, no lockout, and the accounts are fixtures in this file. The
audit called missing authentication the top deployment blocker and that is still true —
what changes here is that the ROLE boundary is now enforced by the server rather than
implied by which screen the demo happened to be showing.

WHY THE ROLES MATTER MORE THAN THE LOGIN. The product's claim is that an SME owns its
own analysis and an adviser sees only what was disclosed to them. Before this module
that claim rested entirely on nobody typing another URL. Now:

    SME       reads its own case in full. Its own documents, its own findings.
    ADVISER   CANNOT read /api/brief. It reads the consented disclosure only, so an
              adviser with no grant sees a 403 — the consent gate becomes the actual
              access path rather than a second opinion about one.
    CARRIER   reads book-level roll-ups. Never an individual SME's documents.

That last boundary is the one a regulator would ask about first, so it is enforced in
`require_role` and tested rather than described in a deck.

BACKWARD COMPATIBILITY, DELIBERATE. An unauthenticated session still gets the SME
workspace, because that is the local demo surface the whole test suite and the
"Show me an example" path depend on. What an unauthenticated caller CANNOT do is reach
an adviser or carrier surface. So the guard denies on wrong role, not on absence of a
role, and `/api/health` says exactly that instead of implying a login wall that is not
there.

NO NEW DEPENDENCY. `hashlib.pbkdf2_hmac` is standard library. Adding passlib or
bcrypt-the-package the night before a submission buys a marginally better KDF and a new
install that can fail, which is the wrong trade for three fixture accounts.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional

# 200k iterations of PBKDF2-HMAC-SHA256. Not Argon2, and for three demo accounts whose
# password is printed on the login screen that is the correct amount of engineering.
_ITERATIONS = 200_000
_ALGORITHM = "pbkdf2_sha256"

SME = "SME"
ADVISER = "ADVISER"
CARRIER = "CARRIER"
ROLES = (SME, ADVISER, CARRIER)

ROLE_LABELS: dict[str, str] = {
    SME: "Business owner",
    ADVISER: "Licensed adviser",
    CARRIER: "Carrier / underwriter",
}

# What each role is allowed to reach, in one place, so the boundary can be read rather
# than reconstructed from decorators scattered across the API.
ROLE_SCOPES: dict[str, tuple[str, ...]] = {
    SME: ("own_case", "own_documents", "own_findings", "grant_consent",
          "own_record", "ask_copilot"),
    ADVISER: ("disclosed_brief", "review_queue", "request_information"),
    CARRIER: ("book_rollup", "market_map", "portfolio_priorities"),
}

ROLE_HOME: dict[str, str] = {SME: "workspace", ADVISER: "adviser", CARRIER: "carrier"}


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """`pbkdf2_sha256$iterations$salt$hash`. Salt per account, never shared."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time comparison. A short-circuiting `==` on a password digest leaks
    timing, and getting that wrong in a file called auth.py is not a good look."""
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


@dataclass(frozen=True)
class Principal:
    """An authenticated identity. `organisation_id` is what scopes data, not the email:
    two people at the same adviser firm must see the same queue."""

    id: str
    email: str
    name: str
    role: str
    organisation_id: str
    organisation_name: str
    job_title: str

    def as_dict(self) -> dict:
        return {
            "id": self.id, "email": self.email, "name": self.name,
            "role": self.role, "role_label": ROLE_LABELS[self.role],
            "organisation_id": self.organisation_id,
            "organisation_name": self.organisation_name,
            "job_title": self.job_title,
            "home": ROLE_HOME[self.role],
            "scopes": list(ROLE_SCOPES[self.role]),
        }


@dataclass(frozen=True)
class Account:
    principal: Principal
    password_hash: str


# ── the three seeded accounts ────────────────────────────────────────────────
#
# One shared password across all three, printed on the login screen. That is a
# deliberate demo affordance, not an oversight: the alternative is a presenter
# fumbling three passwords on a recorded video. It is stated as a demo credential in
# `catalogue()` so nobody mistakes this for a production posture.

DEMO_PASSWORD = "Demo@123"

#: The seeded carrier organisation: a clearly fictional sandbox insurer. One constant,
#: so the consent recipient, the carrier's email sender and the tests cannot drift apart.
SANDBOX_INSURER_NAME = "Lotus General (Sandbox)"

#: The accounts the login screen offers. The carrier account still exists and still
#: signs in — its workspace and its tests are kept intact — but it is not part of the
#: household-and-business product, so it is not advertised.
DEMO_LOGIN_EMAILS: tuple[str, ...] = ("owner@hockleefoods.demo", "adviser@lionbrokers.demo")

_SEED: tuple[tuple[Principal, str], ...] = (
    (Principal(
        id="usr-sme-001",
        email="owner@hockleefoods.demo",
        name="Tan Hock Lee",
        role=SME,
        organisation_id="org-hocklee",
        organisation_name="Hock Lee Foods Pte Ltd",
        job_title="Managing Director",
     ), DEMO_PASSWORD),
    (Principal(
        id="usr-adv-001",
        email="adviser@lionbrokers.demo",
        name="Priya Raman",
        role=ADVISER,
        organisation_id="org-lionbrokers",
        organisation_name="Lion Brokers (Singapore)",
        job_title="Commercial Lines Adviser",
     ), DEMO_PASSWORD),
    # `organisation_name` is not decoration. It is the string a business must NAME to
    # disclose its case to this carrier — `/api/consent` matches on it, and the owner's
    # Share screen lists it as a recipient. So it has to be one insurer's name.
    #
    # It is a FICTIONAL insurer, and says so in its own name. The carrier workspace is
    # kept intact and role-guarded, but it is not part of the household-and-business
    # product this repository now is, so this account is not offered on the demo login
    # screen or in the landing narrative. Naming a real insurer here would put words in a
    # real company's mouth on every screen the carrier role draws. What stays labelled
    # synthetic separately is the DOCUMENT bundle, labelled where it is served (see
    # `_book_available`).
    (Principal(
        id="usr-car-001",
        email="underwriter@carrier.demo",
        name="Marcus Yeo",
        role=CARRIER,
        organisation_id="org-carrier",
        organisation_name=SANDBOX_INSURER_NAME,
        job_title="SME Portfolio Underwriter",
     ), DEMO_PASSWORD),
)

# Hashed once at import. Each account gets its own salt.
ACCOUNTS: dict[str, Account] = {
    principal.email: Account(principal=principal,
                             password_hash=hash_password(password))
    for principal, password in _SEED
}

BY_ID: dict[str, Principal] = {a.principal.id: a.principal for a in ACCOUNTS.values()}


def authenticate(email: str, password: str) -> Optional[Principal]:
    """Return the principal, or None. Same failure for an unknown email and a wrong
    password, and the hash is still computed for an unknown email so the response time
    does not tell an attacker which addresses exist."""
    account = ACCOUNTS.get((email or "").strip().lower())
    if account is None:
        # Dummy verification against a real hash: keeps the timing profile flat.
        verify_password(password or "", next(iter(ACCOUNTS.values())).password_hash)
        return None
    if not verify_password(password or "", account.password_hash):
        return None
    return account.principal


def catalogue() -> dict:
    """The demo accounts, for the login screen. Never includes a hash.

    Serving the credentials is intentional for a judged demo and would be indefensible
    otherwise, so the response says which of those two situations this is.
    """
    return {
        "accounts": [
            {**account.principal.as_dict(), "password": DEMO_PASSWORD,
             # Whether the login screen offers it. The carrier account is seeded and
             # works, for the carrier workspace and its tests, but is not part of the
             # household-and-business product and is not advertised.
             "advertised": account.principal.email in DEMO_LOGIN_EMAILS}
            for account in ACCOUNTS.values()
        ],
        "shared_password": DEMO_PASSWORD,
        "notice": (
            "Seeded demonstration accounts. The password is shown because this build "
            "exists to be evaluated, and it is the same for all three so a presenter "
            "does not have to remember which is which. Passwords are stored as salted "
            "PBKDF2-SHA256 digests and are verified in constant time, but there is no "
            "signup, no reset and no rotation — this is a role boundary for a demo, "
            "not a user management system."),
        "roles": {
            role: {"label": ROLE_LABELS[role], "scopes": list(ROLE_SCOPES[role]),
                   "home": ROLE_HOME[role]}
            for role in ROLES
        },
    }
