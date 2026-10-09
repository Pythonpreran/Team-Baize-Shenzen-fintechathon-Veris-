#!/usr/bin/env python
"""Populate the document corpus from local copies, or from the publishers.

    python scripts/fetch_corpus.py --seed-from ../06-evidence     # primary path
    python scripts/fetch_corpus.py                                # download instead
    python scripts/fetch_corpus.py --seed-from ../06-evidence --only sg-msig-sumo-wording
    python scripts/fetch_corpus.py --status

SEEDING IS THE PRIMARY PATH, not a fallback. Insurer sites move files, rate-limit, and
sit behind bot protection; a build that needs twenty live downloads to run its tests is
a build that fails on a train. So the normal route is a local directory of documents the
team already retrieved, verified against the sha256 in `corpus/manifest.yaml`. The
download route exists so somebody with only the repository can reconstruct the corpus,
and so drift at the publisher is detectable.

A HASH MISMATCH IS NEVER SILENTLY ACCEPTED.
  * Seeding: a mismatch is an error. The local copy is supposed to be the file the
    manifest describes, and the page citations were taken against those exact bytes.
  * Downloading: a mismatch is recorded with BOTH hashes in `fetch-log.json` and the new
    bytes are written to `quarantine/`, leaving any verified copy untouched. The
    publisher is allowed to republish; what is not allowed is for the product to keep
    quoting a page from a document that has changed underneath it. A quarantined
    document counts as not fetched, which the suite already reports as a named skip.

WHAT GETS WRITTEN. Nothing inside the repository. Everything goes to the corpus
directory (`VERIS_CORPUS_DIR`, default `../veris-corpus`), which is gitignored:

    originals/<id>.pdf      publisher bytes for PDF entries
    text/<id>.txt           page-marked text for every entry
    quarantine/<id>.pdf     a retrieval whose hash does not match the manifest
    fetch-log.json          one record per document, merged across runs

TEXT FORMAT. Reproduced to match the extractions the committed page citations were taken
against, byte-for-byte in structure:

    SOURCE_URL: <url>
    CONTENT_TYPE: <media type>
    BYTES: <size of the original>
    <blank>
    <blank>
    === PAGE 1 ===
    <page text>
    <blank>
    <blank>
    === PAGE 2 ===
    ...

CRLF line endings, because that is what the existing corpus uses and a citation offset
computed against CRLF text does not survive being rewritten as LF.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from proven import corpus  # noqa: E402

DEFAULT_SEED = REPO.parent / "06-evidence"

# A publisher's CDN will refuse a bare urllib request. This is the same browser-like
# header the team's retrieval scripts used, so a download reproduces what was retrieved
# by hand rather than failing differently.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "application/pdf,text/html,application/xhtml+xml,*/*;q=0.8",
}
TIMEOUT_S = 90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── text extraction ─────────────────────────────────────────────────────────

def extract_pdf_text(data: bytes, doc: corpus.Document) -> tuple[str, int]:
    """Page-marked text for a PDF, in the format the existing corpus uses.

    pypdf, not an OCR engine: this is the text layer, and a page with no text layer
    comes out empty rather than guessed at. A page the extractor could not read is a
    fact about the document, and the ingest path already reports unreadable pages
    honestly instead of inventing content for them.
    """
    import pypdf                                   # local: only the PDF path needs it

    reader = pypdf.PdfReader(__import__("io").BytesIO(data))
    pages = [(p.extract_text() or "") for p in reader.pages]
    return _render_text(pages, doc, len(data)), len(pages)


def _render_text(pages: list[str], doc: corpus.Document, size: int) -> str:
    url = doc.source_url or "NOT RECORDED AT RETRIEVAL TIME"
    out = [f"SOURCE_URL: {url}\n",
           f"CONTENT_TYPE: {doc.media_type}\n",
           f"BYTES: {size}\n"]
    for n, page in enumerate(pages, start=1):
        out.append(f"\n\n{corpus.PAGE_MARKER.format(n=n)}\n")
        out.append(page)
    return "".join(out)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\r\n" so the single "\n" the renderer emits becomes CRLF on every
    # platform. Writing LF on Linux and CRLF on Windows would give CI and a developer
    # machine two different corpora and two different citation offsets.
    with path.open("w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(text)


# ── one document ────────────────────────────────────────────────────────────

def seed_one(doc: corpus.Document, seed_dir: Path) -> dict:
    """Copy from a local directory, verifying the hash. The primary path."""
    src = seed_dir / doc.seed_path
    if not src.is_file():
        return {"id": doc.id, "ok": False, "action": "seed",
                "error": f"not found in the seed directory: {src}"}
    data = src.read_bytes()
    got = corpus.sha256_bytes(data)
    if got != doc.sha256:
        return {"id": doc.id, "ok": False, "action": "seed",
                "error": ("sha256 mismatch — the local copy is not the file the "
                          "manifest describes, so the committed page citations would "
                          "not line up against it"),
                "expected_sha256": doc.sha256, "actual_sha256": got,
                "bytes": len(data)}
    return _store(doc, data, seed_dir=seed_dir, action="seed")


def download_one(doc: corpus.Document) -> dict:
    """Retrieve from the publisher. Hash drift is recorded, not silently accepted."""
    if not doc.downloadable:
        return {"id": doc.id, "ok": False, "action": "download",
                "error": ("no source_url was recorded for this document, so it cannot "
                          "be downloaded. Seed it from a local copy; re-sourcing the URL "
                          "is an open task in the team's action log.")}
    req = urllib.request.Request(doc.source_url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return {"id": doc.id, "ok": False, "action": "download",
                "error": f"{type(exc).__name__}: {exc}", "source_url": doc.source_url}
    got = corpus.sha256_bytes(data)
    if got != doc.sha256:
        return _quarantine(doc, data, got)
    return _store(doc, data, seed_dir=None, action="download")


def _quarantine(doc: corpus.Document, data: bytes, got: str) -> dict:
    """Keep drifted bytes, but outside the directories anything reads from.

    The publisher is allowed to republish. Overwriting the verified copy in place is
    what is not allowed: the committed page citations were measured against the old
    bytes, and a reader quoting a page has no way to tell that the file underneath it
    changed. So the new bytes are stored under `quarantine/`, both hashes are recorded,
    and the document is reported as NOT fetched — which the suite already handles as a
    named skip and the demo already handles as a degraded mode.
    """
    target = corpus.quarantine_dir() / f"{doc.id}{doc.original_suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {
        "id": doc.id, "ok": False, "action": "download", "bytes": len(data),
        "sha256": got, "when": _now(), "hash_changed": True,
        "expected_sha256": doc.sha256, "actual_sha256": got,
        "quarantined": str(target),
        "error": ("the publisher has changed this file. The new bytes were quarantined "
                  "and both hashes recorded; the verified copy in the corpus was left "
                  "untouched. Re-read the document, re-check every page citation taken "
                  "against the old bytes, then update corpus/manifest.yaml "
                  "deliberately."),
    }


def _warn(record: dict, text: str) -> None:
    """Warnings accumulate. One slot would mean the second warning erased the first."""
    record.setdefault("warnings", []).append(text)


def _store(doc: corpus.Document, data: bytes, *, seed_dir: Optional[Path],
           action: str) -> dict:
    """Write the original and the page-marked text, and report what happened."""
    record: dict = {"id": doc.id, "ok": True, "action": action,
                    "bytes": len(data), "sha256": corpus.sha256_bytes(data),
                    "when": _now()}

    text_target = corpus.text_path(doc)

    if doc.media_type == corpus.TEXT_MEDIA:
        # The retrieval IS the text. Written verbatim: re-rendering it would change
        # byte offsets the committed citations were measured against.
        text_target.parent.mkdir(parents=True, exist_ok=True)
        text_target.write_bytes(data)
        record["text"] = "verbatim"
        record["pages"] = corpus.count_pages(data.decode("utf-8", "replace"))
    else:
        original = corpus.original_path(doc)
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(data)
        record["original"] = str(original)

        seeded_text = None
        if doc.text is not None and seed_dir is not None:
            candidate = seed_dir / doc.text.seed_path
            if candidate.is_file():
                blob = candidate.read_bytes()
                got = corpus.sha256_bytes(blob)
                if got == doc.text.sha256:
                    seeded_text = blob
                else:
                    record["text_seed_hash_changed"] = True
                    record["text_expected_sha256"] = doc.text.sha256
                    record["text_actual_sha256"] = got

        if seeded_text is not None:
            # Prefer the extraction the team verified. A fresh extraction from a newer
            # pypdf can shift offsets, and a shifted offset turns a correct citation
            # into a wrong one pointing at real-looking text.
            text_target.parent.mkdir(parents=True, exist_ok=True)
            text_target.write_bytes(seeded_text)
            record["text"] = "seeded"
            record["pages"] = corpus.count_pages(
                seeded_text.decode("utf-8", "replace"))
        else:
            text, pages = extract_pdf_text(data, doc)
            _write_text(text_target, text)
            record["text"] = "extracted"
            record["pages"] = pages
            if doc.text is not None:
                # The manifest records a verified extraction for this document and this
                # path could not use it — a download has no local seed directory to
                # read it from. `extracted` on its own is too quiet a label for that:
                # the offsets the committed citations were measured against may have
                # moved, and nobody downstream re-checks them.
                _warn(record,
                      "the verified text extraction recorded in the manifest was not "
                      "available on this path, so the text was re-extracted with the "
                      "installed pypdf. A fresh extraction can shift page offsets, so "
                      "re-check any page citation into this document. Seeding from a "
                      "local copy (--seed-from) uses the verified extraction instead.")

    if record.get("pages") is not None and record["pages"] != doc.pages:
        record["page_count_differs"] = True
        record["manifest_pages"] = doc.pages
    return record


# ── the run ─────────────────────────────────────────────────────────────────

def run(*, seed_from: Optional[Path], only: Optional[list[str]]) -> tuple[list[dict], int]:
    docs = list(corpus.documents())
    if only:
        wanted = [corpus.resolve(ref) for ref in only]
        ids = {d.id for d in wanted}
        docs = [d for d in docs if d.id in ids]

    records = []
    for doc in docs:
        if seed_from is not None:
            rec = seed_one(doc, seed_from)
        else:
            rec = download_one(doc)
        records.append(rec)
        _report(rec, doc)

    _write_log(records, seed_from=seed_from)
    failures = [r for r in records if not r.get("ok")]
    return records, len(failures)


def _report(rec: dict, doc: corpus.Document) -> None:
    if not rec.get("ok"):
        print(f"  FAIL  {doc.id}: {rec.get('error')}")
        if rec.get("expected_sha256"):
            print(f"        expected {rec['expected_sha256']}")
            print(f"        actual   {rec['actual_sha256']}")
        if rec.get("quarantined"):
            print(f"        quarantined at {rec['quarantined']}")
        return
    bits = [f"{rec['pages']}pp", rec["text"]]
    if rec.get("page_count_differs"):
        bits.append(f"pages differ from manifest ({rec['manifest_pages']})")
    if rec.get("text_seed_hash_changed"):
        bits.append("seeded text hash changed, re-extracted")
    print(f"  ok    {doc.id}  ({', '.join(bits)})")
    for text in rec.get("warnings", ()):
        print(f"        WARNING: {text}")


def _write_log(records: list[dict], *, seed_from: Optional[Path]) -> None:
    """Merge into the existing log rather than replacing it.

    `--only <id>` fetches one document. Writing the log from that run alone would
    discard the records for the other twenty-two, so the file would describe a corpus of
    one while the directory held twenty-three — and the log is the only account of when
    each document was retrieved and whether its hash drifted.
    """
    path = corpus.fetch_log_path()
    merged: dict[str, dict] = {}
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            for rec in (previous.get("documents") or []):
                if isinstance(rec, dict) and rec.get("id"):
                    merged[str(rec["id"])] = rec
        except (OSError, ValueError):
            merged = {}            # an unreadable log is replaced, not half-trusted
    for rec in records:
        if rec.get("id"):
            merged[str(rec["id"])] = rec

    order = {d.id: i for i, d in enumerate(corpus.documents())}
    log = {
        "when": _now(),
        "mode": "seed" if seed_from is not None else "download",
        "seed_from": str(seed_from) if seed_from is not None else None,
        "corpus_dir": str(corpus.corpus_dir()),
        "manifest": str(corpus.manifest_path()),
        # What the most recent run touched, so a merged log still says what just ran.
        "last_run": [r["id"] for r in records if r.get("id")],
        "documents": sorted(merged.values(),
                            key=lambda r: order.get(str(r.get("id")), len(order))),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Populate the VERIS document corpus outside git.")
    ap.add_argument("--seed-from", nargs="?", const=str(DEFAULT_SEED), default=None,
                    help=("copy from this local directory instead of downloading. "
                          f"With no value, defaults to {DEFAULT_SEED}"))
    ap.add_argument("--only", action="append", default=None,
                    help="limit to this manifest id (repeatable)")
    ap.add_argument("--status", action="store_true",
                    help="report what the corpus holds and exit")
    args = ap.parse_args(argv)

    try:
        corpus.load_manifest(refresh=True)
    except corpus.ManifestError as exc:
        print(f"manifest error: {exc}")
        return 2

    if args.status:
        print(json.dumps(corpus.status(), indent=2))
        return 0

    seed_from = Path(args.seed_from).expanduser().resolve() if args.seed_from else None
    if seed_from is not None and not seed_from.is_dir():
        print(f"seed directory does not exist: {seed_from}")
        return 2

    mode = f"seeding from {seed_from}" if seed_from else "downloading from publishers"
    print(f"corpus dir: {corpus.corpus_dir()}")
    print(f"mode: {mode}")
    records, failures = run(seed_from=seed_from, only=args.only)

    ok = sum(1 for r in records if r.get("ok"))
    changed = sum(1 for r in records if r.get("hash_changed"))
    print(f"\n{ok}/{len(records)} documents in the corpus"
          + (f", {changed} changed at the publisher" if changed else ""))
    print(f"log: {corpus.fetch_log_path()}")
    if failures:
        print(f"{failures} document(s) could not be fetched. The suite will skip the "
              f"tests that need them rather than pretending they ran.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
