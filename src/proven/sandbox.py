"""Local synthetic insurer and bank sandbox used by the VERIS demo spine.

Nothing in this module contacts an insurer, bank, payment rail or credential provider.  It
models deterministic counterparties in a separate SQLite database, writes HMAC-signed
JSONL operation records, and makes its artificial nature explicit in every result.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional


SANDBOX_DB_ENV = "VERIS_SANDBOX_DB_PATH"
SANDBOX_LOG_ENV = "VERIS_SANDBOX_LOG_PATH"
SANDBOX_KEY_ENV = "VERIS_SANDBOX_SIGNING_KEY"
SANDBOX_URL_ENV = "VERIS_SANDBOX_URL"
SANDBOX_ADMIN_TOKEN_ENV = "VERIS_LOCAL_SANDBOX_ADMIN_TOKEN"
SANDBOX_CLIENT_ID_ENV = "VERIS_SANDBOX_CLIENT_ID"
SANDBOX_CLIENT_SECRET_ENV = "VERIS_SANDBOX_CLIENT_SECRET"
SANDBOX_SERVICE_TENANT = "__veris_service__"
_DEFAULT_CLOCK = "2026-10-10T09:00:00+00:00"
_LOCAL_ADMIN_TENANT = "__local_admin__"
_TWO = Decimal("0.01")


def local_service_credentials() -> tuple[str, str, bytes]:
    """Return the generated credential shared only by local VERIS and sandbox services."""
    client_id = os.getenv(SANDBOX_CLIENT_ID_ENV, "").strip()
    encoded_secret = os.getenv(SANDBOX_CLIENT_SECRET_ENV, "").strip()
    if not client_id or len(client_id) > 160 or not encoded_secret:
        raise SandboxUnavailable(
            f"Set {SANDBOX_CLIENT_ID_ENV} and a generated {SANDBOX_CLIENT_SECRET_ENV} "
            "before starting the local synthetic sandbox.")
    try:
        secret = base64.urlsafe_b64decode(
            (encoded_secret + "=" * (-len(encoded_secret) % 4)).encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise SandboxUnavailable(
            f"{SANDBOX_CLIENT_SECRET_ENV} must be URL-safe base64 for 32 random bytes.") from exc
    if len(secret) != 32:
        raise SandboxUnavailable(
            f"{SANDBOX_CLIENT_SECRET_ENV} must encode exactly 32 random bytes.")
    return client_id, encoded_secret, secret


def local_service_access_token(tenant_id: object) -> str:
    """Derive one tenant-bound bearer token from the generated local service credential."""
    tenant = _tenant_id(tenant_id)
    client_id, _encoded_secret, secret = local_service_credentials()
    material = _canonical({"client_id": client_id, "tenant_id": tenant})
    digest = hmac.new(secret, material.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sandbox.{digest}"


class SandboxError(RuntimeError):
    http_status = 400


class SandboxNotFound(SandboxError):
    http_status = 404


class SandboxConflict(SandboxError):
    http_status = 409


class SandboxUnavailable(SandboxError):
    http_status = 503


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_clock (
  id INTEGER PRIMARY KEY CHECK (id=1),
  current_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sandbox_claim (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  settlement_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sandbox_invoice (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL DEFAULT '',
  payee TEXT NOT NULL,
  amount TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sandbox_bank_account (
  id TEXT PRIMARY KEY,
  balance TEXT NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sandbox_bank_account_tenant
  ON sandbox_bank_account (tenant_id, id);
CREATE TABLE IF NOT EXISTS sandbox_bank_transfer (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL DEFAULT '',
  from_account TEXT NOT NULL,
  to_account TEXT NOT NULL,
  amount TEXT NOT NULL,
  reference TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sandbox_request (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sandbox_operation_log (
  seq INTEGER PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '',
  record_json TEXT NOT NULL,
  prev_hmac TEXT NOT NULL,
  hmac TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sandbox_operation_tenant
  ON sandbox_operation_log (tenant_id, seq);
CREATE TABLE IF NOT EXISTS sandbox_operation_checkpoint (
  tenant_id TEXT PRIMARY KEY,
  record_count INTEGER NOT NULL,
  head_hmac TEXT NOT NULL,
  checkpoint_hmac TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _money(value: object) -> str:
    if isinstance(value, float):
        raise SandboxError("Sandbox money must be Decimal or text, never float.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SandboxError("Sandbox money is not a Decimal amount.") from exc
    if not amount.is_finite() or amount < 0:
        raise SandboxError("Sandbox money must be a non-negative finite amount.")
    return format(amount.quantize(_TWO, rounding=ROUND_HALF_UP), "f")


def _tenant_id(value: object) -> str:
    tenant_id = str(value or "").strip()
    if not tenant_id or len(tenant_id) > 160:
        raise SandboxError("A bounded tenant id is required for every sandbox operation.")
    return tenant_id


def beneficiary_account_id(tenant_id: object) -> str:
    """Derive the only settlement-beneficiary account visible to a tenant."""
    return f"sandbox-beneficiary:{_tenant_id(tenant_id)}"


def verify_local_admin_token(value: object) -> bool:
    """Verify the separate local-admin capability without exposing a fallback secret."""
    expected = os.getenv(SANDBOX_ADMIN_TOKEN_ENV, "").strip()
    supplied = str(value or "")
    return bool(expected and hmac.compare_digest(supplied, expected))


def _key_from_environment(path: Path) -> bytes:
    encoded = os.getenv(SANDBOX_KEY_ENV, "").strip()
    if encoded:
        try:
            raw = base64.urlsafe_b64decode((encoded + "=" * (-len(encoded) % 4)).encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SandboxError(f"{SANDBOX_KEY_ENV} must be URL-safe base64.") from exc
        if len(raw) != 32:
            raise SandboxError(f"{SANDBOX_KEY_ENV} must contain 32 bytes.")
        return raw
    key_file = path.with_suffix(path.suffix + ".key")
    try:
        raw = base64.urlsafe_b64decode(key_file.read_text(encoding="ascii").strip().encode("ascii"))
        if len(raw) == 32:
            return raw
    except FileNotFoundError:
        pass
    except (ValueError, UnicodeDecodeError):
        raise SandboxError("The local sandbox signing key is malformed.")
    key_file.parent.mkdir(parents=True, exist_ok=True)
    raw = os.urandom(32)
    try:
        with key_file.open("x", encoding="ascii") as handle:
            handle.write(base64.urlsafe_b64encode(raw).decode("ascii") + "\n")
    except FileExistsError:
        return _key_from_environment(path)
    return raw


def default_service() -> "SandboxService":
    db_path = Path(os.getenv(SANDBOX_DB_ENV, "sandbox/sandbox.sqlite")).expanduser()
    log_path = Path(os.getenv(SANDBOX_LOG_ENV, str(db_path.with_suffix(".operations.jsonl")))).expanduser()
    return SandboxService(db_path, signing_key=_key_from_environment(db_path), log_path=log_path)


class SandboxService:
    """Deterministic synthetic insurer/bank state with tenant-scoped signed operations."""

    def __init__(self, db_path: str | Path, *, signing_key: bytes,
                 log_path: Optional[str | Path] = None):
        self.db_path = Path(db_path)
        if len(signing_key) < 16:
            raise SandboxError("The sandbox signing key is too short.")
        self._key = bytes(signing_key)
        self.log_path = Path(log_path) if log_path is not None else None

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                       definition: str) -> None:
        existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        # Existing local synthetic databases are retained.  Unscoped legacy rows remain
        # inaccessible to an owner rather than being guessed into a tenant chain.
        self._ensure_column(conn, "sandbox_claim", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sandbox_invoice", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sandbox_bank_transfer", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sandbox_request", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sandbox_operation_log", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sandbox_bank_account", "tenant_id", "tenant_id TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sandbox_bank_account", "purpose", "purpose TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sandbox_operation_tenant "
                     "ON sandbox_operation_log (tenant_id, seq)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sandbox_bank_account_tenant "
                     "ON sandbox_bank_account (tenant_id, id)")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            self._migrate_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO sandbox_clock (id, current_at) VALUES (1,?)",
                         (_DEFAULT_CLOCK,))
            conn.execute("INSERT OR IGNORE INTO sandbox_bank_account "
                         "(id, balance, tenant_id, purpose) VALUES (?,?,?,?)",
                         ("sandbox-insurer", "1000000.00", "", "INSURER"))
            conn.execute("UPDATE sandbox_bank_account SET purpose='INSURER' "
                         "WHERE id='sandbox-insurer' AND tenant_id=''")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _clock(self, conn: sqlite3.Connection) -> datetime:
        row = conn.execute("SELECT current_at FROM sandbox_clock WHERE id=1").fetchone()
        return datetime.fromisoformat(str(row["current_at"]))

    def _checkpoint_hmac(self, tenant_id: str, record_count: int, head_hmac: str) -> str:
        material = _canonical({
            "tenant_id": tenant_id, "record_count": int(record_count), "head_hmac": head_hmac,
        })
        return hmac.new(self._key, material.encode("utf-8"), hashlib.sha256).hexdigest()

    def _log(self, conn: sqlite3.Connection, operation: str, payload: Mapping[str, Any], *,
             tenant_id: str) -> dict[str, Any]:
        prior = conn.execute(
            "SELECT seq, record_json, hmac FROM sandbox_operation_log "
            "WHERE tenant_id=? ORDER BY seq DESC LIMIT 1", (tenant_id,)).fetchone()
        if prior is None:
            previous, tenant_seq = "", 1
        else:
            previous = str(prior["hmac"])
            try:
                tenant_seq = int(json.loads(str(prior["record_json"])).get("tenant_seq", 0)) + 1
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SandboxError("The prior synthetic operation record is malformed.") from exc
        anchor_count, anchor_head, anchor_valid = self._operation_anchor(tenant_id)
        if prior is None:
            if anchor_count is not None or not anchor_valid:
                raise SandboxError("The external synthetic-operation anchor is inconsistent.")
        elif (not anchor_valid or anchor_count != tenant_seq - 1
              or not hmac.compare_digest(str(anchor_head or ""), previous)):
            raise SandboxError("The external synthetic-operation anchor does not match the operation history.")
        latest = conn.execute("SELECT seq FROM sandbox_operation_log ORDER BY seq DESC LIMIT 1").fetchone()
        seq = int(latest["seq"]) + 1 if latest else 1
        record = {
            "seq": seq,
            "tenant_id": tenant_id,
            "tenant_seq": tenant_seq,
            "at": self._clock(conn).isoformat(timespec="seconds"),
            "operation": operation,
            "payload_sha256": hashlib.sha256(_canonical(dict(payload)).encode("utf-8")).hexdigest(),
            "synthetic": True,
        }
        material = _canonical({"record": record, "previous": previous})
        signature = hmac.new(self._key, material.encode("utf-8"), hashlib.sha256).hexdigest()
        conn.execute(
            "INSERT INTO sandbox_operation_log (seq, tenant_id, record_json, prev_hmac, hmac) "
            "VALUES (?,?,?,?,?)",
            (seq, tenant_id, _canonical(record), previous, signature),
        )
        checkpoint_hmac = self._checkpoint_hmac(tenant_id, tenant_seq, signature)
        conn.execute(
            "INSERT INTO sandbox_operation_checkpoint "
            "(tenant_id, record_count, head_hmac, checkpoint_hmac, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET record_count=excluded.record_count, "
            "head_hmac=excluded.head_hmac, checkpoint_hmac=excluded.checkpoint_hmac, "
            "updated_at=excluded.updated_at",
            (tenant_id, tenant_seq, signature, checkpoint_hmac, record["at"]),
        )
        signed = {**record, "prev_hmac": previous, "hmac": signature}
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(_canonical(signed) + "\n")
        # The signed head is intentionally persisted in a separate artifact.  Losing the
        # JSONL together with its same-database checkpoint cannot turn history into empty.
        self._write_operation_anchor(tenant_id, tenant_seq, previous, signature, record["at"])
        return signed

    def _jsonl_records(self, tenant_id: str) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
        if self.log_path is None:
            return None, "No signed JSONL operation artifact is configured."
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return [], None
        except OSError as exc:
            return None, f"The signed JSONL operation artifact cannot be read: {exc}."
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(lines, start=1):
            if not line.strip():
                return None, f"The signed JSONL operation artifact has an empty record at line {line_no}."
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                return None, f"The signed JSONL operation artifact is malformed at line {line_no}."
            if not isinstance(parsed, dict):
                return None, f"The signed JSONL operation artifact has a non-object record at line {line_no}."
            if parsed.get("tenant_id") == tenant_id:
                records.append(parsed)
        return records, None

    def _checkpoint(self, tenant_id: str) -> tuple[Optional[int], Optional[str], bool]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT record_count, head_hmac, checkpoint_hmac FROM sandbox_operation_checkpoint "
                "WHERE tenant_id=?", (tenant_id,)).fetchone()
        if row is None:
            return None, None, True
        try:
            count = int(row["record_count"])
        except (TypeError, ValueError):
            return None, None, False
        head_hmac = str(row["head_hmac"] or "")
        valid = hmac.compare_digest(
            str(row["checkpoint_hmac"] or ""), self._checkpoint_hmac(tenant_id, count, head_hmac))
        return count, head_hmac, valid

    def _operation_anchor_path(self) -> Path:
        """Store signed operation heads independently of the JSONL and sandbox database."""
        source = self.log_path if self.log_path is not None else self.db_path
        return source.with_name(f"{source.stem}.operation-anchor.sqlite")

    @contextmanager
    def _operation_anchor_connection(self) -> Iterator[sqlite3.Connection]:
        path = self._operation_anchor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sandbox_operation_anchor ("
                "tenant_id TEXT PRIMARY KEY, record_count INTEGER NOT NULL CHECK (record_count >= 0), "
                "head_hmac TEXT NOT NULL, anchor_hmac TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _operation_anchor_mac(self, tenant_id: str, record_count: int, head_hmac: str) -> str:
        material = _canonical({
            "purpose": "external-sandbox-operation-anchor-v1",
            "tenant_id": tenant_id,
            "record_count": int(record_count),
            "head_hmac": head_hmac,
        })
        return hmac.new(self._key, material.encode("utf-8"), hashlib.sha256).hexdigest()

    def _operation_anchor(self, tenant_id: str) -> tuple[Optional[int], Optional[str], bool]:
        with self._operation_anchor_connection() as conn:
            row = conn.execute(
                "SELECT record_count, head_hmac, anchor_hmac FROM sandbox_operation_anchor "
                "WHERE tenant_id=?", (tenant_id,)).fetchone()
        if row is None:
            return None, None, True
        try:
            count = int(row["record_count"])
        except (TypeError, ValueError):
            return None, None, False
        head_hmac = str(row["head_hmac"] or "")
        valid = count >= 0 and hmac.compare_digest(
            str(row["anchor_hmac"] or ""),
            self._operation_anchor_mac(tenant_id, count, head_hmac))
        return count, head_hmac, valid

    def _write_operation_anchor(self, tenant_id: str, record_count: int,
                                previous: str, head_hmac: str, updated_at: str) -> None:
        with self._operation_anchor_connection() as conn:
            row = conn.execute(
                "SELECT record_count, head_hmac, anchor_hmac FROM sandbox_operation_anchor "
                "WHERE tenant_id=?", (tenant_id,)).fetchone()
            if row is None:
                if record_count != 1 or previous:
                    raise SandboxError("The external synthetic-operation anchor is missing or inconsistent.")
            else:
                try:
                    anchored_count = int(row["record_count"])
                except (TypeError, ValueError) as exc:
                    raise SandboxError("The external synthetic-operation anchor is malformed.") from exc
                anchored_head = str(row["head_hmac"] or "")
                anchored_valid = hmac.compare_digest(
                    str(row["anchor_hmac"] or ""),
                    self._operation_anchor_mac(tenant_id, anchored_count, anchored_head))
                if (not anchored_valid or anchored_count != record_count - 1
                        or not hmac.compare_digest(anchored_head, previous)):
                    raise SandboxError("The external synthetic-operation anchor does not match its history.")
            signature = self._operation_anchor_mac(tenant_id, record_count, head_hmac)
            conn.execute(
                "INSERT INTO sandbox_operation_anchor "
                "(tenant_id, record_count, head_hmac, anchor_hmac, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET record_count=excluded.record_count, "
                "head_hmac=excluded.head_hmac, anchor_hmac=excluded.anchor_hmac, "
                "updated_at=excluded.updated_at",
                (tenant_id, record_count, head_hmac, signature, updated_at),
            )

    def operation_log(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return only one tenant's emitted JSONL records, never the shared SQLite chain."""
        tenant = _tenant_id(tenant_id)
        records, error = self._jsonl_records(tenant)
        return [] if error is not None or records is None else records

    def verify_operation_log(self, tenant_id: str) -> dict[str, Any]:
        """Verify the emitted JSONL and its separately protected tenant checkpoint."""
        tenant = _tenant_id(tenant_id)
        records, error = self._jsonl_records(tenant)
        if error is not None or records is None:
            return {"valid": False, "count": 0, "head_hmac": None,
                    "checkpoint_count": None, "checkpoint_head_hmac": None,
                    "first_invalid_seq": None, "reason": error}
        previous, expected_tenant_seq = "", 1
        for entry in records:
            try:
                record = {key: entry[key] for key in (
                    "seq", "tenant_id", "tenant_seq", "at", "operation", "payload_sha256", "synthetic")}
                tenant_seq = int(record["tenant_seq"])
            except (KeyError, TypeError, ValueError):
                return {"valid": False, "count": len(records), "head_hmac": previous or None,
                        "checkpoint_count": None, "checkpoint_head_hmac": None,
                        "first_invalid_seq": entry.get("seq"), "reason": "A JSONL record is incomplete."}
            expected = hmac.new(
                self._key, _canonical({"record": record, "previous": previous}).encode("utf-8"),
                hashlib.sha256).hexdigest()
            if (tenant_seq != expected_tenant_seq or entry.get("prev_hmac") != previous
                    or not hmac.compare_digest(str(entry.get("hmac") or ""), expected)):
                return {"valid": False, "count": len(records), "head_hmac": previous or None,
                        "checkpoint_count": None, "checkpoint_head_hmac": None,
                        "first_invalid_seq": entry.get("seq"), "reason": "The JSONL chain is invalid."}
            previous, expected_tenant_seq = str(entry["hmac"]), expected_tenant_seq + 1
        checkpoint_count, checkpoint_head, checkpoint_valid = self._checkpoint(tenant)
        if not checkpoint_valid:
            return {"valid": False, "count": len(records), "head_hmac": previous or None,
                    "checkpoint_count": checkpoint_count, "checkpoint_head_hmac": checkpoint_head,
                    "first_invalid_seq": None, "reason": "The protected JSONL checkpoint is invalid."}
        if checkpoint_count is None:
            if records:
                return {"valid": False, "count": len(records), "head_hmac": previous or None,
                        "checkpoint_count": None, "checkpoint_head_hmac": None,
                        "first_invalid_seq": None, "reason": "The protected JSONL checkpoint is missing."}
        elif checkpoint_count != len(records) or checkpoint_head != previous:
            return {"valid": False, "count": len(records), "head_hmac": previous or None,
                    "checkpoint_count": checkpoint_count, "checkpoint_head_hmac": checkpoint_head,
                    "first_invalid_seq": None,
                    "reason": "The JSONL artifact is incomplete or does not match its protected checkpoint."}
        anchor_count, anchor_head, anchor_valid = self._operation_anchor(tenant)
        if not anchor_valid:
            return {"valid": False, "count": len(records), "head_hmac": previous or None,
                    "checkpoint_count": checkpoint_count, "checkpoint_head_hmac": checkpoint_head,
                    "anchor_count": anchor_count, "anchor_head_hmac": anchor_head,
                    "first_invalid_seq": None, "reason": "The external synthetic-operation anchor is invalid."}
        if anchor_count is None:
            if records or checkpoint_count is not None:
                return {"valid": False, "count": len(records), "head_hmac": previous or None,
                        "checkpoint_count": checkpoint_count, "checkpoint_head_hmac": checkpoint_head,
                        "anchor_count": None, "anchor_head_hmac": None,
                        "first_invalid_seq": None,
                        "reason": "The external synthetic-operation anchor is missing."}
        elif anchor_count != len(records) or not hmac.compare_digest(str(anchor_head or ""), previous):
            return {"valid": False, "count": len(records), "head_hmac": previous or None,
                    "checkpoint_count": checkpoint_count, "checkpoint_head_hmac": checkpoint_head,
                    "anchor_count": anchor_count, "anchor_head_hmac": anchor_head,
                    "first_invalid_seq": None,
                    "reason": "The JSONL artifact does not match its external signed anchor."}
        return {"valid": True, "count": len(records), "head_hmac": previous or None,
                "checkpoint_count": checkpoint_count or 0, "checkpoint_head_hmac": checkpoint_head,
                "anchor_count": anchor_count or 0, "anchor_head_hmac": anchor_head,
                "first_invalid_seq": None, "reason": None}

    def clock(self) -> dict[str, Any]:
        with self._connection() as conn:
            return {"current_at": self._clock(conn).isoformat(timespec="seconds"), "synthetic": True}

    def advance_clock(self, days: int) -> dict[str, Any]:
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 365:
            raise SandboxError("Sandbox clock advance must be a whole number of days from 0 to 365.")
        with self._connection() as conn:
            current = self._clock(conn) + timedelta(days=days)
            conn.execute("UPDATE sandbox_clock SET current_at=? WHERE id=1",
                         (current.isoformat(timespec="seconds"),))
            self._log(conn, "CLOCK_ADVANCED", {"days": days}, tenant_id=_LOCAL_ADMIN_TENANT)
            return {"current_at": current.isoformat(timespec="seconds"), "synthetic": True}

    def _claim_out(self, row: sqlite3.Row) -> dict[str, Any]:
        settlement = json.loads(row["settlement_json"]) if row["settlement_json"] else None
        return {
            "claim_id": row["id"], "status": row["status"],
            "submitted_at": row["created_at"], "updated_at": row["updated_at"],
            "settlement": settlement, "synthetic": True,
        }

    def _signed_webhook(self, conn: sqlite3.Connection, event_type: str, claim_id: str, *,
                        tenant_id: str) -> dict[str, Any]:
        event = {"event_id": _id("SBX-EVT"), "event_type": str(event_type),
                 "claim_id": str(claim_id), "tenant_id": tenant_id,
                 "timestamp": self._clock(conn).isoformat(timespec="seconds"), "synthetic": True}
        signature = hmac.new(self._key, _canonical(event).encode("utf-8"), hashlib.sha256).hexdigest()
        self._log(conn, "WEBHOOK_EMITTED", {
            "event_id": event["event_id"], "event_type": event["event_type"], "claim_id": event["claim_id"],
        }, tenant_id=tenant_id)
        return {"event": event, "signature": signature}

    def scenarios(self) -> list[dict[str, Any]]:
        """Seeded counterparty stories, all local and deterministic."""
        return [
            {"id": "HEALTH_REIMBURSEMENT", "status": "ACKNOWLEDGED",
             "note": "Includes one synthetic deduction not found in the supplied wording."},
            {"id": "HEALTH_CASHLESS", "status": "PRE_AUTHORISED",
             "note": "Synthetic cashless pre-authorisation path."},
            {"id": "MOTOR_OWN_DAMAGE", "status": "SURVEYOR_REQUIRED",
             "note": "Synthetic motor own-damage path with a surveyor step."},
            {"id": "TRAVEL_BAGGAGE_DELAY", "status": "ACKNOWLEDGED",
             "note": "Synthetic travel baggage-delay path."},
        ]

    def submit_claim(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise SandboxError("A bounded idempotency key is required to submit a sandbox claim.")
        policy_id = str(payload.get("policy_id") or "").strip()
        tenant_id = _tenant_id(payload.get("tenant_id"))
        if not policy_id:
            raise SandboxError("A sandbox claim needs a policy id.")
        with self._connection() as conn:
            existing = conn.execute("SELECT * FROM sandbox_claim WHERE idempotency_key=?",
                                    (idempotency_key,)).fetchone()
            if existing is not None:
                if str(existing["tenant_id"]) != tenant_id:
                    raise SandboxConflict("A sandbox idempotency key is already bound to another tenant.")
                result = self._claim_out(existing)
                result["idempotent_replay"] = True
                result["webhook"] = self._signed_webhook(
                    conn, "CLAIM_ACKNOWLEDGED", str(existing["id"]), tenant_id=tenant_id)
                return result
            claim_id = _id("SBX-CLM")
            now = self._clock(conn).isoformat(timespec="seconds")
            requested = str(payload.get("claim_type") or "HEALTH_REIMBURSEMENT").upper()
            scenario = {"REIMBURSEMENT": "HEALTH_REIMBURSEMENT", "CASHLESS": "HEALTH_CASHLESS"}.get(requested, requested)
            statuses = {row["id"]: row["status"] for row in self.scenarios()}
            if scenario not in statuses:
                raise SandboxError("The requested synthetic claim scenario is not seeded.")
            settlement = ({
                "basis": "SYNTHETIC_SANDBOX_SCENARIO",
                "loss": "50000.00",
                "deductions": [
                    {"id": "co-payment", "amount": "2500.00", "status": "SUPPORTED_BY_SCENARIO",
                     "evidence_mechanism": "SHARE.CO_PAYMENT"},
                    {"id": "room-category", "amount": "5000.00", "status": "NOT_FOUND_IN_SCENARIO_WORDING",
                     "evidence_mechanism": "CONDITION.PROPORTIONATE_DEDUCTION",
                     "evidence_mode": "ABSENCE_FROM_DEFINED_SCOPE"},
                ],
                "proposed_payable": "42500.00",
                "currency": "INR",
            } if scenario == "HEALTH_REIMBURSEMENT" else {
                "basis": "SYNTHETIC_SANDBOX_SCENARIO", "scenario": scenario,
                "deductions": [], "proposed_payable": None,
            })
            conn.execute(
                "INSERT INTO sandbox_claim (id, idempotency_key, tenant_id, status, payload_json, settlement_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (claim_id, idempotency_key, tenant_id, statuses[scenario], _canonical(dict(payload)),
                 _canonical(settlement), now, now),
            )
            self._log(conn, "CLAIM_SUBMITTED", {"claim_id": claim_id, "policy_id": policy_id,
                                                 "scenario": scenario}, tenant_id=tenant_id)
            row = conn.execute("SELECT * FROM sandbox_claim WHERE id=?", (claim_id,)).fetchone()
            return {**self._claim_out(row), "webhook": self._signed_webhook(
                conn, "CLAIM_ACKNOWLEDGED", claim_id, tenant_id=tenant_id)}

    def _claim_row(self, conn: sqlite3.Connection, claim_id: str, *,
                   tenant_id: Optional[str] = None) -> sqlite3.Row:
        if tenant_id is None:
            row = conn.execute("SELECT * FROM sandbox_claim WHERE id=?", (claim_id,)).fetchone()
        else:
            tenant = _tenant_id(tenant_id)
            row = conn.execute(
                "SELECT * FROM sandbox_claim WHERE id=? AND tenant_id=?", (claim_id, tenant)).fetchone()
        if row is None:
            raise SandboxNotFound("No sandbox claim matches that id for this tenant.")
        return row

    def claim_status(self, claim_id: str, *, tenant_id: Optional[str] = None) -> dict[str, Any]:
        with self._connection() as conn:
            return self._claim_out(self._claim_row(conn, claim_id, tenant_id=tenant_id))

    def open_claim_query(self, claim_id: str, *, tenant_id: str) -> dict[str, Any]:
        """Sandbox-admin-only state change that emits a separately signed query event."""
        tenant = _tenant_id(tenant_id)
        with self._connection() as conn:
            row = self._claim_row(conn, claim_id, tenant_id=tenant)
            if row["status"] != "ACKNOWLEDGED":
                raise SandboxConflict("A synthetic acknowledged claim is required before opening its query.")
            now = self._clock(conn).isoformat(timespec="seconds")
            conn.execute("UPDATE sandbox_claim SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
                         ("QUERY_OPEN", now, claim_id, tenant))
            self._log(conn, "CLAIM_QUERY_OPEN", {"claim_id": claim_id}, tenant_id=tenant)
            return self._signed_webhook(conn, "CLAIM_QUERY_OPEN", claim_id, tenant_id=tenant)

    def reply_to_query(self, claim_id: str, payload: Mapping[str, Any], *,
                       idempotency_key: str, tenant_id: Optional[str] = None) -> dict[str, Any]:
        with self._connection() as conn:
            row = self._claim_row(conn, claim_id, tenant_id=tenant_id)
            if row["status"] != "QUERY_OPEN":
                raise SandboxConflict("A synthetic insurer query must be open before a reply is received.")
            now = self._clock(conn).isoformat(timespec="seconds")
            conn.execute("UPDATE sandbox_claim SET status=?, updated_at=? WHERE id=?",
                         ("UNDER_ASSESSMENT", now, claim_id))
            self._log(conn, "QUERY_REPLY_RECEIVED", {"claim_id": claim_id,
                                                       "idempotency_key": idempotency_key},
                      tenant_id=str(row["tenant_id"]))
            updated = self._claim_row(conn, claim_id, tenant_id=tenant_id)
            return self._claim_out(updated)

    def decide_claim(self, claim_id: str, *, tenant_id: str) -> dict[str, Any]:
        """Sandbox-admin-only decision event; VERIS receives it through the webhook gate."""
        tenant = _tenant_id(tenant_id)
        with self._connection() as conn:
            row = self._claim_row(conn, claim_id, tenant_id=tenant)
            if row["status"] != "UNDER_ASSESSMENT":
                raise SandboxConflict("A synthetic claim under assessment is required before a decision event.")
            now = self._clock(conn).isoformat(timespec="seconds")
            conn.execute("UPDATE sandbox_claim SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
                         ("DECIDED", now, claim_id, tenant))
            self._log(conn, "CLAIM_DECIDED", {"claim_id": claim_id}, tenant_id=tenant)
            return self._signed_webhook(conn, "CLAIM_DECIDED", claim_id, tenant_id=tenant)

    def _transfer(self, conn: sqlite3.Connection, *, tenant_id: str, from_account: str,
                  to_account: str, amount: str, reference: str, idempotency_key: str) -> dict[str, Any]:
        existing = conn.execute("SELECT * FROM sandbox_bank_transfer WHERE idempotency_key=?",
                                (idempotency_key,)).fetchone()
        if existing is not None:
            if str(existing["tenant_id"]) != tenant_id:
                raise SandboxConflict("A sandbox transfer idempotency key is already bound to another tenant.")
            return {"transfer_id": existing["id"], "amount": existing["amount"],
                    "reference": existing["reference"], "idempotent_replay": True}
        amount_text = _money(amount)
        source = conn.execute("SELECT balance FROM sandbox_bank_account WHERE id=?", (from_account,)).fetchone()
        target = conn.execute("SELECT balance FROM sandbox_bank_account WHERE id=?", (to_account,)).fetchone()
        if source is None or target is None:
            raise SandboxNotFound("A synthetic bank account is unavailable.")
        if Decimal(source["balance"]) < Decimal(amount_text):
            raise SandboxConflict("The synthetic bank account has insufficient balance.")
        transfer_id = _id("SBX-TXN")
        now = self._clock(conn).isoformat(timespec="seconds")
        conn.execute("UPDATE sandbox_bank_account SET balance=? WHERE id=?",
                     (_money(Decimal(source["balance"]) - Decimal(amount_text)), from_account))
        conn.execute("UPDATE sandbox_bank_account SET balance=? WHERE id=?",
                     (_money(Decimal(target["balance"]) + Decimal(amount_text)), to_account))
        conn.execute(
            "INSERT INTO sandbox_bank_transfer "
            "(id, idempotency_key, tenant_id, from_account, to_account, amount, reference, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (transfer_id, idempotency_key, tenant_id, from_account, to_account, amount_text, reference, now),
        )
        self._log(conn, "BANK_TRANSFER", {"transfer_id": transfer_id, "reference": reference,
                                            "amount": amount_text}, tenant_id=tenant_id)
        return {"transfer_id": transfer_id, "amount": amount_text, "reference": reference,
                "idempotent_replay": False}

    def dispute_settlement(self, claim_id: str, payload: Mapping[str, Any], *,
                           idempotency_key: str, tenant_id: Optional[str] = None) -> dict[str, Any]:
        with self._connection() as conn:
            row = self._claim_row(conn, claim_id, tenant_id=tenant_id)
            if row["status"] != "DECIDED":
                raise SandboxConflict("A synthetic decided claim is required before a settlement dispute.")
            tenant_id = _tenant_id(row["tenant_id"])
            settlement = json.loads(row["settlement_json"] or "{}")
            deducted = {str(item.get("id")): item for item in settlement.get("deductions", [])}
            unsupported = deducted.get("room-category")
            if not unsupported or unsupported.get("status") != "NOT_FOUND_IN_SCENARIO_WORDING":
                raise SandboxConflict("This synthetic scenario has no unresolved deduction to query.")
            corrected = "47500.00"
            beneficiary_account = self._ensure_tenant_beneficiary(conn, tenant_id)
            transfer = self._transfer(
                conn, tenant_id=tenant_id, from_account="sandbox-insurer", to_account=beneficiary_account,
                amount=corrected, reference=f"corrected-settlement:{claim_id}",
                idempotency_key=f"settlement:{idempotency_key}")
            settlement["corrected_payable"] = corrected
            settlement["correction_reason"] = "The synthetic scenario deduction was not found in its wording."
            settlement["bank_transfer_id"] = transfer["transfer_id"]
            now = self._clock(conn).isoformat(timespec="seconds")
            conn.execute("UPDATE sandbox_claim SET status=?, settlement_json=?, updated_at=? WHERE id=?",
                         ("SETTLED", _canonical(settlement), now, claim_id))
            self._log(conn, "SETTLEMENT_CORRECTED", {"claim_id": claim_id,
                                                       "transfer_id": transfer["transfer_id"]},
                      tenant_id=tenant_id)
            updated = self._claim_row(conn, claim_id, tenant_id=tenant_id)
            return {**self._claim_out(updated), "transfer": transfer, "webhook": self._signed_webhook(
                conn, "SETTLEMENT_CORRECTED", claim_id, tenant_id=tenant_id)}

    @staticmethod
    def _invoice_out(row: sqlite3.Row) -> dict[str, Any]:
        return {"invoice_id": row["id"], "tenant_id": row["tenant_id"],
                "payee": row["payee"], "amount": row["amount"], "status": row["status"],
                "synthetic": True}

    @staticmethod
    def _payer_account_id(tenant_id: str) -> str:
        return f"sandbox-payer:{_tenant_id(tenant_id)}"

    @staticmethod
    def _beneficiary_account_id(tenant_id: str) -> str:
        return beneficiary_account_id(tenant_id)

    def _ensure_tenant_account(self, conn: sqlite3.Connection, *, tenant_id: str,
                               account_id: str, initial_balance: str, purpose: str) -> str:
        tenant = _tenant_id(tenant_id)
        conn.execute(
            "INSERT OR IGNORE INTO sandbox_bank_account (id, balance, tenant_id, purpose) "
            "VALUES (?,?,?,?)", (account_id, initial_balance, tenant, purpose))
        account = conn.execute(
            "SELECT tenant_id, purpose FROM sandbox_bank_account WHERE id=?", (account_id,)).fetchone()
        if (account is None or not hmac.compare_digest(str(account["tenant_id"]), tenant)
                or not hmac.compare_digest(str(account["purpose"]), purpose)):
            raise SandboxError("The synthetic bank account ownership is inconsistent.")
        return account_id

    def _ensure_tenant_payer(self, conn: sqlite3.Connection, tenant_id: str) -> str:
        # This is deterministic local seed funding, never a real balance or payment source.
        return self._ensure_tenant_account(
            conn, tenant_id=tenant_id, account_id=self._payer_account_id(tenant_id),
            initial_balance="5000.00", purpose="PAYER")

    def _ensure_tenant_beneficiary(self, conn: sqlite3.Connection, tenant_id: str) -> str:
        # Corrected synthetic settlements land only in this tenant's local beneficiary account.
        return self._ensure_tenant_account(
            conn, tenant_id=tenant_id, account_id=self._beneficiary_account_id(tenant_id),
            initial_balance="0.00", purpose="BENEFICIARY")

    def issue_invoice(self, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        tenant_id = _tenant_id(payload.get("tenant_id"))
        payee = str(payload.get("payee") or "Lotus General (Sandbox)").strip()
        amount = _money(payload.get("amount") or "1200.00")
        with self._connection() as conn:
            existing = conn.execute("SELECT * FROM sandbox_invoice WHERE idempotency_key=?",
                                    (idempotency_key,)).fetchone()
            if existing is not None:
                if str(existing["tenant_id"]) != tenant_id:
                    raise SandboxConflict("A sandbox invoice idempotency key is already bound to another tenant.")
                return {**self._invoice_out(existing), "idempotent_replay": True}
            self._ensure_tenant_payer(conn, tenant_id)
            invoice_id, now = _id("SBX-INV"), self._clock(conn).isoformat(timespec="seconds")
            conn.execute("INSERT INTO sandbox_invoice (id, idempotency_key, tenant_id, payee, amount, status, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (invoice_id, idempotency_key, tenant_id, payee, amount, "OPEN", now, now))
            self._log(conn, "INVOICE_ISSUED", {"invoice_id": invoice_id, "payee": payee,
                                                 "amount": amount}, tenant_id=tenant_id)
            row = conn.execute("SELECT * FROM sandbox_invoice WHERE id=?", (invoice_id,)).fetchone()
            return {**self._invoice_out(row), "idempotent_replay": False}

    def get_invoice(self, invoice_id: str, *, tenant_id: str) -> dict[str, Any]:
        tenant = _tenant_id(tenant_id)
        with self._connection() as conn:
            invoice = conn.execute(
                "SELECT * FROM sandbox_invoice WHERE id=? AND tenant_id=?", (invoice_id, tenant)).fetchone()
            if invoice is None:
                raise SandboxNotFound("No sandbox invoice matches that id for this tenant.")
            return self._invoice_out(invoice)

    def pay_invoice(self, invoice_id: str, *, tenant_id: str, expected_payee: str,
                    expected_amount: str, idempotency_key: str) -> dict[str, Any]:
        tenant = _tenant_id(tenant_id)
        expected_money = _money(expected_amount)
        with self._connection() as conn:
            invoice = conn.execute(
                "SELECT * FROM sandbox_invoice WHERE id=? AND tenant_id=?", (invoice_id, tenant)).fetchone()
            if invoice is None:
                raise SandboxNotFound("No sandbox invoice matches that id for this tenant.")
            if (not hmac.compare_digest(str(invoice["payee"]), str(expected_payee))
                    or not hmac.compare_digest(str(invoice["amount"]), expected_money)):
                raise SandboxConflict("The resolved sandbox invoice no longer matches the confirmed invoice snapshot.")
            if invoice["status"] == "PAID":
                return {"invoice_id": invoice_id, "status": "PAID", "idempotent_replay": True,
                        "synthetic": True}
            payer_account = self._ensure_tenant_payer(conn, tenant)
            transfer = self._transfer(
                conn, tenant_id=tenant, from_account=payer_account, to_account="sandbox-insurer",
                amount=str(invoice["amount"]), reference=f"invoice:{invoice_id}",
                idempotency_key=f"invoice:{idempotency_key}")
            now = self._clock(conn).isoformat(timespec="seconds")
            conn.execute("UPDATE sandbox_invoice SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
                         ("PAID", now, invoice_id, tenant))
            self._log(conn, "INVOICE_PAID", {"invoice_id": invoice_id,
                                               "transfer_id": transfer["transfer_id"],
                                               "payer_account": payer_account}, tenant_id=tenant)
            return {"invoice_id": invoice_id, "status": "PAID", "transfer": transfer,
                    "payer_account": payer_account, "synthetic": True}

    def request(self, kind: str, payload: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        if kind not in {"ENDORSEMENT_REQUEST", "GRIEVANCE"}:
            raise SandboxError("Unsupported synthetic counterparty request.")
        tenant_id = _tenant_id(payload.get("tenant_id"))
        with self._connection() as conn:
            existing = conn.execute("SELECT * FROM sandbox_request WHERE idempotency_key=?",
                                    (idempotency_key,)).fetchone()
            if existing is not None:
                if str(existing["tenant_id"]) != tenant_id:
                    raise SandboxConflict("A sandbox request idempotency key is already bound to another tenant.")
                return {"request_id": existing["id"], "kind": existing["kind"],
                        "idempotent_replay": True, "synthetic": True}
            request_id, now = _id("SBX-REQ"), self._clock(conn).isoformat(timespec="seconds")
            conn.execute("INSERT INTO sandbox_request (id, kind, idempotency_key, tenant_id, payload_json, created_at) "
                         "VALUES (?,?,?,?,?,?)", (request_id, kind, idempotency_key, tenant_id,
                                                    _canonical(dict(payload)), now))
            self._log(conn, kind, {"request_id": request_id}, tenant_id=tenant_id)
            return {"request_id": request_id, "kind": kind, "idempotent_replay": False,
                    "synthetic": True}

    def bank_statement(self, account_id: str, *, tenant_id: str) -> dict[str, Any]:
        """Return only an authenticated tenant's own synthetic bank account."""
        tenant = _tenant_id(tenant_id)
        requested = str(account_id or "").strip()
        with self._connection() as conn:
            account = conn.execute(
                "SELECT balance, purpose FROM sandbox_bank_account WHERE id=? AND tenant_id=?",
                (requested, tenant)).fetchone()
            if account is None:
                # The same response covers unknown and foreign ids, so account existence is not disclosed.
                raise SandboxNotFound("No tenant-owned synthetic bank account matches that id.")
            rows = conn.execute(
                "SELECT * FROM sandbox_bank_transfer WHERE tenant_id=? "
                "AND (from_account=? OR to_account=?) ORDER BY created_at, id",
                (tenant, requested, requested)).fetchall()
            return {
                "account_id": requested, "balance": account["balance"],
                "purpose": account["purpose"], "synthetic": True,
                "transfers": [{"id": row["id"], "from_account": row["from_account"],
                               "to_account": row["to_account"], "amount": row["amount"],
                               "reference": row["reference"], "created_at": row["created_at"]}
                              for row in rows],
            }

    def bank_transfer_receipt(self, transfer_id: str, *, tenant_id: str) -> dict[str, Any]:
        """Verify an incoming, tenant-owned synthetic settlement transfer by its receipt."""
        tenant = _tenant_id(tenant_id)
        with self._connection() as conn:
            transfer = conn.execute(
                "SELECT * FROM sandbox_bank_transfer WHERE id=? AND tenant_id=?",
                (str(transfer_id or ""), tenant)).fetchone()
            if transfer is None:
                raise SandboxNotFound("No tenant-bound synthetic bank transfer matches that id.")
            beneficiary = conn.execute(
                "SELECT id FROM sandbox_bank_account WHERE id=? AND tenant_id=? AND purpose='BENEFICIARY'",
                (transfer["to_account"], tenant)).fetchone()
            if beneficiary is None:
                raise SandboxNotFound("No tenant-bound incoming synthetic transfer receipt matches that id.")
            return {
                "transfer_id": transfer["id"], "to_account": transfer["to_account"],
                "amount": transfer["amount"], "reference": transfer["reference"],
                "created_at": transfer["created_at"], "synthetic": True,
            }

    def sign_webhook(self, event_type: str, claim_id: str, *, tenant_id: str = "") -> dict[str, Any]:
        tenant = _tenant_id(tenant_id)
        with self._connection() as conn:
            return self._signed_webhook(conn, event_type, claim_id, tenant_id=tenant)

    def verify_webhook(self, event: Mapping[str, Any], signature: str, *,
                       max_age_seconds: int = 300) -> bool:
        expected = hmac.new(self._key, _canonical(dict(event)).encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(signature)):
            return False
        try:
            received = datetime.fromisoformat(str(event["timestamp"]))
        except (KeyError, ValueError):
            return False
        with self._connection() as conn:
            now = self._clock(conn)
        return abs((now - received).total_seconds()) <= max_age_seconds


class SandboxConnector:
    """In-process service adapter retained for isolated sandbox unit tests only.

    Production VERIS uses :mod:`proven.sandbox_transport` and never instantiates this
    adapter, opens the sandbox database, or reads its signing key.
    """

    def __init__(self, service: SandboxService, *, retries: int = 2):
        self.service = service
        self.retries = retries
        self._failures = 0
        self._open_until: Optional[datetime] = None

    def _call(self, func, *args, **kwargs):
        now = datetime.now(timezone.utc)
        if self._open_until is not None and now < self._open_until:
            raise SandboxUnavailable("The synthetic counterparty circuit is open; retry later.")
        last: Optional[Exception] = None
        for _attempt in range(self.retries + 1):
            try:
                value = func(*args, **kwargs)
                self._failures, self._open_until = 0, None
                return value
            except SandboxError:
                raise
            except Exception as exc:  # transport-shaped transient failure only
                last = exc
                self._failures += 1
        self._open_until = now + timedelta(seconds=30)
        raise SandboxUnavailable("The synthetic counterparty did not respond after retries.") from last

    def submit_claim(self, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._call(self.service.submit_claim, payload, idempotency_key=idempotency_key)

    def claim_status(self, claim_id: str) -> dict[str, Any]:
        return self._call(self.service.claim_status, claim_id)

    def reply_to_query(self, claim_id: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._call(self.service.reply_to_query, claim_id, payload, idempotency_key=idempotency_key)

    def dispute_settlement(self, claim_id: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._call(self.service.dispute_settlement, claim_id, payload, idempotency_key=idempotency_key)

    def issue_invoice(self, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._call(self.service.issue_invoice, payload, idempotency_key=idempotency_key)

    def pay_invoice(self, invoice_id: str, idempotency_key: str) -> dict[str, Any]:
        return self._call(self.service.pay_invoice, invoice_id, idempotency_key=idempotency_key)

    def request(self, kind: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._call(self.service.request, kind, payload, idempotency_key=idempotency_key)
