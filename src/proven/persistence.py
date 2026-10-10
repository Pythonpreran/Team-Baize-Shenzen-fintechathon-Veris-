"""Encrypted, tenant-scoped portfolio persistence.

The earlier persistence shim created a shared SQLite connection and schema but no
production call site wrote through it. This module is deliberately different: every
repository operation opens and closes its own file-backed SQLite connection, keeps its
transaction inside that operation, and encrypts sensitive tenant data before SQLite sees
it. A browser session remains the transient home for an analysis case; this repository is
only for the durable household, business, policy, document and consent portfolio.

A tenant receives a random AES-GCM data key. The data key is wrapped with the deployment
master key and stored separately from encrypted fields. Local key removal uses SQLite
secure-delete, a truncating WAL checkpoint and VACUUM, then verifies that the database
artifacts no longer retain the wrapped key before reporting completion. Backups and
physical-media lifecycle remain deployment operations outside this process.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import detect, jurisdiction
from .db import new_id
from .grading import AUTHOR_TO_CLASS

PORTFOLIO_DB_ENV = "VERIS_PORTFOLIO_DB_PATH"
MASTER_KEY_ENV = "VERIS_MASTER_KEY"
DEV_MASTER_KEY_ENV = "VERIS_DEV_MASTER_KEY_PATH"
DEV_MASTER_KEY_FILENAME = ".veris-dev-master-key"

_NONCE_BYTES = 12
_DATA_KEY_BYTES = 32


class PortfolioError(RuntimeError):
    """A portfolio error safe to return to an API caller."""

    http_status = 400


class PortfolioConfigurationError(PortfolioError):
    http_status = 503


class PortfolioValidationError(PortfolioError):
    http_status = 422


class PortfolioNotFound(PortfolioError):
    http_status = 404


class TenantShredded(PortfolioError):
    http_status = 410


class TenantErasureInProgress(PortfolioError):
    """A recoverable local key-erasure protocol has not reached its terminal state."""

    http_status = 503


class PortfolioIntegrityError(PortfolioError):
    http_status = 500


ERASURE_PENDING = "PENDING"
ERASURE_FINALIZING = "FINALIZING"


@dataclass(frozen=True)
class TenantScope:
    """The authenticated principal's tenant boundary.

    Route handlers construct this from the server-side principal. No repository method
    accepts a tenant id supplied as an HTTP query or body field, so a caller cannot move
    an otherwise valid record lookup into another tenant.
    """

    tenant_id: str
    user_id: str
    user_name: str = ""
    user_email: str = ""
    role: str = "OWNER"


@dataclass(frozen=True)
class _TenantErasure:
    """Durable state needed to resume a local tenant-key erasure."""

    stage: str
    recovery_wrapped_key: Optional[bytes]
    original_nonce: Optional[bytes]
    recovery_sha256: bytes
    recovery_length: int
    started_at: str


PORTFOLIO_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  shredded_at TEXT
);

CREATE TABLE IF NOT EXISTS tenant_key (
  tenant_id TEXT PRIMARY KEY REFERENCES tenant(id),
  wrapped_key BLOB NOT NULL,
  created_at TEXT NOT NULL
);

-- An interrupted erase is deliberately not terminal. PENDING retains a fresh wrapper
-- of the tenant data key plus the old wrapper's public nonce, so a restarted process can
-- reconstruct the deleted value and continue or restore it. FINALIZING retains only a
-- digest and length, sufficient to scan for stale bytes without retaining a usable key.
CREATE TABLE IF NOT EXISTS tenant_erasure (
  tenant_id TEXT PRIMARY KEY REFERENCES tenant(id),
  stage TEXT NOT NULL CHECK(stage IN ('PENDING', 'FINALIZING')),
  recovery_wrapped_key BLOB,
  original_nonce BLOB,
  recovery_sha256 BLOB NOT NULL,
  recovery_length INTEGER NOT NULL CHECK(recovery_length > 0),
  started_at TEXT NOT NULL,
  CHECK(
    (stage = 'PENDING' AND recovery_wrapped_key IS NOT NULL AND original_nonce IS NOT NULL)
    OR
    (stage = 'FINALIZING' AND recovery_wrapped_key IS NULL AND original_nonce IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS "user" (
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  id TEXT NOT NULL,
  identity_encrypted BLOB NOT NULL,
  role TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS household (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  details_encrypted BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_household_tenant ON household(tenant_id);

CREATE TABLE IF NOT EXISTS member (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  household_id TEXT NOT NULL REFERENCES household(id),
  details_encrypted BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_member_tenant ON member(tenant_id);

CREATE TABLE IF NOT EXISTS asset (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  household_id TEXT REFERENCES household(id),
  details_encrypted BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_tenant ON asset(tenant_id);

CREATE TABLE IF NOT EXISTS business (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  details_encrypted BLOB NOT NULL,
  source_case_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (tenant_id, source_case_id)
);
CREATE INDEX IF NOT EXISTS idx_business_tenant ON business(tenant_id);

CREATE TABLE IF NOT EXISTS document (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  kind TEXT NOT NULL,
  author_class TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  encrypted_blob BLOB NOT NULL,
  metadata_encrypted BLOB NOT NULL,
  language TEXT NOT NULL,
  country TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_tenant ON document(tenant_id);

CREATE TABLE IF NOT EXISTS policy (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  household_id TEXT REFERENCES household(id),
  business_id TEXT REFERENCES business(id),
  country TEXT NOT NULL,
  line_of_business TEXT NOT NULL,
  details_encrypted BLOB NOT NULL,
  period_start TEXT,
  period_end TEXT,
  status TEXT NOT NULL,
  renewal_date TEXT,
  insured_member_ids TEXT NOT NULL,
  insured_asset_ids TEXT NOT NULL,
  source_document_ids TEXT NOT NULL,
  source_case_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (tenant_id, source_case_id)
);
CREATE INDEX IF NOT EXISTS idx_policy_tenant ON policy(tenant_id);

CREATE TABLE IF NOT EXISTS coverage_item (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  policy_id TEXT NOT NULL REFERENCES policy(id),
  mechanism_code TEXT NOT NULL,
  value_encrypted BLOB,
  evidence_span_ids TEXT NOT NULL,
  evidence_class TEXT NOT NULL,
  source_document_id TEXT REFERENCES document(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coverage_item_policy ON coverage_item(policy_id);

CREATE TABLE IF NOT EXISTS consent (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  details_encrypted BLOB NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_consent_tenant ON consent(tenant_id);

CREATE TABLE IF NOT EXISTS audit (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenant(id),
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit(tenant_id, at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def portfolio_db_path() -> Optional[Path]:
    """Configured durable portfolio database path, if one was supplied."""
    value = os.environ.get(PORTFOLIO_DB_ENV, "").strip()
    return Path(value).expanduser() if value else None


def _dev_master_key_path(db_path: Path) -> Path:
    configured = os.environ.get(DEV_MASTER_KEY_ENV, "").strip()
    return Path(configured).expanduser() if configured else db_path.parent / DEV_MASTER_KEY_FILENAME


def _decode_master_key(encoded: str) -> bytes:
    """Decode one URL-safe base64 AES-256 key without accepting a weak length."""
    text = (encoded or "").strip()
    try:
        padded = text + "=" * (-len(text) % 4)
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise PortfolioConfigurationError(
            f"{MASTER_KEY_ENV} must be URL-safe base64 for an AES-256 key.") from exc
    if len(key) != _DATA_KEY_BYTES:
        raise PortfolioConfigurationError(
            f"{MASTER_KEY_ENV} must decode to exactly {_DATA_KEY_BYTES} bytes.")
    return key


def _read_development_master_key(path: Path) -> bytes:
    """Read a local key another process may still be finishing atomically."""
    last_error: Optional[PortfolioConfigurationError] = None
    for attempt in range(100):
        try:
            return _decode_master_key(path.read_text(encoding="ascii").strip())
        except FileNotFoundError:
            # The winning creator may have failed and removed its incomplete file.
            pass
        except PortfolioConfigurationError as exc:
            # O_EXCL makes ownership exclusive, but a losing process can observe the
            # name before the winner has flushed its complete ASCII payload.
            last_error = exc
        if attempt < 99:
            time.sleep(0.01)
    if last_error is not None:
        raise last_error
    raise PortfolioConfigurationError(
        f"Could not read the local development master key at {path}.")


def _development_master_key(path: Path) -> bytes:
    """Load or atomically create the local-only key file ignored by git."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = base64.urlsafe_b64encode(os.urandom(_DATA_KEY_BYTES)).decode("ascii")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_development_master_key(path)

    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write((encoded + "\n").encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # We created this pathname exclusively, so only our incomplete value is removed.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows may not implement POSIX modes. The file remains outside git either way.
        pass
    return _decode_master_key(encoded)


def master_key_for(db_path: Path) -> tuple[bytes, str]:
    """Return the deployment key and a non-secret description of its source."""
    supplied = os.environ.get(MASTER_KEY_ENV, "").strip()
    if supplied:
        return _decode_master_key(supplied), "environment"
    return _development_master_key(_dev_master_key_path(db_path)), "development-file"


def repository_from_environment() -> "PortfolioRepository":
    """Construct the durable repository only when the deployment configured a DB path."""
    path = portfolio_db_path()
    if path is None:
        raise PortfolioConfigurationError(
            f"Set {PORTFOLIO_DB_ENV} to a file path before using the durable portfolio.")
    key, _source = master_key_for(path)
    return PortfolioRepository(path, key)


def status() -> dict:
    """Describe the durable store honestly without opening or creating one on health checks."""
    path = portfolio_db_path()
    if path is None:
        return {
            "persistent": False,
            "backend": "file-sqlite",
            "path_configured": False,
            "one_connection_per_request": True,
            "encryption": "AES-GCM tenant data keys wrapped by VERIS_MASTER_KEY",
            "notice": (
                f"Durable portfolio storage is not configured. Set {PORTFOLIO_DB_ENV} "
                "to a file path and VERIS_MASTER_KEY to a URL-safe base64 AES-256 key; "
                "a local development key file is generated only when a configured store "
                "is first used."),
        }
    source = "environment" if os.environ.get(MASTER_KEY_ENV, "").strip() else "development-file"
    return {
        "persistent": True,
        "backend": "file-sqlite",
        "path_configured": True,
        "path": str(path),
        "master_key_source": source,
        "one_connection_per_request": True,
        "encryption": "AES-GCM tenant data keys wrapped by VERIS_MASTER_KEY",
        "notice": (
            "Portfolio records are file-backed and tenant-isolated. Each repository "
            "operation opens its own SQLite connection with WAL enabled; sensitive fields "
            "and document bytes are encrypted before they are stored."),
    }


def _json_ids(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PortfolioValidationError(f"{field} must be a list of record ids.")
    out = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            raise PortfolioValidationError(f"{field} contains an empty record id.")
        out.append(text)
    return list(dict.fromkeys(out))


def _text(value: object, field: str, *, required: bool = True, limit: int = 240) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise PortfolioValidationError(f"{field} is required.")
    if len(text) > limit:
        raise PortfolioValidationError(f"{field} is too long.")
    return text


def _country(value: object) -> str:
    text = _text(value, "country", limit=16).upper()
    if not text.replace("-", "").isalnum():
        raise PortfolioValidationError("country must be a short jurisdiction code.")
    return text


def _decimal_text(value: object) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, float):
        raise PortfolioValidationError("money values must be Decimal or text, never float.")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PortfolioValidationError("money value is not a valid Decimal.") from exc
    if not decimal.is_finite():
        raise PortfolioValidationError("money value must be finite.")
    return format(decimal, "f")


def _decoded_ids(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


class PortfolioRepository:
    """The durable portfolio store.

    There is intentionally no cached connection on this object. A FastAPI request calls
    one repository method, and that method's context manager opens, configures, commits
    and closes a fresh SQLite connection. WAL gives separate request connections good
    reader/writer behaviour without recreating the prior shared-threadpool connection.
    """

    def __init__(self, db_path: str | Path, master_key: bytes):
        self.db_path = Path(db_path).expanduser()
        if len(master_key) != _DATA_KEY_BYTES:
            raise PortfolioConfigurationError("The master key must be 32 bytes.")
        self._master_key = master_key

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # Tenant-key deletion must overwrite its SQLite cell before the later WAL
            # checkpoint and VACUUM lifecycle validates that no wrapped key remains.
            conn.execute("PRAGMA secure_delete=ON")
            conn.executescript(PORTFOLIO_SCHEMA)
            # Reads call `_ensure_scope` so a first portfolio view establishes its tenant
            # and user row. Begin immediately keeps that small creation transaction from
            # racing another request for the same newly signed-in tenant.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _wrap_key_with_nonce(self, tenant_id: str, data_key: bytes, nonce: bytes) -> bytes:
        if len(nonce) != _NONCE_BYTES:
            raise PortfolioIntegrityError("Tenant-key wrapping nonce has the wrong length.")
        aad = f"tenant-key:{tenant_id}".encode("utf-8")
        return nonce + AESGCM(self._master_key).encrypt(nonce, data_key, aad)

    def _wrap_key(self, tenant_id: str, data_key: bytes) -> bytes:
        return self._wrap_key_with_nonce(tenant_id, data_key, os.urandom(_NONCE_BYTES))

    def _unwrap_key(self, tenant_id: str, wrapped: bytes) -> bytes:
        if len(wrapped) <= _NONCE_BYTES:
            raise PortfolioIntegrityError("Stored tenant key is incomplete.")
        nonce, ciphertext = wrapped[:_NONCE_BYTES], wrapped[_NONCE_BYTES:]
        try:
            return AESGCM(self._master_key).decrypt(
                nonce, ciphertext, f"tenant-key:{tenant_id}".encode("utf-8"))
        except InvalidTag as exc:
            raise PortfolioIntegrityError(
                "The portfolio master key cannot authenticate this tenant's data key.") from exc

    def _erasure_state(self, conn: sqlite3.Connection, tenant_id: str) -> Optional[_TenantErasure]:
        row = conn.execute(
            "SELECT stage, recovery_wrapped_key, original_nonce, recovery_sha256, "
            "recovery_length, started_at FROM tenant_erasure WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if row is None:
            return None

        stage = str(row["stage"])
        recovery_sha256 = bytes(row["recovery_sha256"])
        recovery_length = int(row["recovery_length"])
        if len(recovery_sha256) != hashlib.sha256().digest_size or recovery_length <= 0:
            raise PortfolioIntegrityError("Tenant-key erasure metadata is malformed.")

        if stage == ERASURE_PENDING:
            if row["recovery_wrapped_key"] is None or row["original_nonce"] is None:
                raise PortfolioIntegrityError("Pending tenant-key erasure cannot be recovered.")
            recovery_wrapped_key = bytes(row["recovery_wrapped_key"])
            original_nonce = bytes(row["original_nonce"])
            if (len(original_nonce) != _NONCE_BYTES
                    or len(recovery_wrapped_key) != recovery_length
                    or hashlib.sha256(recovery_wrapped_key).digest() != recovery_sha256):
                raise PortfolioIntegrityError("Pending tenant-key erasure metadata failed verification.")
            return _TenantErasure(
                stage=stage,
                recovery_wrapped_key=recovery_wrapped_key,
                original_nonce=original_nonce,
                recovery_sha256=recovery_sha256,
                recovery_length=recovery_length,
                started_at=str(row["started_at"]),
            )

        if stage == ERASURE_FINALIZING:
            if row["recovery_wrapped_key"] is not None or row["original_nonce"] is not None:
                raise PortfolioIntegrityError("Finalizing tenant-key erasure retains recoverable key data.")
            return _TenantErasure(
                stage=stage,
                recovery_wrapped_key=None,
                original_nonce=None,
                recovery_sha256=recovery_sha256,
                recovery_length=recovery_length,
                started_at=str(row["started_at"]),
            )

        raise PortfolioIntegrityError("Tenant-key erasure has an unknown state.")

    def _reconstruct_pending_wrapped_key(self, tenant_id: str, erasure: _TenantErasure) -> bytes:
        if (erasure.stage != ERASURE_PENDING or erasure.recovery_wrapped_key is None
                or erasure.original_nonce is None):
            raise PortfolioIntegrityError("Tenant-key erasure cannot reconstruct its deleted key.")
        data_key = self._unwrap_key(tenant_id, erasure.recovery_wrapped_key)
        return self._wrap_key_with_nonce(tenant_id, data_key, erasure.original_nonce)

    def _tenant_key(self, conn: sqlite3.Connection, tenant_id: str) -> bytes:
        tenant = conn.execute(
            "SELECT shredded_at FROM tenant WHERE id=?", (tenant_id,)).fetchone()
        if tenant is None:
            conn.execute("INSERT INTO tenant (id, created_at) VALUES (?,?)", (tenant_id, _now()))
            data_key = os.urandom(_DATA_KEY_BYTES)
            conn.execute(
                "INSERT INTO tenant_key (tenant_id, wrapped_key, created_at) VALUES (?,?,?)",
                (tenant_id, self._wrap_key(tenant_id, data_key), _now()))
            return data_key

        erasure = self._erasure_state(conn, tenant_id)
        if tenant["shredded_at"] is not None:
            if erasure is not None:
                raise PortfolioIntegrityError("A shredded tenant still has erasure metadata.")
            raise TenantShredded("This tenant's data key was deleted, so its portfolio is unreadable.")
        if erasure is not None:
            raise TenantErasureInProgress(
                "Tenant-key erasure is still being validated; retry erasure before accessing this portfolio.")

        row = conn.execute(
            "SELECT wrapped_key FROM tenant_key WHERE tenant_id=?", (tenant_id,)).fetchone()
        if row is None:
            raise TenantShredded("This tenant has no data key, so its portfolio is unreadable.")
        return self._unwrap_key(tenant_id, bytes(row["wrapped_key"]))

    def _seal(self, key: bytes, tenant_id: str, purpose: str, plaintext: bytes) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        aad = f"{tenant_id}:{purpose}".encode("utf-8")
        return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)

    def _open(self, key: bytes, tenant_id: str, purpose: str, ciphertext: bytes) -> bytes:
        if len(ciphertext) <= _NONCE_BYTES:
            raise PortfolioIntegrityError("Stored encrypted portfolio data is incomplete.")
        nonce, payload = ciphertext[:_NONCE_BYTES], ciphertext[_NONCE_BYTES:]
        try:
            return AESGCM(key).decrypt(
                nonce, payload, f"{tenant_id}:{purpose}".encode("utf-8"))
        except InvalidTag as exc:
            raise PortfolioIntegrityError("Stored portfolio data failed authentication.") from exc

    def _seal_json(self, key: bytes, tenant_id: str, purpose: str, value: Mapping[str, Any]) -> bytes:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._seal(key, tenant_id, purpose, encoded)

    def _open_json(self, key: bytes, tenant_id: str, purpose: str, value: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(self._open(key, tenant_id, purpose, bytes(value)).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortfolioIntegrityError("Stored encrypted portfolio data is malformed.") from exc
        if not isinstance(parsed, dict):
            raise PortfolioIntegrityError("Stored encrypted portfolio data has the wrong shape.")
        return parsed

    def _ensure_scope(self, conn: sqlite3.Connection, scope: TenantScope) -> bytes:
        tenant_id = _text(scope.tenant_id, "tenant id", limit=160)
        user_id = _text(scope.user_id, "user id", limit=160)
        key = self._tenant_key(conn, tenant_id)
        now = _now()
        identity = self._seal_json(key, tenant_id, f"user:{user_id}", {
            "name": _text(scope.user_name, "user name", required=False, limit=240),
            "email": _text(scope.user_email, "user email", required=False, limit=240),
        })
        conn.execute(
            'INSERT INTO "user" (tenant_id, id, identity_encrypted, role, created_at, updated_at) '
            'VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id, id) DO UPDATE SET '
            'identity_encrypted=excluded.identity_encrypted, role=excluded.role, '
            'updated_at=excluded.updated_at',
            (tenant_id, user_id, identity, _text(scope.role, "role", limit=80), now, now))
        return key

    @staticmethod
    def _audit(conn: sqlite3.Connection, scope: TenantScope, action: str,
               object_type: str, object_id: str) -> None:
        conn.execute(
            "INSERT INTO audit (id, tenant_id, actor_id, action, object_type, object_id, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (new_id(), scope.tenant_id, scope.user_id, action, object_type, object_id, _now()))

    @staticmethod
    def _row_for_tenant(conn: sqlite3.Connection, table: str, record_id: str,
                        tenant_id: str) -> sqlite3.Row:
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id=? AND tenant_id=?", (record_id, tenant_id)).fetchone()
        if row is None:
            # One message whether the id never existed or belongs to another tenant.
            raise PortfolioNotFound("No portfolio record is available for this tenant.")
        return row

    def _require_ids(self, conn: sqlite3.Connection, table: str, ids: Sequence[str],
                     tenant_id: str) -> None:
        for record_id in ids:
            self._row_for_tenant(conn, table, record_id, tenant_id)

    def _default_household(self, conn: sqlite3.Connection, scope: TenantScope,
                           key: bytes) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM household WHERE tenant_id=? ORDER BY created_at, id LIMIT 1",
            (scope.tenant_id,)).fetchone()
        if row is not None:
            return row
        household_id = new_id()
        conn.execute(
            "INSERT INTO household (id, tenant_id, details_encrypted, created_at) VALUES (?,?,?,?)",
            (household_id, scope.tenant_id,
             self._seal_json(key, scope.tenant_id, f"household:{household_id}",
                             {"name": "Household"}), _now()))
        self._audit(conn, scope, "CREATE_HOUSEHOLD", "household", household_id)
        return self._row_for_tenant(conn, "household", household_id, scope.tenant_id)

    def _household_out(self, row: sqlite3.Row, key: bytes, tenant_id: str) -> dict:
        detail = self._open_json(key, tenant_id, f"household:{row['id']}", row["details_encrypted"])
        return {"id": row["id"], "name": detail.get("name", "Household"),
                "created_at": row["created_at"]}

    def _member_out(self, row: sqlite3.Row, key: bytes, tenant_id: str) -> dict:
        detail = self._open_json(key, tenant_id, f"member:{row['id']}", row["details_encrypted"])
        return {"id": row["id"], "household_id": row["household_id"],
                "name": detail.get("name", ""), "relationship": detail.get("relationship", ""),
                "date_of_birth": detail.get("date_of_birth"),
                "health_facts": detail.get("health_facts"), "created_at": row["created_at"]}

    def _asset_out(self, row: sqlite3.Row, key: bytes, tenant_id: str) -> dict:
        detail = self._open_json(key, tenant_id, f"asset:{row['id']}", row["details_encrypted"])
        return {"id": row["id"], "household_id": row["household_id"],
                "kind": detail.get("kind", ""), "name": detail.get("name", ""),
                "identifiers": detail.get("identifiers", {}), "created_at": row["created_at"]}

    def _business_out(self, row: sqlite3.Row, key: bytes, tenant_id: str) -> dict:
        detail = self._open_json(key, tenant_id, f"business:{row['id']}", row["details_encrypted"])
        return {"id": row["id"], "name": detail.get("name", ""),
                "country": detail.get("country", ""),
                "registration_number": detail.get("registration_number"),
                "source": "SME_ANALYSIS" if row["source_case_id"] else "PORTFOLIO",
                "created_at": row["created_at"]}

    def _coverage_out(self, conn: sqlite3.Connection, policy_id: str, key: bytes,
                      tenant_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM coverage_item WHERE policy_id=? AND tenant_id=? ORDER BY created_at, id",
            (policy_id, tenant_id)).fetchall()
        out = []
        for row in rows:
            value = None
            if row["value_encrypted"] is not None:
                value = self._open_json(
                    key, tenant_id, f"coverage-item:{row['id']}", row["value_encrypted"])
            out.append({
                "id": row["id"], "mechanism_code": row["mechanism_code"],
                "value": value.get("amount") if value else None,
                "unit": value.get("unit") if value else None,
                "evidence_span_ids": _decoded_ids(row["evidence_span_ids"]),
                "evidence_class": row["evidence_class"],
                "source_document_id": row["source_document_id"],
            })
        return out

    def _policy_out(self, conn: sqlite3.Connection, row: sqlite3.Row, key: bytes,
                    tenant_id: str) -> dict:
        detail = self._open_json(key, tenant_id, f"policy:{row['id']}", row["details_encrypted"])
        return {
            "id": row["id"], "household_id": row["household_id"],
            "business_id": row["business_id"], "country": row["country"],
            "line_of_business": row["line_of_business"], "insurer": detail.get("insurer", ""),
            "product": detail.get("product", ""), "masked_number": detail.get("masked_number", ""),
            "period": {"start": row["period_start"], "end": row["period_end"]},
            "status": row["status"], "renewal_date": row["renewal_date"],
            "insured_member_ids": _decoded_ids(row["insured_member_ids"]),
            "insured_asset_ids": _decoded_ids(row["insured_asset_ids"]),
            "source_document_ids": _decoded_ids(row["source_document_ids"]),
            "source": "SME_ANALYSIS" if row["source_case_id"] else "PORTFOLIO",
            "coverage_items": self._coverage_out(conn, row["id"], key, tenant_id),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _derived_author_class(kind: str, country: str) -> str:
        pack = jurisdiction.pack(country) if jurisdiction.has_pack(country) else jurisdiction.pack(jurisdiction.GENERIC)
        author_class = detect.author_class_for(kind, pack)
        if author_class not in AUTHOR_TO_CLASS:
            return "SELF"
        return author_class

    def add_member(self, scope: TenantScope, *, name: str, relationship: str,
                   date_of_birth: Optional[str] = None, health_facts: Optional[str] = None,
                   household_id: Optional[str] = None) -> dict:
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            household = (self._row_for_tenant(conn, "household", household_id, scope.tenant_id)
                         if household_id else self._default_household(conn, scope, key))
            member_id = new_id()
            detail = {
                "name": _text(name, "member name"),
                "relationship": _text(relationship, "relationship"),
                "date_of_birth": _text(date_of_birth, "date of birth", required=False, limit=40) or None,
                "health_facts": _text(health_facts, "health facts", required=False, limit=4_000) or None,
            }
            conn.execute(
                "INSERT INTO member (id, tenant_id, household_id, details_encrypted, created_at) "
                "VALUES (?,?,?,?,?)",
                (member_id, scope.tenant_id, household["id"],
                 self._seal_json(key, scope.tenant_id, f"member:{member_id}", detail), _now()))
            self._audit(conn, scope, "CREATE_MEMBER", "member", member_id)
            return self._member_out(
                self._row_for_tenant(conn, "member", member_id, scope.tenant_id), key, scope.tenant_id)

    def list_members(self, scope: TenantScope) -> list[dict]:
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            rows = conn.execute(
                "SELECT * FROM member WHERE tenant_id=? ORDER BY created_at, id", (scope.tenant_id,)).fetchall()
            return [self._member_out(row, key, scope.tenant_id) for row in rows]

    def add_asset(self, scope: TenantScope, *, kind: str, name: str,
                  identifiers: Optional[Mapping[str, object]] = None,
                  household_id: Optional[str] = None) -> dict:
        if identifiers is not None and not isinstance(identifiers, Mapping):
            raise PortfolioValidationError("asset identifiers must be an object.")
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            household = (self._row_for_tenant(conn, "household", household_id, scope.tenant_id)
                         if household_id else self._default_household(conn, scope, key))
            asset_id = new_id()
            detail = {
                "kind": _text(kind, "asset kind"), "name": _text(name, "asset name"),
                "identifiers": {str(k): str(v) for k, v in (identifiers or {}).items()},
            }
            conn.execute(
                "INSERT INTO asset (id, tenant_id, household_id, details_encrypted, created_at) "
                "VALUES (?,?,?,?,?)",
                (asset_id, scope.tenant_id, household["id"],
                 self._seal_json(key, scope.tenant_id, f"asset:{asset_id}", detail), _now()))
            self._audit(conn, scope, "CREATE_ASSET", "asset", asset_id)
            return self._asset_out(
                self._row_for_tenant(conn, "asset", asset_id, scope.tenant_id), key, scope.tenant_id)

    def list_assets(self, scope: TenantScope) -> list[dict]:
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            rows = conn.execute(
                "SELECT * FROM asset WHERE tenant_id=? ORDER BY created_at, id", (scope.tenant_id,)).fetchall()
            return [self._asset_out(row, key, scope.tenant_id) for row in rows]

    def add_business(self, scope: TenantScope, *, name: str, country: str,
                     registration_number: Optional[str] = None) -> dict:
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            business_id = new_id()
            detail = {
                "name": _text(name, "business name"), "country": _country(country),
                "registration_number": _text(registration_number, "registration number",
                                               required=False, limit=240) or None,
            }
            conn.execute(
                "INSERT INTO business (id, tenant_id, details_encrypted, source_case_id, created_at) "
                "VALUES (?,?,?,?,?)",
                (business_id, scope.tenant_id,
                 self._seal_json(key, scope.tenant_id, f"business:{business_id}", detail),
                 None, _now()))
            self._audit(conn, scope, "CREATE_BUSINESS", "business", business_id)
            return self._business_out(
                self._row_for_tenant(conn, "business", business_id, scope.tenant_id), key, scope.tenant_id)

    def list_businesses(self, scope: TenantScope) -> list[dict]:
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            rows = conn.execute(
                "SELECT * FROM business WHERE tenant_id=? ORDER BY created_at, id", (scope.tenant_id,)).fetchall()
            return [self._business_out(row, key, scope.tenant_id) for row in rows]

    def add_document(self, scope: TenantScope, *, kind: str, blob: bytes,
                     language: str, country: str, filename: str = "") -> dict:
        if not isinstance(blob, bytes) or not blob:
            raise PortfolioValidationError("document blob must contain bytes.")
        country_code = _country(country)
        document_kind = _text(kind, "document kind", limit=120).upper()
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            document_id = new_id()
            author_class = self._derived_author_class(document_kind, country_code)
            conn.execute(
                "INSERT INTO document (id, tenant_id, kind, author_class, sha256, encrypted_blob, "
                "metadata_encrypted, language, country, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (document_id, scope.tenant_id, document_kind, author_class,
                 hashlib.sha256(blob).hexdigest(),
                 self._seal(key, scope.tenant_id, f"document-blob:{document_id}", blob),
                 self._seal_json(key, scope.tenant_id, f"document:{document_id}",
                                 {"filename": _text(filename, "filename", required=False, limit=300)}),
                 _text(language, "document language", limit=40), country_code, _now()))
            self._audit(conn, scope, "STORE_DOCUMENT", "document", document_id)
            return {"id": document_id, "kind": document_kind, "author_class": author_class,
                    "sha256": hashlib.sha256(blob).hexdigest(), "language": language,
                    "country": country_code}

    def get_document(self, scope: TenantScope, document_id: str) -> dict:
        """Read an encrypted document for an internal server-side caller, never a route."""
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            row = self._row_for_tenant(conn, "document", document_id, scope.tenant_id)
            metadata = self._open_json(key, scope.tenant_id, f"document:{document_id}",
                                       row["metadata_encrypted"])
            blob = self._open(key, scope.tenant_id, f"document-blob:{document_id}",
                              row["encrypted_blob"])
            return {"id": row["id"], "kind": row["kind"], "author_class": row["author_class"],
                    "sha256": row["sha256"], "filename": metadata.get("filename", ""),
                    "language": row["language"], "country": row["country"], "blob": blob}

    def _coverage_class(self, conn: sqlite3.Connection, scope: TenantScope,
                        document_id: Optional[str]) -> str:
        if not document_id:
            return "UNESTABLISHED"
        row = self._row_for_tenant(conn, "document", document_id, scope.tenant_id)
        return AUTHOR_TO_CLASS[row["author_class"]]

    def create_policy(self, scope: TenantScope, *, country: str, line_of_business: str,
                      insurer: str, product: str, masked_number: str,
                      period_start: Optional[str] = None, period_end: Optional[str] = None,
                      status: str, renewal_date: Optional[str] = None,
                      household_id: Optional[str] = None, business_id: Optional[str] = None,
                      insured_member_ids: Optional[Sequence[str]] = None,
                      insured_asset_ids: Optional[Sequence[str]] = None,
                      source_document_ids: Optional[Sequence[str]] = None,
                      coverage_items: Optional[Sequence[Mapping[str, object]]] = None) -> dict:
        if household_id and business_id:
            raise PortfolioValidationError("A policy belongs to a household or a business, not both.")
        member_ids = _json_ids(insured_member_ids, "insured member ids")
        asset_ids = _json_ids(insured_asset_ids, "insured asset ids")
        document_ids = _json_ids(source_document_ids, "source document ids")
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            if household_id:
                household = self._row_for_tenant(conn, "household", household_id, scope.tenant_id)
            elif business_id:
                household = None
                self._row_for_tenant(conn, "business", business_id, scope.tenant_id)
            else:
                household = self._default_household(conn, scope, key)
                household_id = household["id"]
            self._require_ids(conn, "member", member_ids, scope.tenant_id)
            self._require_ids(conn, "asset", asset_ids, scope.tenant_id)
            self._require_ids(conn, "document", document_ids, scope.tenant_id)

            policy_id = new_id()
            now = _now()
            conn.execute(
                "INSERT INTO policy (id, tenant_id, household_id, business_id, country, line_of_business, "
                "details_encrypted, period_start, period_end, status, renewal_date, insured_member_ids, "
                "insured_asset_ids, source_document_ids, source_case_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (policy_id, scope.tenant_id, household_id, business_id, _country(country),
                 _text(line_of_business, "line of business", limit=120).upper(),
                 self._seal_json(key, scope.tenant_id, f"policy:{policy_id}", {
                     "insurer": _text(insurer, "insurer"), "product": _text(product, "product"),
                     "masked_number": _text(masked_number, "masked policy number", required=False),
                 }),
                 _text(period_start, "period start", required=False, limit=40) or None,
                 _text(period_end, "period end", required=False, limit=40) or None,
                 _text(status, "policy status", limit=80),
                 _text(renewal_date, "renewal date", required=False, limit=40) or None,
                 json.dumps(member_ids), json.dumps(asset_ids), json.dumps(document_ids),
                 None, now, now))
            for item in coverage_items or ():
                if not isinstance(item, Mapping):
                    raise PortfolioValidationError("coverage items must be objects.")
                if "evidence_class" in item:
                    raise PortfolioValidationError("evidence class is derived from the source document.")
                coverage_id = new_id()
                source_id = _text(item.get("source_document_id"), "source document id",
                                  required=False, limit=160) or None
                if source_id and source_id not in document_ids:
                    raise PortfolioValidationError(
                        "a coverage item's source document must be in source document ids.")
                evidence_class = self._coverage_class(conn, scope, source_id)
                amount = _decimal_text(item.get("value"))
                value = None if amount is None else self._seal_json(
                    key, scope.tenant_id, f"coverage-item:{coverage_id}", {
                        "amount": amount,
                        "unit": _text(item.get("unit"), "coverage unit", required=False, limit=80) or None,
                    })
                conn.execute(
                    "INSERT INTO coverage_item (id, tenant_id, policy_id, mechanism_code, value_encrypted, "
                    "evidence_span_ids, evidence_class, source_document_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (coverage_id, scope.tenant_id, policy_id,
                     _text(item.get("mechanism_code"), "mechanism code", limit=160), value,
                     json.dumps(_json_ids(item.get("evidence_span_ids"), "evidence span ids")),
                     evidence_class, source_id, _now()))
            self._audit(conn, scope, "CREATE_POLICY", "policy", policy_id)
            return self._policy_out(
                conn, self._row_for_tenant(conn, "policy", policy_id, scope.tenant_id),
                key, scope.tenant_id)

    def list_policies(self, scope: TenantScope) -> list[dict]:
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            rows = conn.execute(
                "SELECT * FROM policy WHERE tenant_id=? ORDER BY created_at, id", (scope.tenant_id,)).fetchall()
            return [self._policy_out(conn, row, key, scope.tenant_id) for row in rows]

    def get_policy(self, scope: TenantScope, policy_id: str) -> dict:
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            row = self._row_for_tenant(conn, "policy", policy_id, scope.tenant_id)
            return self._policy_out(conn, row, key, scope.tenant_id)

    def record_consent(self, scope: TenantScope, *, purpose: str, data_categories: Sequence[str],
                       recipient: str, duration: str) -> dict:
        """Persist consent data encrypted; a later consent UI can reuse this model."""
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            consent_id = new_id()
            detail = {
                "purpose": _text(purpose, "consent purpose"),
                "data_categories": _json_ids(data_categories, "data categories"),
                "recipient": _text(recipient, "consent recipient"),
                "duration": _text(duration, "consent duration"),
            }
            conn.execute(
                "INSERT INTO consent (id, tenant_id, details_encrypted, created_at) VALUES (?,?,?,?)",
                (consent_id, scope.tenant_id,
                 self._seal_json(key, scope.tenant_id, f"consent:{consent_id}", detail), _now()))
            self._audit(conn, scope, "RECORD_CONSENT", "consent", consent_id)
            return {"id": consent_id, **detail}

    def wrap_sme_analysis(self, scope: TenantScope, state: Mapping[str, object]) -> Optional[dict]:
        """Represent the existing SME analysis as one business policy bundle.

        This copies no finding, source text or document bytes from the transient case.
        It records only the business/policy metadata already visible in the Brief, so
        deleting the case still removes its findings while durable portfolio facts remain.
        """
        brief = state.get("brief") if isinstance(state, Mapping) else None
        case_id = state.get("case_id") if isinstance(state, Mapping) else None
        if not isinstance(brief, Mapping) or brief.get("deleted") or not case_id:
            return None
        source_case_id = _text(case_id, "case id", limit=160)
        business_name = _text(brief.get("business"), "business name")
        jurisdiction_view = brief.get("jurisdiction")
        country = (jurisdiction_view.get("detected") if isinstance(jurisdiction_view, Mapping)
                   else None) or "GENERIC"
        carrier = _text(brief.get("carrier"), "carrier", required=False) or "Unknown insurer"

        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            existing = conn.execute(
                "SELECT * FROM policy WHERE tenant_id=? AND source_case_id=?",
                (scope.tenant_id, source_case_id)).fetchone()
            if existing is not None:
                return self._policy_out(conn, existing, key, scope.tenant_id)
            business_id = new_id()
            conn.execute(
                "INSERT INTO business (id, tenant_id, details_encrypted, source_case_id, created_at) "
                "VALUES (?,?,?,?,?)",
                (business_id, scope.tenant_id,
                 self._seal_json(key, scope.tenant_id, f"business:{business_id}", {
                     "name": business_name, "country": _country(country), "registration_number": None,
                 }), source_case_id, _now()))
            policy_id = new_id()
            now = _now()
            conn.execute(
                "INSERT INTO policy (id, tenant_id, household_id, business_id, country, line_of_business, "
                "details_encrypted, period_start, period_end, status, renewal_date, insured_member_ids, "
                "insured_asset_ids, source_document_ids, source_case_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (policy_id, scope.tenant_id, None, business_id, _country(country), "SME_PACKAGE",
                 self._seal_json(key, scope.tenant_id, f"policy:{policy_id}", {
                     "insurer": carrier, "product": "Existing SME analysis bundle", "masked_number": "",
                 }), None, None, "ANALYSED", None, "[]", "[]", "[]", source_case_id, now, now))
            self._audit(conn, scope, "WRAP_SME_ANALYSIS", "policy", policy_id)
            return self._policy_out(
                conn, self._row_for_tenant(conn, "policy", policy_id, scope.tenant_id),
                key, scope.tenant_id)

    def portfolio(self, scope: TenantScope) -> dict:
        with self._connection() as conn:
            key = self._ensure_scope(conn, scope)
            household = conn.execute(
                "SELECT * FROM household WHERE tenant_id=? ORDER BY created_at, id LIMIT 1",
                (scope.tenant_id,)).fetchone()
            members = conn.execute(
                "SELECT * FROM member WHERE tenant_id=? ORDER BY created_at, id", (scope.tenant_id,)).fetchall()
            assets = conn.execute(
                "SELECT * FROM asset WHERE tenant_id=? ORDER BY created_at, id", (scope.tenant_id,)).fetchall()
            businesses = conn.execute(
                "SELECT * FROM business WHERE tenant_id=? ORDER BY created_at, id", (scope.tenant_id,)).fetchall()
            policies = conn.execute(
                "SELECT * FROM policy WHERE tenant_id=? ORDER BY created_at, id", (scope.tenant_id,)).fetchall()
            return {
                "tenant_id": scope.tenant_id,
                "persistent": True,
                "household": self._household_out(household, key, scope.tenant_id) if household else None,
                "members": [self._member_out(row, key, scope.tenant_id) for row in members],
                "assets": [self._asset_out(row, key, scope.tenant_id) for row in assets],
                "businesses": [self._business_out(row, key, scope.tenant_id) for row in businesses],
                "policies": [self._policy_out(conn, row, key, scope.tenant_id) for row in policies],
            }

    @staticmethod
    def _checkpoint_truncate(conn: sqlite3.Connection) -> None:
        """Flush WAL frames before a completed local key erasure is reported."""
        last: object = None
        for attempt in range(100):
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row is not None and int(row[0]) == 0:
                    return
                last = row
            except sqlite3.OperationalError as exc:
                last = exc
            if attempt < 99:
                time.sleep(0.01)
        raise PortfolioIntegrityError(
            "Could not complete secure tenant-key removal while another database "
            f"operation is active: {last!r}")

    @staticmethod
    def _artifact_contains(path: Path, needle: bytes) -> bool:
        """Search a database artifact without loading document blobs into memory."""
        overlap = max(0, len(needle) - 1)
        tail = b""
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                if needle in tail + chunk:
                    return True
                tail = (tail + chunk)[-overlap:] if overlap else b""
        return False

    @staticmethod
    def _artifact_contains_digest(path: Path, digest: bytes, length: int) -> bool:
        """Find a deleted key by digest when retaining its bytes would defeat erasure."""
        if len(digest) != hashlib.sha256().digest_size or length <= 0:
            raise PortfolioIntegrityError("Tenant-key erasure digest metadata is malformed.")
        tail = b""
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                data = tail + chunk
                last_start = len(data) - length
                for start in range(max(0, last_start + 1)):
                    if hashlib.sha256(data[start:start + length]).digest() == digest:
                        return True
                tail = data[-(length - 1):] if length > 1 else b""
        return False

    def _purge_sqlite_artifacts(self) -> None:
        """Checkpoint and rebuild local artifacts after deleting sensitive key data."""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA secure_delete=ON")
            self._checkpoint_truncate(conn)
            conn.execute("VACUUM")
            self._checkpoint_truncate(conn)
        finally:
            conn.close()

    def _purge_deleted_tenant_key(self, wrapped_key: bytes) -> None:
        """Validate that local SQLite artifacts no longer retain one wrapped key."""
        self._purge_sqlite_artifacts()
        for suffix in ("", "-wal", "-shm"):
            artifact = self.db_path.with_name(self.db_path.name + suffix)
            if artifact.exists() and self._artifact_contains(artifact, wrapped_key):
                raise PortfolioIntegrityError(
                    f"Secure tenant-key removal left key material in {artifact.name}.")

    def _purge_deleted_tenant_key_digest(self, digest: bytes, length: int) -> None:
        """Validate deletion without retaining a usable recovery wrapper."""
        self._purge_sqlite_artifacts()
        for suffix in ("", "-wal", "-shm"):
            artifact = self.db_path.with_name(self.db_path.name + suffix)
            if artifact.exists() and self._artifact_contains_digest(artifact, digest, length):
                raise PortfolioIntegrityError(
                    f"Secure tenant-key removal left recovery key material in {artifact.name}.")

    def _start_crypto_shred(self, conn: sqlite3.Connection, scope: TenantScope) -> _TenantErasure:
        """Persist a recoverable pending state before deleting the live key wrapper."""
        tenant_id = _text(scope.tenant_id, "tenant id", limit=160)
        row = conn.execute(
            "SELECT wrapped_key FROM tenant_key WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if row is None:
            raise TenantShredded("This tenant has no data key, so its portfolio is unreadable.")
        wrapped_key = bytes(row["wrapped_key"])
        original_nonce = wrapped_key[:_NONCE_BYTES]
        data_key = self._unwrap_key(tenant_id, wrapped_key)
        recovery_wrapped_key = self._wrap_key(tenant_id, data_key)
        while recovery_wrapped_key[:_NONCE_BYTES] == original_nonce:
            recovery_wrapped_key = self._wrap_key(tenant_id, data_key)
        digest = hashlib.sha256(recovery_wrapped_key).digest()
        started_at = _now()
        conn.execute(
            "INSERT INTO tenant_erasure (tenant_id, stage, recovery_wrapped_key, original_nonce, "
            "recovery_sha256, recovery_length, started_at) VALUES (?,?,?,?,?,?,?)",
            (tenant_id, ERASURE_PENDING, recovery_wrapped_key, original_nonce,
             digest, len(recovery_wrapped_key), started_at),
        )
        conn.execute("DELETE FROM tenant_key WHERE tenant_id=?", (tenant_id,))
        return _TenantErasure(
            stage=ERASURE_PENDING,
            recovery_wrapped_key=recovery_wrapped_key,
            original_nonce=original_nonce,
            recovery_sha256=digest,
            recovery_length=len(recovery_wrapped_key),
            started_at=started_at,
        )

    def _restore_pending_erasure(self, scope: TenantScope, erasure: _TenantErasure) -> None:
        """Restore a live key when initial artifact validation fails normally."""
        tenant_id = _text(scope.tenant_id, "tenant id", limit=160)
        wrapped_key = self._reconstruct_pending_wrapped_key(tenant_id, erasure)
        with self._connection() as conn:
            current = self._erasure_state(conn, tenant_id)
            if current is None or current.stage != ERASURE_PENDING:
                raise PortfolioIntegrityError("Pending tenant-key erasure changed before recovery.")
            if current.recovery_wrapped_key != erasure.recovery_wrapped_key:
                raise PortfolioIntegrityError("Pending tenant-key erasure recovery data changed.")
            tenant = conn.execute(
                "SELECT shredded_at FROM tenant WHERE id=?", (tenant_id,)
            ).fetchone()
            if tenant is None or tenant["shredded_at"] is not None:
                raise PortfolioIntegrityError("Pending tenant-key erasure cannot restore its tenant.")
            live = conn.execute(
                "SELECT wrapped_key FROM tenant_key WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            if live is not None and bytes(live["wrapped_key"]) != wrapped_key:
                raise PortfolioIntegrityError("Pending tenant-key erasure found a different live key.")
            if live is None:
                conn.execute(
                    "INSERT INTO tenant_key (tenant_id, wrapped_key, created_at) VALUES (?,?,?)",
                    (tenant_id, wrapped_key, _now()),
                )
            conn.execute("DELETE FROM tenant_erasure WHERE tenant_id=?", (tenant_id,))

    def _advance_erasure_to_finalizing(self, tenant_id: str, erasure: _TenantErasure) -> _TenantErasure:
        """Drop recoverable data only after the original wrapper has been purged."""
        if erasure.stage != ERASURE_PENDING:
            raise PortfolioIntegrityError("Only pending tenant-key erasure can enter final validation.")
        with self._connection() as conn:
            current = self._erasure_state(conn, tenant_id)
            if current != erasure:
                raise PortfolioIntegrityError("Pending tenant-key erasure changed before final validation.")
            tenant = conn.execute(
                "SELECT shredded_at FROM tenant WHERE id=?", (tenant_id,)
            ).fetchone()
            if tenant is None or tenant["shredded_at"] is not None:
                raise PortfolioIntegrityError("Pending tenant-key erasure cannot enter final validation.")
            if conn.execute(
                    "SELECT 1 FROM tenant_key WHERE tenant_id=?", (tenant_id,)).fetchone() is not None:
                raise PortfolioIntegrityError("Pending tenant-key erasure unexpectedly has a live key.")
            # Delete rather than overwrite so secure_delete and the following VACUUM erase
            # the recovery wrapper before the digest-only final-validation state is stored.
            conn.execute("DELETE FROM tenant_erasure WHERE tenant_id=?", (tenant_id,))
            conn.execute(
                "INSERT INTO tenant_erasure (tenant_id, stage, recovery_wrapped_key, original_nonce, "
                "recovery_sha256, recovery_length, started_at) VALUES (?,?,?,?,?,?,?)",
                (tenant_id, ERASURE_FINALIZING, None, None, erasure.recovery_sha256,
                 erasure.recovery_length, erasure.started_at),
            )
        return _TenantErasure(
            stage=ERASURE_FINALIZING,
            recovery_wrapped_key=None,
            original_nonce=None,
            recovery_sha256=erasure.recovery_sha256,
            recovery_length=erasure.recovery_length,
            started_at=erasure.started_at,
        )

    def _complete_erasure(self, scope: TenantScope, erasure: _TenantErasure) -> str:
        """Commit the terminal state only after both wrapper values are absent locally."""
        tenant_id = _text(scope.tenant_id, "tenant id", limit=160)
        completed_at = _now()
        with self._connection() as conn:
            current = self._erasure_state(conn, tenant_id)
            if current != erasure or current.stage != ERASURE_FINALIZING:
                raise PortfolioIntegrityError("Tenant-key erasure changed before completion.")
            tenant = conn.execute(
                "SELECT shredded_at FROM tenant WHERE id=?", (tenant_id,)
            ).fetchone()
            if tenant is None or tenant["shredded_at"] is not None:
                raise PortfolioIntegrityError("Tenant-key erasure cannot mark this tenant as shredded.")
            if conn.execute(
                    "SELECT 1 FROM tenant_key WHERE tenant_id=?", (tenant_id,)).fetchone() is not None:
                raise PortfolioIntegrityError("Tenant-key erasure unexpectedly has a live key.")
            conn.execute("UPDATE tenant SET shredded_at=? WHERE id=?", (completed_at, tenant_id))
            conn.execute("DELETE FROM tenant_erasure WHERE tenant_id=?", (tenant_id,))
            self._audit(conn, scope, "CRYPTO_SHRED", "tenant", tenant_id)
        return completed_at

    def crypto_shred(self, scope: TenantScope) -> dict:
        """Destroy a tenant key through a restart-safe local erasure state machine.

        PENDING retains a separately wrapped recovery value until the old live wrapper
        has been physically purged. FINALIZING retains only its digest, so an interrupted
        final scan can resume but no process can recover the data key. `shredded_at` is
        committed only after both validation passes complete.
        """
        tenant_id = _text(scope.tenant_id, "tenant id", limit=160)
        with self._connection() as conn:
            erasure = self._erasure_state(conn, tenant_id)
            if erasure is None:
                self._ensure_scope(conn, scope)
                erasure = self._start_crypto_shred(conn, scope)
            else:
                tenant = conn.execute(
                    "SELECT shredded_at FROM tenant WHERE id=?", (tenant_id,)
                ).fetchone()
                if tenant is None or tenant["shredded_at"] is not None:
                    raise PortfolioIntegrityError("Tenant-key erasure state does not match its tenant.")

        if erasure.stage == ERASURE_PENDING:
            original_wrapped_key = self._reconstruct_pending_wrapped_key(tenant_id, erasure)
            try:
                self._purge_deleted_tenant_key(original_wrapped_key)
            except Exception as exc:
                try:
                    self._restore_pending_erasure(scope, erasure)
                except Exception as restore_exc:
                    raise PortfolioIntegrityError(
                        "Secure tenant-key removal failed and its pending key could not be restored.") from restore_exc
                raise PortfolioIntegrityError(
                    "Secure tenant-key removal did not complete; the tenant key was restored.") from exc
            erasure = self._advance_erasure_to_finalizing(tenant_id, erasure)

        if erasure.stage == ERASURE_FINALIZING:
            try:
                self._purge_deleted_tenant_key_digest(
                    erasure.recovery_sha256, erasure.recovery_length)
            except Exception as exc:
                raise PortfolioIntegrityError(
                    "Secure tenant-key removal is awaiting final artifact validation; retry erasure.") from exc
            completed_at = self._complete_erasure(scope, erasure)
            return {"tenant_id": tenant_id, "shredded": True, "at": completed_at}

        raise PortfolioIntegrityError("Tenant-key erasure has an unknown state.")
