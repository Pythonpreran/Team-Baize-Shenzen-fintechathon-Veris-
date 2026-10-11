"""Encrypted reviewer-console records and exportable label evidence.

A reviewer can agree or disagree with a server-derived finding, record severity and a
comment, and export only the agreed records that include a declared qualification.  The
store never fabricates an expert label: absent credentials, disagreement, or missing
review keeps a record out of the measured expert set.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import persistence, security

REVIEW_DB_ENV = "VERIS_REVIEW_DB_PATH"
_NONCE_BYTES = 12


class ReviewerStoreError(RuntimeError):
    http_status = 400


class ReviewerStoreConfigurationError(ReviewerStoreError):
    http_status = 503


class ReviewerStoreNotFound(ReviewerStoreError):
    http_status = 404


class ReviewerStoreValidationError(ReviewerStoreError):
    http_status = 422


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _new_id() -> str:
    return uuid.uuid4().hex


def _text(value: object, label: str, *, limit: int) -> str:
    clean = " ".join(str(value or "").split())
    if not clean or len(clean) > limit:
        raise ReviewerStoreValidationError(f"{label} is required and must be at most {limit} characters.")
    return clean


class ReviewerStore:
    """Per-tenant encrypted queue and review labels using fresh SQLite connections."""

    def __init__(self, path: str | Path, master_key: bytes):
        self.path = Path(path).expanduser()
        if len(master_key) != 32:
            raise ReviewerStoreConfigurationError("Reviewer storage requires a 32-byte master key.")
        self._key = hashlib.sha256(b"veris-reviewer-store-v1\x00" + master_key).digest()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS reviewer_finding ("
                "id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, reviewer_org_id TEXT NOT NULL, "
                "payload_encrypted BLOB NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_reviewer_finding_org "
                "ON reviewer_finding(reviewer_org_id, created_at);"
                "CREATE TABLE IF NOT EXISTS reviewer_label ("
                "id TEXT PRIMARY KEY, finding_id TEXT NOT NULL, reviewer_id TEXT NOT NULL, "
                "review_encrypted BLOB NOT NULL, created_at TEXT NOT NULL, "
                "UNIQUE(finding_id, reviewer_id));"
                "CREATE INDEX IF NOT EXISTS idx_reviewer_label_finding "
                "ON reviewer_label(finding_id, created_at);")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _seal(self, tenant_id: str, purpose: str, value: Mapping[str, Any]) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        aad = f"reviewer:{tenant_id}:{purpose}".encode("utf-8")
        return nonce + AESGCM(self._key).encrypt(nonce, _canonical(value), aad)

    def _open(self, tenant_id: str, purpose: str, value: bytes) -> dict[str, Any]:
        if len(value) <= _NONCE_BYTES:
            raise ReviewerStoreError("The encrypted reviewer record is incomplete.")
        nonce, ciphertext = value[:_NONCE_BYTES], value[_NONCE_BYTES:]
        try:
            raw = AESGCM(self._key).decrypt(
                nonce, ciphertext, f"reviewer:{tenant_id}:{purpose}".encode("utf-8"))
            parsed = json.loads(raw.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewerStoreError("The encrypted reviewer record cannot be authenticated.") from exc
        if not isinstance(parsed, dict):
            raise ReviewerStoreError("The encrypted reviewer record has the wrong shape.")
        return parsed

    def enqueue(self, *, tenant_id: str, reviewer_org_id: str,
                finding: Mapping[str, Any], source: str = "OWNER_SUBMITTED") -> dict:
        org = _text(reviewer_org_id, "reviewer organisation id", limit=160)
        rule_id = _text(finding.get("rule_id"), "finding rule id", limit=160)
        state = _text(finding.get("output_state"), "finding output state", limit=80)
        evidence_class = _text(finding.get("evidence_class"), "finding evidence class", limit=80)
        payload = {
            "rule_id": rule_id,
            "output_state": state,
            "evidence_class": evidence_class,
            "headline": str(finding.get("headline") or "")[:800],
            "evidence": list(finding.get("evidence") or [])[:16],
        }
        finding_id, created_at = _new_id(), _now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO reviewer_finding "
                "(id,tenant_id,reviewer_org_id,payload_encrypted,source,created_at) VALUES (?,?,?,?,?,?)",
                (finding_id, tenant_id, org,
                 self._seal(tenant_id, f"finding:{finding_id}", payload), source, created_at),
            )
        return {"id": finding_id, "tenant_id": tenant_id, "reviewer_org_id": org,
                "source": source, "created_at": created_at, **payload, "review": None}

    def _row_out(self, row: sqlite3.Row, review: sqlite3.Row | None = None) -> dict:
        finding_id, tenant_id = str(row["id"]), str(row["tenant_id"])
        payload = self._open(tenant_id, f"finding:{finding_id}", bytes(row["payload_encrypted"]))
        out = {
            "id": finding_id, "tenant_id": tenant_id,
            "reviewer_org_id": row["reviewer_org_id"], "source": row["source"],
            "created_at": row["created_at"], **payload, "review": None,
        }
        if review is not None:
            out["review"] = self._open(tenant_id, f"label:{review['id']}",
                                        bytes(review["review_encrypted"])) | {
                "id": review["id"], "reviewer_id": review["reviewer_id"],
                "created_at": review["created_at"],
            }
        return out

    def queue(self, reviewer_org_id: str) -> list[dict]:
        org = _text(reviewer_org_id, "reviewer organisation id", limit=160)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT finding.*, label.id AS label_id, label.reviewer_id AS label_reviewer_id, "
                "label.review_encrypted AS label_encrypted, label.created_at AS label_created_at "
                "FROM reviewer_finding AS finding LEFT JOIN reviewer_label AS label "
                "ON label.finding_id=finding.id WHERE finding.reviewer_org_id=? "
                "ORDER BY finding.created_at, finding.id", (org,)).fetchall()
            output = []
            for row in rows:
                review = None
                if row["label_id"] is not None:
                    review = {
                        "id": row["label_id"], "reviewer_id": row["label_reviewer_id"],
                        "review_encrypted": row["label_encrypted"], "created_at": row["label_created_at"],
                    }
                output.append(self._row_out(row, review))
            return output

    def record_review(self, *, reviewer_org_id: str, reviewer_id: str,
                      reviewer_name: str, credentials: str, finding_id: str,
                      decision: str, severity: str, comment: str) -> dict:
        org = _text(reviewer_org_id, "reviewer organisation id", limit=160)
        decision = _text(decision, "decision", limit=16).upper()
        severity = _text(severity, "severity", limit=16).upper()
        if decision not in {"AGREE", "DISAGREE"}:
            raise ReviewerStoreValidationError("Review decision must be AGREE or DISAGREE.")
        if severity not in {"LOW", "MODERATE", "HIGH"}:
            raise ReviewerStoreValidationError("Review severity must be LOW, MODERATE or HIGH.")
        if len(comment or "") > 2_000:
            raise ReviewerStoreValidationError("Review comment must be at most 2000 characters.")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM reviewer_finding WHERE id=? AND reviewer_org_id=?",
                (finding_id, org)).fetchone()
            if row is None:
                raise ReviewerStoreNotFound("No assigned reviewer finding matches that request.")
            tenant_id = str(row["tenant_id"])
            label_id, created_at = _new_id(), _now()
            payload = {
                "decision": decision,
                "severity": severity,
                "comment": str(comment or "").strip(),
                "reviewer_name": _text(reviewer_name, "reviewer name", limit=160),
                "credentials": " ".join(str(credentials or "").split())[:160],
            }
            try:
                connection.execute(
                    "INSERT INTO reviewer_label (id,finding_id,reviewer_id,review_encrypted,created_at) "
                    "VALUES (?,?,?,?,?)",
                    (label_id, finding_id, reviewer_id,
                     self._seal(tenant_id, f"label:{label_id}", payload), created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ReviewerStoreValidationError(
                    "This reviewer already recorded a decision for that finding.") from exc
            return {"id": label_id, "finding_id": finding_id, "reviewer_id": reviewer_id,
                    "created_at": created_at, **payload}

    def export_labels(self, reviewer_org_id: str) -> dict:
        """Export only labels whose human reviewer agreed and declared credentials."""
        exportable, blocked = [], []
        for item in self.queue(reviewer_org_id):
            review = item.get("review") or {}
            credentials = str(review.get("credentials") or "").strip()
            if review.get("decision") != "AGREE" or not credentials:
                blocked.append({"finding_id": item["id"], "reason": (
                    "needs an AGREE decision and non-empty reviewer credentials")})
                continue
            exportable.append({
                "label_id": f"reviewer-{item['id']}",
                "derivation": "REVIEWED_BY_EXPERT",
                "reviewer": {"name": review["reviewer_name"], "credentials": credentials},
                "reviewed_at": review["created_at"],
                "scope": "PARTIAL",
                "expected_findings": {
                    item["rule_id"]: {
                        "output_state": item["output_state"],
                        "evidence_class": item["evidence_class"],
                    },
                },
                "source": item["source"],
            })
        return {
            "labels": exportable,
            "exportable_count": len(exportable),
            "blocked": blocked,
            "notice": ("An exportable label records an agree decision and the reviewer's "
                       "declared credentials. It does not verify a licence; organisers or "
                       "the team must retain the human evidence separately."),
        }


def store_from_environment() -> ReviewerStore:
    configured = (os.getenv(REVIEW_DB_ENV) or "").strip()
    portfolio_path = persistence.portfolio_db_path()
    if configured:
        path = Path(configured).expanduser()
    elif portfolio_path is not None:
        path = portfolio_path.with_name(f"{portfolio_path.stem}.reviewer.sqlite")
    else:
        raise ReviewerStoreConfigurationError(
            f"Set {REVIEW_DB_ENV} (or {persistence.PORTFOLIO_DB_ENV}) before using reviewer records.")
    key, source = persistence.master_key_for(path)
    if security.hosted_mode() and source != "environment":
        raise ReviewerStoreConfigurationError(
            f"Hosted reviewer records require {persistence.MASTER_KEY_ENV} from the environment.")
    return ReviewerStore(path, key)
