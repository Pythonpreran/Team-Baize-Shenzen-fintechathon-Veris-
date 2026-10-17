"""Evidence-bound local claim copilot state for the synthetic VERIS demo.

Claims here are preparation and tracking records, not coverage or settlement decisions.
Every local state transition names its permitted actor.  External-looking events come only
from the separate synthetic sandbox after HMAC validation; this module never contacts a
real insurer or bank.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import jurisdiction, persistence


class ClaimError(RuntimeError):
    http_status = 400


class ClaimNotFound(ClaimError):
    http_status = 404


class ClaimTransitionError(ClaimError):
    http_status = 409


class ClaimValidationError(ClaimError):
    http_status = 422


CLAIM_STATES = (
    "DRAFTING", "READY", "SUBMITTED", "ACKNOWLEDGED", "SURVEYOR_REQUIRED",
    "QUERY_OPEN", "UNDER_ASSESSMENT", "DECIDED", "SETTLED", "DISPUTED",
    "ESCALATED", "CLOSED",
)

# `(from_state, to_state) -> permitted actor`.  There is deliberately no transition that
# lets the agent decide a claim outcome: insurer-side sandbox events may only advance the
# synthetic workflow state, and the owner still sees the state as synthetic.  The motor
# surveyor step is likewise counterparty-owned and cannot be skipped by VERIS.
_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("DRAFTING", "READY"): frozenset({"OWNER", "AGENT", "DELEGATE"}),
    ("READY", "SUBMITTED"): frozenset({"AGENT"}),
    ("SUBMITTED", "ACKNOWLEDGED"): frozenset({"INSURER"}),
    ("SUBMITTED", "SURVEYOR_REQUIRED"): frozenset({"INSURER"}),
    ("SURVEYOR_REQUIRED", "UNDER_ASSESSMENT"): frozenset({"INSURER"}),
    ("ACKNOWLEDGED", "QUERY_OPEN"): frozenset({"INSURER"}),
    ("ACKNOWLEDGED", "UNDER_ASSESSMENT"): frozenset({"INSURER"}),
    ("QUERY_OPEN", "UNDER_ASSESSMENT"): frozenset({"AGENT", "INSURER"}),
    ("UNDER_ASSESSMENT", "DECIDED"): frozenset({"INSURER"}),
    ("DECIDED", "SETTLED"): frozenset({"INSURER"}),
    ("DECIDED", "DISPUTED"): frozenset({"OWNER", "AGENT"}),
    ("DISPUTED", "SETTLED"): frozenset({"INSURER"}),
    ("QUERY_OPEN", "ESCALATED"): frozenset({"OWNER", "AGENT"}),
    ("ACKNOWLEDGED", "ESCALATED"): frozenset({"OWNER", "AGENT"}),
    ("SETTLED", "CLOSED"): frozenset({"OWNER"}),
    ("ESCALATED", "CLOSED"): frozenset({"OWNER"}),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_case (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  policy_id TEXT NOT NULL,
  state TEXT NOT NULL,
  details_encrypted BLOB NOT NULL,
  packet_manifest_sha256 TEXT,
  sandbox_claim_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_case_tenant ON claim_case (tenant_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_case_sandbox
  ON claim_case (tenant_id, sandbox_claim_id) WHERE sandbox_claim_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS claim_transition (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  actor TEXT NOT NULL,
  note_encrypted BLOB NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_transition_case ON claim_transition (tenant_id, claim_id, at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _claim_id() -> str:
    return f"claim-{uuid.uuid4().hex}"


_FIXED_POLICY_TIMEZONES = {
    # Every in-scope pack uses a fixed civil offset with no daylight saving.  Windows
    # Python commonly ships without the IANA database; this keeps the pack-selected
    # timezone deterministic without adding a runtime dependency or falling back to the
    # server's local clock.
    "Asia/Hong_Kong": timedelta(hours=8),
    "Asia/Kolkata": timedelta(hours=5, minutes=30),
    "Asia/Kuala_Lumpur": timedelta(hours=8),
    "Asia/Singapore": timedelta(hours=8),
}


def _policy_timezone(policy: Mapping[str, Any]):
    country = str(policy.get("country") or "").upper()
    try:
        timezone_name = jurisdiction.pack(country).timezone
    except jurisdiction.PackError as exc:
        raise ClaimValidationError(
            "The policy jurisdiction must provide a recognised timezone before claim timing is calculated.") from exc
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        fixed_offset = _FIXED_POLICY_TIMEZONES.get(timezone_name)
        if fixed_offset is not None:
            return timezone(fixed_offset, name=timezone_name)
        raise ClaimValidationError(
            "The policy jurisdiction timezone is unavailable on this server; no deadline is calculated.") from exc


def claims_db_path(portfolio_path: Path) -> Path:
    return portfolio_path.with_name(f"{portfolio_path.stem}.claims.sqlite")


def repository_from_environment() -> "ClaimsRepository":
    path = persistence.portfolio_db_path()
    if path is None:
        raise ClaimError(f"Set {persistence.PORTFOLIO_DB_ENV} before using claims.")
    key, _source = persistence.master_key_for(path)
    return ClaimsRepository(claims_db_path(path), key)


class ClaimsRepository:
    def __init__(self, db_path: str | Path, master_key: bytes):
        self.db_path = Path(db_path)
        if len(master_key) != 32:
            raise ClaimError("Claims storage requires a 32-byte master key.")
        self._key = hashlib.sha256(b"veris-claims-v1\x00" + master_key).digest()

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
        nonce = os.urandom(12)
        return nonce + AESGCM(self._key).encrypt(
            nonce, _canonical(dict(value)).encode("utf-8"),
            f"claim:{tenant_id}:{purpose}".encode("utf-8"))

    def _open(self, tenant_id: str, purpose: str, blob: bytes) -> dict[str, Any]:
        try:
            raw = AESGCM(self._key).decrypt(
                blob[:12], blob[12:], f"claim:{tenant_id}:{purpose}".encode("utf-8"))
            value = json.loads(raw.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClaimError("The encrypted claim record cannot be authenticated.") from exc
        if not isinstance(value, dict):
            raise ClaimError("The encrypted claim record is malformed.")
        return value

    def _out(self, row: sqlite3.Row) -> dict[str, Any]:
        details = self._open(str(row["tenant_id"]), f"details:{row['id']}", bytes(row["details_encrypted"]))
        return {
            "id": row["id"], "policy_id": row["policy_id"], "state": row["state"],
            "packet_manifest_sha256": row["packet_manifest_sha256"],
            "sandbox_claim_id": row["sandbox_claim_id"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], **details,
        }

    def create(self, scope: persistence.TenantScope, *, policy: Mapping[str, Any],
               incident_at: str, mode: str) -> dict[str, Any]:
        try:
            incident = datetime.fromisoformat(incident_at)
        except ValueError as exc:
            raise ClaimValidationError("Claim intake needs an ISO date and time with an explicit UTC offset.") from exc
        if incident.tzinfo is None or incident.utcoffset() is None:
            raise ClaimValidationError(
                "Claim intake needs an explicit UTC offset so timing can be normalised to the policy jurisdiction.")
        line = str(policy.get("line_of_business") or "").upper()
        selected_mode = str(mode or "").upper()
        if line == "HEALTH":
            if selected_mode not in {"CASHLESS", "REIMBURSEMENT"}:
                raise ClaimValidationError("Choose CASHLESS or REIMBURSEMENT for health intake.")
        elif line == "MOTOR":
            if selected_mode != "MOTOR_OWN_DAMAGE":
                raise ClaimValidationError(
                    "The local motor copilot supports the synthetic MOTOR_OWN_DAMAGE path only.")
        else:
            raise ClaimValidationError(
                "Claim intake is available only for the implemented health and motor copilot paths.")
        incident = incident.astimezone(_policy_timezone(policy))
        claim_id, now = _claim_id(), _now()
        checklist = checklist_for(policy, selected_mode)
        deadline = deadline_for(policy, incident)
        details = {
            "line_of_business": line, "country": str(policy.get("country") or ""),
            "mode": selected_mode, "incident_at": incident.isoformat(), "checklist": checklist,
            "deadline": deadline, "packet": None, "query_draft": None,
            "surveyor": ({
                "state": "AWAITING_SYNTHETIC_COUNTERPARTY",
                "notice": "The synthetic motor scenario waits for a counterparty-owned surveyor step after submission.",
            } if line == "MOTOR" else None),
            "synthetic": bool(policy.get("source") == "SYNTHETIC_EXAMPLE"),
        }
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO claim_case (id, tenant_id, policy_id, state, details_encrypted, "
                "packet_manifest_sha256, sandbox_claim_id, created_at, updated_at) VALUES (?,?,?,?,?,?,NULL,?,?)",
                (claim_id, scope.tenant_id, str(policy["id"]), "DRAFTING",
                 self._seal(scope.tenant_id, f"details:{claim_id}", details), None, now, now),
            )
            self._transition(conn, scope.tenant_id, claim_id, "", "DRAFTING", "OWNER", "Intake created.")
            row = conn.execute("SELECT * FROM claim_case WHERE id=?", (claim_id,)).fetchone()
            return self._out(row)

    def list(self, scope: persistence.TenantScope) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM claim_case WHERE tenant_id=? ORDER BY created_at DESC, id",
                                (scope.tenant_id,)).fetchall()
            return [self._out(row) for row in rows]

    def get(self, scope: persistence.TenantScope, claim_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM claim_case WHERE id=? AND tenant_id=?",
                               (claim_id, scope.tenant_id)).fetchone()
            if row is None:
                raise ClaimNotFound("No claim belongs to this portfolio.")
            value = self._out(row)
            value["transitions"] = self._transitions(conn, scope.tenant_id, claim_id)
            value["allowed_transitions"] = self.allowed_transitions(value["state"])
            return value

    def _transitions(self, conn: sqlite3.Connection, tenant_id: str, claim_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM claim_transition WHERE tenant_id=? AND claim_id=? ORDER BY rowid",
            (tenant_id, claim_id)).fetchall()
        return [
            {"from": row["from_state"], "to": row["to_state"], "actor": row["actor"],
             "note": self._open(tenant_id, f"transition:{row['id']}", bytes(row["note_encrypted"])).get("note", ""),
             "at": row["at"]}
            for row in rows
        ]

    @staticmethod
    def allowed_transitions(state: str) -> list[dict[str, Any]]:
        return [
            {"to": to_state, "actors": sorted(actors)}
            for (from_state, to_state), actors in _TRANSITIONS.items() if from_state == state
        ]

    def _transition(self, conn: sqlite3.Connection, tenant_id: str, claim_id: str,
                    from_state: str, to_state: str, actor: str, note: str) -> None:
        transition_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO claim_transition (id, tenant_id, claim_id, from_state, to_state, actor, note_encrypted, at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (transition_id, tenant_id, claim_id, from_state, to_state, actor,
             self._seal(tenant_id, f"transition:{transition_id}", {"note": note}), _now()),
        )

    def transition(self, scope: persistence.TenantScope, claim_id: str, *, to_state: str,
                   actor: str, note: str = "") -> dict[str, Any]:
        target, actor = str(to_state).upper(), str(actor).upper()
        if target not in CLAIM_STATES:
            raise ClaimValidationError("Unknown claim state.")
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM claim_case WHERE id=? AND tenant_id=?",
                               (claim_id, scope.tenant_id)).fetchone()
            if row is None:
                raise ClaimNotFound("No claim belongs to this portfolio.")
            current = str(row["state"])
            permitted = _TRANSITIONS.get((current, target), frozenset())
            if actor not in permitted:
                raise ClaimTransitionError(
                    f"{actor} cannot move a claim from {current} to {target}.")
            now = _now()
            details = self._open(scope.tenant_id, f"details:{claim_id}", bytes(row["details_encrypted"]))
            if str(details.get("line_of_business") or "").upper() == "MOTOR":
                if target == "SURVEYOR_REQUIRED":
                    details["surveyor"] = {
                        "state": "SURVEYOR_REQUIRED",
                        "notice": "The local synthetic insurer recorded a counterparty-owned surveyor step.",
                    }
                elif target == "UNDER_ASSESSMENT" and current == "SURVEYOR_REQUIRED":
                    details["surveyor"] = {
                        "state": "COMPLETED_BY_SYNTHETIC_COUNTERPARTY",
                        "notice": "A signed local synthetic counterparty event recorded survey completion; VERIS did not determine the result.",
                    }
            conn.execute("UPDATE claim_case SET state=?, details_encrypted=?, updated_at=? WHERE id=? AND tenant_id=?",
                         (target, self._seal(scope.tenant_id, f"details:{claim_id}", details), now,
                          claim_id, scope.tenant_id))
            self._transition(conn, scope.tenant_id, claim_id, current, target, actor, note)
            updated = conn.execute("SELECT * FROM claim_case WHERE id=?", (claim_id,)).fetchone()
            return self._out(updated)

    def packet(self, scope: persistence.TenantScope, claim_id: str,
               documents: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM claim_case WHERE id=? AND tenant_id=?",
                               (claim_id, scope.tenant_id)).fetchone()
            if row is None:
                raise ClaimNotFound("No claim belongs to this portfolio.")
            details = self._open(scope.tenant_id, f"details:{claim_id}", bytes(row["details_encrypted"]))
            entries = []
            document_kinds: set[str] = set()
            for document in documents:
                document_id = str(document.get("id") or "")
                sha256 = str(document.get("sha256") or "")
                kind = str(document.get("kind") or "").upper()
                if not document_id or len(sha256) != 64 or not kind:
                    raise ClaimValidationError(
                        "Each claim packet document needs an id, SHA-256 and established document kind.")
                entries.append({"document_id": document_id, "sha256": sha256, "kind": kind})
                document_kinds.add(kind)
            if not entries:
                raise ClaimValidationError("A claim packet needs at least one owner-held document.")
            checklist = details.get("checklist") if isinstance(details.get("checklist"), list) else []
            missing = [str(item.get("item") or "document") for item in checklist
                       if isinstance(item, Mapping) and item.get("packet_required")
                       and str(item.get("document_kind") or "") not in document_kinds]
            if missing:
                raise ClaimValidationError(
                    "The claim packet is missing required checklist documents: " + ", ".join(missing) + ".")
            manifest = {"claim_id": claim_id, "documents": sorted(entries, key=lambda item: item["document_id"])}
            manifest_sha = hashlib.sha256(_canonical(manifest).encode("utf-8")).hexdigest()
            details["packet"] = {
                "index": entries,
                "manifest": manifest,
                "manifest_sha256": manifest_sha,
                "checklist_complete": True,
                "cover_letter": (
                    "Please find the listed documents for synthetic sandbox review. "
                    "This packet does not assert cover, liability or a payment outcome."),
            }
            now = _now()
            conn.execute("UPDATE claim_case SET details_encrypted=?, packet_manifest_sha256=?, updated_at=? "
                         "WHERE id=? AND tenant_id=?",
                         (self._seal(scope.tenant_id, f"details:{claim_id}", details), manifest_sha, now,
                          claim_id, scope.tenant_id))
            return dict(details["packet"])

    def verify_packet(self, packet: Mapping[str, Any]) -> bool:
        manifest = packet.get("manifest")
        expected = str(packet.get("manifest_sha256") or "")
        return isinstance(manifest, Mapping) and bool(expected) and hmac_compare(
            hashlib.sha256(_canonical(dict(manifest)).encode("utf-8")).hexdigest(), expected)

    def set_sandbox_claim(self, scope: persistence.TenantScope, claim_id: str,
                          sandbox_claim_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM claim_case WHERE id=? AND tenant_id=?",
                               (claim_id, scope.tenant_id)).fetchone()
            if row is None:
                raise ClaimNotFound("No claim belongs to this portfolio.")
            conn.execute("UPDATE claim_case SET sandbox_claim_id=?, updated_at=? WHERE id=? AND tenant_id=?",
                         (sandbox_claim_id, _now(), claim_id, scope.tenant_id))
            updated = conn.execute("SELECT * FROM claim_case WHERE id=?", (claim_id,)).fetchone()
            return self._out(updated)

    def by_sandbox_claim(self, tenant_id: str, sandbox_claim_id: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM claim_case WHERE tenant_id=? AND sandbox_claim_id=?",
                               (tenant_id, sandbox_claim_id)).fetchone()
            return self._out(row) if row is not None else None

    def attach_documents(self, scope: persistence.TenantScope, claim_id: str,
                         document_ids: Sequence[str]) -> list[str]:
        ids = [str(document_id) for document_id in document_ids if str(document_id)]
        if not ids:
            raise ClaimValidationError("At least one uploaded claim document is required.")
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM claim_case WHERE id=? AND tenant_id=?",
                               (claim_id, scope.tenant_id)).fetchone()
            if row is None:
                raise ClaimNotFound("No claim belongs to this portfolio.")
            details = self._open(scope.tenant_id, f"details:{claim_id}", bytes(row["details_encrypted"]))
            current = [str(value) for value in details.get("uploaded_document_ids", [])]
            details["uploaded_document_ids"] = list(dict.fromkeys([*current, *ids]))
            conn.execute("UPDATE claim_case SET details_encrypted=?, updated_at=? WHERE id=? AND tenant_id=?",
                         (self._seal(scope.tenant_id, f"details:{claim_id}", details), _now(),
                          claim_id, scope.tenant_id))
            return list(details["uploaded_document_ids"])

    def set_query_draft(self, scope: persistence.TenantScope, claim_id: str,
                        draft: Mapping[str, Any]) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM claim_case WHERE id=? AND tenant_id=?",
                               (claim_id, scope.tenant_id)).fetchone()
            if row is None:
                raise ClaimNotFound("No claim belongs to this portfolio.")
            details = self._open(scope.tenant_id, f"details:{claim_id}", bytes(row["details_encrypted"]))
            details["query_draft"] = dict(draft)
            conn.execute("UPDATE claim_case SET details_encrypted=?, updated_at=? WHERE id=? AND tenant_id=?",
                         (self._seal(scope.tenant_id, f"details:{claim_id}", details), _now(),
                          claim_id, scope.tenant_id))
            return dict(draft)

    def remove_synthetic_claims(self, scope: persistence.TenantScope,
                                policy_ids: Sequence[str]) -> list[str]:
        """Delete this tenant's claim workflow records on removed synthetic policies.

        Only the demo reset calls this, after the portfolio repository has removed the
        synthetic policies themselves. The claim row and its transitions describe the
        synthetic example's workflow. The documents and photos the owner uploaded to it
        live in the portfolio store and are NOT removed: the API protects every upload
        id from the reset, so they stay on the step-up erase path.
        """
        wanted = {str(value) for value in policy_ids if str(value)}
        if not wanted:
            return []
        removed: list[str] = []
        with self._connection() as conn:
            rows = conn.execute("SELECT id, policy_id FROM claim_case WHERE tenant_id=? ORDER BY created_at, id",
                                (scope.tenant_id,)).fetchall()
            for row in rows:
                if str(row["policy_id"]) not in wanted:
                    continue
                conn.execute("DELETE FROM claim_transition WHERE tenant_id=? AND claim_id=?",
                             (scope.tenant_id, row["id"]))
                conn.execute("DELETE FROM claim_case WHERE tenant_id=? AND id=?",
                             (scope.tenant_id, row["id"]))
                removed.append(str(row["id"]))
        return removed

    def set_settlement_check(self, scope: persistence.TenantScope, claim_id: str,
                             check: Mapping[str, Any]) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM claim_case WHERE id=? AND tenant_id=?",
                               (claim_id, scope.tenant_id)).fetchone()
            if row is None:
                raise ClaimNotFound("No claim belongs to this portfolio.")
            details = self._open(scope.tenant_id, f"details:{claim_id}", bytes(row["details_encrypted"]))
            details["settlement_check"] = dict(check)
            conn.execute("UPDATE claim_case SET details_encrypted=?, updated_at=? WHERE id=? AND tenant_id=?",
                         (self._seal(scope.tenant_id, f"details:{claim_id}", details), _now(),
                          claim_id, scope.tenant_id))
            return dict(check)


def hmac_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left, right)


def _policy_items(policy: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return mapped policy terms without promoting a caller-selected evidence class."""
    out: list[Mapping[str, Any]] = []
    for key in ("health_items", "motor_items", "property_travel_items", "coverage_items"):
        values = policy.get(key)
        if isinstance(values, list):
            out.extend(item for item in values if isinstance(item, Mapping))
    return out


def _missing_source(required_document: str,
                    source_document_id: str | None = None) -> dict[str, Any]:
    """An open question about where a requirement comes from, named as one.

    `state` is spelled `INFORMATION_MISSING`, the same string the portfolio payloads,
    `deadline_for` and the escalation gates already use, so one idea has one spelling.
    The record stays a truthy mapping: callers keep treating "has a source record" and
    "has a source" as the same check, and read what it is — a question still open.
    """
    return {"state": "INFORMATION_MISSING", "document": None, "page": None, "quote": None,
            "source_url": None, "source_document_id": source_document_id,
            "required_document": required_document}


def _schedule_source(policy: Mapping[str, Any], mechanism: str,
                     required_document: str) -> dict[str, Any]:
    """The source for the "Policy schedule" item, and for no other item on the list.

    The schedule is the one requirement the schedule document itself establishes, so this
    is the one place the label "the policy schedule" is a reading rather than an
    assumption. It is a separate helper precisely to keep that label out of the generic
    path: a claim-procedure or cashless-network clause mapped from the policy WORDING
    must never be announced as the schedule.
    """
    cited = _citation_for(policy, mechanism, required_document)
    if cited.get("state") == "DOCUMENTED":
        return cited
    ids = [str(value) for value in (policy.get("source_document_ids") or []) if value]
    if ids:
        return {"state": "DOCUMENTED", "document": "the policy schedule", "page": None,
                "quote": None, "source_url": None, "source_document_id": ids[0],
                "required_document": None}
    return cited


def _citation_for(policy: Mapping[str, Any], mechanism: str,
                  required_document: str) -> dict[str, Any]:
    """What a checklist item rests on, or an honest record that nothing does yet.

    MEASURED DEFECT. Every item on a health checklist cited "the policy schedule". Two
    causes, both here. The health branch looked up `CLAIM.REIMBURSEMENT` while the health
    profile emits `CONDITION.REIMBURSEMENT` (src/proven/lob/HEALTH.yaml), so the lookup
    never matched; and the unmatched branch then returned `{"document": "the policy
    schedule"}` for anything it could not find — a document label attached to a
    requirement that document does not establish, which is a fabricated citation and the
    one output this product may not produce (PLAN rule 15).

    An unmatched mechanism returns the missing marker above and NAMES the document that
    would establish the item. No branch here labels anything "the policy schedule": that
    reading belongs to `_schedule_source()` and to the schedule item alone.
    """
    for item in _policy_items(policy):
        if item.get("mechanism_code") != mechanism:
            continue
        citation = item.get("citation")
        if isinstance(citation, Mapping) and (citation.get("quote") or citation.get("source_document_id")):
            return {"state": "DOCUMENTED",
                    "document": citation.get("document"), "page": citation.get("page"),
                    "quote": citation.get("quote"), "source_url": citation.get("source_url"),
                    "source_document_id": citation.get("source_document_id"),
                    "required_document": None}
        if item.get("source_document_id"):
            # A mapped term that names its document but carries no citation. The id is
            # real and travels with the record; the document's NAME is not known here,
            # and the schedule label this branch used to return was an assumption about
            # which document it was. So the id is kept and the question stays open.
            return _missing_source(required_document, str(item["source_document_id"]))
    return _missing_source(required_document)


def checklist_for(policy: Mapping[str, Any], mode: str) -> list[dict[str, Any]]:
    """A deterministic checklist whose required packet roles are persisted document kinds."""
    line = str(policy.get("line_of_business") or "").upper()
    if line == "MOTOR":
        schedule = _schedule_source(policy, "COVERED.MOTOR_THIRD_PARTY",
                                    "the policy schedule's third-party cover entry")
        depreciation = _citation_for(policy, "CONDITION.DEPRECIATION_SCHEDULE",
                                     "the policy wording's depreciation schedule")
        # A police report is established by an incident record or a wording clause, never
        # by the third-party cover entry. It used to cite that entry, which said a
        # document requires a report when nothing read said so.
        report = _citation_for(policy, "CONDITION.POLICE_REPORT",
                               "the policy wording's claim procedure")
        # Same class of mislabel, one item along. A vehicle identifier is established by
        # the registration certificate itself or by a wording clause that names it, and
        # no mapped motor term does either — there is no mechanism for it in the
        # vocabulary today. This item used to carry `schedule`, the third-party COVER
        # entry with its quote and page, which read as a cover clause requiring a vehicle
        # identifier. It now reports the open question and names what would close it.
        registration = _missing_source("the vehicle registration certificate")
        return [
            {"item": "Policy schedule", "reason": "Identifies the motor policy and insured vehicle.",
             "source": schedule, "document_kind": "SCHEDULE", "packet_required": True},
            {"item": "Vehicle registration", "reason": "Records the vehicle identifier for the synthetic own-damage packet.",
             "source": registration, "document_kind": "RC", "packet_required": True},
            {"item": "Repair estimate or invoice", "reason": "Records the declared repair evidence used for the surveyor step.",
             "source": depreciation, "document_kind": "SUPPLIER_INVOICE", "packet_required": True},
            {"item": "Police report, if one exists", "reason": "A report is not assumed; add it only where the incident record or policy wording establishes it.",
             "source": report, "document_kind": "FIR", "packet_required": False},
        ]
    # CONDITION.REIMBURSEMENT, not CLAIM.REIMBURSEMENT: the first is what HEALTH.yaml
    # emits, and the second matched nothing — which is how four items came to share one
    # fabricated source. See _citation_for().
    procedure = _citation_for(policy, "CONDITION.REIMBURSEMENT",
                              "the policy wording's claim procedure")
    deadline = _citation_for(policy, "DEADLINE.CLAIM_INTIMATION",
                             "the policy wording's claim-notice clause")
    schedule = _schedule_source(policy, "COVERED.FAMILY_FLOATER",
                                "the policy schedule's cover entry")
    cashless = _citation_for(policy, "CONDITION.CASHLESS_NETWORK",
                             "the policy wording's cashless-network clause")
    mode_reason = cashless if mode == "CASHLESS" else procedure
    return [
        {"item": "Policy schedule", "reason": "Identifies the policy and insured member.",
         "source": schedule, "document_kind": "SCHEDULE", "packet_required": True},
        {"item": "Hospital discharge summary", "reason": "Records the admission and treatment documents to include.",
         "source": mode_reason, "document_kind": "HOSPITAL_DISCHARGE_SUMMARY", "packet_required": True},
        {"item": "Itemised hospital bills", "reason": "Provides the expense documents for the packet.",
         "source": procedure, "document_kind": "HOSPITAL_BILL", "packet_required": True},
        {"item": "Claim notice details", "reason": "Supports the timing record without asserting a consequence.",
         "source": deadline, "document_kind": None, "packet_required": False},
    ]


def deadline_for(policy: Mapping[str, Any], incident: datetime) -> dict[str, Any]:
    """Calculate only a documented policy timing in the policy jurisdiction's timezone."""
    if incident.tzinfo is None or incident.utcoffset() is None:
        raise ClaimValidationError("A deadline needs an incident time with an explicit UTC offset.")
    incident = incident.astimezone(_policy_timezone(policy))
    for item in _policy_items(policy):
        if item.get("mechanism_code") != "DEADLINE.CLAIM_INTIMATION":
            continue
        if item.get("state") != "DOCUMENTED" or item.get("value") in (None, ""):
            break
        try:
            value = int(str(item["value"]))
        except ValueError:
            break
        unit = str(item.get("unit") or "").lower()
        if unit == "hours":
            due = incident + timedelta(hours=value)
        elif unit == "days":
            due = incident + timedelta(days=value)
        elif unit == "working_days":
            due_date = jurisdiction.add_working_days(incident.date(), value, str(policy.get("country") or "GENERIC"))
            due = datetime.combine(due_date, incident.timetz())
        else:
            break
        return {"state": "DOCUMENTED", "due_at": due.isoformat(), "unit": unit,
                "value": str(value),
                "source": _citation_for(policy, "DEADLINE.CLAIM_INTIMATION",
                                        "the policy wording's claim-notice clause"),
                "notice": "This is the policy timing quoted below; VERIS does not state a consequence of delay."}
    return {"state": "INFORMATION_MISSING", "due_at": None, "required_document": "the policy wording",
            "notice": "No timing is calculated until the policy wording establishes it."}


def escalation_gate(policy: Mapping[str, Any], claim: Mapping[str, Any], *, now_at: str) -> dict[str, Any]:
    """Allow escalation only after a documented deadline and sourced first ladder step.

    The simulated clock may make a claim overdue, but it cannot fill an unsourced
    complaint ladder.  In that case the caller receives an abstention rather than a
    generic grievance action.
    """
    deadline = claim.get("deadline") if isinstance(claim.get("deadline"), Mapping) else {}
    if deadline.get("state") != "DOCUMENTED" or not deadline.get("due_at"):
        return {
            "output_state": "INFORMATION_MISSING", "eligible": False, "entry": None,
            "required_document": "a documented policy or regulatory claim timing",
            "notice": "No escalation is prepared until a documented claim timing establishes that it is overdue.",
        }
    try:
        due_at = datetime.fromisoformat(str(deadline["due_at"]))
        current_at = datetime.fromisoformat(str(now_at))
    except ValueError:
        return {
            "output_state": "INFORMATION_MISSING", "eligible": False, "entry": None,
            "required_document": "an explicit-offset policy timing and sandbox clock record",
            "notice": "No escalation is prepared until the timing records can be compared safely.",
        }
    if (due_at.tzinfo is None or due_at.utcoffset() is None
            or current_at.tzinfo is None or current_at.utcoffset() is None):
        return {
            "output_state": "INFORMATION_MISSING", "eligible": False, "entry": None,
            "required_document": "an explicit-offset policy timing and sandbox clock record",
            "notice": "No escalation is prepared until the timing records carry explicit offsets.",
        }
    if current_at <= due_at:
        return {
            "output_state": "DOCUMENTED", "eligible": False, "entry": None,
            "deadline_due_at": due_at.isoformat(),
            "notice": "The documented policy timing has not elapsed on the local synthetic clock.",
        }
    try:
        selected = jurisdiction.pack(str(policy.get("country") or "").upper())
    except jurisdiction.PackError:
        return {
            "output_state": "INFORMATION_MISSING", "eligible": False, "entry": None,
            "required_document": "a jurisdiction pack with a sourced complaint ladder",
            "notice": "No escalation is prepared until the policy jurisdiction has a sourced complaint ladder.",
        }
    first_step = selected.complaint_ladder[0] if selected.complaint_ladder else None
    if first_step is None or not first_step.owner_facing:
        return {
            "output_state": "INFORMATION_MISSING", "eligible": False, "entry": None,
            "required_document": "a primary-source first complaint-ladder step",
            "notice": "No escalation is prepared because the first complaint-ladder step is not yet sourced.",
        }
    return {
        "output_state": "DOCUMENTED", "eligible": True,
        "deadline_due_at": due_at.isoformat(), "entry": first_step.provenance(),
        "notice": "A cited overdue-claim escalation can be prepared for your confirmation; it makes no claim outcome decision.",
    }


def _deduction_citation(policy: Mapping[str, Any], mechanism: object) -> Optional[dict[str, Any]]:
    code = str(mechanism or "")
    if not code:
        return None
    for item in policy.get("health_items", []) if isinstance(policy.get("health_items"), list) else []:
        if not isinstance(item, Mapping) or item.get("mechanism_code") != code:
            continue
        citation = item.get("citation")
        if item.get("state") != "DOCUMENTED" or not isinstance(citation, Mapping) or not citation.get("quote"):
            return None
        return {
            "document": citation.get("document"), "page": citation.get("page"),
            "quote": citation.get("quote"), "source_url": citation.get("source_url"),
        }
    return None


def classify_deductions(policy: Mapping[str, Any], settlement: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Classify deductions only when a cited policy definition binds the result."""
    out = []
    for deduction in settlement.get("deductions", []) if isinstance(settlement.get("deductions"), list) else []:
        if not isinstance(deduction, Mapping):
            continue
        source_status = str(deduction.get("status") or "")
        citation = _deduction_citation(policy, deduction.get("evidence_mechanism"))
        if source_status == "SUPPORTED_BY_SCENARIO" and citation is not None:
            status = "SUPPORTED"
        elif (source_status == "NOT_FOUND_IN_SCENARIO_WORDING"
              and deduction.get("evidence_mode") == "ABSENCE_FROM_DEFINED_SCOPE"
              and citation is not None):
            status = "NOT_FOUND"
        else:
            status = "NEEDS_REVIEW"
        out.append({"id": str(deduction.get("id") or ""), "amount": str(deduction.get("amount") or ""),
                    "classification": status, "clause_quote": citation.get("quote") if citation else None,
                    "citation": citation})
    return out
