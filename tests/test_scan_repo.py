"""The repository scanner. Every test here plants something and checks it is caught.

WHY PLANT RATHER THAN ASSERT THE REPO IS CLEAN. A scanner that reports nothing is
indistinguishable from a scanner that looks at nothing, and the second one passes CI
forever. So each rule gets a positive test with a deliberately planted finding, plus a
negative test with the honest thing that looks similar — because a guard with false
positives gets switched off, and a switched-off guard protects nothing.

The scanner is not pointed at the real repository here; that assertion — the brand
guard — lives in `test_licensing_and_branding.py`.

NO FORBIDDEN NAME IS WRITTEN OUT IN THIS FILE. The rule is that those names appear nowhere
in the repository, and this file is in the repository. Every planted finding is built at
runtime from the scanner's own decoded needle list, and the last test in the forbidden-
strings section reads this file and the scanner to hold them both to that.
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

spec = importlib.util.spec_from_file_location("_script_scan_repo",
                                              SCRIPTS / "scan_repo.py")
scan_repo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scan_repo
spec.loader.exec_module(scan_repo)

# The needles, taken from the scanner rather than written out (see the module docstring).
NEEDLES = scan_repo.FORBIDDEN_STRINGS
STEM = sorted(scan_repo.FORBIDDEN_STEMS)[0]
#: The one all-lowercase needle: the replaced renderer's import name, which is the shape
#: that turns up inside identifiers.
IMPORT_NAME = next(n for n in NEEDLES if n.islower())
#: Any ordinary whole-word needle, for tests that need one name and not a particular one.
A_NAME = next(n for n in NEEDLES if n.istitle() and n.isalpha() and n not in {STEM})
REG_ACRONYM, REG_NAME = scan_repo.REGULATOR_ALLOWLIST


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ── secrets ─────────────────────────────────────────────────────────────────

def test_a_planted_model_key_is_caught(tmp_path):
    _write(tmp_path, "src/thing.py",
           'CLIENT = make("sk-' + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6" + '")\n')
    report = scan_repo.scan(tmp_path)
    secrets = report.by_kind("SECRET")
    assert secrets, "a key-shaped literal went unreported"
    assert secrets[0].path == "src/thing.py"
    assert report.ok is False


def test_a_planted_private_key_block_is_caught(tmp_path):
    _write(tmp_path, "deploy/id_rsa.txt",
           "-----BEGIN RSA PRIVATE KEY-----\nMIIEoggg\n-----END RSA PRIVATE KEY-----\n")
    report = scan_repo.scan(tmp_path)
    assert report.by_kind("SECRET")


def test_a_planted_assigned_secret_is_caught(tmp_path):
    _write(tmp_path, "config.js", 'const client_secret = "9f8a7b6c5d4e3f2a1b0c";\n')
    report = scan_repo.scan(tmp_path)
    assert report.by_kind("SECRET")


def test_the_reported_excerpt_does_not_reproduce_the_whole_secret(tmp_path):
    """A scanner that prints the key in full moves the leak into the CI log, which is
    retained, searchable, and often more public than the file was."""
    key = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    _write(tmp_path, "x.py", f'K = "{key}"\n')
    report = scan_repo.scan(tmp_path)
    found = report.by_kind("SECRET")
    assert found
    assert key not in found[0].excerpt
    assert "..." in found[0].excerpt


def test_an_environment_template_with_names_only_is_not_a_finding(tmp_path):
    """`.env.example` is committed on purpose. It must list names and nothing else."""
    _write(tmp_path, ".env.example",
           "OPENAI_API_KEY=\nPROVEN_SMTP_PASSWORD=\nVERIS_CORPUS_DIR=\n")
    report = scan_repo.scan(tmp_path)
    assert report.by_kind("SECRET") == []
    assert report.by_kind("FORBIDDEN_FILE") == []


def test_a_placeholder_is_not_reported_as_a_secret(tmp_path):
    _write(tmp_path, "docs/DEPLOYMENT.md",
           'export API_KEY="your-key-here"\n'
           'password: "${DB_PASSWORD}"\n'
           'secret = os.environ["APP_SECRET"]\n')
    assert scan_repo.scan(tmp_path).by_kind("SECRET") == []


# ── forbidden files ─────────────────────────────────────────────────────────

def test_a_committed_env_file_is_caught(tmp_path):
    _write(tmp_path, ".env", "OPENAI_API_KEY=whatever\n")
    report = scan_repo.scan(tmp_path)
    assert [f.path for f in report.by_kind("FORBIDDEN_FILE")] == [".env"]


def test_a_committed_database_is_caught(tmp_path):
    (tmp_path / "veris.db").write_bytes(b"SQLite format 3\x00")
    report = scan_repo.scan(tmp_path)
    assert report.by_kind("FORBIDDEN_FILE")


def test_sqlite_wal_and_shm_sidecars_are_caught_for_every_supported_extension(tmp_path):
    """WAL files can retain recent encrypted records and wrapped tenant keys too."""
    names = (
        "portfolio.db-wal", "portfolio.db-shm",
        "portfolio.sqlite-wal", "portfolio.sqlite-shm",
        "portfolio.sqlite3-wal", "portfolio.sqlite3-shm",
    )
    for name in names:
        (tmp_path / name).write_bytes(b"SQLite sidecar")
    found = scan_repo.scan(tmp_path).by_kind("FORBIDDEN_FILE")
    assert {item.path for item in found} == set(names)


def test_sqlite_wal_and_shm_sidecars_are_gitignored_for_every_supported_extension():
    names = (
        "portfolio.db-wal", "portfolio.db-shm",
        "portfolio.sqlite-wal", "portfolio.sqlite-shm",
        "portfolio.sqlite3-wal", "portfolio.sqlite3-shm",
    )
    for name in names:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", name],
            cwd=SCRIPTS.parent, check=False)
        assert result.returncode == 0, f"{name} is not ignored"


def test_a_third_party_pdf_outside_the_synthetic_set_is_caught(tmp_path):
    """The insurer wordings are copyrighted. This is the rule that keeps them in the
    corpus, outside git, rather than in a directory somebody tidied them into."""
    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "chubb-selectplus-wording.pdf").write_bytes(b"%PDF-1.4\n")
    report = scan_repo.scan(tmp_path)
    found = report.by_kind("FORBIDDEN_FILE")
    assert found and "corpus" in found[0].detail


def test_the_synthetic_demo_documents_are_allowed(tmp_path):
    (tmp_path / "demo-documents").mkdir()
    (tmp_path / "demo-documents" / "02-policy-schedule.pdf").write_bytes(b"%PDF-1.4\n")
    assert scan_repo.scan(tmp_path).by_kind("FORBIDDEN_FILE") == []


def test_the_real_wording_is_caught_even_inside_the_synthetic_directory(tmp_path):
    """It used to sit alongside the thirteen synthetic documents, which is exactly why
    the directory allowance alone is not enough: the one real document in there is the
    one that must not ship."""
    (tmp_path / "demo-documents").mkdir()
    (tmp_path / "demo-documents" / "01-policy-wording.pdf").write_bytes(b"%PDF-1.4\n")
    found = scan_repo.scan(tmp_path).by_kind("FORBIDDEN_FILE")
    assert found and "real insurer wording" in found[0].detail


def test_a_private_key_file_is_caught(tmp_path):
    _write(tmp_path, "certs/server.key", "whatever\n")
    assert scan_repo.scan(tmp_path).by_kind("FORBIDDEN_FILE")


def test_a_resume_is_caught(tmp_path):
    """Personal data, and it has turned up in a repository before. The rule is cheap and
    the failure mode is a real person's address in public git history."""
    (tmp_path / "Shamanth-resume.pdf").write_bytes(b"%PDF-1.4\n")
    assert scan_repo.scan(tmp_path).by_kind("FORBIDDEN_FILE")


# ── forbidden strings ───────────────────────────────────────────────────────

def test_a_planted_forbidden_string_is_caught(tmp_path):
    _write(tmp_path, "web/landing/index.html", f"<p>Built at the {STEM}</p>\n")
    report = scan_repo.scan(tmp_path)
    found = report.by_kind("FORBIDDEN_STRING")
    assert found
    assert found[0].line == 1
    assert STEM in found[0].detail


def test_every_forbidden_string_is_actually_checked(tmp_path):
    """One rule per name, each planted on its own line and expected back. A list that
    grew an entry nothing tests is a list that stops being true."""
    lines = [f"token {s} here" for s in scan_repo.FORBIDDEN_STRINGS]
    _write(tmp_path, "notes.md", "\n".join(lines) + "\n")
    report = scan_repo.scan(tmp_path)
    caught = {f.line for f in report.by_kind("FORBIDDEN_STRING")}
    assert caught == set(range(1, len(lines) + 1)), (
        "a forbidden string in the list is not being detected")


def test_a_forbidden_string_inside_an_identifier_is_still_caught(tmp_path):
    """These names turn up as imports and attributes, not just prose."""
    _write(tmp_path, "src/ocr.py",
           f"import {IMPORT_NAME}\nrender = {IMPORT_NAME}.open(path)\n")
    assert len(scan_repo.scan(tmp_path).by_kind("FORBIDDEN_STRING")) == 2


@pytest.mark.parametrize("make", [
    lambda s: s + "e",                      # the event's "-ate" spelling
    lambda s: s + "or",                     # and its "-ator" spelling
    lambda s: (s + "or").upper() + " 2026",
    lambda s: s.lower() + "ion",
    lambda s: f"the {s}e final",
], ids=["ate", "ator", "upper-case", "lower-case", "mid-sentence"])
def test_a_stem_catches_every_word_it_begins(tmp_path, make):
    """REGRESSION. The stem was matched as a whole word, so it caught neither of the two
    words it exists to catch, and the scanner reported zero findings while two sat on the
    landing page. A stem must match at a word start with nothing required after it."""
    word = make(STEM)
    _write(tmp_path, "web/landing/index.html", f"<p>Built for the {word}</p>\n")
    found = scan_repo.scan(tmp_path).by_kind("FORBIDDEN_STRING")
    assert len(found) == 1, "a word beginning with the stem was not caught"


def test_a_stem_still_needs_a_word_start(tmp_path):
    """The left boundary is kept, so the stem buried inside a longer token is not a
    finding — the same rule the whole-word needles follow."""
    _write(tmp_path, "docs/notes.md", f"x{STEM.lower()}or is not the banned word\n")
    assert scan_repo.scan(tmp_path).by_kind("FORBIDDEN_STRING") == []


def test_every_declared_stem_is_one_of_the_forbidden_strings():
    assert scan_repo.FORBIDDEN_STEMS <= set(scan_repo.FORBIDDEN_STRINGS)


def test_an_ordinary_word_containing_a_forbidden_sequence_is_not_a_finding(tmp_path):
    """A guard that fires on innocent English gets switched off, and a switched-off
    guard protects nothing. A needle buried inside a longer word is not the banned token,
    whichever side the extra letters are on."""
    words = [n for n in NEEDLES if n not in scan_repo.FORBIDDEN_STEMS]
    _write(tmp_path, "docs/notes.md",
           "The scaffold was offset and the staff offsite.\n"
           + "".join(f"{n}esque and pre{n.lower()} are not names.\n" for n in words))
    assert scan_repo.scan(tmp_path).by_kind("FORBIDDEN_STRING") == []


def test_the_regulator_name_is_allowed_only_in_the_jurisdiction_packs(tmp_path):
    """A jurisdiction pack that cannot name the insurance regulator cannot cite it. So
    the name is permitted in the Singapore pack and in the generated compliance matrix,
    and nowhere else — not in branding, not in the README."""
    _write(tmp_path, "src/proven/jurisdictions/SG.yaml", f"regulator: {REG_NAME}\n")
    _write(tmp_path, "docs/COMPLIANCE.md", f"| rule | {REG_ACRONYM} | ... |\n")
    assert scan_repo.scan(tmp_path).by_kind("FORBIDDEN_STRING") == []

    _write(tmp_path, "README.md", f"Regulated by {REG_ACRONYM}.\n")
    found = scan_repo.scan(tmp_path).by_kind("FORBIDDEN_STRING")
    assert [f.path for f in found] == ["README.md"]


def test_the_regulator_allowlist_is_two_exact_files_not_a_directory(tmp_path):
    """The exception is the SINGAPORE pack, not every pack; the corpus manifest is not on
    it; and a path that merely begins like an allowed one is not allowed. Each of these
    passed when the allowlist was a list of prefixes that included the manifest."""
    near_misses = (
        "src/proven/jurisdictions/GENERIC.yaml",     # another country's pack
        "src/proven/jurisdictions/MY.yaml",
        "src/proven/jurisdictions/SG.yaml.bak",      # begins like the allowed file
        "src/proven/jurisdictions/SG/notes.yaml",
        "src/proven/jurisdictions/__init__.py",      # the pack loader, not a pack
        "corpus/manifest.yaml",                      # the corpus manifest
        "docs/COMPLIANCE.md.orig",
    )
    for rel in near_misses:
        _write(tmp_path, rel, f"publisher: {REG_NAME} ({REG_ACRONYM})\n")
    found = scan_repo.scan(tmp_path).by_kind("FORBIDDEN_STRING")
    assert sorted({f.path for f in found}) == sorted(near_misses)


def test_neither_the_scanner_nor_its_tests_carry_a_forbidden_name_literally():
    """The self-exemption below stops the scan from seeing these two files at all, so it
    cannot be what keeps them clean. This reads them directly: the rule that the names
    appear nowhere in the repository includes the two files that enforce it."""
    regexes = [scan_repo._stem_regex(n) if n in scan_repo.FORBIDDEN_STEMS
               else scan_repo._word_regex(n) for n in NEEDLES]
    regexes += [scan_repo._word_regex(n) for n in scan_repo.REGULATOR_ALLOWLIST]
    regexes.append(scan_repo._word_regex(scan_repo._VENDOR_CLIENT_ID_FILE))
    for rel in sorted(scan_repo._SELF_SCAN_FILES):
        text = (SCRIPTS.parent / rel).read_text(encoding="utf-8")
        hits = [rx.pattern for rx in regexes if rx.search(text)]
        assert not hits, f"{rel} carries a forbidden name literally"


def test_the_scanner_and_its_own_tests_are_not_reported(tmp_path):
    """Both hold every needle by definition: one declares the patterns, the other plants
    a finding per rule. If either reported itself the guard could never be green, and a
    guard that can never be green gets removed."""
    report = scan_repo.scan(SCRIPTS.parent)
    offenders = [f.path for f in report.findings
                 if f.path in scan_repo._SELF_SCAN_FILES]
    assert offenders == []


def test_the_exemption_covers_exactly_two_files(tmp_path):
    """Named explicitly so widening it is a visible change rather than a quiet one. An
    exemption list that grows without anyone noticing is how a scanner stops scanning."""
    assert scan_repo._SELF_SCAN_FILES == {
        "scripts/scan_repo.py", "tests/test_scan_repo.py"}


# ── canaries are not credentials ────────────────────────────────────────────

def test_a_self_declaring_canary_is_not_reported(tmp_path):
    """Two tests set a deliberately recognisable fake key and sweep every endpoint to
    prove it is never served. Those literals have to look like keys or the tests prove
    nothing, and they are the opposite of a leak — they exist to catch one."""
    _write(tmp_path, "tests/test_ai.py",
           'monkeypatch.setenv("OPENAI_API_KEY", "sk-test-CANARY-must-never-be-served")\n'
           'canary = "sk-live-LEAKCANARY-0123456789"\n')
    assert scan_repo.scan(tmp_path).by_kind("SECRET") == []


def test_a_real_shaped_key_inside_a_test_file_is_still_caught(tmp_path):
    """The canary exemption must not become a blanket pass for test files. A key pasted
    into a test is as committed, and as public, as one pasted anywhere else."""
    _write(tmp_path, "tests/test_thing.py",
           'KEY = "sk-' + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6" + '"\n')
    assert scan_repo.scan(tmp_path).by_kind("SECRET")


# ── reporting ───────────────────────────────────────────────────────────────

def test_a_clean_tree_is_reported_clean_and_exits_zero(tmp_path, capsys):
    _write(tmp_path, "src/ok.py", "VALUE = 1\n")
    assert scan_repo.main(["--path", str(tmp_path)]) == 0
    assert "clean" in capsys.readouterr().out


def test_a_finding_exits_non_zero(tmp_path):
    _write(tmp_path, ".env", "X=1\n")
    assert scan_repo.main(["--path", str(tmp_path)]) == 1


def test_the_text_report_survives_an_excerpt_the_console_cannot_encode(tmp_path,
                                                                      monkeypatch):
    """A report that stops halfway must not look like a report that finished.

    The excerpts are raw source lines, and this tree carries typographic quotes, rupee
    amounts and mojibake. `print` on a Windows cp1252 console raises UnicodeEncodeError
    on the first character it cannot represent, and the process then exits 1 — which is
    the same exit code an ordinary non-clean scan returns. Eleven findings of 293 would
    print and the reader would have no way to know the rest existed.

    Pytest's own capture is UTF-8, so no test that uses `capsys` can see this. This one
    drives a genuinely cp1252 stream with `errors="strict"`, which is what a console
    does.
    """
    _write(tmp_path, "web/landing/index.html",
           f"<p>Built at the {STEM} \u2014 \u20b95,00,000 \u2192 settled</p>\n")
    _write(tmp_path, "docs/notes.md", f"provider: {A_NAME} \u2192 replaced\n")

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict",
                              write_through=True)
    monkeypatch.setattr(sys, "stdout", stream)
    code = scan_repo.main(["--path", str(tmp_path)])
    stream.flush()
    out = stream.buffer.getvalue().decode("cp1252")

    assert code == 1
    # The whole report, not a prefix of it: both findings and the closing total.
    assert "web/landing/index.html:1" in out
    assert "docs/notes.md:1" in out
    assert "2 finding(s)" in out


def test_json_output_is_machine_readable(tmp_path, capsys):
    import json
    _write(tmp_path, ".env", "X=1\n")
    scan_repo.main(["--path", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["findings"][0]["kind"] == "FORBIDDEN_FILE"


def test_generated_and_vendored_directories_are_not_scanned(tmp_path):
    """Scanning a virtualenv finds other people's test fixtures and reports them as
    secrets. The noise is what kills the guard."""
    _write(tmp_path, ".venv/lib/x.py", 'K = "sk-' + "A" * 30 + '"\n')
    _write(tmp_path, "__pycache__/x.py", 'K = "sk-' + "A" * 30 + '"\n')
    assert scan_repo.scan(tmp_path).ok is True
