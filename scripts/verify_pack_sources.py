#!/usr/bin/env python
"""Check every quote in the jurisdiction packs against the primary source it cites.

    python scripts/verify_pack_sources.py            # every pack, fetched live
    python scripts/verify_pack_sources.py --pack SG  # one pack
    python scripts/verify_pack_sources.py --json     # machine-readable report

WHY THIS EXISTS. A pack entry marked SOURCE_QUOTED says: these exact words are in the
regulator's or legislature's own document at this address, retrieved on this date. That
claim is only worth something if a machine has looked. So this fetches each source, turns
it into plain text, and checks the quote is really in it.

  SOURCE_QUOTED   the quote MUST be found. A miss is a failure (exit 1): either the quote
                  was wrong, or the source changed and the entry has to be re-verified.
  HUMAN_VERIFIED  a quote, where one is recorded, is checked too. A miss is a warning,
                  because the entry rests on the person named in `verified_by`.
  CANDIDATE       nothing to check; it is never shown to an owner.

A source that cannot be fetched is reported as UNREACHABLE and is not a pass. Per the plan,
an entry whose primary source cannot be fetched belongs at CANDIDATE, with a line in the
human-actions log, until somebody can fetch it.

WHAT "FOUND" MEANS. Both the quote and the fetched text are reduced to the same form:
whitespace collapsed, and the spaces an HTML page puts inside brackets and before
punctuation — "( b )", "per year ." — removed. Nothing else is normalised: not case, not
dashes, not quotation marks. A quote that differs from its source in any word is a miss.

A source address that is a corpus document's own published address is read from the local
corpus (verified against the manifest hash) instead of being downloaded again.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import re
import sys
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proven import corpus, jurisdiction  # noqa: E402

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_TIMEOUT = 40

FOUND = "FOUND"
MISSING = "MISSING"
UNREACHABLE = "UNREACHABLE"


def normalise(text: str) -> str:
    """The comparable form. See WHAT "FOUND" MEANS in the module docstring."""
    t = " ".join(str(text).split())
    t = re.sub(r"\(\s+", "(", t)
    t = re.sub(r"\s+([).,;:])", r"\1", t)
    return t


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def fetch_live(url: str) -> str:
    """The source as plain text. Raises on any network or decoding failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "text/html,application/pdf,*/*"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    if "pdf" in ctype.lower() or data[:5] == b"%PDF-":
        return pdf_to_text(data)
    return html_to_text(data.decode("utf-8", "replace"))


def corpus_text_for(url: str) -> Optional[str]:
    """The local corpus copy of a published document whose address this is, if held."""
    try:
        docs = corpus.documents()
    except corpus.CorpusError:
        return None
    for d in docs:
        if d.source_url and d.source_url == url:
            path = corpus.present_text(d.id)
            if path is not None:
                return path.read_text(encoding="utf-8")
    return None


def fetch(url: str) -> str:
    local = corpus_text_for(url)
    return local if local is not None else fetch_live(url)


@dataclass
class Check:
    pack: str
    entry: str
    status: str
    source_url: Optional[str]
    result: str                 # FOUND | MISSING | UNREACHABLE
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == jurisdiction.SOURCE_QUOTED and self.result != FOUND


def _quoted(p: "jurisdiction.Pack") -> list[tuple[str, "jurisdiction.Verification"]]:
    """Every (id, verification) in the pack that records a quote and a source."""
    out = [(e.id, e.verification) for e in p.entries()]
    out += [(f"{p.code}:kind:{k.kind}", k.verification) for k in p.document_kinds]
    seen: set[tuple[str, str]] = set()
    unique = []
    for ident, v in out:
        if not v.quote or not v.source_url:
            continue
        key = (ident, v.source_url)
        if key not in seen:
            seen.add(key)
            unique.append((ident, v))
    return unique


def check_pack(p: "jurisdiction.Pack",
               fetcher: Callable[[str], str] = fetch) -> list[Check]:
    cache: dict[str, str | Exception] = {}
    checks: list[Check] = []
    for ident, v in _quoted(p):
        url = v.source_url
        if url not in cache:
            try:
                cache[url] = fetcher(url)
            except Exception as exc:                  # any failure is "not confirmed"
                cache[url] = exc
        got = cache[url]
        if isinstance(got, Exception):
            checks.append(Check(p.code, ident, v.status, url, UNREACHABLE,
                                f"{type(got).__name__}: {str(got)[:160]}"))
            continue
        found = normalise(v.quote) in normalise(got)
        checks.append(Check(p.code, ident, v.status, url, FOUND if found else MISSING,
                            "" if found else "the quote is not in the fetched text"))
    return checks


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--pack", action="append", help="check one pack (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    codes = args.pack or [c for c in jurisdiction.pack_codes()]
    checks: list[Check] = []
    for code in codes:
        checks.extend(check_pack(jurisdiction.pack(code.upper())))

    failed = [c for c in checks if c.failed]
    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in checks],
                          "failed": len(failed)}, indent=2, ensure_ascii=False))
    else:
        for c in checks:
            mark = "ok  " if c.result == FOUND else ("FAIL" if c.failed else "warn")
            print(f"{mark} {c.result:<11} {c.status:<14} {c.entry}  {c.detail}")
        print(f"\n{len(checks)} quote(s) checked, {len(failed)} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
