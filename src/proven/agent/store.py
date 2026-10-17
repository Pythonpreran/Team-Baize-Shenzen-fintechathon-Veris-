"""Encrypted, tenant-scoped state for the VERIS agent runtime.

The portfolio repository owns policy documents and household facts.  This store owns only
agent control records: consent scopes, mandates, exact-payload confirmations, TOTP seeds,
webhook replay ids and the append-only agent ledger.  It deliberately has no finding,
coverage-decision, premium or outcome columns.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

import pyotp
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import persistence


class AgentStoreError(RuntimeError):
    """A safe failure from the agent-control store."""

    http_status = 400


class AgentStoreConfigurationError(AgentStoreError):
    http_status = 503


class AgentStoreNotFound(AgentStoreError):
    http_status = 404


class AgentStoreConflict(AgentStoreError):
    http_status = 409


class AgentStoreValidationError(AgentStoreError):
    http_status = 422


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_consent (
  tenant_id TEXT PRIMARY KEY,
  scopes_encrypted BLOB NOT NULL,
  granted_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_mandate (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  details_encrypted BLOB NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_mandate_tenant
  ON agent_mandate (tenant_id, created_at);

CREATE TABLE IF NOT EXISTS agent_pending_action (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload_encrypted BLOB NOT NULL,
  summary_encrypted BLOB NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_pending_tenant
  ON agent_pending_action (tenant_id, created_at);

CREATE TABLE IF NOT EXISTS agent_ledger (
  tenant_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  id TEXT NOT NULL,
  kind TEXT NOT NULL,
  record_encrypted BLOB NOT NULL,
  prev_hmac TEXT NOT NULL,
  hmac TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, seq),
  UNIQUE (id)
);

CREATE TABLE IF NOT EXISTS agent_totp (
  tenant_id TEXT PRIMARY KEY,
  secret_encrypted BLOB NOT NULL,
  enrolled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_webhook_replay (
  tenant_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, event_id)
);
"""

_NONCE_BYTES = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Mapping[str, Any] | Sequence[Any] | str | int | float | bool | None) -> str:
    """A stable, JSON-only representation used for hashes and authenticated records."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _new_id() -> str:
    return uuid.uuid4().hex


def agent_db_path(portfolio_path: Path) -> Path:
    """Keep agent controls beside, but separate from, encrypted portfolio records."""
    return portfolio_path.with_name(f"{portfolio_path.stem}.agent.sqlite")


def store_from_environment() -> "AgentStore":
    path = persistence.portfolio_db_path()
    if path is None:
        raise AgentStoreConfigurationError(
            f"Set {persistence.PORTFOLIO_DB_ENV} before using the agent runtime.")
    key, _source = persistence.master_key_for(path)
    return AgentStore(agent_db_path(path), key)


class AgentStore:
    """SQLite agent controls with a fresh connection for every operation.

    The key is derived from the portfolio master key but domain-separated, so an agent
    record cannot be decrypted with the portfolio key material directly.  Ledger data is
    encrypted at rest and also HMAC chained; those are different guarantees and both are
    needed for an auditable local demonstration.
    """

    def __init__(self, db_path: str | Path, master_key: bytes):
        self.db_path = Path(db_path).expanduser()
        if len(master_key) != 32:
            raise AgentStoreConfigurationError("The agent store requires a 32-byte master key.")
        self._encryption_key = hashlib.sha256(
            b"veris-agent-encryption-v1\x00" + master_key).digest()
        self._ledger_key = hashlib.sha256(
            b"veris-agent-ledger-v1\x00" + master_key).digest()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _seal(self, tenant_id: str, purpose: str, value: Mapping[str, Any]) -> bytes:
        nonce = __import__("os").urandom(_NONCE_BYTES)
        aad = f"agent:{tenant_id}:{purpose}".encode("utf-8")
        return nonce + AESGCM(self._encryption_key).encrypt(
            nonce, _canonical(value).encode("utf-8"), aad)

    def _open(self, tenant_id: str, purpose: str, blob: bytes) -> dict[str, Any]:
        if len(blob) <= _NONCE_BYTES:
            raise AgentStoreError("The encrypted agent record is incomplete.")
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            raw = AESGCM(self._encryption_key).decrypt(
                nonce, ciphertext, f"agent:{tenant_id}:{purpose}".encode("utf-8"))
            parsed = json.loads(raw.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentStoreError("The encrypted agent record cannot be authenticated.") from exc
        if not isinstance(parsed, dict):
            raise AgentStoreError("The encrypted agent record is malformed.")
        return parsed

    @staticmethod
    def _scopes(scopes: Sequence[str]) -> list[str]:
        values = sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
        if not values:
            raise AgentStoreValidationError("At least one agent consent scope is required.")
        if any(len(scope) > 120 for scope in values):
            raise AgentStoreValidationError("An agent consent scope is too long.")
        return values

    def grant_consent(self, tenant_id: str, scopes: Sequence[str]) -> dict[str, Any]:
        values = self._scopes(scopes)
        now = _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO agent_consent (tenant_id, scopes_encrypted, granted_at, revoked_at) "
                "VALUES (?,?,?,NULL) ON CONFLICT(tenant_id) DO UPDATE SET "
                "scopes_encrypted=excluded.scopes_encrypted, granted_at=excluded.granted_at, "
                "revoked_at=NULL",
                (tenant_id, self._seal(tenant_id, "consent", {"scopes": values}), now),
            )
        return {"granted": True, "scopes": values, "granted_at": now}

    def revoke_consent(self, tenant_id: str) -> dict[str, Any]:
        now = _now()
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE agent_consent SET revoked_at=? WHERE tenant_id=? AND revoked_at IS NULL",
                (now, tenant_id)).rowcount
        return {"revoked": bool(changed), "revoked_at": now if changed else None}

    def consent(self, tenant_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT scopes_encrypted, granted_at, revoked_at FROM agent_consent WHERE tenant_id=?",
                (tenant_id,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                return {"granted": False, "scopes": [], "granted_at": None}
            details = self._open(tenant_id, "consent", bytes(row["scopes_encrypted"]))
        scopes = details.get("scopes")
        return {
            "granted": True,
            "scopes": [str(item) for item in scopes] if isinstance(scopes, list) else [],
            "granted_at": row["granted_at"],
        }

    def has_consent(self, tenant_id: str, required_scope: str) -> bool:
        record = self.consent(tenant_id)
        return record["granted"] and (
            "agent:*" in record["scopes"] or required_scope in record["scopes"])

    @staticmethod
    def _money(value: str) -> str:
        try:
            money = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise AgentStoreValidationError("A mandate cap must be a Decimal string.") from exc
        if not money.is_finite() or money < 0:
            raise AgentStoreValidationError("A mandate cap must be a non-negative finite amount.")
        return format(money, "f")

    def create_mandate(self, tenant_id: str, *, scopes: Sequence[str], per_payment_cap: str,
                       payee_allowlist: Sequence[str], valid_from: str, valid_until: str,
                       advance_notice_required: bool) -> dict[str, Any]:
        mandate_scopes = self._scopes(scopes)
        payees = sorted({str(payee).strip() for payee in payee_allowlist if str(payee).strip()})
        if not payees:
            raise AgentStoreValidationError("A mandate needs at least one allowed payee.")
        try:
            start, end = date.fromisoformat(valid_from), date.fromisoformat(valid_until)
        except ValueError as exc:
            raise AgentStoreValidationError("Mandate dates must be ISO calendar dates.") from exc
        if end < start:
            raise AgentStoreValidationError("A mandate cannot end before it starts.")
        mandate_id, now = _new_id(), _now()
        details = {
            "scopes": mandate_scopes,
            "per_payment_cap": self._money(per_payment_cap),
            "payee_allowlist": payees,
            "valid_from": start.isoformat(),
            "valid_until": end.isoformat(),
            "advance_notice_required": bool(advance_notice_required),
        }
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO agent_mandate (id, tenant_id, details_encrypted, created_at, revoked_at) "
                "VALUES (?,?,?,?,NULL)",
                (mandate_id, tenant_id, self._seal(tenant_id, f"mandate:{mandate_id}", details), now),
            )
        return {"id": mandate_id, **details, "created_at": now, "revoked_at": None}

    def list_mandates(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, details_encrypted, created_at, revoked_at FROM agent_mandate "
                "WHERE tenant_id=? ORDER BY created_at, id", (tenant_id,)).fetchall()
            return [
                {"id": row["id"],
                 **self._open(tenant_id, f"mandate:{row['id']}", bytes(row["details_encrypted"])),
                 "created_at": row["created_at"], "revoked_at": row["revoked_at"]}
                for row in rows
            ]

    def revoke_mandate(self, tenant_id: str, mandate_id: str) -> dict[str, Any]:
        now = _now()
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE agent_mandate SET revoked_at=? WHERE id=? AND tenant_id=? AND revoked_at IS NULL",
                (now, mandate_id, tenant_id)).rowcount
        if not changed:
            raise AgentStoreNotFound("No active mandate matches that request.")
        return {"id": mandate_id, "revoked_at": now}

    def active_mandate(self, tenant_id: str, *, scope: str, payee: str,
                       amount: str, notice_at: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Return one live mandate only when every stored control authorises this action.

        A pending exact-payload confirmation is the visible advance notice in this local
        demo.  Mandates that require that notice are never selected until its recorded
        timestamp is valid and no later than the attempted action.
        """
        now = datetime.now(timezone.utc)
        try:
            requested = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            return None
        for mandate in self.list_mandates(tenant_id):
            if mandate["revoked_at"] is not None:
                continue
            try:
                valid_from = date.fromisoformat(str(mandate["valid_from"]))
                valid_until = date.fromisoformat(str(mandate["valid_until"]))
            except ValueError:
                continue
            if not (valid_from <= now.date() <= valid_until):
                continue
            if scope not in mandate["scopes"] and "agent:*" not in mandate["scopes"]:
                continue
            if payee not in mandate["payee_allowlist"]:
                continue
            if requested > Decimal(mandate["per_payment_cap"]):
                continue
            if mandate["advance_notice_required"]:
                try:
                    recorded_notice = datetime.fromisoformat(str(notice_at or ""))
                except ValueError:
                    continue
                if recorded_notice.tzinfo is None or recorded_notice > now:
                    continue
            return mandate
        return None

    def create_pending_action(self, tenant_id: str, *, tool_name: str,
                              payload: Mapping[str, Any], summary: str,
                              ttl_seconds: int = 300) -> dict[str, Any]:
        if ttl_seconds < 1 or ttl_seconds > 3_600:
            raise AgentStoreValidationError("A confirmation expiry must be between 1 and 3600 seconds.")
        action_id, now = _new_id(), datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        payload_dict = dict(payload)
        payload_hash = hashlib.sha256(_canonical(payload_dict).encode("utf-8")).hexdigest()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO agent_pending_action "
                "(id, tenant_id, tool_name, payload_hash, payload_encrypted, summary_encrypted, "
                "expires_at, created_at, consumed_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
                (action_id, tenant_id, tool_name, payload_hash,
                 self._seal(tenant_id, f"pending-payload:{action_id}", payload_dict),
                 self._seal(tenant_id, f"pending-summary:{action_id}", {"summary": summary}),
                 expires.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
            )
        return {
            "id": action_id, "tool_name": tool_name, "payload_hash": payload_hash,
            "summary": summary, "expires_at": expires.isoformat(timespec="seconds"),
            "created_at": now.isoformat(timespec="seconds"), "consumed_at": None,
        }

    def list_pending_actions(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_pending_action WHERE tenant_id=? AND consumed_at IS NULL "
                "ORDER BY created_at, id", (tenant_id,)).fetchall()
            out = []
            for row in rows:
                action_id = str(row["id"])
                payload = self._open(tenant_id, f"pending-payload:{action_id}", bytes(row["payload_encrypted"]))
                summary = self._open(tenant_id, f"pending-summary:{action_id}", bytes(row["summary_encrypted"]))
                out.append({
                    "id": action_id, "tool_name": row["tool_name"], "payload_hash": row["payload_hash"],
                    "payload": payload, "summary": str(summary.get("summary") or ""),
                    "expires_at": row["expires_at"], "created_at": row["created_at"],
                })
            return out

    def pending_action(self, tenant_id: str, action_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_pending_action WHERE id=? AND tenant_id=?",
                (action_id, tenant_id)).fetchone()
            if row is None:
                raise AgentStoreNotFound("No pending action matches that request.")
            payload = self._open(tenant_id, f"pending-payload:{action_id}", bytes(row["payload_encrypted"]))
            summary = self._open(tenant_id, f"pending-summary:{action_id}", bytes(row["summary_encrypted"]))
        return {
            "id": row["id"], "tool_name": row["tool_name"], "payload_hash": row["payload_hash"],
            "payload": payload, "summary": str(summary.get("summary") or ""),
            "expires_at": row["expires_at"], "created_at": row["created_at"],
            "consumed_at": row["consumed_at"],
        }

    def withdraw_pending_actions(self, tenant_id: str, *, claim_ids: Sequence[str] = (),
                                 policy_ids: Sequence[str] = ()) -> int:
        """Close unconfirmed actions that name a claim or policy the demo reset removed.

        The row is marked consumed rather than deleted, so a later confirmation attempt is
        refused as already used and the confirmation history stays countable. Only this
        tenant's rows are read, and only an exact claim or policy id match is withdrawn.
        """
        claims, policies = {str(v) for v in claim_ids if str(v)}, {str(v) for v in policy_ids if str(v)}
        if not claims and not policies:
            return 0
        withdrawn = 0
        now = _now()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, payload_encrypted FROM agent_pending_action "
                "WHERE tenant_id=? AND consumed_at IS NULL", (tenant_id,)).fetchall()
            for row in rows:
                payload = self._open(tenant_id, f"pending-payload:{row['id']}", bytes(row["payload_encrypted"]))
                if (str(payload.get("claim_id") or "") in claims
                        or str(payload.get("policy_id") or "") in policies):
                    withdrawn += conn.execute(
                        "UPDATE agent_pending_action SET consumed_at=? WHERE id=? AND tenant_id=? "
                        "AND consumed_at IS NULL", (now, row["id"], tenant_id)).rowcount
        return withdrawn

    def consume_pending_action(self, tenant_id: str, action_id: str, payload_hash: str) -> dict[str, Any]:
        current = self.pending_action(tenant_id, action_id)
        if current["consumed_at"] is not None:
            raise AgentStoreConflict("This confirmation was already used.")
        if not hmac.compare_digest(str(payload_hash), str(current["payload_hash"])):
            raise AgentStoreConflict("The confirmation does not match the exact action payload.")
        try:
            expired = datetime.fromisoformat(current["expires_at"]) <= datetime.now(timezone.utc)
        except ValueError:
            expired = True
        if expired:
            raise AgentStoreConflict("This confirmation has expired; prepare the action again.")
        now = _now()
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE agent_pending_action SET consumed_at=? WHERE id=? AND tenant_id=? "
                "AND consumed_at IS NULL", (now, action_id, tenant_id)).rowcount
        if not changed:
            raise AgentStoreConflict("This confirmation was already used.")
        current["consumed_at"] = now
        return current

    def _ledger_mac(self, tenant_id: str, seq: int, previous: str,
                    record: Mapping[str, Any]) -> str:
        material = _canonical({
            "tenant_id": tenant_id,
            "seq": seq,
            "previous": previous,
            "record_sha256": hashlib.sha256(_canonical(dict(record)).encode("utf-8")).hexdigest(),
        })
        return hmac.new(self._ledger_key, material.encode("utf-8"), hashlib.sha256).hexdigest()

    def _ledger_checkpoint_mac(self, tenant_id: str, record_count: int, head_hmac: str) -> str:
        material = _canonical({
            "tenant_id": tenant_id,
            "record_count": int(record_count),
            "head_hmac": head_hmac,
        })
        return hmac.new(self._ledger_key, material.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _ensure_ledger_checkpoint_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_ledger_checkpoint ("
            "tenant_id TEXT PRIMARY KEY, record_count INTEGER NOT NULL CHECK (record_count >= 0), "
            "head_hmac TEXT NOT NULL, checkpoint_hmac TEXT NOT NULL, updated_at TEXT NOT NULL)")

    def _ledger_checkpoint_from_connection(self, conn: sqlite3.Connection,
                                           tenant_id: str) -> tuple[Optional[int], Optional[str], bool]:
        self._ensure_ledger_checkpoint_table(conn)
        row = conn.execute(
            "SELECT record_count, head_hmac, checkpoint_hmac FROM agent_ledger_checkpoint "
            "WHERE tenant_id=?", (tenant_id,)).fetchone()
        if row is None:
            return None, None, True
        try:
            count = int(row["record_count"])
        except (TypeError, ValueError):
            return None, None, False
        head_hmac = str(row["head_hmac"] or "")
        expected = self._ledger_checkpoint_mac(tenant_id, count, head_hmac)
        valid = count >= 0 and hmac.compare_digest(str(row["checkpoint_hmac"] or ""), expected)
        return count, head_hmac, valid

    def _ledger_checkpoint(self, tenant_id: str) -> tuple[Optional[int], Optional[str], bool]:
        with self._connection() as conn:
            return self._ledger_checkpoint_from_connection(conn, tenant_id)

    def _ledger_anchor_path(self) -> Path:
        """Keep signed ledger heads outside the mutable ledger database."""
        return self.db_path.with_name(f"{self.db_path.stem}.ledger-anchor.sqlite")

    @contextmanager
    def _ledger_anchor_connection(self) -> Iterator[sqlite3.Connection]:
        path = self._ledger_anchor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS agent_ledger_anchor ("
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

    def _ledger_anchor_mac(self, tenant_id: str, record_count: int, head_hmac: str) -> str:
        material = _canonical({
            "purpose": "external-agent-ledger-anchor-v1",
            "tenant_id": tenant_id,
            "record_count": int(record_count),
            "head_hmac": head_hmac,
        })
        return hmac.new(self._ledger_key, material.encode("utf-8"), hashlib.sha256).hexdigest()

    def _ledger_anchor(self, tenant_id: str) -> tuple[Optional[int], Optional[str], bool]:
        with self._ledger_anchor_connection() as conn:
            row = conn.execute(
                "SELECT record_count, head_hmac, anchor_hmac FROM agent_ledger_anchor "
                "WHERE tenant_id=?", (tenant_id,)).fetchone()
        if row is None:
            return None, None, True
        try:
            count = int(row["record_count"])
        except (TypeError, ValueError):
            return None, None, False
        head_hmac = str(row["head_hmac"] or "")
        expected = self._ledger_anchor_mac(tenant_id, count, head_hmac)
        valid = count >= 0 and hmac.compare_digest(str(row["anchor_hmac"] or ""), expected)
        return count, head_hmac, valid

    def _write_ledger_anchor(self, tenant_id: str, record_count: int,
                             previous: str, head_hmac: str, updated_at: str) -> None:
        with self._ledger_anchor_connection() as conn:
            row = conn.execute(
                "SELECT record_count, head_hmac, anchor_hmac FROM agent_ledger_anchor "
                "WHERE tenant_id=?", (tenant_id,)).fetchone()
            if row is None:
                if record_count != 1 or previous:
                    raise AgentStoreError("The external agent-ledger anchor is missing or inconsistent.")
            else:
                try:
                    anchored_count = int(row["record_count"])
                except (TypeError, ValueError) as exc:
                    raise AgentStoreError("The external agent-ledger anchor is malformed.") from exc
                anchored_head = str(row["head_hmac"] or "")
                anchored_valid = hmac.compare_digest(
                    str(row["anchor_hmac"] or ""),
                    self._ledger_anchor_mac(tenant_id, anchored_count, anchored_head))
                if (not anchored_valid or anchored_count != record_count - 1
                        or not hmac.compare_digest(anchored_head, previous)):
                    raise AgentStoreError("The external agent-ledger anchor does not match its history.")
            signature = self._ledger_anchor_mac(tenant_id, record_count, head_hmac)
            conn.execute(
                "INSERT INTO agent_ledger_anchor "
                "(tenant_id, record_count, head_hmac, anchor_hmac, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET record_count=excluded.record_count, "
                "head_hmac=excluded.head_hmac, anchor_hmac=excluded.anchor_hmac, "
                "updated_at=excluded.updated_at",
                (tenant_id, record_count, head_hmac, signature, updated_at),
            )

    def append_ledger(self, tenant_id: str, *, kind: str, actor: str,
                      detail: Mapping[str, Any]) -> dict[str, Any]:
        record_id, now = _new_id(), _now()
        record = {"id": record_id, "kind": str(kind), "actor": str(actor),
                  "detail": dict(detail), "created_at": now}
        with self._connection() as conn:
            checkpoint_count, checkpoint_head, checkpoint_valid = \
                self._ledger_checkpoint_from_connection(conn, tenant_id)
            anchor_count, anchor_head, anchor_valid = self._ledger_anchor(tenant_id)
            prior = conn.execute(
                "SELECT seq, hmac FROM agent_ledger WHERE tenant_id=? ORDER BY seq DESC LIMIT 1",
                (tenant_id,)).fetchone()
            if prior is None:
                if (checkpoint_count is not None or not checkpoint_valid
                        or anchor_count is not None or not anchor_valid):
                    raise AgentStoreError("The protected agent ledger checkpoint is inconsistent.")
            elif (not checkpoint_valid or checkpoint_count != int(prior["seq"])
                  or checkpoint_head != str(prior["hmac"])
                  or not anchor_valid or anchor_count != int(prior["seq"])
                  or not hmac.compare_digest(str(anchor_head or ""), str(prior["hmac"]))):
                raise AgentStoreError(
                    "The protected agent ledger checkpoint does not match the stored audit record.")
            seq = int(prior["seq"]) + 1 if prior is not None else 1
            previous = str(prior["hmac"]) if prior is not None else ""
            digest = self._ledger_mac(tenant_id, seq, previous, record)
            conn.execute(
                "INSERT INTO agent_ledger "
                "(tenant_id, seq, id, kind, record_encrypted, prev_hmac, hmac, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tenant_id, seq, record_id, str(kind),
                 self._seal(tenant_id, f"ledger:{record_id}", record), previous, digest, now),
            )
            checkpoint_hmac = self._ledger_checkpoint_mac(tenant_id, seq, digest)
            conn.execute(
                "INSERT INTO agent_ledger_checkpoint "
                "(tenant_id, record_count, head_hmac, checkpoint_hmac, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET record_count=excluded.record_count, "
                "head_hmac=excluded.head_hmac, checkpoint_hmac=excluded.checkpoint_hmac, "
                "updated_at=excluded.updated_at",
                (tenant_id, seq, digest, checkpoint_hmac, now),
            )
        # A separate signed head makes deletion of both the mutable chain and its local
        # checkpoint distinguishable from a genuinely new tenant history.
        self._write_ledger_anchor(tenant_id, seq, previous, digest, now)
        return {"seq": seq, "hmac": digest, "prev_hmac": previous, **record}

    def ledger(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_ledger WHERE tenant_id=? ORDER BY seq", (tenant_id,)).fetchall()
            return [
                {"seq": int(row["seq"]), "hmac": row["hmac"], "prev_hmac": row["prev_hmac"],
                 **self._open(tenant_id, f"ledger:{row['id']}", bytes(row["record_encrypted"]))}
                for row in rows
            ]

    def verify_ledger(self, tenant_id: str) -> dict[str, Any]:
        expected_seq, previous = 1, ""
        records = self.ledger(tenant_id)
        for item in records:
            if item["seq"] != expected_seq:
                return {"valid": False, "count": len(records), "first_invalid_seq": item["seq"]}
            record = {key: item[key] for key in ("id", "kind", "actor", "detail", "created_at")}
            expected = self._ledger_mac(tenant_id, item["seq"], previous, record)
            if (item["prev_hmac"] != previous
                    or not hmac.compare_digest(str(item["hmac"]), expected)):
                return {"valid": False, "count": len(records), "first_invalid_seq": item["seq"]}
            previous, expected_seq = item["hmac"], expected_seq + 1
        checkpoint_count, checkpoint_head, checkpoint_valid = self._ledger_checkpoint(tenant_id)
        if records and checkpoint_count is None:
            return {"valid": False, "count": len(records), "first_invalid_seq": None,
                    "reason": "The protected agent ledger checkpoint is missing."}
        if checkpoint_count is not None and (
                not checkpoint_valid or checkpoint_count != len(records) or checkpoint_head != previous):
            return {"valid": False, "count": len(records), "first_invalid_seq": None,
                    "checkpoint_count": checkpoint_count, "checkpoint_head_hmac": checkpoint_head,
                    "reason": "The agent ledger is incomplete or does not match its protected checkpoint."}
        anchor_count, anchor_head, anchor_valid = self._ledger_anchor(tenant_id)
        if not anchor_valid:
            return {"valid": False, "count": len(records), "first_invalid_seq": None,
                    "anchor_count": anchor_count, "anchor_head_hmac": anchor_head,
                    "reason": "The external agent-ledger anchor is invalid."}
        if anchor_count is None:
            if records or checkpoint_count is not None:
                return {"valid": False, "count": len(records), "first_invalid_seq": None,
                        "anchor_count": None, "anchor_head_hmac": None,
                        "reason": "The external agent-ledger anchor is missing."}
        elif anchor_count != len(records) or not hmac.compare_digest(str(anchor_head or ""), previous):
            return {"valid": False, "count": len(records), "first_invalid_seq": None,
                    "anchor_count": anchor_count, "anchor_head_hmac": anchor_head,
                    "reason": "The agent ledger does not match its external signed anchor."}
        return {"valid": True, "count": len(records), "first_invalid_seq": None,
                "head_hmac": previous or None,
                "checkpoint_count": checkpoint_count or 0,
                "checkpoint_head_hmac": checkpoint_head,
                "anchor_count": anchor_count or 0,
                "anchor_head_hmac": anchor_head}

    def enrol_totp(self, tenant_id: str) -> dict[str, str]:
        secret, now = pyotp.random_base32(), _now()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO agent_totp (tenant_id, secret_encrypted, enrolled_at) VALUES (?,?,?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET secret_encrypted=excluded.secret_encrypted, "
                "enrolled_at=excluded.enrolled_at",
                (tenant_id, self._seal(tenant_id, "totp", {"secret": secret}), now),
            )
        return {"secret": secret, "enrolled_at": now}

    def verify_totp(self, tenant_id: str, code: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT secret_encrypted FROM agent_totp WHERE tenant_id=?", (tenant_id,)).fetchone()
            if row is None:
                return False
            details = self._open(tenant_id, "totp", bytes(row["secret_encrypted"]))
        secret = str(details.get("secret") or "")
        return bool(secret and pyotp.TOTP(secret).verify(str(code or ""), valid_window=1))

    def record_webhook_once(self, tenant_id: str, event_id: str) -> None:
        if not event_id or len(event_id) > 160:
            raise AgentStoreValidationError("A webhook event id is required.")
        try:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO agent_webhook_replay (tenant_id, event_id, received_at) VALUES (?,?,?)",
                    (tenant_id, event_id, _now()),
                )
        except sqlite3.IntegrityError as exc:
            raise AgentStoreConflict("This signed webhook was already received.") from exc
