"""Authentication, roles, hosted invite accounts and login protections.

Local mode intentionally retains three clearly labelled demonstration accounts so an
offline evaluator can exercise every role without receiving credentials out of band.
Hosted mode never exposes those accounts: it uses a durable invite-only account store,
600,000-round PBKDF2-SHA256, account lockout, and a per-client rate limiter.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_ITERATIONS = 600_000
_ALGORITHM = "pbkdf2_sha256"

SME = "SME"
ADVISER = "ADVISER"
CARRIER = "CARRIER"
REVIEWER = "REVIEWER"
ROLES = (SME, ADVISER, CARRIER, REVIEWER)

ROLE_LABELS: dict[str, str] = {
    SME: "Business owner",
    ADVISER: "Licensed adviser",
    CARRIER: "Carrier / underwriter",
    REVIEWER: "Independent reviewer",
}

ROLE_SCOPES: dict[str, tuple[str, ...]] = {
    SME: ("own_case", "own_documents", "own_findings", "grant_consent",
          "own_record", "ask_copilot"),
    ADVISER: ("disclosed_brief", "review_queue", "request_information"),
    CARRIER: ("book_rollup", "market_map", "portfolio_priorities"),
    REVIEWER: ("review_assigned_findings", "record_review_label", "export_review_labels"),
}

ROLE_HOME: dict[str, str] = {
    SME: "workspace", ADVISER: "adviser", CARRIER: "carrier", REVIEWER: "reviewer",
}

HOSTED_AUTH_DB_ENV = "VERIS_AUTH_DB_PATH"
HOSTED_INVITES_ENV = "VERIS_JUDGE_INVITE_CODES"
LOGIN_LOCKOUT_SECONDS = 15 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_RATE_WINDOW_SECONDS = 60
LOGIN_RATE_LIMIT = 10


class AuthConfigurationError(RuntimeError):
    """A hosted authentication setting is absent or unsafe."""


class InviteError(RuntimeError):
    """A signup request cannot consume the supplied invitation."""


@dataclass(frozen=True)
class Principal:
    """An authenticated identity; organisation_id is the tenant boundary."""

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


@dataclass(frozen=True)
class HostedAuthentication:
    principal: Optional[Principal]
    locked: bool = False


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """Return a salted PBKDF2-SHA256 record using the current work factor."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Verify a PBKDF2 record using constant-time digest comparison."""
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


# ── local demonstration accounts ────────────────────────────────────────────

DEMO_PASSWORD = "Demo@123"
SANDBOX_INSURER_NAME = "Lotus General (Sandbox)"
DEMO_LOGIN_EMAILS: tuple[str, ...] = ("owner@hockleefoods.demo", "adviser@lionbrokers.demo")

_SEED: tuple[tuple[Principal, str], ...] = (
    (Principal(
        id="usr-sme-001", email="owner@hockleefoods.demo", name="Tan Hock Lee",
        role=SME, organisation_id="org-hocklee",
        organisation_name="Hock Lee Foods Pte Ltd", job_title="Managing Director",
    ), DEMO_PASSWORD),
    (Principal(
        id="usr-adv-001", email="adviser@lionbrokers.demo", name="Priya Raman",
        role=ADVISER, organisation_id="org-lionbrokers",
        organisation_name="Lion Brokers (Singapore)", job_title="Commercial Lines Adviser",
    ), DEMO_PASSWORD),
    (Principal(
        id="usr-car-001", email="underwriter@carrier.demo", name="Marcus Yeo",
        role=CARRIER, organisation_id="org-carrier",
        organisation_name=SANDBOX_INSURER_NAME, job_title="SME Portfolio Underwriter",
    ), DEMO_PASSWORD),
)

ACCOUNTS: dict[str, Account] = {
    principal.email: Account(principal=principal, password_hash=hash_password(password))
    for principal, password in _SEED
}
BY_ID: dict[str, Principal] = {account.principal.id: account.principal for account in ACCOUNTS.values()}


def authenticate(email: str, password: str) -> Optional[Principal]:
    """Authenticate a local demonstration account without account enumeration."""
    account = ACCOUNTS.get((email or "").strip().lower())
    if account is None:
        verify_password(password or "", next(iter(ACCOUNTS.values())).password_hash)
        return None
    if not verify_password(password or "", account.password_hash):
        return None
    return account.principal


def catalogue() -> dict:
    """The local-only demonstration account catalogue; never includes hashes."""
    return {
        "accounts": [
            {**account.principal.as_dict(), "password": DEMO_PASSWORD,
             "advertised": account.principal.email in DEMO_LOGIN_EMAILS}
            for account in ACCOUNTS.values()
        ],
        "shared_password": DEMO_PASSWORD,
        "notice": (
            "Seeded local demonstration accounts. The password is shown only for the "
            "offline demo; hosted mode uses invite-code signup and never exposes these "
            "accounts. Passwords use salted PBKDF2-SHA256 with 600000 iterations; this is "
            "not a user management system in local mode."),
        "roles": {
            role: {"label": ROLE_LABELS[role], "scopes": list(ROLE_SCOPES[role]),
                   "home": ROLE_HOME[role]}
            for role in ROLES
        },
    }


# ── hosted invite-only accounts ──────────────────────────────────────────────

def _normalise_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not re.fullmatch(r"[^@\s]{1,120}@[^@\s]{1,120}\.[^@\s]{2,63}", email):
        raise InviteError("Enter a valid email address.")
    return email


def _invite_roles() -> dict[str, str]:
    """Read invite codes from the environment without ever returning their values."""
    raw = (os.getenv(HOSTED_INVITES_ENV) or "").strip()
    if not raw:
        raise AuthConfigurationError(
            f"Set {HOSTED_INVITES_ENV} with one-time invite-code to role mappings.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    values: dict[str, str] = {}
    if isinstance(parsed, dict):
        values = {str(code): str(role).upper() for code, role in parsed.items()}
    else:
        for entry in raw.split(","):
            code, sep, role = entry.strip().partition(":")
            if code:
                values[code] = (role.upper() if sep and role else SME)
    if not values or any(role not in {SME, ADVISER, REVIEWER} for role in values.values()):
        raise AuthConfigurationError(
            f"{HOSTED_INVITES_ENV} must map non-empty invite codes to SME, ADVISER or REVIEWER.")
    return values


def _invite_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class HostedAccountStore:
    """Small durable account store for hosted judges and reviewers.

    It stores password hashes and one-way invite digests only.  An operator retains the
    plaintext invitation codes in the deployment environment, not in the database or a
    response payload.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def _connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hosted_account ("
            "id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL, "
            "organisation_id TEXT NOT NULL, organisation_name TEXT NOT NULL, job_title TEXT NOT NULL, "
            "password_hash TEXT NOT NULL, invite_digest TEXT UNIQUE NOT NULL, failed_attempts INTEGER NOT NULL DEFAULT 0, "
            "first_failed_at REAL, locked_until REAL)")
        return conn

    @staticmethod
    def _principal(row: sqlite3.Row) -> Principal:
        return Principal(
            id=str(row["id"]), email=str(row["email"]), name=str(row["name"]),
            role=str(row["role"]), organisation_id=str(row["organisation_id"]),
            organisation_name=str(row["organisation_name"]), job_title=str(row["job_title"]),
        )

    def signup(self, *, email: str, password: str, invite_code: str, name: str,
               credentials: str = "") -> Principal:
        email = _normalise_email(email)
        clean_name = " ".join(str(name or "").split())[:160]
        if not clean_name:
            raise InviteError("Enter the name that should appear in the reviewer record.")
        if len(password or "") < 12:
            raise InviteError("Hosted passwords must contain at least 12 characters.")
        roles = _invite_roles()
        supplied = str(invite_code or "")
        role = next((candidate_role for code, candidate_role in roles.items()
                     if hmac.compare_digest(code, supplied)), None)
        if role is None:
            raise InviteError("That invitation code is not valid.")
        invite_digest = _invite_digest(supplied)
        account_id = f"usr-{uuid.uuid4().hex}"
        organisation_id = f"tenant-{hashlib.sha256(email.encode('utf-8')).hexdigest()[:20]}"
        organisation_name = ("Independent review workspace" if role == REVIEWER
                             else "Judge workspace")
        job_title = ("Reviewer — credentials declared at signup" if role == REVIEWER
                     else "Evaluation participant")
        if credentials.strip() and role == REVIEWER:
            job_title = f"Reviewer: {' '.join(credentials.split())[:120]}"
        with self._lock, self._connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO hosted_account "
                    "(id,email,name,role,organisation_id,organisation_name,job_title,password_hash,invite_digest) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (account_id, email, clean_name, role, organisation_id, organisation_name,
                     job_title, hash_password(password), invite_digest),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise InviteError("That email or invitation code has already been used.") from exc
        return Principal(account_id, email, clean_name, role, organisation_id,
                         organisation_name, job_title)

    def authenticate(self, email: str, password: str, *, now: Optional[float] = None
                     ) -> HostedAuthentication:
        now = time.time() if now is None else float(now)
        key = (email or "").strip().lower()
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM hosted_account WHERE email=?", (key,)).fetchone()
            if row is None:
                verify_password(password or "", next(iter(ACCOUNTS.values())).password_hash)
                return HostedAuthentication(None)
            locked_until = row["locked_until"]
            if locked_until is not None and float(locked_until) > now:
                verify_password(password or "", str(row["password_hash"]))
                return HostedAuthentication(None, locked=True)
            if verify_password(password or "", str(row["password_hash"])):
                conn.execute(
                    "UPDATE hosted_account SET failed_attempts=0, first_failed_at=NULL, locked_until=NULL "
                    "WHERE id=?", (row["id"],))
                conn.commit()
                return HostedAuthentication(self._principal(row))
            first = row["first_failed_at"]
            failures = int(row["failed_attempts"] or 0)
            if first is None or now - float(first) > LOGIN_LOCKOUT_SECONDS:
                first, failures = now, 0
            failures += 1
            locked = failures >= LOGIN_FAILURE_LIMIT
            conn.execute(
                "UPDATE hosted_account SET failed_attempts=?, first_failed_at=?, locked_until=? WHERE id=?",
                (failures, first, now + LOGIN_LOCKOUT_SECONDS if locked else None, row["id"]),
            )
            conn.commit()
            return HostedAuthentication(None, locked=locked)


def hosted_store_from_environment() -> HostedAccountStore:
    value = (os.getenv(HOSTED_AUTH_DB_ENV) or "").strip()
    if not value:
        raise AuthConfigurationError(
            f"Set {HOSTED_AUTH_DB_ENV} outside the repository for hosted accounts.")
    return HostedAccountStore(value)


def verify_principal_password(principal: Principal, password: str, *, hosted: bool) -> bool:
    """Re-authenticate exactly the session principal without exposing password records.

    Hosted re-authentication deliberately goes through ``HostedAccountStore.authenticate``:
    it preserves the same PBKDF, lockout and unknown-account behaviour as sign-in.  The
    returned principal id must still match the session identity before it can satisfy a
    step-up request.
    """
    if hosted:
        result = hosted_store_from_environment().authenticate(principal.email, password)
        candidate = result.principal
        return bool(candidate and hmac.compare_digest(candidate.id, principal.id))
    account = ACCOUNTS.get(principal.email)
    return bool(
        account
        and hmac.compare_digest(account.principal.id, principal.id)
        and verify_password(password, account.password_hash)
    )


def login_rate_limit_keys(email: str, caller: str) -> tuple[str, str]:
    """Return bounded account and trusted-client buckets without retaining raw email."""
    normalised_email = (email or "").strip().casefold()[:200]
    account_digest = hashlib.sha256(normalised_email.encode("utf-8")).hexdigest()
    client = (caller or "unknown").strip().lower()[:200] or "unknown"
    return f"account:{account_digest}", f"client:{client}"


class LoginRateLimiter:
    """Fixed-window limiter atomically keyed by account identity and caller address."""

    def __init__(self, *, limit: int = LOGIN_RATE_LIMIT,
                 window_seconds: int = LOGIN_RATE_WINDOW_SECONDS):
        self.limit = limit
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.RLock()

    def allow(self, *callers: str, now: Optional[float] = None) -> bool:
        stamp = time.time() if now is None else float(now)
        keys = tuple(dict.fromkeys(
            str(caller or "unknown")[:200] for caller in callers
        )) or ("unknown",)
        with self._lock:
            windows = {
                key: [t for t in self._attempts.get(key, [])
                      if stamp - t < self.window_seconds]
                for key in keys
            }
            if any(len(window) >= self.limit for window in windows.values()):
                self._attempts.update(windows)
                return False
            for key, window in windows.items():
                window.append(stamp)
                self._attempts[key] = window
            return True

    def clear(self) -> None:
        with self._lock:
            self._attempts.clear()


LOGIN_RATE_LIMITER = LoginRateLimiter()
