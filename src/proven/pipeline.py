"""End-to-end orchestration: ingest -> account -> extract -> resolve -> compute ->
grade -> gate -> persist -> Review Brief. Plus case lifecycle, the consent gate,
change events and the append-only portable record.

Two entry points, one code path:

  `run_case()`    the demo bundle — the real Chubb wording plus labelled synthetic
                  fixtures for the documents a real SME would supply.
  `run_bundle()`  documents a user uploaded. Same accounting, same extraction, same
                  rules, same gates. Nothing about the analysis is special-cased for
                  the demo, which is the only way the demo can be evidence of the
                  product.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

from . import (account, actions, brief, corpus, detect, events, extract, ingest,
               jurisdiction, resolve, rules)
from .db import connect, init_db, new_id
from .errors import BundleError, ConsentRequired, IngestError
from .ingest import DocStore, LoadedDoc
from .models import Finding, Provision
from .taxonomy import Taxonomy

# The published wordings the worked example reads, named by CORPUS ID rather than by
# path. These used to be hard-coded paths into a sibling research folder, which meant a
# clean clone could not run and nothing recorded what the documents were. They are
# third-party copyrighted material, so they live outside git; `corpus/manifest.yaml`
# holds the publisher, the source URL and the sha256, and `scripts/fetch_corpus.py`
# puts the bytes in place.
CHUBB_ID = "sg-chubb-selectplus-wording"
MSIG_ID = "sg-msig-sumo-wording"
LIBERTY_ID = "sg-liberty-smecare-wording"

# Resolved once at import so these stay plain `Path` objects: every caller does
# `.exists()`, `.read_bytes()` or `.name` on them, and an absent corpus must read as a
# missing file rather than raise on import. The corpus directory is immutable for the
# life of a process.
CHUBB_PDF = corpus.original_path(CHUBB_ID)
MSIG_TXT = corpus.text_path(MSIG_ID)
LIBERTY_TXT = corpus.text_path(LIBERTY_ID)

DEMO_SEARCHED = [
    "Chubb Select+ base wording (28 pp, all pages)",
    "Policy Schedule (fixture)",
    "CPF contribution record, WIC schedule, tenancy, accounts, ACRA profile, "
    "supplier invoice, endorsements 01 and 02 (fixtures)",
]
DEMO_NOT_SEARCHED = [
    "MSIG and Liberty full theft clauses — keyword-counted only, not read in full",
    "JTC lease insurance covenant — unread (trigger row, not a demo dependency)",
    "Business interruption section — not operative in the Chubb Lite variant",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── uploaded bundles ────────────────────────────────────────────────────────

@dataclass
class UploadedFile:
    """One file as it arrived. `declared_kind` is an owner HINT only: it selects the
    document kind, never the author_class, because author_class derives evidence
    class and a caller who could set it could manufacture a PROVEN finding."""

    filename: str
    data: bytes
    declared_kind: Optional[str] = None


@dataclass
class IngestedDoc:
    doc: LoadedDoc
    kind_detection: detect.Detection
    carrier_detection: Optional[detect.Detection] = None


def _validate_upload(files: list[UploadedFile]) -> None:
    if not files:
        raise BundleError(
            "No files were received. Attach at least one PDF — the policy wording "
            "is the one that unlocks the clause analysis.")
    if len(files) > ingest.MAX_FILES:
        raise BundleError(
            f"{len(files)} files received; the limit is {ingest.MAX_FILES} per "
            f"bundle. Upload the policy wording and schedule first.")
    total = sum(len(f.data) for f in files)
    if total > ingest.MAX_BUNDLE_BYTES:
        raise BundleError(
            f"The bundle is {total // (1024 * 1024)} MB. The limit is "
            f"{ingest.MAX_BUNDLE_BYTES // (1024 * 1024)} MB.")


def ingest_bundle(
    conn: sqlite3.Connection, business_id: str, files: list[UploadedFile],
    on_progress: Optional[Callable[[dict], None]] = None,
) -> tuple[DocStore, Optional[str], str, list[dict], "jurisdiction.AppliedJurisdiction"]:
    """Ingest an uploaded bundle. Returns (store, base_doc_id, carrier, per-file
    report, the jurisdiction the bundle is read under).

    Failures are per-file and non-fatal: one corrupt PDF in a bundle of six is
    reported against that file and the other five are still reviewed. A bundle in
    which NOTHING could be read raises, because a Brief built on zero documents
    would be a Brief about nothing.

    `on_progress` is an optional callback invoked with a plain dict as each file is
    read. It exists so the upload screen can tick files off as they are actually
    processed rather than animating a guess: the caller streams these events, and a
    tick therefore means the text layer came out, not that a timer fired. It is
    called synchronously on this thread and must not raise — see `_safe_progress`.
    """
    _validate_upload(files)
    store = DocStore()
    report: list[dict] = []
    pending: list[tuple[UploadedFile, list[str], detect.Detection]] = []
    emit = on_progress or (lambda _event: None)
    total = len(files)

    # Pass 1 — extract and identify. Nothing is written until the carrier is known,
    # because carrier is a document-level column and must be consistent.
    for index, f in enumerate(files):
        name = ingest.safe_filename(f.filename)
        # Emitted BEFORE the parse, not after, so a caller streaming these events can
        # show which file is being read while it is being read. The alternative — one
        # event per file on completion — cannot distinguish "reading file 3" from
        # "stalled on file 3", and those look identical to somebody waiting.
        emit({"event": "reading", "index": index, "total": total,
              "filename": name})
        try:
            pages = ingest.extract_pdf_pages_cached(f.data, name)
        except IngestError as exc:
            store.warn(name, exc.code, exc.message)
            report.append({"filename": name, "accepted": False,
                           "code": exc.code, "message": exc.message})
            emit({"event": "read", "index": index, "total": total,
                  "filename": name, "accepted": False,
                  "code": exc.code, "message": exc.message})
            continue
        declared = (f.declared_kind or "").strip().upper() or None
        if declared and declared in detect.DECLARABLE_KINDS:
            kind_det = detect.Detection(declared, "DECLARED",
                                        matched="declared by uploader")
        else:
            kind_det = detect.detect_kind(pages, name)
        pending.append((f, pages, kind_det))
        # The tick lands here: the text layer came out and the kind is known. The
        # document_id does not exist yet — it cannot, because carrier is a
        # document-level column and no row may be written before the vote below — so
        # this event deliberately does not carry one.
        emit({"event": "read", "index": index, "total": total,
              "filename": name, "accepted": True, "pages": len(pages),
              "kind": kind_det.value, "kind_confidence": kind_det.confidence})

    if not pending:
        raise BundleError(
            "None of the uploaded files could be read. Every one is listed with the "
            "reason. Native PDFs only — a photograph or a scan has no text layer.",
            detail=json.dumps(report))

    # Which country's documents these are — decided from every readable document, and
    # BEFORE any row is written, because the pack that applies is what says who authors
    # each kind of document, and author_class is a document-level column just as carrier
    # is. Nothing is guessed: a bundle that does not establish a country it has a pack
    # for is read under GENERIC.
    applied = jurisdiction.assess(
        [(ingest.safe_filename(f.filename), pages) for f, pages, _k in pending])
    pack = applied.pack

    # Carrier is decided by the insurer-authored documents only, and a tie or a
    # blank is UNKNOWN rather than a guess.
    carrier_votes: dict[str, int] = {}
    carrier_evidence: Optional[detect.Detection] = None
    for _f, pages, kind_det in pending:
        if detect.author_class_for(kind_det.value, pack) != "INSURER":
            continue
        det = detect.detect_carrier(pages)
        if det.value != detect.UNKNOWN_CARRIER:
            carrier_votes[det.value] = carrier_votes.get(det.value, 0) + 1
            if carrier_evidence is None or det.confidence == "HIGH":
                carrier_evidence = det
    carrier = detect.UNKNOWN_CARRIER
    if carrier_votes:
        ranked = sorted(carrier_votes.items(), key=lambda kv: -kv[1])
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            carrier = ranked[0][0]

    # Pass 2 — write.
    base_doc_id: Optional[str] = None
    for f, pages, kind_det in pending:
        kind = kind_det.value
        author_class = detect.author_class_for(kind, pack)
        name = ingest.safe_filename(f.filename)
        doc_carrier = carrier if author_class == "INSURER" else None
        doc_id, _ = ingest.ingest_pdf_bytes(
            conn, f.data, name, kind if kind != "UNKNOWN" else "UNKNOWN",
            author_class, business_id, doc_carrier)
        # An uploaded endorsement carries no structured fields, so read its effect
        # from its text into the same shape a fixture endorsement has. Without this an
        # uploaded endorsement produced no finding: the conflicting-sub-limit
        # escalation and the illegible-scan abstention only ever fired on fixtures.
        doc_data = account.parse_endorsement(pages) if kind == "ENDORSEMENT" else {}
        store.add(LoadedDoc(doc_id, kind, author_class, data=doc_data,
                            carrier=doc_carrier, pages=pages, filename=name))
        if kind == "BASE_WORDING" and base_doc_id is None:
            base_doc_id = doc_id
        report.append({
            "filename": name, "accepted": True, "document_id": doc_id,
            "kind": kind, "kind_confidence": kind_det.confidence,
            "kind_matched_on": kind_det.matched,
            "author_class": author_class, "pages": len(pages),
            "carrier": doc_carrier,
        })
        emit({"event": "stored", "filename": name, "document_id": doc_id,
              "kind": kind, "author_class": author_class,
              "pages": len(pages), "carrier": doc_carrier})
        if kind == "UNKNOWN":
            store.warn(name, "KIND_UNKNOWN",
                       f"{name} could not be identified, so it is treated as "
                       f"owner-authored (the weakest evidence class). Set its type "
                       f"and re-upload to have it read properly.")

    if carrier == detect.UNKNOWN_CARRIER and base_doc_id is not None:
        store.warn(
            "policy wording", "CARRIER_UNKNOWN",
            "The carrier could not be identified from the wording, so no verified "
            "clause map applies. Provisions are still detected and quoted, but no "
            "finding is built on this wording's clause references.")
    return store, base_doc_id, carrier, report, applied


def run_bundle(
    files: list[UploadedFile],
    db_path: str = ":memory:",
    business_id: str = "uploaded-bundle",
    business_name: str = "Your business",
    conn: Optional[sqlite3.Connection] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run the full pipeline over documents a user supplied.

    `on_progress` is forwarded to `ingest_bundle` and additionally receives the
    `analysing` phase, so a streaming caller can distinguish "still reading your
    files" from "reading finished, now working out what it means". Those take
    noticeably different amounts of time and conflating them is what makes a progress
    indicator feel like it has stopped.
    """
    conn = conn or connect(db_path)
    init_db(conn)
    case_id = open_case(conn, business_id)
    store, base_doc_id, carrier, report, applied = ingest_bundle(
        conn, business_id, files, on_progress=on_progress)
    if on_progress is not None:
        on_progress({"event": "analysing", "documents": len(store.documents()),
                     "carrier_detected": carrier})
    result = _analyse(
        conn=conn, case_id=case_id, business_id=business_id,
        business_name=business_name, carrier=carrier, store=store,
        base_doc_id=base_doc_id, synthetic=False,
        searched=_searched_from_store(store, base_doc_id),
        not_searched=_not_searched_from_store(store, base_doc_id, carrier, applied),
        applied=applied)
    result["brief"]["intake"] = {
        "files": report,
        "warnings": store.warnings,
        "carrier_detected": carrier,
        # The same record the Brief carries: the country detected, how sure, the reasons
        # both ways, the pack applied and what it did not apply.
        "jurisdiction": result["brief"]["jurisdiction"],
        "accepted_count": sum(1 for r in report if r.get("accepted")),
        "rejected_count": sum(1 for r in report if not r.get("accepted")),
    }
    result["intake"] = report
    return result


def _searched_from_store(store: DocStore, base_doc_id: Optional[str]) -> list[str]:
    lines = []
    for doc in store.documents():
        pages = len(doc.pages) or 1
        lines.append(
            f"{doc.kind.replace('_', ' ').title()} — {doc.filename} "
            f"({pages} page{'s' if pages != 1 else ''}, all pages)")
    if not lines:
        lines.append("Nothing — no document could be read.")
    return lines


def _not_searched_from_store(
    store: DocStore, base_doc_id: Optional[str], carrier: str,
    applied: "jurisdiction.AppliedJurisdiction",
) -> list[str]:
    """The falsifiable coverage claim for an uploaded bundle: state precisely what was
    absent, so a reader can tell silence from absence."""
    out: list[str] = []
    if base_doc_id is None:
        out.append(
            "No policy wording was provided, so no clause of the policy was read. "
            "Nothing here is a statement about what the policy covers.")
    if "SCHEDULE" not in store.kinds():
        out.append(
            "No policy schedule was provided, so sums insured, deductibles and "
            "elected extensions were not read.")
    # Which supporting documents are worth naming is a property of the country: a CPF
    # record means something in Singapore and nothing under GENERIC. The pack lists them,
    # in the order it wants them asked for.
    out.extend(applied.pack.absent_notes(store.kinds()))
    if not applied.pack_available:
        out.append(
            "Country-specific rules were not run: no jurisdiction pack applies to these "
            "documents (see the jurisdiction notice).")
    if carrier == detect.UNKNOWN_CARRIER and base_doc_id is not None:
        out.append(
            "This carrier's clause references have not been read by a human, so no "
            "clause-cited finding was produced from the wording.")
    out.append("OCR is not used. Any page without a text layer was not read.")
    return out


# ── case lifecycle ──────────────────────────────────────────────────────────

def open_case(conn: sqlite3.Connection, business_id: str) -> str:
    case_id = new_id()
    conn.execute(
        'INSERT INTO "case" (id, business_id, opened_at, state) VALUES (?,?,?,?)',
        (case_id, business_id, _now(), "OPEN"))
    _audit(conn, "system", "OPEN_CASE", "case", case_id)
    conn.commit()
    return case_id


def _audit(conn, actor, action, object_type, object_id):
    conn.execute(
        "INSERT INTO audit (id, actor, action, object_type, object_id, at) "
        "VALUES (?,?,?,?,?,?)",
        (new_id(), actor, action, object_type, object_id, _now()))


def _persist_finding(conn: sqlite3.Connection, case_id: str, f: Finding) -> str:
    fid = new_id()
    conn.execute(
        "INSERT INTO finding (id, case_id, rule_id, rule_version, output_state, "
        "evidence_class, headline, resolution_path, detail_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (fid, case_id, f.rule_id, f.rule_version, f.output_state, f.evidence_class,
         f.headline, f.resolution_path, json.dumps(f.detail, default=str), _now()))
    for o in f.operands:
        oid = new_id()
        conn.execute(
            "INSERT INTO operand (id, case_id, name, value, unit, span_id, "
            "evidence_class) VALUES (?,?,?,?,?,?,?)",
            (oid, case_id, o.name, o.value, o.unit, o.span_id, o.evidence_class))
        conn.execute(
            "INSERT INTO finding_operand (finding_id, operand_id) VALUES (?,?)",
            (fid, oid))
    for s in f.spans:
        conn.execute(
            "INSERT INTO finding_span (finding_id, span_id, role) VALUES (?,?,?)",
            (fid, s.id, "EVIDENCE"))
    return fid


def _analyse(
    conn: sqlite3.Connection, case_id: str, business_id: str, business_name: str,
    carrier: str, store: DocStore, base_doc_id: Optional[str], synthetic: bool,
    searched: list[str], not_searched: list[str],
    applied: "jurisdiction.AppliedJurisdiction",
) -> dict:
    """The shared analysis path. Demo and upload differ only in how the store was
    filled."""
    if base_doc_id is not None:
        acc = account.account_base_wording(conn, base_doc_id, carrier)
        # The set a finding may cite is the HUMAN-VERIFIED map for this carrier, not
        # the set the detector segmented. See account.mapped_codes for why.
        verified = account.mapped_codes(carrier)
    else:
        acc = account.empty_accounting(carrier)
        verified = set()

    # Precedence — read the construction clause FIRST (spec §6.2). Chubb has none,
    # so precedence is asserted by market convention and that is stated on record.
    endorsement_provisions = account.account_endorsements(conn, store)
    provisions = (_load_provisions(conn, base_doc_id) if base_doc_id else []) \
        + endorsement_provisions
    resolution = resolve.resolve(
        provisions, has_construction_clause=acc.has_construction_clause)

    facts = extract.extract_facts(store, pack=applied.pack)

    # Only the rules the pack that applies supports, with their figures filled in from it.
    # Another country's rule is not run, and is named in the jurisdiction record instead.
    bound, not_applied, dropped = rules.bind(rules.load_rules(), applied.pack)
    ctx = rules.Context(
        conn=conn, case_id=case_id, store=store, base_doc_id=base_doc_id,
        carrier=carrier, taxonomy=Taxonomy.load(), rules=bound,
        facts=facts, loss_scenario=store.loss_scenario,
        unresolved_codes=set(resolution.unresolved), verified_codes=verified,
        pack=applied.pack)
    findings = rules.evaluate_all(ctx)
    for f in findings:
        _persist_finding(conn, case_id, f)

    # Portable record — append only established (third-party/authority/insurer)
    # facts. Never a finding, never a self-declared figure, never a black mark.
    _seed_record(conn, business_id, facts, base_doc_id, carrier)
    conn.commit()

    if facts.notes:
        not_searched = list(not_searched) + facts.notes

    the_brief = brief.build_brief(
        business_name=business_name, carrier=carrier, accounting=acc,
        findings=findings, searched=searched, not_searched=not_searched,
        synthetic=synthetic, precedence_note=resolution.note,
        facts=facts.as_dict())
    the_brief["case_id"] = case_id
    the_brief["business_id"] = business_id
    the_brief["events"] = recorded_events(conn, case_id)
    # Which country's law the case was read under, why, and what that left out. The
    # derived screens read the pack from this same record (`jurisdiction.pack_of`), so the
    # Brief and every screen built over it cannot disagree about the country.
    jurisdiction_view = applied.as_dict(rules_not_applied=not_applied,
                                        sentences_dropped=dropped)
    the_brief["jurisdiction"] = jurisdiction_view

    # ── the action loop ──────────────────────────────────────────────────────
    # Prepare (never send) a correction for every finding that maps to one. This is what
    # makes the product close a gap rather than describe one. Two properties matter:
    #
    #   IDEMPOTENT.  `raise_for_case` keys on (rule_id, interest), so re-running the
    #     pipeline — which happens on every cold request — cannot produce a second copy of
    #     a request an adviser is already working.
    #   NON-FATAL.   An action is downstream of the analysis, so a failure here must not
    #     cost the user their brief. It degrades to no actions and says so.
    actions.init(conn)
    try:
        prepared = actions.raise_for_case(
            conn, case_id, the_brief.get("findings") or [], facts=facts.as_dict())
        the_brief["actions_prepared"] = len(prepared)
    except Exception as exc:                                   # pragma: no cover
        the_brief["actions_prepared"] = 0
        the_brief["action_note"] = (
            f"No corrections were prepared for this case ({type(exc).__name__}). The "
            f"findings above are unaffected.")
    # base_doc_id and carrier travel with the state because the coverage-semantics
    # endpoints (alignment, drift) operate on the wording itself rather than on the
    # assembled brief, and re-deriving them from the database on each request would
    # duplicate the detection logic that already ran here.
    return {"brief": the_brief, "conn": conn, "case_id": case_id,
            "business_id": business_id, "findings": findings,
            "resolution": resolution, "facts": facts, "store": store,
            "base_doc_id": base_doc_id, "carrier": carrier,
            "jurisdiction": jurisdiction_view}


def run_case(
    db_path: str = ":memory:",
    business_id: str = "hock-lee-foods",
    business_name: str = "Hock Lee Foods Pte Ltd",
    carrier: str = "CHUBB",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Run the demo bundle and return the Review Brief plus the live connection.

    Needs the published base wording, which is NOT in this repository: it is a third
    party's copyrighted publication and lives in the document corpus. If it is absent
    this raises `corpus.CorpusMissing` naming the document and the command that fetches
    it, rather than letting the ingest layer report a generic "could not be opened" about
    a path the reader has no reason to recognise.

    Deliberately an exception and not a degraded case. A worked example assembled from
    the fixtures alone would still render — with three fifths of its findings silently
    absent, because those findings are clauses in the wording. A quieter demo that has
    lost most of its evidence is the worst of the three outcomes.
    """
    conn = conn or connect(db_path)
    init_db(conn)
    case_id = open_case(conn, business_id)

    corpus.require_original(CHUBB_ID)
    base_doc_id = ingest.ingest_pdf(
        conn, CHUBB_PDF, kind="BASE_WORDING", author_class="INSURER",
        business_id=business_id, carrier=carrier)
    store = ingest.ingest_fixtures(conn, business_id)
    # The wording is part of the store too, so extraction and the searched list see
    # the same set of documents the rules do.
    wording_pages = [ingest.page_text(conn, base_doc_id, p)
                     for p in range(1, _page_count(conn, base_doc_id) + 1)]
    store.add(LoadedDoc(base_doc_id, "BASE_WORDING", "INSURER", data={},
                        carrier=carrier, pages=wording_pages,
                        filename=CHUBB_PDF.name))

    # Detected, not declared, even for the worked example: the wording's own
    # governing-law clause is what makes this a Singapore case.
    return _analyse(
        conn=conn, case_id=case_id, business_id=business_id,
        business_name=business_name, carrier=carrier, store=store,
        base_doc_id=base_doc_id, synthetic=True,
        searched=DEMO_SEARCHED, not_searched=DEMO_NOT_SEARCHED,
        applied=jurisdiction.assess(store.documents()))


def _page_count(conn: sqlite3.Connection, document_id: str) -> int:
    row = conn.execute(
        "SELECT page_count FROM document WHERE id=?", (document_id,)).fetchone()
    return int(row["page_count"] or 0) if row else 0


def _load_provisions(conn: sqlite3.Connection, document_id: str) -> list[Provision]:
    rows = conn.execute(
        "SELECT * FROM provision WHERE document_id=?", (document_id,)).fetchall()
    return [Provision(
        id=r["id"], document_id=r["document_id"], span_id=r["span_id"],
        clause_ref=r["clause_ref"], taxonomy_code=r["taxonomy_code"],
        status=r["status"], authority_rank=r["authority_rank"]) for r in rows]


# Which extracted facts are eligible for the portable record, and under what name.
# ONLY facts whose basis document was authored by somebody other than the business.
# A self-declared figure is a case fact, never a record fact.
_RECORD_FACTS: tuple[tuple[str, str, Optional[str]], ...] = (
    ("contributor_count", "employee_headcount", None),
    ("primary_ssic", "primary_ssic_2025", None),
    ("permitted_use", "permitted_use", None),
    ("invoice_equipment_value", "equipment_item_value", "SGD"),
)


def _seed_record(conn, business_id, facts, base_doc_id, carrier) -> None:
    """Derive the portable record from established facts only. Each fact points at
    the third-party / authority / insurer document that is its basis, and carries
    that document's real author_class rather than an assumed one."""
    for fact_name, attribute, unit in _RECORD_FACTS:
        fact = facts.get(fact_name)
        if not fact.established or fact.author_class == "SELF":
            continue
        append_record_fact(
            conn, business_id, attribute, str(fact.value), fact.doc.id,
            fact.author_class, unit=unit)
    if base_doc_id and carrier and carrier != detect.UNKNOWN_CARRIER:
        append_record_fact(
            conn, business_id, "policy_carrier", carrier, base_doc_id, "INSURER")


# ── consent gate ────────────────────────────────────────────────────────────

def grant_disclosure(
    conn: sqlite3.Connection, case_id: str, recipient: str, fields: list[str],
) -> str:
    """No Brief moves without an affirmative act, granular by document. Writes a
    disclosure row and an audit row. `fields` is an access control list, enforced in
    `disclosed_brief`, not a label."""
    did = new_id()
    conn.execute(
        "INSERT INTO disclosure (id, case_id, recipient, fields, granted_at) "
        "VALUES (?,?,?,?,?)",
        (did, case_id, recipient, json.dumps(sorted(set(fields))), _now()))
    _audit(conn, recipient, "GRANT_DISCLOSURE", "case", case_id)
    conn.commit()
    return did


def revoke_disclosure(
    conn: sqlite3.Connection, case_id: str, recipient: Optional[str] = None,
) -> int:
    """Revoke live disclosures for this case (optionally for one recipient) and
    return how many were revoked. Revocation is what makes the consent claim true:
    a grant that cannot be withdrawn is not consent, it is a transfer."""
    if recipient:
        rows = conn.execute(
            "SELECT id FROM disclosure WHERE case_id=? AND recipient=? "
            "AND revoked_at IS NULL", (case_id, recipient)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM disclosure WHERE case_id=? AND revoked_at IS NULL",
            (case_id,)).fetchall()
    for r in rows:
        conn.execute("UPDATE disclosure SET revoked_at=? WHERE id=?",
                     (_now(), r["id"]))
    _audit(conn, recipient or "owner", "REVOKE_DISCLOSURE", "case", case_id)
    conn.commit()
    return len(rows)


def disclosure_granted(conn: sqlite3.Connection, case_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM disclosure WHERE case_id=? AND granted_at IS NOT NULL "
        "AND revoked_at IS NULL LIMIT 1", (case_id,)).fetchone()
    return row is not None


def live_disclosures(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, recipient, fields, granted_at FROM disclosure WHERE case_id=? "
        "AND granted_at IS NOT NULL AND revoked_at IS NULL ORDER BY granted_at",
        (case_id,)).fetchall()
    out = []
    for r in rows:
        try:
            fields = json.loads(r["fields"])
        except (json.JSONDecodeError, TypeError):
            fields = []
        out.append({"disclosure_id": r["id"], "recipient": r["recipient"],
                    "fields": fields, "granted_at": r["granted_at"]})
    return out


def granted_fields(
    conn: sqlite3.Connection, case_id: str, recipient: str,
) -> Optional[set[str]]:
    """The union of document kinds this recipient may see, or None when no live
    disclosure names them."""
    live = [d for d in live_disclosures(conn, case_id)
            if d["recipient"] == recipient]
    if not live:
        return None
    out: set[str] = set()
    for d in live:
        out.update(str(f).strip().upper() for f in d["fields"])
    return out


def _document_kinds(conn: sqlite3.Connection, span_ids: list[str]) -> set[str]:
    if not span_ids:
        return set()
    marks = ",".join("?" * len(span_ids))
    rows = conn.execute(
        f"SELECT DISTINCT d.kind AS kind FROM span s JOIN document d "
        f"ON d.id = s.document_id WHERE s.id IN ({marks})", span_ids).fetchall()
    return {r["kind"] for r in rows}


def disclosed_brief(
    conn: sqlite3.Connection, case_id: str, the_brief: dict, recipient: str,
) -> dict:
    """The adviser's view. THIS is the consent gate, and it is an access control
    rather than a checkbox.

    Refuses entirely when no live disclosure names the recipient, and otherwise
    returns only the findings evidenced by the document kinds actually granted.
    Findings that were withheld are COUNTED and their required document kinds named,
    so the adviser can ask for what they need instead of being shown a silently
    shortened list. Every access writes an audit row: a pull, logged.
    """
    allowed = granted_fields(conn, case_id, recipient)
    if allowed is None:
        raise ConsentRequired(
            f"No live disclosure names '{recipient}'. The business must grant "
            f"disclosure, naming the documents, before any Brief moves.")

    kept: list[dict] = []
    withheld_findings = 0
    # Counts findings that need a given document kind, so a finding needing two
    # undisclosed documents appears under both — but `withheld_count` below counts
    # FINDINGS, not (finding, document) pairs. Summing the map would overstate.
    needed_but_not_disclosed: dict[str, int] = {}
    for f in the_brief.get("findings", []):
        span_ids = [s.get("span_id") for s in f.get("spans", []) if s.get("span_id")]
        kinds = _document_kinds(conn, span_ids)
        if kinds and not kinds <= allowed:
            withheld_findings += 1
            for k in sorted(kinds - allowed):
                needed_but_not_disclosed[k] = needed_but_not_disclosed.get(k, 0) + 1
            continue
        kept.append(f)

    accounting_visible = "BASE_WORDING" in allowed
    _audit(conn, recipient, "READ_DISCLOSED_BRIEF", "case", case_id)
    conn.commit()
    return {
        "recipient": recipient,
        "business": the_brief.get("business"),
        "carrier": the_brief.get("carrier"),
        "synthetic": the_brief.get("synthetic"),
        "synthetic_notice": the_brief.get("synthetic_notice"),
        "disclosed_fields": sorted(allowed),
        "accounting": the_brief.get("accounting") if accounting_visible else None,
        "accounting_withheld": not accounting_visible,
        "precedence_note": the_brief.get("precedence_note")
        if accounting_visible else None,
        "findings": kept,
        "findings_withheld_count": withheld_findings,
        "withheld_pending_documents": dict(sorted(
            needed_but_not_disclosed.items())),
        "searched": the_brief.get("searched", []),
        "not_searched": the_brief.get("not_searched", []),
        "notice": ("This is a consent-scoped view. Findings evidenced by documents "
                   "that were not disclosed are withheld and counted, never "
                   "silently omitted. Every read is logged to the audit trail."),
    }


# ── change events ───────────────────────────────────────────────────────────

def record_change_events(
    conn: sqlite3.Connection, case_id: str, codes: list[str],
) -> list[str]:
    """Record owner-asserted change events. Unknown codes are dropped: the event
    vocabulary is closed, so an arbitrary string cannot become a stored fact."""
    valid = events.validate(codes)
    existing = set(recorded_events(conn, case_id))
    for code in valid:
        if code in existing:
            continue
        conn.execute(
            "INSERT INTO change_event (id, case_id, event_code, recorded_at) "
            "VALUES (?,?,?,?)", (new_id(), case_id, code, _now()))
        _audit(conn, "owner", "RECORD_CHANGE_EVENT", "case", case_id)
    conn.commit()
    return valid


def clear_change_events(conn: sqlite3.Connection, case_id: str) -> None:
    conn.execute("DELETE FROM change_event WHERE case_id=?", (case_id,))
    conn.commit()


def recorded_events(conn: sqlite3.Connection, case_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT event_code FROM change_event WHERE case_id=? ORDER BY recorded_at",
        (case_id,)).fetchall()
    return [r["event_code"] for r in rows]


def annotate_events(the_brief: dict, codes: list[str]) -> dict:
    """Mark which findings the recorded events point at. An event never changes a
    finding's state, class or content — it changes which ones are foregrounded, and
    that is the whole of its authority."""
    relevant = events.rules_for(codes)
    the_brief["events"] = events.validate(codes)
    the_brief["event_relevant_rules"] = sorted(relevant)
    the_brief["event_requested_documents"] = events.documents_for(codes)
    for f in the_brief.get("findings", []):
        f["event_relevant"] = f.get("rule_id") in relevant
    return the_brief


# ── case deletion ───────────────────────────────────────────────────────────

def delete_case(conn: sqlite3.Connection, case_id: str) -> None:
    """Deleting a case removes every finding (and its operands) — the negative
    findings live only under the case. record_fact is untouched: it is portable and
    has nowhere to store a black mark."""
    fids = [r["id"] for r in conn.execute(
        "SELECT id FROM finding WHERE case_id=?", (case_id,)).fetchall()]
    for fid in fids:
        conn.execute("DELETE FROM finding_operand WHERE finding_id=?", (fid,))
        conn.execute("DELETE FROM finding_span WHERE finding_id=?", (fid,))
    conn.execute("DELETE FROM finding WHERE case_id=?", (case_id,))
    conn.execute("DELETE FROM operand WHERE case_id=?", (case_id,))
    # Actions too. This build already shipped the defect where deletion cleared the brief
    # and left three other views serving the deleted business's data; an action list is the
    # worst version of that mistake because it names a business, a figure and a request.
    try:
        actions.delete_for_case(conn, case_id)
    except sqlite3.OperationalError:
        pass          # action table not created on this connection; nothing to remove
    # A deleted case cannot keep disclosing. Revoking on delete is what stops a live
    # grant outliving the data it was granted over.
    conn.execute("UPDATE disclosure SET revoked_at=? WHERE case_id=? "
                 "AND revoked_at IS NULL", (_now(), case_id))
    conn.execute('UPDATE "case" SET deleted_at=?, state=? WHERE id=?',
                 (_now(), "DELETED", case_id))
    _audit(conn, "system", "DELETE_CASE", "case", case_id)
    conn.commit()


def count_findings(conn: sqlite3.Connection, case_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM finding WHERE case_id=?", (case_id,)
    ).fetchone()["c"]


# ── portable record (append-only, hash-chained, established facts only) ──────

def _record_payload(
    seq: int, prev_hash: str, business_id: str, attribute: str, value: str,
    unit: Optional[str], basis_document_id: str, author_class: str,
    as_at_date: Optional[str], appended_at: str,
) -> str:
    """The exact bytes that are hashed. ONE definition, used by both the writer and
    the verifier, so an exported record can be re-verified by a third party and the
    two implementations cannot drift apart.

    `seq` is inside the payload so reordering the facts breaks the hashes as well as
    the links — two independent detections of the same tampering.
    """
    return "|".join([str(seq), prev_hash, business_id, attribute, value, str(unit),
                     basis_document_id, author_class, str(as_at_date), appended_at])


def append_record_fact(
    conn: sqlite3.Connection, business_id: str, attribute: str, value: str,
    basis_document_id: str, author_class: str, unit: Optional[str] = None,
    basis_span_id: Optional[str] = None, as_at_date: Optional[str] = None,
) -> str:
    """Append one established fact and extend the hash chain.

    Ordering is by the explicit `seq` counter, never by `appended_at`: several facts
    appended inside one clock tick share a timestamp, and ordering by a value that
    can tie made the writer link to an arbitrary predecessor and corrupt the chain.
    """
    row = conn.execute(
        "SELECT seq, hash FROM record_fact WHERE business_id=? "
        "ORDER BY seq DESC LIMIT 1", (business_id,)).fetchone()
    prev_hash = row["hash"] if row else ""
    seq = (int(row["seq"]) + 1) if row else 1
    rid = new_id()
    appended_at = _now()
    payload = _record_payload(seq, prev_hash, business_id, attribute, value, unit,
                              basis_document_id, author_class, as_at_date,
                              appended_at)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO record_fact (id, business_id, seq, attribute, value, unit, "
        "basis_document_id, basis_span_id, author_class, as_at_date, appended_at, "
        "prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, business_id, seq, attribute, value, unit, basis_document_id,
         basis_span_id, author_class, as_at_date, appended_at, prev_hash, h))
    conn.commit()
    return rid


def count_record_facts(conn: sqlite3.Connection, business_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM record_fact WHERE business_id=?", (business_id,)
    ).fetchone()["c"]


RECORD_EXPORT_VERSION = 1
RECORD_EXPORT_FORMAT = "proven.record.v1"


def export_record(conn: sqlite3.Connection, business_id: str) -> dict:
    """The portable record as a self-contained, self-verifying document.

    Portability was previously a claim about an in-memory table that vanished on
    restart. This is the artefact that makes the claim true: every field needed to
    recompute the hash chain travels with it, plus the basis document's filename and
    sha256 so a recipient can tie a fact to the file it came from. `verify_record()`
    below re-verifies an export with no access to this database, which is the
    property "portable" has to mean.
    """
    rows = conn.execute(
        "SELECT rf.*, d.filename AS basis_filename, d.sha256 AS basis_sha256, "
        "d.kind AS basis_kind FROM record_fact rf "
        "LEFT JOIN document d ON d.id = rf.basis_document_id "
        "WHERE rf.business_id=? ORDER BY rf.seq",
        (business_id,)).fetchall()
    facts = [{
        "seq": r["seq"],
        "attribute": r["attribute"], "value": r["value"], "unit": r["unit"],
        "author_class": r["author_class"], "as_at_date": r["as_at_date"],
        "appended_at": r["appended_at"], "prev_hash": r["prev_hash"],
        "hash": r["hash"], "basis_document_id": r["basis_document_id"],
        "basis_document_filename": r["basis_filename"],
        "basis_document_sha256": r["basis_sha256"],
        "basis_document_kind": r["basis_kind"],
    } for r in rows]
    doc = {
        "format": RECORD_EXPORT_FORMAT,
        "version": RECORD_EXPORT_VERSION,
        "business_id": business_id,
        "exported_at": _now(),
        "hash_payload": ("sha256 of seq|prev_hash|business_id|attribute|value|unit|"
                         "basis_document_id|author_class|as_at_date|appended_at, "
                         "joined with '|', where a null unit or as_at_date is the "
                         "literal string 'None'"),
        "notice": ("Established facts only. There is deliberately no breach, "
                   "violation, deficiency or score field in this format, and adding "
                   "one would break the chain for every existing record."),
        "fact_count": len(facts),
        "facts": facts,
    }
    doc["verification"] = verify_record(doc)
    return doc


def verify_record(exported: dict) -> dict:
    """Re-verify an exported record from the export alone.

    Recomputes every hash and checks every link. Returns which fact broke and how,
    rather than a bare boolean — an integrity claim a recipient cannot localise is
    not much of a claim.
    """
    facts = exported.get("facts") or []
    business_id = exported.get("business_id", "")
    problems: list[dict] = []
    prev_hash = ""
    expected_seq = 1
    for i, f in enumerate(facts):
        if f.get("prev_hash", "") != prev_hash:
            problems.append({"index": i, "attribute": f.get("attribute"),
                             "problem": "CHAIN_BROKEN",
                             "expected_prev_hash": prev_hash,
                             "found_prev_hash": f.get("prev_hash")})
        seq = f.get("seq", expected_seq)
        if seq != expected_seq:
            problems.append({"index": i, "attribute": f.get("attribute"),
                             "problem": "SEQUENCE_GAP",
                             "expected_seq": expected_seq, "found_seq": seq})
        payload = _record_payload(
            seq, f.get("prev_hash", "") or "", business_id, f.get("attribute", ""),
            f.get("value", ""), f.get("unit"), f.get("basis_document_id", ""),
            f.get("author_class", ""), f.get("as_at_date"),
            f.get("appended_at", ""))
        recomputed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if recomputed != f.get("hash"):
            problems.append({"index": i, "attribute": f.get("attribute"),
                             "problem": "HASH_MISMATCH",
                             "recomputed": recomputed, "stored": f.get("hash")})
        prev_hash = f.get("hash", "")
        expected_seq = seq + 1
    return {"chain_valid": not problems, "fact_count": len(facts),
            "problems": problems,
            "format_recognised": exported.get("format") == RECORD_EXPORT_FORMAT}


# ── cross-carrier comparison — "the pitch, live" ────────────────────────────
# The same average rule, run against three real wordings, produces three different
# answers, decided entirely by what each document says. Chubb and MSIG state no
# formula (MARKET_CONVENTION); Liberty states it verbatim (PROVEN). Chubb bites
# below 85%; MSIG and Liberty bite at the first dollar of shortfall. This is
# computed from per-carrier taxonomy facts plus, for Liberty, a real anchored span
# in its wording — not asserted from a slide.

_COMPARE_SUM_INSURED = Decimal("100000")
_COMPARE_VALUE_AT_RISK = Decimal("150000")
_COMPARE_LOSS = Decimal("60000")
_LIBERTY_FORMULA_ANCHOR = "such proportion of the loss"

# The comparison is a pure function of three immutable files and three constants, so
# it is computed once per process. It cost ~4s per call and was on the initial page
# load, which is a four-second blank panel every time anyone opens the demo.
_COMPARE_CACHE: dict[str, dict] = {}


def compare_carriers(business_id: str = "cross-carrier-demo") -> dict:
    """Ingest all three wordings and run the identical average scenario against
    each, returning the divergent results with a real Liberty formula span."""
    cached = _COMPARE_CACHE.get(business_id)
    if cached is not None:
        return cached
    result = _compare_carriers_uncached(business_id)
    _COMPARE_CACHE[business_id] = result
    return result


def _compare_carriers_uncached(business_id: str) -> dict:
    from decimal import Decimal as D

    from .arithmetic import settle

    conn = connect(":memory:")
    try:
        init_db(conn)
        tax = Taxonomy.load()

        # Through the corpus's read-time verification rather than `path.exists()`. An
        # absent wording leaves its carrier out of the comparison, as before; a present
        # wording whose bytes are not the manifest's raises `CorpusDrifted` instead of
        # being compared and cited as the published text.
        chubb_pdf = corpus.present_original(CHUBB_ID)
        msig_txt = corpus.present_text(MSIG_ID)
        liberty_txt = corpus.present_text(LIBERTY_ID)
        chubb = ingest.ingest_pdf(
            conn, chubb_pdf, "BASE_WORDING", "INSURER", business_id,
            carrier="CHUBB") if chubb_pdf else None
        msig = ingest.ingest_text_wording(
            conn, msig_txt, "BASE_WORDING", "INSURER", business_id, carrier="MSIG") \
            if msig_txt else None
        liberty = ingest.ingest_text_wording(
            conn, liberty_txt, "BASE_WORDING", "INSURER", business_id,
            carrier="LIBERTY") if liberty_txt else None

        rows = []
        for carrier, doc_id in (("CHUBB", chubb), ("MSIG", msig),
                                ("LIBERTY", liberty)):
            if doc_id is None:
                continue
            threshold = tax.average_threshold(carrier)
            formula_stated = tax.average_formula_stated(carrier)
            formula_span = None
            formula_quote = None
            if formula_stated:
                span = account.find_clause_span(
                    conn, doc_id, _LIBERTY_FORMULA_ANCHOR)
                if span is not None:
                    formula_span = span.id
                    formula_quote = span.quoted_text
            s = settle(loss=_COMPARE_LOSS, sum_insured=_COMPARE_SUM_INSURED,
                       value_at_risk=_COMPARE_VALUE_AT_RISK,
                       threshold_pct=threshold, sub_limit=None, deductible=D("0"),
                       formula_span=formula_span)
            # A carrier can carry more than one average provision. Liberty carries
            # two, on different bases, with different thresholds. Reporting only one
            # would repeat the mistake this comparison exists to expose.
            candidates = tax.average_instances_for(carrier)
            ambiguous = tax.average_basis_ambiguous(carrier)
            if len(candidates) > 1:
                threshold_label = (
                    f"{len(candidates)} provisions, basis-dependent: " +
                    "; ".join(
                        f"{c.get('settlement_basis', 'ANY').lower()} "
                        + ("no threshold" if c.get("threshold_pct") is None
                           else f"{Decimal(str(c['threshold_pct'])) * 100:.0f}%")
                        for c in candidates))
            elif threshold is None:
                threshold_label = "none — any shortfall bites"
            else:
                threshold_label = f"{Decimal(str(threshold)) * 100:.0f}%"

            rows.append({
                "carrier": carrier,
                "threshold": threshold_label,
                "threshold_basis": s.threshold_basis,
                "average_triggered": s.average_triggered,
                "provision_count": len(candidates),
                "basis_ambiguous": ambiguous,
                "provisions": [
                    {"verified_in": c.get("verified_in"),
                     "settlement_basis": c.get("settlement_basis", "ANY"),
                     "threshold_pct": (None if c.get("threshold_pct") is None
                                       else str(c["threshold_pct"]))}
                    for c in candidates],
                "formula_basis": s.formula_basis,
                "formula_basis_class": s.formula_basis_class,
                "illustration_sgd": str(s.illustration),
                "formula_quote": formula_quote,
            })
    finally:
        conn.close()

    return {
        "scenario": {
            "sum_insured_sgd": str(_COMPARE_SUM_INSURED),
            "value_at_risk_sgd": str(_COMPARE_VALUE_AT_RISK),
            "assessed_loss_sgd": str(_COMPARE_LOSS),
        },
        "note": ("Same clause type, same engine, three real wordings. The evidence "
                 "class is decided by reading each document, not by our assumption. "
                 "A tool hardcoding Chubb's 85% would under-report every MSIG "
                 "insured, whose shortfall bites from the first dollar. And a tool "
                 "reading only Liberty's General Condition 4 would over-report every "
                 "Liberty insured on a reinstatement claim, by removing the 85% "
                 "cushion its 'Average Relief' clause actually grants. Wrong in both "
                 "directions is what a single hardcoded threshold buys you."),
        "carriers": rows,
    }
