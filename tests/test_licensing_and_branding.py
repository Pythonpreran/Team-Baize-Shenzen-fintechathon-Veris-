"""The repository is licence-clean, branded as VERIS, and free of what it must not carry.

Four groups, each pinning something the rebranding and licensing work established:

  1. THE BRAND GUARD. The real repository scans clean — no secret, no forbidden file, no
     forbidden string — and the regulator-name allowlist stays exactly as narrow as it is
     documented to be. This is the test that makes the scanner's verdict part of the
     suite rather than a step somebody remembers to run.
  2. LICENSING. Apache-2.0, a NOTICE with the credits bundled licences require,
     generated third-party notices that cover every pinned runtime requirement with every
     licence file each component declares and that name the platform they describe, an
     install pinned package by package so those notices describe what a fresh install
     ships, and an OPEN-SOURCE.md that gives each component, service and data source its
     role.
  3. DE-SCOPING. The live accounting connector is gone and the seam is documented in its
     place; the carrier workspace speaks for a fictional insurer and is not offered at
     sign-in.
  4. SYNTHETIC DOCUMENTS. Any synthetic document bearing a real insurer's name says on
     every page that no insurer issued it, and every committed synthetic PDF is exactly
     what its generator produces — including after a checkout that converts line
     endings, because git is told the PDFs are binary.
"""
from __future__ import annotations

import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pdfbuild
import pdfbuild_demo
import pdfbuild_renewal
from proven import auth, connectors, detect
from proven.api import app

REPO = Path(__file__).resolve().parents[1]
TEAM = ("Preran S Gowda", "Shamanth M S", "Shreya Belavatha Jayaram", "Shashank S")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _scanner():
    spec = importlib.util.spec_from_file_location(
        "_scan_repo_for_brand_guard", REPO / "scripts" / "scan_repo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_requirements() -> list[tuple[str, str]]:
    """(name, pinned version) for every runtime pin in requirements.txt."""
    out: list[tuple[str, str]] = []
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            if "development and tests" in line:
                break
            continue
        m = re.match(r"^\s*([A-Za-z0-9._-]+)==([^\s#]+)", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


#: What pip treats as a comment in a requirements or constraints file: a `#` at the start
#: of a line or after whitespace.
_PIP_COMMENT = re.compile(r"(^|\s+)#.*$")


def _pip_lines(path: Path) -> list[tuple[int, str]]:
    """(line number, content) for every non-blank line, comments removed, as pip reads it."""
    out = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = _PIP_COMMENT.sub("", raw).strip()
        if line:
            out.append((n, line))
    return out


def _constraint_lines():
    """(line number, parsed requirement) for every pin in constraints.txt."""
    from packaging.requirements import Requirement
    return [(n, Requirement(line)) for n, line in _pip_lines(REPO / "constraints.txt")]


def _pins() -> dict:
    """canonical name -> parsed pin, from constraints.txt."""
    return {_canon(req.name): req for _, req in _constraint_lines()}


def _pinned_version(req) -> str:
    return next(iter(req.specifier)).version


def _pin_applies_here(req) -> bool:
    return req.marker is None or req.marker.evaluate()


def _closure_against_pins(gen):
    """Compare the installed closure with constraints.txt.

    Returns (needed, unpinned, differing): every package the runtime and development
    requirements resolve to here (canonical name -> distribution name), the ones with no
    pin that applies on this platform written as the line to add, and the ones installed
    at a version other than their pin. The closure is the notices generator's own, so
    this test and THIRD_PARTY_NOTICES.md agree on what "the closure" is.
    """
    from importlib import metadata

    runtime_roots, dev_roots = gen.pinned_requirements()
    needed = {_canon(n): n for n in gen.closure(dev_roots)}
    needed.update({_canon(n): n for n in gen.closure(runtime_roots)})
    pins = _pins()
    unpinned, differing = [], []
    for key, name in sorted(needed.items()):
        installed = metadata.version(name)
        req = pins.get(key)
        if req is None or not _pin_applies_here(req):
            unpinned.append(f"{name}=={installed}")
        elif _pinned_version(req) != installed:
            differing.append(f"{name}: installed {installed}, pinned {_pinned_version(req)}")
    return needed, unpinned, differing


# ── 1. the brand guard ───────────────────────────────────────────────────────

def test_the_repository_scans_clean():
    """No secret, no forbidden file, no forbidden string — anywhere in the tree."""
    report = _scanner().scan(REPO)
    found = [f"{f.kind} {f.path}:{f.line} {f.detail}" for f in report.findings]
    assert report.files_scanned > 100
    assert not found, "the repository is not clean:\n" + "\n".join(found)


def test_the_regulator_allowlist_is_exactly_as_narrow_as_documented():
    """The regulator's name may appear in the Singapore jurisdiction pack and the
    generated compliance matrix and nowhere else — not in another country's pack, not in
    the corpus manifest, not even in the scanner that declares the rule, which keeps it
    encoded. A path quietly added here would widen the exception without anyone
    deciding to."""
    scan_repo = _scanner()
    assert scan_repo.REGULATOR_ALLOWED_PATHS == (
        "src/proven/jurisdictions/SG.yaml", "docs/COMPLIANCE.md")
    assert scan_repo.STRING_EXEMPTIONS == {}, "an exemption is a decision; none is taken"


def test_the_landing_page_and_readme_name_the_four_team_members_and_no_address():
    landing = (REPO / "web" / "landing" / "index.html").read_text(encoding="utf-8")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for name in TEAM:
        assert name in landing, f"{name} is missing from the landing page"
        assert name in readme, f"{name} is missing from the README"
    assert not EMAIL.findall(landing), "an email address is on the landing page"
    assert not EMAIL.findall(readme), "an email address is in the README"


def test_the_api_describes_the_product_it_now_is():
    schema = TestClient(app).get("/openapi.json").json()
    assert schema["info"]["title"] == "VERIS"
    assert "households and small businesses" in schema["info"]["description"]
    # The posture statement survives the rebrand: /docs still says there is no auth.
    assert "No authentication" in schema["info"]["description"]


# ── 2. licensing ─────────────────────────────────────────────────────────────

def test_the_licence_is_apache_2_and_the_package_says_so():
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
    assert "END OF TERMS AND CONDITIONS" in text
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["license"] == {"text": "Apache-2.0"}


def test_the_notice_names_the_authors_and_disowns_the_third_party_documents():
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    for name in TEAM:
        assert name in notice
    assert "NOT distributed" in notice
    assert "SYNTHETIC SAMPLE" in notice
    assert not EMAIL.findall(notice)


def test_third_party_notices_cover_every_pinned_runtime_requirement():
    notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    rows = {}
    for m in re.finditer(r"^\| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \|", notices, re.MULTILINE):
        rows[_canon(m.group(1))] = (m.group(2).strip(), m.group(3).strip())
    for name, version in _runtime_requirements():
        assert _canon(name) in rows, f"{name} is missing from THIRD_PARTY_NOTICES.md"
        assert rows[_canon(name)][0] == version, (
            f"{name} is pinned at {version} but the notices describe "
            f"{rows[_canon(name)][0]} — regenerate them")


def test_no_runtime_component_is_under_a_copyleft_licence():
    """The renderer this build replaced was copyleft, which is incompatible with an
    Apache-2.0 release of the whole. Checked on what the notices say is shipped."""
    notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    runtime = notices.split("## Runtime components", 1)[1].split("## Development-only", 1)[0]
    copyleft = [line for line in runtime.splitlines()
                if line.startswith("|") and re.search(r"\b(A?GPL|EUPL|SSPL)\b", line)]
    assert not copyleft, f"copyleft runtime component(s): {copyleft}"


def _notices_generator():
    spec = importlib.util.spec_from_file_location(
        "_gen_third_party_notices", REPO / "scripts" / "gen_third_party_notices.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _notices() -> str:
    return (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")


def _runtime_table(notices: str) -> list[tuple[str, str]]:
    """(name, version) for every row of the runtime components table."""
    table = notices.split("## Runtime components", 1)[1].split("## Development-only", 1)[0]
    return [(m.group(1), m.group(2)) for m in re.finditer(
        r"^\| ([^|]+?) \| ([^|]+?) \| [^|]+? \|", table, re.MULTILINE)
        if m.group(1) not in {"Package", "---"}]


def _licence_sections(notices: str) -> dict[str, str]:
    """`name version` -> the body of its licence-text section.

    Fence-aware: a licence file written in markdown has headings of its own, and a
    heading inside a fenced text block is part of that text, not a new section."""
    body = notices.split("## Licence texts of the runtime components", 1)[1]
    sections: dict[str, list[str]] = {}
    current = None
    fenced = False
    for line in body.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("### "):
            current = line[4:].strip()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def test_every_runtime_component_has_its_licence_text_reproduced():
    """NOTICE promises the licence text of every runtime component. The renderer added
    for licensing reasons was the one component whose section said it shipped none,
    while its wheel declares eighteen licence files; a table row with a licence NAME is
    not the text several of those licences require to accompany a binary."""
    notices = _notices()
    sections = _licence_sections(notices)
    rows = _runtime_table(notices)
    assert len(rows) > 20
    for name, version in rows:
        body = sections.get(f"{name} {version}")
        assert body is not None, f"{name} {version} has no licence-text section"
        assert "ships no licence file" not in body, f"{name} {version} has no licence text"
        assert re.search(r"^```text\n\S", body, re.MULTILINE), (
            f"{name} {version}: no licence text block")


def test_the_renderer_section_reproduces_the_licences_of_what_is_compiled_into_it():
    """The PDFium binary carries FreeType, ICU, libjpeg-turbo and more. Their terms
    travel with it, each labelled with the file the wheel ships it in."""
    sections = _licence_sections(_notices())
    body = next(v for k, v in sections.items() if k.startswith("pypdfium2 "))
    for needle in ("Apache License", "The FreeType Project LICENSE", "UNICODE LICENSE",
                   "Independent JPEG Group", "The PDFium Authors", "COPYRIGHT NOTICE"):
        assert needle in body, f"the pypdfium2 section does not reproduce {needle!r}"
    labels = re.findall(r"^From `([^`]+)`:$", body, re.MULTILINE)
    assert len(labels) >= 10 and len(labels) == len(set(labels))
    # FreeType's licence is Latin-1. Read as UTF-8 its copyright sign is lost; here it
    # survives, in the credit line FreeType asks for.
    assert "Portions of this software are copyright \u00a9" in body


#: FreeType's binding redistribution term (its Legal Terms, section 2), and the IJG's, as
#: the bundled licence texts word them.
_FREETYPE_BINARY_TERM = ("Redistribution in binary form must provide a disclaimer that "
                         "states that the software is based in part of the work of the "
                         "FreeType Team")
_IJG_BINARY_TERM = "this software is based in part on the work of the Independent JPEG Group"


def test_the_notice_carries_the_credits_the_bundled_licences_require():
    """NOTICE is the documentation that accompanies every distribution of this work, so
    it carries the statements bundled native libraries attach to binary redistribution.

    FreeType words its request twice. The introduction asks the documentation to
    acknowledge that the FreeType code is used; the binding Legal Terms, section 2,
    require a binary's distribution documentation to state that the software is based in
    part on the work of the FreeType Team. The first alone is not the second. The IJG
    terms require the documentation accompanying executable code to state that it is
    based in part on the work of the Independent JPEG Group."""
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    flat = " ".join(notice.split())
    assert "uses the FreeType code (www.freetype.org)" in flat
    assert "based in part on the work of the FreeType Team" in flat
    assert "based in part on the work of the Independent JPEG Group" in flat
    assert "names the platform it was generated for" in flat


def test_the_notice_names_every_component_that_bundles_those_libraries():
    """NOTICE says which components ship FreeType and libjpeg-turbo: the PDFium binary in
    pypdfium2 and the Pillow wheel. Held to the licence texts actually reproduced, so a
    component that starts or stops bundling them shows up here rather than leaving
    NOTICE describing a different set of binaries."""
    sections = _licence_sections(_notices())
    flat_notice = " ".join((REPO / "NOTICE").read_text(encoding="utf-8").split())
    bundlers = []
    for heading, body in sections.items():
        flat = " ".join(body.split())
        if _FREETYPE_BINARY_TERM in flat or _IJG_BINARY_TERM in flat:
            bundlers.append(heading.split()[0].lower())
            assert _FREETYPE_BINARY_TERM in flat and _IJG_BINARY_TERM in flat, (
                f"{heading} bundles one of FreeType and libjpeg-turbo but not the other; "
                "NOTICE says each bundler ships both")
    assert sorted(bundlers) == ["pillow", "pypdfium2"], bundlers
    assert "the PDFium binary that the pypdfium2 binding ships" in flat_notice
    assert "the Pillow wheel" in flat_notice


def test_the_generator_reads_every_licence_file_a_runtime_package_declares():
    """Platform-independent: whatever is installed here, every `License-File` its
    metadata names is read, in order, and none is empty."""
    gen = _notices_generator()
    from importlib import metadata

    runtime_roots, _ = gen.pinned_requirements()
    multi = 0
    for name in gen.closure(runtime_roots):
        declared = metadata.distribution(name).metadata.get_all("License-File") or []
        files = gen.licence_files(name)
        assert [path for path, _ in files] == declared, name
        assert all(text.strip() for _, text in files), f"{name}: an empty licence file"
        multi += len(declared) > 1
    assert multi >= 1, "no runtime package declares several licence files"


def test_a_latin_1_licence_file_is_decoded_rather_than_dropped():
    gen = _notices_generator()
    assert gen._decode("copyright \u00a9 1996".encode("latin-1")) == "copyright \u00a9 1996"
    assert gen._decode("\ufeffMIT".encode("utf-8")) == "MIT"
    assert gen._comparable("a\r\nb\r\n\r\n") == "a\nb"


def test_the_notices_say_which_platform_they_describe():
    """Markers and binary wheels make the closure platform-dependent (colorama is there
    only on Windows; a wheel's bundled native code is per platform). The file has to say
    which platform it is about, and name the entries that would differ elsewhere."""
    gen = _notices_generator()
    notices = _notices()
    described = gen.described_platform(notices)
    assert described and " on " in described, "the notices do not name their platform"
    head = notices.split("## Runtime components", 1)[0]
    assert "## Platform" in head
    assert "Binary wheels, built for this platform:" in head
    assert "pypdfium2" in head.split("Binary wheels, built for this platform:", 1)[1]


# ── the whole install is pinned ─────────────────────────────────────────────
# requirements.txt pins what VERIS asks for directly; constraints.txt pins everything
# that pulls in. Before it existed, 23 of the 39 packages in a fresh install were
# whatever was newest that day. The notices record every version, so the first upstream
# release of any of them would have made a clean clone fail the up-to-date test below on
# the platform the team works on, while Linux CI skipped that test and the build's own
# long-lived environment never re-resolved: nothing automated would have seen it. These
# tests hold the pin set complete, and the parts that do not depend on the platform run
# everywhere, Linux CI included.

def test_every_constraint_is_one_exact_pin_of_one_package():
    seen: dict[str, int] = {}
    for n, req in _constraint_lines():
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", (
            f"constraints.txt:{n}: {req} is not an exact pin")
        assert "*" not in specs[0].version, f"constraints.txt:{n}: {req} is a wildcard"
        assert not req.extras and req.url is None, f"constraints.txt:{n}: {req}"
        key = _canon(req.name)
        assert key not in seen, f"{req.name} is pinned twice (lines {seen[key]} and {n})"
        seen[key] = n
    assert len(seen) > 20


def test_every_package_an_install_resolves_to_is_pinned_at_its_installed_version():
    """The test the finding asked for. A package a later task adds (and whatever it
    pulls in) fails here, with the line to add, until it is pinned."""
    gen = _notices_generator()
    needed, unpinned, differing = _closure_against_pins(gen)
    assert len(needed) > 20, "the dependency closure came out empty"
    assert not unpinned, ("installed, needed, and not pinned in constraints.txt; add:\n"
                          + "\n".join(unpinned))
    assert not differing, ("installed at a version other than its pin; reinstall with "
                           "`pip install -r requirements.txt`:\n" + "\n".join(differing))


def test_the_constraints_pin_nothing_the_install_does_not_need():
    """A pin left behind after a dependency is dropped would sit in the file looking like
    part of the deployment. A pin whose marker excludes this platform is judged where
    it applies."""
    gen = _notices_generator()
    needed, _, _ = _closure_against_pins(gen)
    stale = [str(req) for key, req in _pins().items()
             if _pin_applies_here(req) and key not in needed]
    assert not stale, f"pinned in constraints.txt but needed by nothing installed: {stale}"


def test_the_direct_pins_agree_with_the_constraints():
    """requirements.txt, pyproject.toml and constraints.txt each pin the direct
    dependencies. If two of them disagree, pip refuses the install outright."""
    from packaging.requirements import Requirement

    direct = [("requirements.txt", Requirement(line))
              for _, line in _pip_lines(REPO / "requirements.txt")
              if not line.startswith("-")]
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for spec in project["dependencies"]:
        direct.append(("pyproject.toml", Requirement(spec)))
    for group, specs in project.get("optional-dependencies", {}).items():
        direct += [(f"pyproject.toml [{group}]", Requirement(spec)) for spec in specs]
    assert len(direct) > 15
    pins = _pins()
    for source, req in direct:
        pin = pins.get(_canon(req.name))
        assert pin is not None, f"{req.name} ({source}) has no pin in constraints.txt"
        assert req.specifier.contains(_pinned_version(pin)), (
            f"{req.name}: {source} says {req.specifier}, constraints.txt pins "
            f"{_pinned_version(pin)}")
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", (
            f"{req.name} in {source} is not pinned exactly")


def test_every_documented_install_applies_the_constraints():
    """The pins bind only an install that reads them. requirements.txt applies
    constraints.txt itself, so the one install command that README and CI use cannot
    leave it out; this holds both halves of that in place."""
    assert "-c constraints.txt" in [line for _, line in _pip_lines(REPO / "requirements.txt")]
    for path in (REPO / "README.md", REPO / ".github" / "workflows" / "ci.yml"):
        text = path.read_text(encoding="utf-8")
        installs = re.findall(r"pip install (?!--upgrade pip\b)[^\n#]*", text)
        assert installs, f"{path.name} documents no install"
        assert all("-r requirements.txt" in i for i in installs), (
            f"{path.name} installs without requirements.txt: {installs}")


def test_every_file_pip_reads_is_ascii_or_declares_utf8():
    """A fresh Windows pip decodes requirement files using the locale unless they
    are ASCII or declare an encoding in either of their first two lines."""
    for name in ("requirements.txt", "constraints.txt"):
        raw = (REPO / name).read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError:
            first_two = raw.decode("utf-8").splitlines()[:2]
            assert "# -*- coding: utf-8 -*-" in first_two, (
                f"{name} is non-ASCII and lacks a PEP 263 UTF-8 declaration")


def _dev_table(notices: str) -> list[tuple[str, str]]:
    """(name, version) for every row of the development-only tools table."""
    table = notices.split("## Development-only tools", 1)[1].split("## Licence texts", 1)[0]
    return [(m.group(1), m.group(2)) for m in re.finditer(
        r"^\| ([^|]+?) \| ([^|]+?) \| [^|]+? \|", table, re.MULTILINE)
        if m.group(1) not in {"Package", "---"}]


def test_the_notices_record_exactly_the_pinned_versions():
    """Platform-independent, so it runs where the full comparison below is skipped: a pin
    moved without regenerating the notices, or notices generated from an environment
    that is not the pinned one, fail here on every platform."""
    notices = _notices()
    rows = _runtime_table(notices) + _dev_table(notices)
    assert len(rows) > 20
    pins = _pins()
    wrong = []
    for name, version in rows:
        pin = pins.get(_canon(name))
        if pin is None:
            wrong.append(f"{name} {version} is listed but not pinned in constraints.txt")
        elif _pinned_version(pin) != version:
            wrong.append(f"{name}: the notices record {version}, constraints.txt pins "
                         f"{_pinned_version(pin)}")
    assert not wrong, "\n".join(wrong)
    listed = {_canon(name) for name, _ in rows}
    # A pin with a platform marker is listed only in notices generated where it holds.
    absent = [str(req) for key, req in pins.items() if req.marker is None and key not in listed]
    assert not absent, f"pinned on every platform but absent from the notices: {absent}"


def test_the_notices_are_current_on_the_platform_they_describe():
    """`--check`, as a test, wherever it is meaningful. On another platform the closure
    legitimately differs, so it is skipped by name rather than reported as staleness. On
    this one a fresh install reproduces the file, because every package is pinned; an
    environment that has drifted from the pins is reported as that, not as stale notices
    (regenerating from it would write the wrong versions)."""
    gen = _notices_generator()
    committed = _notices()
    described, here = gen.described_platform(committed), gen.target_platform()
    if described != here:
        pytest.skip(f"THIRD_PARTY_NOTICES.md describes {described}; this is {here}")
    _, unpinned, differing = _closure_against_pins(gen)
    assert not unpinned and not differing, (
        "this environment is not the pinned one, so it cannot reproduce the notices; "
        "reinstall with `pip install -r requirements.txt`:\n"
        + "\n".join(unpinned + differing))
    assert gen.build() == committed, (
        "THIRD_PARTY_NOTICES.md is out of date: run scripts/gen_third_party_notices.py")


def test_the_check_refuses_to_compare_across_platforms(tmp_path, monkeypatch, capsys):
    gen = _notices_generator()
    elsewhere = tmp_path / "THIRD_PARTY_NOTICES.md"
    elsewhere.write_text("# Third-party notices\n\nDescribes: CPython 3.12 on "
                         "plan9-mips.\n", encoding="utf-8")
    monkeypatch.setattr(gen, "OUTPUT", elsewhere)
    monkeypatch.setattr(gen, "build", lambda: pytest.fail("built across platforms"))
    assert gen.main(["--check"]) == 1
    assert "describes CPython 3.12 on plan9-mips" in capsys.readouterr().out


def test_open_source_md_gives_every_runtime_dependency_and_service_a_role():
    text = (REPO / "OPEN-SOURCE.md").read_text(encoding="utf-8").lower()
    for name, _ in _runtime_requirements():
        assert name.lower() in text, f"OPEN-SOURCE.md does not mention {name}"
    for service in ("openai-compatible", "data.gov.sg", "singstat", "onemap", "smtp",
                    "tesseract", "corpus/manifest.yaml"):
        assert service in text, f"OPEN-SOURCE.md does not describe {service}"
    assert "documented seam, not a" in text
    assert "role in veris" in text


def test_the_security_policy_records_the_accounting_seam_as_a_known_gap():
    text = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    assert "Known risks" in text
    assert "Documented seam, not a built feature" in text
    assert not EMAIL.findall(text), "SECURITY.md must not carry a personal address"


# ── 3. de-scoping ────────────────────────────────────────────────────────────

def test_the_accounting_feed_is_simulated_and_the_live_integration_is_a_seam():
    body = TestClient(app).get("/api/signals").json()
    assert body["events"] and all(e["simulated"] is True for e in body["events"])
    assert body["live"]["any_live"] is False and body["live"]["live_count"] == 0
    seam = body["accounting_integration"]
    assert seam["status"] == "DOCUMENTED_SEAM" and seam["built"] is False
    assert seam == connectors.ACCOUNTING_SEAM


def test_no_route_offers_an_oauth_handshake():
    """The removed connector was the only OAuth flow. A connect/callback route coming back
    would be a live integration arriving without the seam's rules attached."""
    # The product's own routes. FastAPI's interactive docs page carries an OAuth redirect
    # helper of its own (`/docs/oauth2-redirect`); that is the framework, not a connector.
    paths = {getattr(r, "path", "") for r in app.routes
             if getattr(r, "path", "").startswith("/api/")}
    assert paths, "no product routes found"
    assert not [p for p in paths if re.search(r"callback|authori[sz]e|oauth", p)]


def test_no_module_reads_a_client_secret_from_the_environment():
    offenders = [p.name for p in sorted((REPO / "src" / "proven").rglob("*.py"))
                 if "CLIENT_SECRET" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_the_carrier_workspace_speaks_for_a_fictional_sandbox_insurer():
    carrier = next(a.principal for a in auth.ACCOUNTS.values()
                   if a.principal.role == auth.CARRIER)
    assert carrier.organisation_name == auth.SANDBOX_INSURER_NAME
    assert auth.SANDBOX_INSURER_NAME == "Lotus General (Sandbox)"
    assert "LOTUS_GENERAL" in detect.CARRIER_MARKERS


def test_the_carrier_account_works_but_is_not_offered_at_sign_in():
    client = TestClient(app)
    accounts = client.get("/api/auth/accounts").json()["accounts"]
    advertised = {a["email"] for a in accounts if a["advertised"]}
    assert advertised == set(auth.DEMO_LOGIN_EMAILS)
    assert "underwriter@carrier.demo" not in advertised
    # Kept intact and role-guarded: it still signs in.
    r = client.post("/api/auth/login", json={"email": "underwriter@carrier.demo",
                                             "password": auth.DEMO_PASSWORD})
    assert r.status_code == 200

    # And the login screen's own list is the same two accounts.
    js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    block = js[js.index("const DEMO_ACCOUNTS = ["):]
    block = block[:block.index("];")]
    assert set(re.findall(r'email: "([^"]+)"', block)) == set(auth.DEMO_LOGIN_EMAILS)


# ── 4. synthetic documents ───────────────────────────────────────────────────

def _page_texts(data: bytes) -> list[str]:
    from pypdf import PdfReader
    return [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]


_REAL_INSURER_MARKERS = tuple(
    marker for key, markers in detect.CARRIER_MARKERS.items() if key != "LOTUS_GENERAL"
    for marker in markers)


def test_every_synthetic_document_naming_a_real_insurer_says_no_insurer_issued_it():
    checked = 0
    for pdf in sorted((REPO / "demo-documents").glob("*.pdf")):
        pages = _page_texts(pdf.read_bytes())
        names_insurer = any(m in text.lower() for text in pages
                            for m in _REAL_INSURER_MARKERS)
        if not names_insurer:
            continue
        checked += 1
        for n, text in enumerate(pages, start=1):
            assert pdfbuild.SYNTHETIC_MARK in text, (
                f"{pdf.name} page {n} names a real insurer without the synthetic mark")
    assert checked >= 4, f"only {checked} insurer-named synthetic documents found"


def test_the_committed_synthetic_pdfs_are_exactly_what_their_generators_produce():
    """Regenerated, not edited: a committed PDF that drifts from its builder is one
    nobody can reproduce, and the mark above would then be a claim about a file the
    generator no longer makes."""
    builders = dict(pdfbuild_demo.BUILDERS)
    builders["renewal-prior-policy.pdf"] = pdfbuild_renewal.prior_policy_pdf
    builders["renewal-current-policy.pdf"] = pdfbuild_renewal.current_policy_pdf
    for name, build in builders.items():
        committed = (REPO / "demo-documents" / name).read_bytes()
        assert committed == build(), f"{name} differs from its generator"


# The synthetic PDFs have uncompressed, all-ASCII streams, so git's content sniffing
# calls them TEXT. Under Git for Windows' default core.autocrlf=true a checkout then
# rewrote every LF inside them as CRLF: all fourteen came out longer than their
# generators make them, with stale xref offsets, so a clean Windows clone failed the
# test above and hashed every example document differently from a Linux clone. Linux CI
# converts nothing and could not see it. The tests below hold the fix in place.

def _git() -> str:
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        pytest.skip("not a git work tree: git's attributes cannot be asked here")
    return git


def _git_pdfs(git: str, *, untracked: bool) -> list[str]:
    cmd = [git, "-C", str(REPO), "ls-files", "-z", "--cached"]
    if untracked:
        cmd += ["--others", "--exclude-standard"]
    out = subprocess.run([*cmd, "--", "*.pdf"], capture_output=True, check=True).stdout
    return sorted({p for p in out.decode("utf-8").split("\0") if p})


def test_the_attributes_file_declares_pdfs_binary():
    """Readable without git, for a tree exported from the repository."""
    rules = [line.split() for line in
             (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert ["*.pdf", "binary"] in rules


def test_git_treats_every_pdf_in_the_tree_as_binary():
    """Asked of git itself, so a later, more specific line that turned conversion back
    on for some directory would be caught too. Untracked PDFs are included: the rule
    has to hold before a new example document is ever committed."""
    git = _git()
    pdfs = _git_pdfs(git, untracked=True)
    assert len(pdfs) >= 14, pdfs
    out = subprocess.run([git, "-C", str(REPO), "check-attr", "-z", "text", "diff",
                          "merge", "--", *pdfs], capture_output=True, check=True).stdout
    fields = out.decode("utf-8").split("\0")
    triples = [fields[i:i + 3] for i in range(0, len(fields) - 2, 3)]
    assert len(triples) == 3 * len(pdfs)
    converted = sorted({path for path, attr, value in triples if value != "unset"})
    assert not converted, f"git would convert line endings inside: {converted}"


def test_a_converting_checkout_leaves_every_committed_pdf_byte_for_byte(tmp_path):
    """The defect itself, reproduced on any platform: check the PDFs out of the index the
    way a default Git for Windows install does, and compare with the bytes git stores
    (which the test above proves are the generators' bytes)."""
    git = _git()
    pdfs = _git_pdfs(git, untracked=False)
    assert len(pdfs) >= 14, pdfs
    subprocess.run([git, "-C", str(REPO), "-c", "core.autocrlf=true", "checkout-index",
                    "-f", f"--prefix={tmp_path.as_posix()}/", "--", *pdfs],
                   capture_output=True, check=True)
    changed = []
    for rel in pdfs:
        stored = subprocess.run([git, "-C", str(REPO), "cat-file", "blob", f":{rel}"],
                                capture_output=True, check=True).stdout
        if (tmp_path / rel).read_bytes() != stored:
            changed.append(rel)
    assert not changed, f"a line-ending-converting checkout rewrote: {changed}"


def test_the_mark_is_a_real_em_dash_in_the_text_layer_and_leaves_the_body_alone():
    plain = pdfbuild.build_pdf([["A body line"]])
    marked = pdfbuild.build_pdf([["A body line"]], banner=pdfbuild.SYNTHETIC_MARK)
    text = _page_texts(marked)[0]
    assert text.splitlines()[0] == "SYNTHETIC SAMPLE \u2014 NOT ISSUED BY ANY INSURER"
    assert "A body line" in text
    # Without a banner the bytes are exactly what the builder always produced.
    assert pdfbuild.build_pdf([["A body line"]], banner=None) == plain


@pytest.mark.parametrize("name", sorted(pdfbuild_demo.BUILDERS))
def test_each_generated_document_still_extracts(name):
    pages = _page_texts(pdfbuild_demo.BUILDERS[name]())
    assert pages and any(t.strip() for t in pages)
