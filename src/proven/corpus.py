"""The document corpus: third-party publications the engine reads but never ships.

WHY THIS MODULE EXISTS. Until now the engine opened insurer wordings through hard-coded
paths into a sibling research folder — `../06-evidence/policy-wordings/<file>.pdf`. Three
problems with that, and the third is the serious one:

  1. A clean clone could not run. The paths pointed outside the repository at a
     directory nobody cloning it would have.
  2. There was no record of what those documents WERE. A page citation into a file whose
     publisher, URL and retrieval hash live only in a folder name is not checkable.
  3. The obvious fix — commit the PDFs — is not available. They are other people's
     copyrighted material. We have a reader's right to them, not a publisher's.

So the documents live OUTSIDE git in a corpus directory, and the committed part is
`corpus/manifest.yaml`: id, title, publisher, source URL, country, document class, the
exact sha256 we read, and what each document is NOT authority for. Code resolves
documents by MANIFEST ID. No module builds a path out of a filename again.

ABSENCE IS A SUPPORTED STATE, NOT AN ERROR. A corpus that has not been fetched yet is a
degraded mode: the engine still works, the synthetic examples still load, and the tests
that genuinely need a published wording skip with a reason (`requires_corpus`). What
must never happen is a figure quoted from a document nobody can produce.

LAYOUT under the corpus directory (`VERIS_CORPUS_DIR`, default `../veris-corpus`):

    originals/<id>.pdf      publisher bytes, for entries whose media_type is a PDF
    text/<id>.txt           page-marked text for EVERY entry, uniform and flat
    fetch-log.json          what was fetched, when, from where, and any hash drift

`text/` is flat and keyed by id on purpose: the clause comparison aligns a directory of
wordings, and a flat directory keyed by a stable id is the one shape that stays correct
when a document is re-retrieved from a different host under a different filename.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# `src/proven/corpus.py` -> parents[2] is the repository root.
REPO = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO / "corpus" / "manifest.yaml"


def manifest_path() -> Path:
    """`VERIS_CORPUS_MANIFEST` overrides, so an installed package can be pointed at
    the manifest it was built against rather than guessing from its own location."""
    env = os.environ.get("VERIS_CORPUS_MANIFEST", "").strip()
    return Path(env).expanduser() if env else MANIFEST_PATH

#: Where the documents live when `VERIS_CORPUS_DIR` is not set. Deliberately a sibling
#: of the repository rather than a directory inside it: a path inside the repo is one
#: `git add -A` away from committing a copyrighted wording.
DEFAULT_CORPUS_DIRNAME = "veris-corpus"

PDF_MEDIA = "application/pdf"
TEXT_MEDIA = "text/plain"

#: Counted page markers use this exact form. `ingest` and the clause comparison split on
#: `"=== PAGE "`, which also matches the longer `"===== PAGE n ====="` rules the earlier
#: extractions used, so both formats read correctly and neither needs rewriting.
PAGE_MARKER = "=== PAGE {n} ==="


class CorpusError(RuntimeError):
    """Base class. Anything wrong with the manifest or the corpus directory."""


class ManifestError(CorpusError):
    """The manifest is malformed, or a document was asked for that it does not list."""


class CorpusMissing(CorpusError):
    """The manifest lists the document but the corpus directory does not hold it."""

    def __init__(self, message: str, document_id: Optional[str] = None) -> None:
        super().__init__(message)
        #: Which document, so a caller can check whether it is REALLY absent rather
        #: than trusting the exception type. `tests/conftest.py` turns an absent
        #: document into a named skip; on a seeded machine this exception is a bug, and
        #: a skip there would hide a mis-resolved id behind a green run.
        self.document_id = document_id


class CorpusDrifted(CorpusError):
    """The stored bytes are not the bytes the manifest describes.

    Fetching verifies the hash. Until now nothing verified it again, so a file replaced
    by hand, restored from a stale backup or left behind by an interrupted write would
    be read as authoritative — and every page citation taken against the manifest's
    bytes would point at text it was not taken from. That is a fabricated citation
    rendered by the product's own evidence viewer, which is the one failure this corpus
    exists to prevent.
    """


class TextSeed(BaseModel):
    """A text extraction the team has already produced and verified.

    Preferred over re-extracting, because the committed page citations were taken
    against THAT text. A fresh extraction from a newer pypdf can shift offsets, and a
    shifted offset turns a correct citation into a wrong one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    seed_path: str
    sha256: str
    pages: int = Field(ge=0)

    @field_validator("sha256")
    @classmethod
    def _hex64(cls, v: str) -> str:
        return _check_sha256(v)


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    publisher: str
    source_url: Optional[str]
    source_url_status: Literal["RECORDED", "UNRECORDED"]
    country: Literal["SG", "MY", "IN", "HK", "CN", "GENERIC"]
    line_of_business: Literal["SME_PACKAGE", "HEALTH", "HOME", "MARINE", "NOT_APPLICABLE"]
    document_class: Literal[
        "POLICY_WORDING", "BENEFITS_SCHEDULE", "PROPOSAL_FORM", "FACTSHEET",
        "BROCHURE", "WEBPAGE", "STATUTE", "JUDGMENT", "PRACTICE_GUIDE"]
    media_type: Literal["application/pdf", "text/plain"]
    seed_path: str
    sha256: str
    bytes: int = Field(ge=0)
    pages: int = Field(ge=0)
    redistribute: bool
    usage_note: str
    text: Optional[TextSeed] = None
    #: Expected SHA-256 of a page-marked extraction generated from this PDF.  Unlike
    #: ``text``, this records no seed path: the extractor must reproduce it from the
    #: separately hash-verified original before a reader may quote it.
    text_sha256: Optional[str] = None

    @field_validator("text_sha256")
    @classmethod
    def _text_hex64(cls, v: Optional[str]) -> Optional[str]:
        return _check_sha256(v) if v is not None else None

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c == "-" for c in v):
            raise ValueError(
                f"corpus id {v!r} must be non-empty and contain only letters, digits "
                f"and hyphens — it is used as a filename under the corpus directory")
        return v

    @field_validator("sha256")
    @classmethod
    def _hex64(cls, v: str) -> str:
        return _check_sha256(v)

    @field_validator("redistribute")
    @classmethod
    def _never_redistribute(cls, v: bool) -> bool:
        # Not a style check. A `true` here would mean someone had decided a third
        # party's wording could be committed, and the whole point of the corpus is that
        # no such entry exists.
        if v:
            raise ValueError(
                "redistribute must be false for every corpus document: these are "
                "third-party publications and the repository has no right to ship them")
        return v

    @field_validator("usage_note")
    @classmethod
    def _usage_note_present(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "usage_note is required: a document with no stated use is a document "
                "nobody can tell the limits of")
        return v

    def model_post_init(self, _context) -> None:
        if self.source_url_status == "RECORDED" and not (self.source_url or "").strip():
            raise ValueError(
                f"{self.id}: source_url_status is RECORDED but source_url is empty")
        if self.source_url_status == "UNRECORDED" and self.source_url:
            raise ValueError(
                f"{self.id}: source_url_status is UNRECORDED but a source_url is set")
        if self.source_url and not self.source_url.startswith(("http://", "https://")):
            raise ValueError(
                f"{self.id}: source_url must be an http(s) URL, got {self.source_url!r}")

    @property
    def seed_name(self) -> str:
        """The basename of the published file, as retrieved.

        Kept as a documented ALIAS for lookup so the evaluation labels, which record
        the filename a human read, keep resolving. Ids remain the primary key.
        """
        return Path(self.seed_path).name

    @property
    def original_suffix(self) -> str:
        return ".pdf" if self.media_type == PDF_MEDIA else ".txt"

    @property
    def downloadable(self) -> bool:
        return self.source_url_status == "RECORDED" and bool(self.source_url)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    documents: list[Document]

    @field_validator("documents")
    @classmethod
    def _unique_ids(cls, docs: list[Document]) -> list[Document]:
        if not docs:
            raise ValueError("the manifest lists no documents")
        seen: set[str] = set()
        for d in docs:
            if d.id in seen:
                raise ValueError(f"duplicate corpus id {d.id!r}")
            seen.add(d.id)
        return docs


def _check_sha256(v: str) -> str:
    v = (v or "").strip().lower()
    if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
        raise ValueError(f"sha256 must be 64 hex characters, got {v!r}")
    return v


# ── loading ─────────────────────────────────────────────────────────────────
# Cached by resolved path. The manifest is a committed file that does not change while
# the process runs, and it is read on nearly every import path below.

_CACHE: dict[str, Manifest] = {}


def load_manifest(path: Optional[Path | str] = None, *,
                  refresh: bool = False) -> Manifest:
    """Parse and validate the manifest. Raises `ManifestError` on anything malformed."""
    p = Path(path) if path is not None else manifest_path()
    key = str(p)
    if not refresh and key in _CACHE:
        return _CACHE[key]
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"corpus manifest not found at {p}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"corpus manifest at {p} is not valid YAML: {exc}") from exc
    try:
        manifest = Manifest.model_validate(raw)
    except Exception as exc:                      # pydantic ValidationError, or ValueError
        raise ManifestError(f"corpus manifest at {p} is invalid: {exc}") from exc
    _CACHE[key] = manifest
    return manifest


def documents() -> tuple[Document, ...]:
    return tuple(load_manifest().documents)


def resolve(ref: str | Document) -> Document:
    """A manifest id, or the published filename as a documented alias.

    Ids are the primary key. The filename alias exists because the evaluation labels
    record the file a human actually read, and rewriting that provenance into ids would
    lose the link between a label and the document it was taken from.
    """
    if isinstance(ref, Document):
        return ref
    for d in documents():
        if d.id == ref:
            return d
    matches = [d for d in documents() if d.seed_name == ref]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ManifestError(
            f"{ref!r} is the filename of {len(matches)} corpus documents "
            f"({', '.join(m.id for m in matches)}); use the id instead")
    raise ManifestError(
        f"no corpus document with id or filename {ref!r}. Known ids: "
        f"{', '.join(d.id for d in documents())}")


# ── the corpus directory ────────────────────────────────────────────────────

def corpus_dir() -> Path:
    """Read from the environment on every call, not captured at import.

    Captured-at-import would make `VERIS_CORPUS_DIR` unsettable from a test and from a
    deployment that exports it after the process has started loading modules.
    """
    env = os.environ.get("VERIS_CORPUS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return REPO.parent / DEFAULT_CORPUS_DIRNAME


def originals_dir() -> Path:
    return corpus_dir() / "originals"


def text_dir() -> Path:
    return corpus_dir() / "text"


def fetch_log_path() -> Path:
    return corpus_dir() / "fetch-log.json"


def quarantine_dir() -> Path:
    """Where a retrieval that does not match the manifest is put instead.

    A publisher is allowed to republish, and the fetcher records it when they do. What
    it must not do is overwrite the verified copy with the new bytes: the committed page
    citations were measured against the old ones, and no reader compares a stored file
    against the manifest before quoting it. So drifted bytes land here — kept, hashed
    and reported, but outside the directories anything reads from.
    """
    return corpus_dir() / "quarantine"


def original_path(ref: str | Document) -> Path:
    """Where the publisher's own bytes live.

    For a text retrieval there are no separate "original" bytes — the retrieval IS the
    text — so this returns the same path as `text_path`, which keeps callers from
    holding two names for one file.
    """
    d = resolve(ref)
    if d.media_type == TEXT_MEDIA:
        return text_path(d)
    return originals_dir() / f"{d.id}{d.original_suffix}"


def text_path(ref: str | Document) -> Path:
    return text_path_in(text_dir(), ref)


def text_path_in(directory: Path | str, ref: str | Document) -> Path:
    """The same lookup, inside a directory the caller names.

    The clause comparison takes its corpus directory as an argument, so it cannot use
    `text_path()`. Resolving the id FIRST is the part that matters: a path built by hand
    as `directory / f"{id}.txt"` turns a mistyped id into a file that merely does not
    exist, and both callers skip a document whose file is absent. The typo would then
    quietly shrink the comparison instead of failing, which is indistinguishable from an
    aligner that has stopped binding anything.
    """
    d = resolve(ref)
    return Path(directory) / f"{d.id}.txt"


def available(ref: str | Document) -> bool:
    """Both artefacts a reader needs: the bytes to cite, and the text to search."""
    d = resolve(ref)
    return original_path(d).is_file() and text_path(d).is_file()


def missing(refs: Optional[Iterable[str | Document]] = None) -> list[str]:
    todo = [resolve(r) for r in refs] if refs is not None else list(documents())
    return [d.id for d in todo if not available(d)]


def is_seeded(refs: Optional[Iterable[str | Document]] = None) -> bool:
    return not missing(refs)


_HOW_TO_FETCH = (
    "Fetch it with:  python scripts/fetch_corpus.py --seed-from ../06-evidence   "
    "(or without --seed-from to download from the publisher). The corpus lives outside "
    "git because these are third-party publications; see corpus/manifest.yaml.")


def require_original(ref: str | Document) -> Path:
    d = resolve(ref)
    p = original_path(d)
    if not p.is_file():
        raise CorpusMissing(
            f"corpus document {d.id!r} ({d.title}) is not present at {p}. "
            f"{_HOW_TO_FETCH}", document_id=d.id)
    verify_stored(d, p)
    return p


def require_text(ref: str | Document) -> Path:
    d = resolve(ref)
    p = text_path(d)
    if not p.is_file():
        raise CorpusMissing(
            f"the page-marked text for corpus document {d.id!r} ({d.title}) is not "
            f"present at {p}. {_HOW_TO_FETCH}", document_id=d.id)
    verify_text(d, p)
    return p


def present_original(ref: str | Document) -> Optional[Path]:
    """`require_original`, for a caller to whom absence is a supported answer.

    Returns None when the document has not been fetched, so a comparison can leave that
    carrier out exactly as it did when it tested `path.exists()`. What it does NOT do is
    turn a drifted file into None: `CorpusDrifted` propagates, because a wrong file is not
    a missing one, and reading it would cite pages it does not contain.
    """
    try:
        return require_original(ref)
    except CorpusMissing:
        return None


def present_text(ref: str | Document) -> Optional[Path]:
    """`require_text`, with absence as None and drift still raised. See above."""
    try:
        return require_text(ref)
    except CorpusMissing:
        return None


def verify_text(ref: str | Document, path: Optional[Path] = None) -> Path:
    """Verify page-marked text wherever the manifest declares its expected hash.

    The text file is the artefact the committed page offsets were MEASURED against, so it
    is the one a wrong copy corrupts. Three cases have a known expected hash:

      * a `text/plain` retrieval, where the retrieval IS the publisher's bytes and the
        manifest's own `sha256` applies to this file;
      * a PDF whose verified extraction was SEEDED (`text: {seed_path, sha256}` in the
        manifest, and `text: "seeded"` in the fetch log); and
      * a PDF whose `text_sha256` records the deterministic page-marked extraction made
        from its separately verified original. This is used when no redistributable
        extraction seed exists.

    A PDF whose text was re-extracted on a download path and declares neither a seed nor
    `text_sha256` has no single expected value — its hash depends on which pypdf produced
    it — so it is not checked here; the PDF it came from is verified by `require_original`
    instead, and the fetcher recorded a warning about shifted offsets when it extracted it.

    `path` defaults to the corpus copy; the clause comparison passes the file it is about
    to read from its own directory, so the check covers the bytes actually aligned.
    """
    d = resolve(ref)
    p = Path(path) if path is not None else text_path(d)
    if d.media_type == TEXT_MEDIA:
        return verify_stored(d, p)
    if d.text is not None and _text_was_seeded(d):
        return _verify_hash(d, p, d.text.sha256, what="page-marked text")
    if d.text_sha256 is not None:
        return _verify_hash(d, p, d.text_sha256, what="page-marked text")
    return p


def _text_was_seeded(d: Document) -> bool:
    """Whether the fetch log records this document's text as written from its seed."""
    record = _fetch_log_records().get(d.id) or {}
    return record.get("text") == "seeded"


_LOG_CACHE: dict[str, tuple[tuple[int, int], dict[str, dict]]] = {}


def _fetch_log_records() -> dict[str, dict]:
    """The fetch log's per-document records, keyed by id. `{}` when there is no log.

    Re-read when the file changes (size and mtime), so a re-fetch in the same process is
    seen. An unreadable log yields `{}`: with no record that the text was seeded there is
    no expected hash to hold it to, which is the same position a download leaves it in.
    """
    import json

    p = fetch_log_path()
    try:
        stat = p.stat()
    except OSError:
        return {}
    fingerprint = (stat.st_size, stat.st_mtime_ns)
    cached = _LOG_CACHE.get(str(p))
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        rows = raw.get("documents") if isinstance(raw, dict) else None
        records = {str(r.get("id")): r for r in (rows or []) if isinstance(r, dict)}
    except (OSError, ValueError):
        records = {}
    _LOG_CACHE[str(p)] = (fingerprint, records)
    return records


# ── verification at read time, not only at fetch time ───────────────────────
# Cached on (size, mtime_ns) so this is one hash per file per process rather than one
# per case run. A file that changes gets re-verified, because its fingerprint changes.

_VERIFIED: dict[str, tuple[int, int]] = {}


def verify_stored(ref: str | Document, path: Optional[Path] = None) -> Path:
    """Hash the stored bytes against the manifest. Raises `CorpusDrifted` on a mismatch.

    Not advisory. A document whose bytes are not the bytes the manifest describes cannot
    support the citations taken against it, so reading it is refused rather than warned
    about: the alternative is a correct-looking quotation over a page it was never on.
    """
    d = resolve(ref)
    p = Path(path) if path is not None else original_path(d)
    return _verify_hash(d, p, d.sha256, what="file")


def _verify_hash(d: Document, p: Path, expected: str, *, what: str) -> Path:
    try:
        stat = p.stat()
    except OSError as exc:
        raise CorpusMissing(
            f"corpus document {d.id!r} could not be read at {p}: {exc}",
            document_id=d.id) from exc
    fingerprint = (stat.st_size, stat.st_mtime_ns)
    key = f"{p}|{expected}"
    if _VERIFIED.get(key) == fingerprint:
        return p
    got = sha256_file(p)
    if got != expected:
        raise CorpusDrifted(
            f"corpus document {d.id!r} ({d.title}) at {p} is not the {what} the manifest "
            f"describes: expected sha256 {expected}, found {got}. Page citations were "
            f"taken against the manifest's bytes, so this copy is not used. Re-fetch "
            f"it, or re-read the document and update corpus/manifest.yaml deliberately.")
    _VERIFIED[key] = fingerprint
    return p


# ── hashing and page splitting ──────────────────────────────────────────────

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def split_pages(raw: str) -> list[str]:
    """Split page-marked text the same way every other reader in the codebase does.

    Falls back to one page so a retrieval with no markers — an HTML page — still
    contributes rather than silently scoring nothing.

    NOTE the leading chunk. A retrieval that carries the `SOURCE_URL:` header lines puts
    those before the first marker, so they come back as the first element. That is how
    the clause comparison and the alignment harness have always read these files and
    their page numbering is calibrated to it, so it is preserved exactly rather than
    tidied. Use `count_pages` when you want the number of real pages.
    """
    pages = [p for p in raw.split("=== PAGE ") if p.strip()]
    return pages or [raw]


def count_pages(raw: str) -> int:
    """How many page markers the text actually carries.

    Counts markers, so it is not confused by the header block that `split_pages`
    necessarily returns as its first element, and 0 is the right answer for an HTML
    retrieval that has no pagination at all.
    """
    return raw.count("=== PAGE ")


def status() -> dict:
    """What a reader, a judge or a CI log needs to know about corpus state.

    Reports by DETECTION, never by assertion: present/absent is measured off the disk
    at call time, so it cannot claim a document the process could not actually open.
    """
    docs = documents()
    present = [d.id for d in docs if available(d)]
    absent = [d.id for d in docs if not available(d)]
    return {
        "manifest": str(manifest_path()),
        "corpus_dir": str(corpus_dir()),
        "documents": len(docs),
        "present": present,
        "absent": absent,
        "complete": not absent,
        "unrecorded_source_urls": [
            d.id for d in docs if d.source_url_status == "UNRECORDED"],
        "redistributable": [],      # structurally empty: the validator forbids any
    }
