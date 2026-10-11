"""Golden snapshots of the Singapore SME output, pinned before the jurisdiction packs.

WHY THIS EXISTS. The jurisdiction-pack change moved every Singapore constant the SME engine
used — the work injury thresholds, the work-pass medical minimum, the CPF / ACRA / MOM
authorship mapping, the currency symbol, the premium-payment-warranty day count — out of
Python and into `src/proven/jurisdictions/SG.yaml`, and made the rules engine run only the
rules of the pack a bundle's own documents select. On a Singapore case that change is meant
to be invisible. "Meant to be" is not evidence, so the output was captured from the tree
BEFORE the change and is compared byte for byte after it (`tests/test_sme_golden.py`).

    python tests/sme_golden.py --write      # capture (only ever run on a known-good tree)
    python tests/sme_golden.py --check      # compare, print the first difference

THREE SCENARIOS, because the change touches three different intake paths:

  demo                The worked example as `/api/brief` builds it: the published wording
                      from the corpus plus the JSON fixtures (`pipeline.run_case`).
  example-upload      The example documents uploaded through `/api/bundle` with the server
                      attaching the wording — the path a viewer actually watches, and the one
                      where author_class is derived from the detected kind.
  example-synthetic   The same upload WITHOUT the wording. Needs no corpus, so a clean clone
                      and CI hold the guarantee too.

Each scenario records the Brief and every derived screen that reads the changed constants.

NORMALISATION REMOVES ONLY WHAT IS RANDOM PER RUN: 32-hex row ids, the session-derived
business id, and wall-clock timestamps. One sub-tree is dropped, `orchestration`, because the
orchestrator ages external signals against today's date, so its overlay would change on a
date rather than on a code change. Nothing else is loosened — every headline, figure, state,
class, quote and offset is compared exactly.

THE PUBLISHED WORDING IS NOT COMMITTED, NOT EVEN AS A SNAPSHOT. Two scenarios read a real
insurer's copyrighted wording, and their output quotes it at length — clause quotations,
unaccounted provisions, the warranty register's excerpts: over a hundred passages. Storing
those in git would redistribute a large part of a document this repository deliberately
keeps outside it (corpus/manifest.yaml, `redistribute: false`). So for those scenarios any
string that shares a 40-character run with the wording is stored as its length and SHA-256
instead of its text (`protect_wording`). The comparison stays exact — one changed character
in an excerpt changes its hash — and `test_no_committed_golden_file_quotes_the_wording`
holds the files to it.

`ADDED_KEYS` names the keys introduced after the capture. Each is additive (it adds a field,
it changes none) and each is asserted on its own in `tests/test_jurisdiction.py`. A key is
added to that set deliberately, with a reason, never to make a difference go away.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

GOLDEN_DIR = HERE / "golden"

#: Keys that did not exist when the snapshots were taken. Removed before comparison, at any
#: depth, and asserted separately where they were introduced.
#:   jurisdiction — the detected country, its confidence, the reasons both ways and the pack
#:                  applied (T03). Additive: it reports a decision, it alters no finding.
#:   pack_sources — the quote, source link and verification status of each pack entry a
#:                  finding, expectation, register entry or statutory block states a figure
#:                  from (T03; PLAN rule 17). Additive: provenance beside unchanged text.
ADDED_KEYS: frozenset[str] = frozenset({"jurisdiction", "pack_sources"})

#: Dropped because it is a function of today's date, not of the code (see the docstring).
DATE_DEPENDENT_KEYS: frozenset[str] = frozenset({"orchestration"})

_ID = re.compile(r"\b[0-9a-f]{32}\b")
_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_UPLOADED_BUSINESS = re.compile(r"\buploaded-[A-Za-z0-9_-]{8}(?![A-Za-z0-9_-])")

#: The derived screens each scenario records, in addition to the Brief.
DERIVED_ROUTES: tuple[str, ...] = (
    "/api/stages", "/api/warranty", "/api/cyber", "/api/score", "/api/blindspots",
    "/api/gap-summary", "/api/supply-chain", "/api/bi", "/api/information-value",
)


def normalise(obj: Any) -> Any:
    """The comparable form of a payload. See NORMALISATION in the module docstring."""
    if isinstance(obj, dict):
        return {k: normalise(v) for k, v in obj.items()
                if k not in ADDED_KEYS and k not in DATE_DEPENDENT_KEYS}
    if isinstance(obj, list):
        return [normalise(v) for v in obj]
    if isinstance(obj, str):
        s = _ID.sub("<id>", obj)
        s = _TS.sub("<ts>", s)
        return _UPLOADED_BUSINESS.sub("uploaded-<sid>", s)
    return obj


#: A run this long, shared with the wording, makes a string a quotation of it. Long enough
#: that ordinary insurance phrasing in our own sentences does not trip it, short enough
#: that no clause quotation slips under it.
WORDING_WINDOW = 40
WORDING_CORPUS_ID = "sg-chubb-selectplus-wording"


@lru_cache(maxsize=1)
def wording_windows() -> Optional[frozenset[str]]:
    """Every 40-character run of the published wording (whitespace collapsed), or None
    when the corpus does not hold it.

    Read from the PDF through the same extractor the engine quotes from, so a quotation in
    the output is matched against exactly the text it was cut from, and from the corpus's
    verified text copy as well, in case the two extractions differ anywhere.
    """
    from proven import corpus, ingest
    try:
        pdf = corpus.present_original(WORDING_CORPUS_ID)
        seed = corpus.present_text(WORDING_CORPUS_ID)
    except corpus.CorpusError:
        return None
    if pdf is None:
        return None
    texts = ["\n".join(ingest.extract_pdf_pages_cached(pdf.read_bytes(), pdf.name))]
    if seed is not None:
        texts.append(seed.read_text(encoding="utf-8"))
    windows: set[str] = set()
    for raw in texts:
        text = " ".join(raw.split())
        windows.update(text[i:i + WORDING_WINDOW]
                       for i in range(len(text) - WORDING_WINDOW + 1))
    return frozenset(windows)


def quotes_wording(s: str, windows: frozenset[str]) -> bool:
    t = " ".join(s.split())
    return any(t[i:i + WORDING_WINDOW] in windows
               for i in range(len(t) - WORDING_WINDOW + 1))


def protect_wording(obj: Any, windows: frozenset[str]) -> Any:
    """Replace every string that quotes the published wording by its length and hash.

    Applied to the NORMALISED form, so a stored snapshot and a fresh capture are reduced
    the same way. See THE PUBLISHED WORDING IS NOT COMMITTED in the module docstring.
    """
    if isinstance(obj, dict):
        return {k: protect_wording(v, windows) for k, v in obj.items()}
    if isinstance(obj, list):
        return [protect_wording(v, windows) for v in obj]
    if isinstance(obj, str) and quotes_wording(obj, windows):
        digest = hashlib.sha256(obj.encode("utf-8")).hexdigest()
        return f"<quotes the published wording: {len(obj)} chars, sha256 {digest}>"
    return obj


def comparable(name: str, payload: Any) -> Any:
    """What is stored for, and compared against, scenario `name`."""
    out = normalise(payload)
    if SCENARIOS[name][1]:
        windows = wording_windows()
        if windows is None:
            raise RuntimeError(f"{name} reads the published wording, and the corpus does "
                               f"not hold it")
        out = protect_wording(out, windows)
    return out


def _client():
    from fastapi.testclient import TestClient

    from proven.api import app
    return TestClient(app)


def _derived(client) -> dict:
    out = {}
    for route in DERIVED_ROUTES:
        r = client.get(route)
        out[route] = {"status": r.status_code, "body": r.json()}
    return out


def _example_files(client) -> tuple[dict, list]:
    manifest = client.get("/api/example/manifest").json()
    files = []
    for doc in manifest["documents"]:
        r = client.get("/api/example/file/" + doc["name"])
        assert r.status_code == 200, (doc["name"], r.status_code)
        files.append(("files", (doc["name"], r.content, "application/pdf")))
    return manifest, files


def capture_demo() -> dict:
    with _client() as client:
        brief = client.get("/api/brief")
        assert brief.status_code == 200, brief.text[:300]
        return {"brief": brief.json(), "derived": _derived(client)}


def _capture_upload(attach: bool) -> dict:
    with _client() as client:
        manifest, files = _example_files(client)
        data = {"business_name": manifest["business_name"]}
        if attach:
            data["attach_example_wording"] = "1"
        r = client.post("/api/bundle", files=files, data=data)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        return {"brief": body["brief"], "intake": body["intake"],
                "derived": _derived(client)}


def capture_example_upload() -> dict:
    return _capture_upload(attach=True)


def capture_example_synthetic() -> dict:
    return _capture_upload(attach=False)


#: name -> (capture function, needs the published wording from the corpus)
SCENARIOS: dict[str, tuple[Callable[[], dict], bool]] = {
    "demo": (capture_demo, True),
    "example-upload": (capture_example_upload, True),
    "example-synthetic": (capture_example_synthetic, False),
}


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"sme-{name}.json"


def dumps(name: str, payload: Any) -> str:
    return json.dumps(comparable(name, payload), indent=1, sort_keys=True,
                      ensure_ascii=False) + "\n"


def load(name: str) -> Any:
    return json.loads(golden_path(name).read_text(encoding="utf-8"))


def first_difference(expected: Any, actual: Any, where: str = "$") -> str | None:
    """A readable pointer to the first place two normalised payloads differ."""
    if type(expected) is not type(actual):
        return f"{where}: type {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        for k in sorted(set(expected) | set(actual)):
            if k not in actual:
                return f"{where}.{k}: missing now"
            if k not in expected:
                return f"{where}.{k}: new key {json.dumps(actual[k])[:200]}"
            d = first_difference(expected[k], actual[k], f"{where}.{k}")
            if d:
                return d
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{where}: length {len(expected)} != {len(actual)}"
        for i, (e, a) in enumerate(zip(expected, actual)):
            d = first_difference(e, a, f"{where}[{i}]")
            if d:
                return d
        return None
    if expected != actual:
        return f"{where}: {json.dumps(expected)[:300]} != {json.dumps(actual)[:300]}"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--protect-stored", action="store_true",
                       help="apply protect_wording to the stored snapshots in place, "
                            "without recapturing them")
    args = ap.parse_args(argv)

    if args.protect_stored:
        # Keeps the snapshots' provenance — they stay the capture of the tree they were
        # taken from — while removing the wording text from them.
        windows = wording_windows()
        if windows is None:
            print("the corpus does not hold the published wording; nothing done")
            return 1
        for name, (_capture, needs_corpus) in SCENARIOS.items():
            if not needs_corpus or not golden_path(name).exists():
                continue
            protected = protect_wording(load(name), windows)
            golden_path(name).write_text(
                json.dumps(protected, indent=1, sort_keys=True, ensure_ascii=False)
                + "\n", encoding="utf-8", newline="\n")
            print(f"{name}: protected in place")
        return 0

    # The same offline posture the suite runs under (see conftest.py).
    import os
    os.environ["PROVEN_LIVE_SOURCES"] = "0"
    for key in [k for k in os.environ if k.upper().endswith("_API_KEY")]:
        os.environ.pop(key, None)

    from proven import corpus
    wording_missing = bool(corpus.missing(["sg-chubb-selectplus-wording"]))
    status = 0
    GOLDEN_DIR.mkdir(exist_ok=True)
    for name, (capture, needs_corpus) in SCENARIOS.items():
        if needs_corpus and wording_missing:
            print(f"{name}: skipped, the published wording is not in the corpus")
            continue
        text = dumps(name, capture())
        path = golden_path(name)
        if args.write:
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"{name}: wrote {path.name} ({len(text)} chars)")
            continue
        diff = first_difference(load(name), json.loads(text))
        print(f"{name}: {'unchanged' if diff is None else 'CHANGED at ' + diff}")
        status |= diff is not None
    return status


if __name__ == "__main__":
    raise SystemExit(main())
