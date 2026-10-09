"""The repository is licence-clean, branded as VERIS, and free of what it must not carry.

Four groups, each pinning something the rebranding and licensing work established:

  1. THE BRAND GUARD. The real repository scans clean — no secret, no forbidden file, no
     forbidden string — and the regulator-name allowlist stays exactly as narrow as it is
     documented to be. This is the test that makes the scanner's verdict part of the
     suite rather than a step somebody remembers to run.
  2. LICENSING. Apache-2.0, a NOTICE, generated third-party notices that cover every
     pinned runtime requirement, and an OPEN-SOURCE.md that gives each component, service
     and data source its role.
  3. DE-SCOPING. The live accounting connector is gone and the seam is documented in its
     place; the carrier workspace speaks for a fictional insurer and is not offered at
     sign-in.
  4. SYNTHETIC DOCUMENTS. Any synthetic document bearing a real insurer's name says on
     every page that no insurer issued it, and every committed synthetic PDF is exactly
     what its generator produces.
"""
from __future__ import annotations

import importlib.util
import io
import re
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


# ── 1. the brand guard ───────────────────────────────────────────────────────

def test_the_repository_scans_clean():
    """No secret, no forbidden file, no forbidden string — anywhere in the tree."""
    report = _scanner().scan(REPO)
    found = [f"{f.kind} {f.path}:{f.line} {f.detail}" for f in report.findings]
    assert report.files_scanned > 100
    assert not found, "the repository is not clean:\n" + "\n".join(found)


def test_the_regulator_allowlist_is_exactly_as_narrow_as_documented():
    """The regulator's name may appear in the jurisdiction packs and the generated
    compliance matrix and nowhere else — not even in the scanner that declares the rule,
    which keeps it encoded. A path quietly added here would widen the exception without
    anyone deciding to."""
    scan_repo = _scanner()
    assert scan_repo.REGULATOR_ALLOWED_PATHS == (
        "src/proven/jurisdictions/", "corpus/manifest.yaml", "docs/COMPLIANCE.md")
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
