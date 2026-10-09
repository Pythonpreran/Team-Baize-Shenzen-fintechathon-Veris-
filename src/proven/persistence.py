"""Persistence layer — cases survive a process restart.

THE PROBLEM THIS SOLVES. The Merged base runs purely in-memory SQLite. Restart the
server and every case is gone. That is fine for a demo where you reload the example,
but it is not a product — a product remembers what you gave it.

DESIGN. Opt-in via PROVEN_DB_PATH environment variable. When set:
  - The database is file-backed SQLite at that path.
  - Each session → case mapping is stored in a `session_case` table.
  - After analysis, a JSON snapshot of the derived brief is saved.
  - On reconnect, the session looks up its case and reloads from the snapshot.

When PROVEN_DB_PATH is not set (the default), everything runs in-memory as before and
all existing tests pass unchanged. The persistence layer is additive, not a rewrite.

OWNER SCOPING. A session can only reload a case it created. Case B is never returned
to session A. This is the same isolation the session system already enforces, extended
to survive across restarts.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import db

# ── configuration ────────────────────────────────────────────────────────────

def db_path() -> Optional[str]:
    """The configured persistent database path, or None for in-memory."""
    return os.environ.get("PROVEN_DB_PATH", "").strip() or None


def is_persistent() -> bool:
    return db_path() is not None


# ── schema (additive, layered on top of db.SCHEMA) ───────────────────────────

PERSISTENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_case (
  session_id  TEXT PRIMARY KEY,
  case_id     TEXT NOT NULL,
  owner       TEXT NOT NULL,
  business_id TEXT,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_snapshot (
  case_id      TEXT PRIMARY KEY,
  owner        TEXT NOT NULL,
  business_id  TEXT,
  status       TEXT NOT NULL DEFAULT 'ACTIVE',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_snapshot_owner ON case_snapshot (owner);

CREATE TABLE IF NOT EXISTS scheduler_run (
  run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  agent       TEXT NOT NULL,
  timestamp   TEXT NOT NULL,
  mode        TEXT NOT NULL,
  signals     INTEGER NOT NULL DEFAULT 0,
  elevated    INTEGER NOT NULL DEFAULT 0,
  summary     TEXT,
  duration_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scheduler_run_ts ON scheduler_run (timestamp DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── connection management ────────────────────────────────────────────────────

_shared_conn: Optional[sqlite3.Connection] = None


def get_shared_conn() -> Optional[sqlite3.Connection]:
    """Return the persistent shared connection, or None if not configured."""
    global _shared_conn
    path = db_path()
    if not path:
        return None
    if _shared_conn is None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _shared_conn = sqlite3.connect(path, check_same_thread=False)
        _shared_conn.row_factory = sqlite3.Row
        _shared_conn.execute("PRAGMA journal_mode=WAL")
        _shared_conn.execute("PRAGMA foreign_keys=ON")
        # Apply the base schema + persistence schema
        db.init_db(_shared_conn)
        for stmt in PERSISTENCE_SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                _shared_conn.execute(stmt)
        _shared_conn.commit()
    return _shared_conn


def close_shared() -> None:
    global _shared_conn
    if _shared_conn:
        try:
            _shared_conn.close()
        except Exception:
            pass
        _shared_conn = None


# ── session → case mapping ───────────────────────────────────────────────────

def save_session_case(session_id: str, case_id: str, owner: str,
                      business_id: str = "") -> None:
    conn = get_shared_conn()
    if not conn:
        return
    conn.execute(
        "INSERT OR REPLACE INTO session_case (session_id, case_id, owner, "
        "business_id, updated_at) VALUES (?,?,?,?,?)",
        (session_id, case_id, owner, business_id, _now()))
    conn.commit()


def load_session_case(session_id: str) -> Optional[dict]:
    conn = get_shared_conn()
    if not conn:
        return None
    row = conn.execute(
        "SELECT case_id, owner, business_id FROM session_case WHERE session_id=?",
        (session_id,)).fetchone()
    return dict(row) if row else None


# ── case snapshots ───────────────────────────────────────────────────────────

def save_snapshot(case_id: str, owner: str, business_id: str,
                  payload: dict) -> None:
    conn = get_shared_conn()
    if not conn:
        return
    now = _now()
    conn.execute(
        "INSERT OR REPLACE INTO case_snapshot (case_id, owner, business_id, status, "
        "created_at, updated_at, payload_json) VALUES (?,?,?,?,?,?,?)",
        (case_id, owner, business_id, "ACTIVE", now, now,
         json.dumps(payload, default=str)))
    conn.commit()


def load_snapshot(case_id: str, owner: str) -> Optional[dict]:
    """Load a case snapshot, enforcing owner isolation. Deleted snapshots are excluded."""
    conn = get_shared_conn()
    if not conn:
        return None
    row = conn.execute(
        "SELECT payload_json FROM case_snapshot "
        "WHERE case_id=? AND owner=? AND status='ACTIVE'",
        (case_id, owner)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def delete_snapshot(case_id: str) -> None:
    conn = get_shared_conn()
    if not conn:
        return
    conn.execute("UPDATE case_snapshot SET status='DELETED', updated_at=? "
                 "WHERE case_id=?", (_now(), case_id))
    conn.commit()


# ── scheduler run persistence ────────────────────────────────────────────────

def save_scheduler_run(agent: str, timestamp: str, mode: str, signals: int,
                       elevated: bool, summary: str, duration_ms: int = 0) -> None:
    conn = get_shared_conn()
    if not conn:
        return
    conn.execute(
        "INSERT INTO scheduler_run (agent, timestamp, mode, signals, elevated, "
        "summary, duration_ms) VALUES (?,?,?,?,?,?,?)",
        (agent, timestamp, mode, signals, int(elevated), summary, duration_ms))
    conn.commit()


def load_scheduler_history(limit: int = 50) -> list[dict]:
    conn = get_shared_conn()
    if not conn:
        return []
    rows = conn.execute(
        "SELECT agent, timestamp, mode, signals, elevated, summary, duration_ms "
        "FROM scheduler_run ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
    return [{"agent": r["agent"], "timestamp": r["timestamp"], "mode": r["mode"],
             "signals_produced": r["signals"], "elevated": bool(r["elevated"]),
             "summary": r["summary"], "duration_ms": r["duration_ms"]}
            for r in reversed(rows)]


# ── status ───────────────────────────────────────────────────────────────────

# Whether anything in the codebase actually CALLS the save functions below.
#
# MEASURED, not assumed: `save_snapshot()` and `save_session_case()` are defined in this
# module and invoked from nowhere. Enabling PROVEN_DB_PATH and building a full case leaves
# session_case, case_snapshot and record_fact at 0 rows each — the schema is created and
# stays empty.
#
# This constant exists so `status()` cannot report a working feature that is not wired.
# Flip it to True in the same commit that adds the call sites, and the notice below
# corrects itself.
SNAPSHOT_WRITES_WIRED = False


def status() -> dict:
    path = db_path()
    conn = get_shared_conn()
    configured = is_persistent()
    # `persistent` answers "will a case survive a restart", which is the only question a
    # reader is asking. Configuring a path does not make that true while the writes are
    # unwired, so the flag reports the end-to-end behaviour rather than the setting.
    effective = bool(configured and SNAPSHOT_WRITES_WIRED)
    if configured and not SNAPSHOT_WRITES_WIRED:
        notice = (
            f"A database path is configured ({path}) and the schema exists there, but "
            f"cases do NOT survive a restart: the snapshot write path is not wired up. "
            f"save_snapshot() and save_session_case() have no call sites, so every table "
            f"stays empty. Reported as not persistent because that is the behaviour.")
    elif configured:
        notice = (f"Persistent file-backed SQLite at {path}. Cases survive a restart.")
    else:
        notice = ("In-memory mode (default). Cases are lost on restart. Setting "
                  "PROVEN_DB_PATH alone will not change that until the snapshot write "
                  "path is wired — see SNAPSHOT_WRITES_WIRED in persistence.py.")
    return {
        "persistent": effective,
        "path_configured": configured,
        "snapshot_writes_wired": SNAPSHOT_WRITES_WIRED,
        "backend": "file-sqlite" if path else "memory",
        "path": path,
        "connected": conn is not None,
        "notice": notice,
    }
