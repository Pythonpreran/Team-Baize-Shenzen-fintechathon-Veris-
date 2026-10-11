"""Layer 1 — Locate. Extract, hash, store. Concludes nothing.

Rules that are not optional:
  * page_text is written once and never updated. Corrections go to text_fix.
  * Every span records char_start, char_end and a hash of quoted_text.
  * Fixtures are ingested through the SAME path as real PDFs, with is_fixture = 1,
    so the demo exercises real code.

Native PDF only (pypdf). OCR belongs in the evaluation harness, and an OCR failure
must never be what decides whether a citation lands.

UNTRUSTED INPUT. Since `POST /api/bundle` exists, every function here that touches
a caller-supplied file assumes the file is hostile: byte cap before parse, page cap
before extraction, encryption detected rather than cracked, and a missing text layer
reported rather than guessed at. All of it raises `errors.IngestError` subclasses
carrying a message an owner can act on.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import new_id
from .errors import (
    DocumentTooLarge,
    EmptyDocument,
    EncryptedDocument,
    IngestError,
    NoTextLayer,
    PdfParseResourceError,
    PdfParseTimeout,
    ProvenError,
    UnsupportedDocument,
)
from .models import Span

# ── intake limits ───────────────────────────────────────────────────────────
# Enforced BEFORE pypdf sees the bytes. A 2GB PDF bomb or a 50,000-page document
# must be refused at the door, not discovered by the parser.
MAX_FILE_BYTES = 20 * 1024 * 1024      # 20 MB per document
MAX_BUNDLE_BYTES = 60 * 1024 * 1024    # 60 MB per upload
MAX_PAGES = 400                        # generous for a policy pack, finite
# A real SME bundle is a wording, a schedule, a handful of supporting documents and
# any endorsements. The synthetic example alone is 13 (wording + 9 supporting + 3
# endorsements), so 16 leaves headroom without being an unbounded intake.
MAX_FILES = 16
PDF_MAGIC = b"%PDF-"

# ── extraction cache ────────────────────────────────────────────────────────
# Keyed on the sha256 of the file's own bytes, so it is content-addressed: the same
# bytes always yield the same pages and different bytes can never collide onto a
# cached entry. There is no invalidation problem because the key IS the content.
#
# Why it matters: parsing the 28-page demo wording costs ~1.2s, and the pipeline is
# re-run on every reset, every eval label and every test. Without this the same
# immutable file was parsed twenty times per test session, and the accumulated pypdf
# object graph made later runs three times slower than the first.
_PAGE_CACHE: dict[str, list[str]] = {}
_PAGE_CACHE_MAX_ENTRIES = 8


def _cache_get(sha: str) -> Optional[list[str]]:
    pages = _PAGE_CACHE.get(sha)
    return list(pages) if pages is not None else None


def _cache_put(sha: str, pages: list[str]) -> None:
    if len(_PAGE_CACHE) >= _PAGE_CACHE_MAX_ENTRIES:
        _PAGE_CACHE.pop(next(iter(_PAGE_CACHE)))     # oldest out, FIFO
    _PAGE_CACHE[sha] = list(pages)


def clear_page_cache() -> None:
    """Drop the extraction cache. For tests that want a cold parse."""
    _PAGE_CACHE.clear()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def insert_document(
    conn: sqlite3.Connection, business_id: str, kind: str, author_class: str,
    filename: str, sha: str, page_count: int, is_fixture: int = 0,
    carrier: Optional[str] = None, doc_id: Optional[str] = None,
) -> str:
    doc_id = doc_id or new_id()
    conn.execute(
        "INSERT INTO document (id, business_id, kind, author_class, carrier, "
        "filename, sha256, page_count, is_fixture, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (doc_id, business_id, kind, author_class, carrier, filename, sha,
         page_count, is_fixture, _now()),
    )
    return doc_id


def insert_page_text(
    conn: sqlite3.Connection, document_id: str, page_no: int, text: str,
    method: str = "pypdf",
) -> None:
    conn.execute(
        "INSERT INTO page_text (document_id, page_no, text, method) VALUES (?,?,?,?)",
        (document_id, page_no, text, method),
    )


def page_text(conn: sqlite3.Connection, document_id: str, page_no: int) -> str:
    row = conn.execute(
        "SELECT text FROM page_text WHERE document_id=? AND page_no=?",
        (document_id, page_no),
    ).fetchone()
    return row["text"] if row else ""


def safe_filename(name: str) -> str:
    """Strip every path component and control character from a caller-supplied
    filename. The name is stored and later rendered, and `../..` in a filename is
    the oldest upload bug there is. We never write uploads to disk, but the name
    still reaches the DB and the DOM."""
    base = Path(str(name or "document.pdf")).name
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    base = re.sub(r'[<>:"/\\|?*]', "_", base).strip() or "document.pdf"
    return base[:120]


def _validate_pdf_bytes(data: bytes, filename: str) -> None:
    """Apply cheap bounds before any parser process receives caller bytes."""
    if not data:
        raise EmptyDocument(f"{filename} is empty (0 bytes). Re-export and retry.")
    if len(data) > MAX_FILE_BYTES:
        raise DocumentTooLarge(
            f"{filename} is {len(data) // (1024 * 1024)} MB. The limit is "
            f"{MAX_FILE_BYTES // (1024 * 1024)} MB per document.")
    if not data.lstrip()[:1024].startswith(PDF_MAGIC):
        raise UnsupportedDocument(
            f"{filename} is not a PDF. Only native PDFs are read — a photo or a "
            f"scan has no text layer, and this build does not guess at it.")


def _extract_pdf_pages_direct(data: bytes, filename: str = "document.pdf") -> list[str]:
    """The deterministic parser implementation, run in an isolated child when hosted."""
    from pypdf import PdfReader           # lazy: engine tests need no PDF
    from pypdf.errors import PdfReadError
    import io

    _validate_pdf_bytes(data, filename)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # An empty owner password is common and harmless to try; a real user
            # password is refused rather than attacked.
            try:
                if reader.decrypt("") == 0:
                    raise EncryptedDocument(
                        f"{filename} is password-protected. Save an unprotected "
                        f"copy and upload that.")
            except EncryptedDocument:
                raise
            except Exception as exc:                  # pragma: no cover - pypdf
                raise EncryptedDocument(
                    f"{filename} is password-protected and could not be opened."
                ) from exc
        page_count = len(reader.pages)
    except (EncryptedDocument, DocumentTooLarge):
        raise
    except PdfReadError as exc:
        raise UnsupportedDocument(
            f"{filename} could not be parsed as a PDF (the file looks corrupt). "
            f"Re-download it from your insurer's portal.", detail=str(exc)) from exc
    except Exception as exc:                          # defensive: pypdf is broad
        raise IngestError(f"{filename} could not be read.", detail=str(exc)) from exc

    if page_count == 0:
        raise EmptyDocument(f"{filename} contains no pages.")
    if page_count > MAX_PAGES:
        raise DocumentTooLarge(
            f"{filename} has {page_count} pages. The limit is {MAX_PAGES}. Upload "
            f"the policy wording and schedule separately.")

    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            # One unreadable page must not lose the other pages. The empty page is
            # visible downstream as unsearched rather than as a silent gap.
            pages.append("")

    if not any(page.strip() for page in pages):
        raise NoTextLayer(
            f"{filename} has no text layer — it is almost certainly a scan. This "
            f"build reads native PDFs only and will not guess at a scan's "
            f"contents. Ask your insurer for the digital original.")
    return pages


def _set_child_memory_limit(limit: int) -> bool:
    """Apply a hard address-space cap where the operating system exposes one."""
    try:
        import resource  # Unix only; the hosted container runs Linux.
    except ImportError:
        return False
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (OSError, ValueError):
        return False
    return True


def _pdf_parse_worker(data: bytes, filename: str, connection, memory_limit: int) -> None:
    """Top-level target so Windows' spawn start method can import it safely."""
    try:
        memory_limited = _set_child_memory_limit(memory_limit)
        pages = _extract_pdf_pages_direct(data, filename)
        connection.send({"ok": True, "pages": pages, "memory_limited": memory_limited})
    except ProvenError as exc:
        connection.send({"ok": False, "code": exc.code, "message": exc.message,
                         "detail": exc.detail})
    except BaseException as exc:  # child boundaries must never leak a parser traceback
        connection.send({"ok": False, "code": "PDF_PARSE_RESOURCE_LIMIT",
                         "message": f"{filename} could not be parsed safely.",
                         "detail": type(exc).__name__})
    finally:
        connection.close()


_PARSE_ERRORS = {
    "EMPTY_DOCUMENT": EmptyDocument,
    "DOCUMENT_TOO_LARGE": DocumentTooLarge,
    "ENCRYPTED_DOCUMENT": EncryptedDocument,
    "UNSUPPORTED_DOCUMENT": UnsupportedDocument,
    "NO_TEXT_LAYER": NoTextLayer,
    "PDF_PARSE_TIMEOUT": PdfParseTimeout,
    "PDF_PARSE_RESOURCE_LIMIT": PdfParseResourceError,
}


def extract_pdf_pages_isolated(data: bytes, filename: str = "document.pdf") -> list[str]:
    """Parse hostile PDF bytes in a bounded child process for hosted deployments."""
    from . import security

    _validate_pdf_bytes(data, filename)
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_pdf_parse_worker,
        args=(data, filename, send, security.pdf_parse_memory_bytes()),
        daemon=True,
    )
    process.start()
    send.close()
    process.join(security.pdf_parse_timeout_seconds())
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
        receive.close()
        raise PdfParseTimeout(
            f"{filename} took too long to parse safely. Re-export a smaller native PDF.")
    try:
        payload = receive.recv() if receive.poll(0.2) else None
    except (EOFError, OSError):
        payload = None
    finally:
        receive.close()
    if not isinstance(payload, dict):
        raise PdfParseResourceError(
            f"{filename} could not be parsed within the hosted resource limits.")
    if payload.get("ok") is True and isinstance(payload.get("pages"), list):
        return [str(page) for page in payload["pages"]]
    error_type = _PARSE_ERRORS.get(str(payload.get("code") or ""), PdfParseResourceError)
    raise error_type(str(payload.get("message") or f"{filename} could not be parsed safely."),
                     detail=str(payload.get("detail") or "") or None)


def extract_pdf_pages(data: bytes, filename: str = "document.pdf") -> list[str]:
    """Validate and extract per-page PDF text, isolating hostile parsing when hosted."""
    from . import security

    if security.hosted_mode():
        return extract_pdf_pages_isolated(data, filename)
    return _extract_pdf_pages_direct(data, filename)


def extract_pdf_pages_cached(data: bytes, filename: str = "document.pdf"
                             ) -> list[str]:
    """`extract_pdf_pages` with the content-addressed cache in front of it.

    A cache hit can only occur for bytes that already passed every validation check,
    so nothing is skipped: identical bytes have identical validity.
    """
    sha = sha256_bytes(data)
    pages = _cache_get(sha)
    if pages is None:
        pages = extract_pdf_pages(data, filename)
        _cache_put(sha, pages)
    return pages


def ingest_pdf_bytes(
    conn: sqlite3.Connection, data: bytes, filename: str, kind: str,
    author_class: str, business_id: str, carrier: Optional[str] = None,
) -> tuple[str, list[str]]:
    """Ingest an in-memory PDF. Returns (document_id, per-page text).

    Uploaded files are never written to disk: there is no temp-file lifecycle to
    leak, and no path for a caller-supplied name to influence the filesystem.
    """
    filename = safe_filename(filename)
    sha = sha256_bytes(data)
    pages = _cache_get(sha)
    if pages is None:
        pages = extract_pdf_pages(data, filename)
        _cache_put(sha, pages)
    doc_id = insert_document(
        conn, business_id, kind, author_class, filename, sha,
        len(pages), is_fixture=0, carrier=carrier)
    for page_no, text in enumerate(pages, start=1):
        insert_page_text(conn, doc_id, page_no, text, method="pypdf")
    conn.commit()
    return doc_id, pages


def ingest_pdf(
    conn: sqlite3.Connection, path: str | Path, kind: str, author_class: str,
    business_id: str, carrier: Optional[str] = None,
) -> str:
    """Extract every page with pypdf, hash the file, store. Returns document_id.

    Goes through the same validation as an upload, so a corrupt bundled asset fails
    the same way a corrupt upload does instead of raising from inside pypdf.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise IngestError(
            f"{path.name} could not be opened.", detail=str(exc)) from exc
    doc_id, _ = ingest_pdf_bytes(
        conn, data, path.name, kind, author_class, business_id, carrier)
    return doc_id


_PAGE_MARKER = re.compile(r"^=+\s*PAGE\s+(\d+)\s*=+\s*$", re.M | re.I)


def ingest_text_wording(
    conn: sqlite3.Connection, path: str | Path, kind: str, author_class: str,
    business_id: str, carrier: Optional[str] = None,
) -> str:
    """Ingest a pre-extracted wording .txt whose pages are delimited by
    `=== PAGE N ===` markers (MSIG, Liberty). Same document/page_text path as a PDF,
    so the cross-carrier demo exercises real ingestion rather than a special case.
    Native extraction only — no OCR in the demo path."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise IngestError(
            f"{path.name} could not be read.", detail=str(exc)) from exc
    if not raw.strip():
        raise EmptyDocument(f"{path.name} contains no text.")
    sha = sha256_bytes(raw.encode("utf-8"))

    # Split on page markers, keeping the page number that precedes each block.
    parts = _PAGE_MARKER.split(raw)
    # parts = [preamble, "1", page1_text, "2", page2_text, ...]
    pages: list[tuple[int, str]] = []
    if len(parts) >= 3:
        it = iter(parts[1:])
        for num, body in zip(it, it):
            pages.append((int(num), body))
    else:
        pages = [(1, raw)]

    doc_id = insert_document(
        conn, business_id, kind, author_class, path.name, sha,
        len(pages), is_fixture=0, carrier=carrier)
    for page_no, body in pages:
        insert_page_text(conn, doc_id, page_no, body, method="pypdf-preextracted")
    conn.commit()
    return doc_id


def make_span(
    conn: sqlite3.Connection, document_id: str, page_no: int, needle: str,
) -> Optional[Span]:
    """Find `needle` in the stored page text, insert a span over it, return it.
    Anchored to real offsets so the abstention gate can verify fidelity."""
    text = page_text(conn, document_id, page_no)
    idx = text.find(needle)
    if idx < 0:
        return None
    start, end = idx, idx + len(needle)
    quoted = text[start:end]
    span = Span(
        id=new_id(), document_id=document_id, page_no=page_no,
        char_start=start, char_end=end, quoted_text=quoted,
        sha256=sha256_bytes(quoted.encode("utf-8")),
    )
    conn.execute(
        "INSERT INTO span (id, document_id, page_no, char_start, char_end, "
        "quoted_text, sha256) VALUES (?,?,?,?,?,?,?)",
        (span.id, span.document_id, span.page_no, span.char_start, span.char_end,
         span.quoted_text, span.sha256),
    )
    return span


# ── Fixture ingestion ───────────────────────────────────────────────────────
# The two JSON fixture files are rendered to a canonical text page and ingested
# through the same document/page_text/span path as a real PDF. Each logical
# document keeps its own author_class, because author_class derives evidence_class.

# parents[2] is the repository root. See the note in align.py: the previous
# `parents[3] / "proven"` read the fixtures from outside the checkout.
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _canonical_text(obj: dict) -> str:
    """Deterministic flat text rendering of a fixture dict, so needle lookups are
    stable and spans anchor to real offsets."""
    lines: list[str] = []

    def walk(o, prefix=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    lines.append(f"{prefix}{k}:")
                    walk(v, prefix + "  ")
                else:
                    lines.append(f"{prefix}{k}: {v}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                if isinstance(v, (dict, list)):
                    lines.append(f"{prefix}- [{i}]")
                    walk(v, prefix + "  ")
                else:
                    lines.append(f"{prefix}- {v}")
        else:
            lines.append(f"{prefix}{o}")

    walk(obj)
    return "\n".join(lines)


class LoadedDoc:
    """An ingested document as the rules layer sees it.

    Two provenances, one shape:
      * fixture / structured  -> `data` is the parsed JSON, `pages` is the rendered
                                 canonical text
      * uploaded PDF          -> `data` is empty, `pages` is the extracted text

    Keeping one type means the extraction layer (extract.py) does not branch on
    provenance, which is what stops "works on fixtures only" from creeping back in.
    """

    def __init__(self, doc_id: str, kind: str, author_class: str,
                 data: Optional[dict] = None, carrier: Optional[str] = None,
                 pages: Optional[list[str]] = None, filename: str = "",
                 is_fixture: bool = False):
        self.id = doc_id
        self.kind = kind
        self.author_class = author_class
        self.data = data if data is not None else {}
        self.carrier = carrier
        self.pages = pages or []
        self.filename = filename
        self.is_fixture = is_fixture

    @property
    def text(self) -> str:
        return "\n".join(self.pages)


class DocStore:
    """Holds ingested documents, allowing several of the same kind (e.g. two
    endorsements). `first(kind)` returns one or None; `all(kind)` returns the list."""

    def __init__(self) -> None:
        self._by_kind: dict[str, list[LoadedDoc]] = {}
        self.loss_scenario: dict = {}
        # Non-fatal intake problems, surfaced to the user rather than swallowed.
        self.warnings: list[dict] = []

    def add(self, doc: LoadedDoc) -> None:
        self._by_kind.setdefault(doc.kind, []).append(doc)

    def first(self, kind: str) -> Optional[LoadedDoc]:
        docs = self._by_kind.get(kind, [])
        return docs[0] if docs else None

    def all(self, kind: str) -> list[LoadedDoc]:
        return list(self._by_kind.get(kind, []))

    def kinds(self) -> list[str]:
        return list(self._by_kind.keys())

    def documents(self) -> list[LoadedDoc]:
        return [d for docs in self._by_kind.values() for d in docs]

    def warn(self, filename: str, code: str, message: str) -> None:
        self.warnings.append(
            {"filename": filename, "code": code, "message": message})


def ingest_fixtures(
    conn: sqlite3.Connection, business_id: str,
    fixture_dir: Path | str = FIXTURE_DIR,
) -> DocStore:
    """Load the Hock Lee fixtures. The schedule and each supporting document become
    their own immutable document row, ingested through the same path as a PDF."""
    fixture_dir = Path(fixture_dir)
    store = DocStore()

    def _load_one(kind: str, author_class: str, data: dict, filename: str):
        text = _canonical_text(data)
        sha = sha256_bytes(json.dumps(data, sort_keys=True).encode("utf-8"))
        doc_id = insert_document(
            conn, business_id, kind, author_class, filename, sha,
            page_count=1, is_fixture=1)
        insert_page_text(conn, doc_id, 1, text, method="fixture")
        store.add(LoadedDoc(doc_id, kind, author_class, data, pages=[text],
                            filename=filename, is_fixture=True))

    def _read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise IngestError(
                f"Fixture {path.name} is missing from {path.parent}.") from exc
        except json.JSONDecodeError as exc:
            raise IngestError(
                f"Fixture {path.name} is not valid JSON (line {exc.lineno}).",
                detail=str(exc)) from exc

    schedule = _read_json(fixture_dir / "hock-lee-schedule.json")
    _load_one(schedule["_document_kind"], schedule["_author_class"], schedule,
              "hock-lee-schedule.json")

    supporting = _read_json(fixture_dir / "hock-lee-supporting-documents.json")
    for doc in supporting.get("documents", []):
        _load_one(doc["_document_kind"], doc["_author_class"], doc,
                  "hock-lee-supporting-documents.json")

    # Optional: conflicting endorsements that produce a precedence tie (gate 4).
    conflicting = fixture_dir / "hock-lee-conflicting-endorsements.json"
    if conflicting.exists():
        data = _read_json(conflicting)
        for doc in data.get("documents", []):
            _load_one(doc["_document_kind"], doc["_author_class"], doc,
                      "hock-lee-conflicting-endorsements.json")

    store.loss_scenario = supporting.get("loss_scenario_for_demo", {})
    conn.commit()
    return store
