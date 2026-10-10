#!/usr/bin/env python
"""Pre-commit guard: secrets, files that must never be committed, forbidden strings.

    python scripts/scan_repo.py            # scan the repository, exit non-zero on a finding
    python scripts/scan_repo.py --json     # same, machine-readable
    python scripts/scan_repo.py --path X   # scan somewhere else (used by the tests)

THREE CLASSES OF FINDING, and they fail for different reasons.

  SECRET        An API key, a private key or a password in a tracked file. A submission
                that ships a key in git history is a disclosure incident, not a style
                problem, and history cannot be un-pushed.

  FORBIDDEN     A file that must not be in the repository at all: `.env`, a database, a
  FILE          a resume, or a third-party PDF. The PDFs are the subtle one — the
                insurer wordings the engine reads are other people's copyrighted
                material. We have a reader's right to them, not a publisher's, so they
                live in the corpus outside git. The synthetic example documents ARE
                committed, because we generated them.

  FORBIDDEN     A name that must not appear in a public repository. Checked as a whole
  STRING        word so an ordinary English word containing the sequence does not trip
                it.

THE ALLOWLIST IS NARROW AND DELIBERATE. Singapore's insurance regulator is named by its
full name and by its acronym, and a jurisdiction pack that cannot name the regulator
cannot cite it. So both are permitted in exactly two files, the Singapore jurisdiction
pack and the generated compliance matrix, and nowhere else: not in another country's
pack, not in the corpus manifest, not in branding, not in the README. Anything beyond
those two paths is a finding.

THE NAMES ARE STORED ENCODED. The rule is that the forbidden names appear NOWHERE in this
repository, and this file is part of the repository. So the needles below are base64 and
decoded at import. That is not secrecy — decoding is one line — it is so the file that
enforces the rule does not break it, and so a reader searching the public tree for one of
those names finds nothing at all. The same applies to the scanner's own tests, which build
their planted findings from the decoded values rather than from literals.

This scanner reports; it does not edit. Rebranding is a deliberate change with a commit
message, not something a script does behind the author's back.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

REPO = Path(__file__).resolve().parent.parent

# ── what not to look inside ─────────────────────────────────────────────────

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".hypothesis",
    "node_modules", ".mypy_cache", ".ruff_cache", ".agents", "veris-corpus",
}

# Binary and generated formats. A finding inside a PNG is noise; a PNG that should not
# be here is caught by the forbidden-FILE rules instead.
SKIP_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".zip", ".gz", ".db", ".sqlite", ".pyc", ".so", ".dll", ".exe",
}

MAX_SCAN_BYTES = 4 * 1024 * 1024

# ── secrets ─────────────────────────────────────────────────────────────────
# Shaped patterns only. "password" as a word appears in honest places — a login form, a
# docstring about PBKDF2 — and a scanner that cries wolf gets switched off.

SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("openai-style key", r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    ("bearer token literal", r"\bBearer\s+[A-Za-z0-9_\-\.]{24,}\b"),
    ("private key block", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("aws access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("google api key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("slack token", r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b"),
    ("github token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("assigned secret literal",
     r"(?i)\b(?:api[_-]?key|secret|client[_-]?secret|access[_-]?token|password)\b"
     r"\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']"),
)

# A value that is obviously not a secret: a placeholder, an env lookup, a format slot,
# a name rather than a value — or a CANARY.
#
# The canary case is worth spelling out. Two tests set a deliberately recognisable
# fake key and then sweep every endpoint to prove it is never served. Those literals
# have to look like keys or the tests prove nothing, and they are the opposite of a
# leak: they exist to catch one. A literal that announces itself as a canary or a test
# value is exempt; a key-shaped literal that does not is a finding wherever it sits,
# including inside a test file.
SECRET_FALSE_POSITIVE = re.compile(
    r"(?i)(x{6,}|\.{3,}|your[-_ ]?|example|placeholder|changeme|redacted|dummy|"
    r"<[^>]+>|\{[^}]*\}|\$\{|os\.environ|getenv|process\.env|"
    r"sk-proj-xxx|sk-test[-_]|sk-live-LEAK|canary|test[-_]?only|"
    r"NOT[-_ ]?A[-_ ]?REAL)")

# ── forbidden files ─────────────────────────────────────────────────────────

def _decoded(*encoded: str) -> tuple[str, ...]:
    """The needles, decoded. See "THE NAMES ARE STORED ENCODED" above."""
    return tuple(base64.b64decode(e).decode("utf-8") for e in encoded)


# A loose credential note for a removed vendor integration; named by its old filename.
_VENDOR_CLIENT_ID_FILE = _decoded("eGVyb19jbGllbnRfaWQudHh0")[0]

FORBIDDEN_NAMES = {".env", _VENDOR_CLIENT_ID_FILE, "veris.db", "proven.db"}
FORBIDDEN_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("environment file with values", r"^\.env(\..+)?$"),
    ("database file", r".+\.(db|sqlite|sqlite3)$"),
    ("credential file", r".*(_secret|-secret|secrets?)\.(txt|json|ya?ml|pem|key)$"),
    ("private key", r".*\.(pem|key|p12|pfx)$"),
    ("resume", r"(?i).*(resume|curriculum[-_ ]?vitae|\bcv\b).*\.(pdf|docx?)$"),
)
ALLOWED_ENV_FILES = {".env.example"}

#: The synthetic documents the team generated with `tests/pdfbuild*.py`. These are the
#: only PDFs the repository may contain: we wrote them, so there is no third party whose
#: material is being redistributed.
SYNTHETIC_PDF_DIRS = ("demo-documents",)
REAL_WORDING_PDF = "01-policy-wording.pdf"

# ── forbidden strings ───────────────────────────────────────────────────────

#: Two event names, an event organiser, the prior event's own name stem, an insurer that
#: was the earlier build's seeded carrier, an accounting vendor whose connector was
#: removed, a model provider that was removed, and the copyleft PDF renderer that was
#: replaced together with its import name. Base64; see the module docstring.
FORBIDDEN_STRINGS: tuple[str, ...] = _decoded(
    "U0ZG", "U2luZ2Fwb3JlIEZpblRlY2ggRmVzdGl2YWw=", "R0ZUTg==", "SGFja2NlbGVyYXQ=",
    "WnVyaWNo", "WGVybw==", "R3JvcQ==", "UHlNdVBERg==", "Zml0eg==",
)

#: Needles that are STEMS, not words: matched at a word start with no right-hand
#: boundary, because the forbidden thing is every word they begin. The event-name stem
#: (fourth above) is listed as a stem precisely so that it catches both the event's
#: "-ate" and "-ator" spellings. Matched as a whole word it caught neither — the scanner
#: reported zero such findings while two sat on the landing page, and a clean report was
#: being trusted for a string it could not see.
FORBIDDEN_STEMS: frozenset[str] = frozenset({FORBIDDEN_STRINGS[3]})

#: The regulator's acronym and full name, and the only two FILES they may appear in: the
#: Singapore jurisdiction pack and the generated compliance matrix. Exact paths, not
#: prefixes. A directory prefix admitted every future country's pack, and the corpus
#: manifest was listed too; the rule allows neither. A corpus document published by the
#: regulator would have to be named some other way, and that is a decision to take when
#: one exists, not latitude to leave open in advance.
REGULATOR_ALLOWLIST: tuple[str, ...] = _decoded(
    "TUFT", "TW9uZXRhcnkgQXV0aG9yaXR5IG9mIFNpbmdhcG9yZQ==")
REGULATOR_ALLOWED_PATHS: tuple[str, ...] = (
    "src/proven/jurisdictions/SG.yaml",
    "docs/COMPLIANCE.md",
)

#: Paths exempt from one specific forbidden string, with the reason recorded. Kept
#: empty-by-default on purpose: an exemption is a decision, and an undated, unexplained
#: exemption is how a guard stops guarding anything.
STRING_EXEMPTIONS: dict[str, tuple[str, ...]] = {}

#: This scanner and its test file. See the note in `check_file_content`.
_SELF_SCAN_FILES = frozenset({"scripts/scan_repo.py", "tests/test_scan_repo.py"})


@dataclass
class Finding:
    kind: str                  # SECRET | FORBIDDEN_FILE | FORBIDDEN_STRING
    path: str
    line: int
    detail: str
    excerpt: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "line": self.line,
                "detail": self.detail, "excerpt": self.excerpt}


@dataclass
class Report:
    root: str
    files_scanned: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def as_dict(self) -> dict:
        return {"root": self.root, "files_scanned": self.files_scanned,
                "ok": self.ok, "findings": [f.as_dict() for f in self.findings]}


def _walk(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        yield p


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def _word_regex(needle: str) -> re.Pattern[str]:
    """Whole-token match, so a needle inside a longer word does not trip the guard.

    `\\b` alone is not enough: the forbidden names turn up inside identifiers and file
    paths (a `name_render` helper, a `name.open(...)` call), which must still be caught,
    while an unrelated English word that merely contains the letters must not be. So the
    needle must not be flanked by a letter or digit, but may be flanked by punctuation.
    """
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])",
                      re.IGNORECASE)


def _stem_regex(needle: str) -> re.Pattern[str]:
    """A word START: not preceded by a letter or digit, followed by anything."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(needle), re.IGNORECASE)


_FORBIDDEN_RES = tuple(
    (s, _stem_regex(s) if s in FORBIDDEN_STEMS else _word_regex(s))
    for s in FORBIDDEN_STRINGS)
_REGULATOR_RES = tuple((s, _word_regex(s)) for s in REGULATOR_ALLOWLIST)
_SECRET_RES = tuple((label, re.compile(pat)) for label, pat in SECRET_PATTERNS)
_FORBIDDEN_NAME_RES = tuple(
    (label, re.compile(pat)) for label, pat in FORBIDDEN_NAME_PATTERNS)


def check_file_name(root: Path, p: Path) -> list[Finding]:
    rel = _rel(root, p)
    out: list[Finding] = []
    name = p.name

    if name in ALLOWED_ENV_FILES:
        return out

    if name in FORBIDDEN_NAMES:
        out.append(Finding("FORBIDDEN_FILE", rel, 0,
                           f"{name} must never be committed"))
        return out

    for label, rx in _FORBIDDEN_NAME_RES:
        if rx.match(name):
            out.append(Finding("FORBIDDEN_FILE", rel, 0,
                               f"{label}: {name} must never be committed"))
            return out

    if p.suffix.lower() == ".pdf":
        in_synthetic = any(rel.startswith(d + "/") for d in SYNTHETIC_PDF_DIRS)
        if not in_synthetic:
            out.append(Finding(
                "FORBIDDEN_FILE", rel, 0,
                "third-party PDF: policy wordings and judgments are copyrighted and "
                "belong in the corpus outside git, resolved by id from "
                "corpus/manifest.yaml"))
        elif name == REAL_WORDING_PDF:
            out.append(Finding(
                "FORBIDDEN_FILE", rel, 0,
                f"{name} is the real insurer wording, not a synthetic sample. It is "
                f"attached server-side from the corpus and must not be committed"))
    return out


def _regulator_allowed(rel: str) -> bool:
    return rel in REGULATOR_ALLOWED_PATHS


def check_file_content(root: Path, p: Path) -> list[Finding]:
    rel = _rel(root, p)
    out: list[Finding] = []
    if p.suffix.lower() in SKIP_SUFFIXES:
        return out
    try:
        if p.stat().st_size > MAX_SCAN_BYTES:
            return out
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out

    # The scanner and its own test file hold every SECRET PATTERN by definition: one
    # declares them, the other plants a key-shaped literal, a private-key block and an
    # assigned secret so a scanner that has stopped looking at anything cannot pass.
    # Neither can be clean of those while doing its job, and a guard that can never be
    # green gets deleted. The exemption is these two files and nothing else. The
    # forbidden NAMES are a different matter: both files keep them encoded and build
    # them at runtime, so neither carries one literally — and a test in the suite reads
    # both files to hold them to that, because this exemption would otherwise hide it.
    self_scan = rel in _SELF_SCAN_FILES
    exempt = STRING_EXEMPTIONS.get(rel, ())

    for n, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:
            line = line[:4000]

        if not self_scan:
            for label, rx in _SECRET_RES:
                m = rx.search(line)
                if not m:
                    continue
                if SECRET_FALSE_POSITIVE.search(line):
                    continue
                out.append(Finding("SECRET", rel, n, label, _mask(m.group(0))))

        if self_scan:
            continue

        for needle, rx in _FORBIDDEN_RES:
            if needle in exempt:
                continue
            if rx.search(line):
                out.append(Finding("FORBIDDEN_STRING", rel, n,
                                   f"forbidden string {needle!r}", line.strip()[:200]))

        if not _regulator_allowed(rel):
            for needle, rx in _REGULATOR_RES:
                if rx.search(line):
                    out.append(Finding(
                        "FORBIDDEN_STRING", rel, n,
                        f"{needle!r} is allowed only in the Singapore jurisdiction pack "
                        f"and the generated compliance matrix, not here",
                        line.strip()[:200]))
    return out


def _mask(value: str) -> str:
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]} ({len(value)} chars)"


# ── reporting that cannot stop halfway ──────────────────────────────────────
# The excerpts are raw source lines, and a tree mid-rebranding carries typographic
# quotes, currency symbols and mojibake. `print` on a cp1252 console raises
# UnicodeEncodeError on the first character it cannot represent and the process dies
# with exit 1 — the SAME exit code an ordinary non-clean scan returns. So a report that
# stopped after eleven of 293 findings is indistinguishable from a complete one, which
# makes this guard unreadable in exactly the window it matters: the rebranding task
# runs it to decide when the tree is clean.
#
# Every unencodable character is replaced for the console only. The finding itself is
# untouched, and `--json` is unaffected because `json.dumps` escapes non-ASCII already.

def _printable(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, "replace").decode(encoding, "replace")
    except LookupError:                   # an encoding Python cannot name: fall back
        return text.encode("ascii", "replace").decode("ascii")


def _say(text: str = "") -> None:
    print(_printable(text))


def scan(root: Optional[Path | str] = None) -> Report:
    base = Path(root) if root is not None else REPO
    report = Report(root=str(base))
    for p in _walk(base):
        report.files_scanned += 1
        report.findings.extend(check_file_name(base, p))
        report.findings.extend(check_file_content(base, p))
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scan the repository for things that must "
                                             "never be committed.")
    ap.add_argument("--path", default=None, help="directory to scan (default: the repo)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    report = scan(args.path)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
        return 0 if report.ok else 1

    _say(f"scanned {report.files_scanned} files under {report.root}")
    if report.ok:
        _say("clean: no secrets, no forbidden files, no forbidden strings")
        return 0

    for kind in ("SECRET", "FORBIDDEN_FILE", "FORBIDDEN_STRING"):
        group = report.by_kind(kind)
        if not group:
            continue
        _say(f"\n{kind}  ({len(group)})")
        for f in group:
            where = f"{f.path}:{f.line}" if f.line else f.path
            _say(f"  {where}  {f.detail}")
            if f.excerpt:
                _say(f"      {f.excerpt}")
    _say(f"\n{len(report.findings)} finding(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
