"""Tests for persistence and OCR — ported from Shashank's architecture."""
import os
import tempfile

import pytest

from proven import ocr, persistence


# ── OCR ─────────────────────────────────────────────────────────────────────

def test_ocr_status_reports_honestly():
    """OCR must never claim to work when it cannot."""
    s = ocr.status()
    assert s["provider"] == "tesseract"
    assert s["local"] is True
    assert isinstance(s["available"], bool)
    assert s["note"]  # always has an explanation


def test_ocr_image_kind_identifies_common_formats():
    assert ocr.image_kind(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100) == "PNG"
    assert ocr.image_kind(b"\xff\xd8\xff" + b"\x00" * 100) == "JPEG"
    assert ocr.image_kind(b"not an image") is None
    assert ocr.image_kind(b"") is None


def test_ocr_graceful_degradation_on_bad_input():
    """OCR must return EMPTY, never raise, on garbage input."""
    p = ocr.TesseractOCRProvider()
    result = p.ocr_image(b"this is not an image")
    assert result.status == "EMPTY"
    assert result.text == ""


def test_ocr_rejects_oversized_input():
    p = ocr.TesseractOCRProvider()
    huge = b"\x00" * (ocr.MAX_IMAGE_BYTES + 1)
    result = p.ocr_image(huge)
    assert result.status == "EMPTY"


def test_pdf_image_extraction_handles_non_pdf():
    result = ocr.extract_pdf_page_images(b"not a pdf")
    assert result == {}


def test_ocr_mode_defaults_to_auto():
    # Clear any env override
    old = os.environ.pop("PROVEN_OCR", None)
    try:
        assert ocr.ocr_mode() == "auto"
    finally:
        if old is not None:
            os.environ["PROVEN_OCR"] = old


# ── Persistence ─────────────────────────────────────────────────────────────

def test_persistence_status_in_memory_by_default():
    """Without PROVEN_DB_PATH, everything is in-memory and the module is dormant."""
    old = os.environ.pop("PROVEN_DB_PATH", None)
    try:
        # Reset the module state
        persistence._shared_conn = None
        s = persistence.status()
        assert s["persistent"] is False
        assert s["backend"] == "memory"
        assert s["connected"] is False
    finally:
        if old is not None:
            os.environ["PROVEN_DB_PATH"] = old


def test_persistence_file_backed_works():
    """With PROVEN_DB_PATH set, the database is file-backed and survives."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    old = os.environ.get("PROVEN_DB_PATH")
    try:
        os.environ["PROVEN_DB_PATH"] = path
        persistence._shared_conn = None  # force re-init
        conn = persistence.get_shared_conn()
        assert conn is not None

        # Save and reload a session mapping
        persistence.save_session_case("sess-1", "case-1", "owner-1", "biz-1")
        loaded = persistence.load_session_case("sess-1")
        assert loaded is not None
        assert loaded["case_id"] == "case-1"
        assert loaded["owner"] == "owner-1"

        # Save and reload a snapshot
        persistence.save_snapshot("case-1", "owner-1", "biz-1",
                                  {"brief": {"findings": [1, 2, 3]}})
        snap = persistence.load_snapshot("case-1", "owner-1")
        assert snap is not None
        assert len(snap["brief"]["findings"]) == 3

        # Owner isolation: different owner cannot load
        assert persistence.load_snapshot("case-1", "other-owner") is None

        # Scheduler run
        persistence.save_scheduler_run("discovery", "2026-08-14T10:00:00Z",
                                       "DEMO_FIXTURE", 3, True, "test run")
        history = persistence.load_scheduler_history(10)
        assert len(history) == 1
        assert history[0]["agent"] == "discovery"
        assert history[0]["elevated"] is True

        persistence.close_shared()
    finally:
        persistence._shared_conn = None
        if old is not None:
            os.environ["PROVEN_DB_PATH"] = old
        else:
            os.environ.pop("PROVEN_DB_PATH", None)
        try:
            os.unlink(path)
        except PermissionError:
            pass  # Windows file lock; harmless in test cleanup


def test_persistence_snapshot_deletion():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    old = os.environ.get("PROVEN_DB_PATH")
    try:
        os.environ["PROVEN_DB_PATH"] = path
        persistence._shared_conn = None
        persistence.save_snapshot("case-del", "owner-1", "biz-1", {"test": True})
        assert persistence.load_snapshot("case-del", "owner-1") is not None
        persistence.delete_snapshot("case-del")
        # After deletion, status='DELETED' so load returns None
        assert persistence.load_snapshot("case-del", "owner-1") is None
        persistence.close_shared()
    finally:
        persistence._shared_conn = None
        if old is not None:
            os.environ["PROVEN_DB_PATH"] = old
        else:
            os.environ.pop("PROVEN_DB_PATH", None)
        try:
            os.unlink(path)
        except PermissionError:
            pass  # Windows file lock; harmless in test cleanup


def test_persistence_never_claims_a_restart_survives_while_the_writes_are_unwired():
    """The flag may not promise more than the code delivers.

    HOW THIS WAS FOUND. `PROVEN_DB_PATH` was set on a demo machine and /api/health duly
    reported `persistent: true` with the notice "Cases survive a restart." A full case was
    then built and the file inspected directly: `session_case`, `case_snapshot` and
    `record_fact` were all zero rows. The schema is created and stays empty, because
    `save_snapshot()` and `save_session_case()` are defined in persistence.py and called
    from nowhere in the codebase.

    That is the product's own unforgivable failure — a component reporting itself as
    working when it is not — so `status()` now reports the end-to-end behaviour rather than
    the setting, and this test fails the build if the two ever drift apart again.

    WHEN THE WRITES ARE WIRED: set SNAPSHOT_WRITES_WIRED = True in the same commit that
    adds the call sites. This test then asserts the opposite direction automatically.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "veris.db")
        old = os.environ.get("PROVEN_DB_PATH")
        persistence.close_shared()
        os.environ["PROVEN_DB_PATH"] = path
        try:
            st = persistence.status()
            assert st["path_configured"] is True, "the path was set and is not reported"
            assert st["snapshot_writes_wired"] == persistence.SNAPSHOT_WRITES_WIRED

            if persistence.SNAPSHOT_WRITES_WIRED:
                assert st["persistent"] is True
                assert "survive" in st["notice"].lower()
            else:
                # The whole point: configured but not durable, and saying so.
                assert st["persistent"] is False, (
                    "status() claims cases survive a restart while save_snapshot() has "
                    "no call sites")
                assert "not wired" in st["notice"].lower()
        finally:
            persistence.close_shared()
            if old is None:
                os.environ.pop("PROVEN_DB_PATH", None)
            else:
                os.environ["PROVEN_DB_PATH"] = old


def test_ocr_returns_a_page_instead_of_raising_on_success():
    """REGRESSION. The success path raised NameError, so OCR never worked.

    `ocr_image()` ended with `conditioning=getattr(self, last_conditioning, {})` — the
    attribute name unquoted — which raises
    `NameError: name 'last_conditioning' is not defined`. It fires only on the branch that
    HAS text to return, so OCR either abstained early (no binary, blank page) or crashed at
    the exact moment it succeeded. No run in this project ever got a result out of it.

    It stayed invisible because every OCR test skips when no tesseract binary is present,
    and no machine here had one installed at the same time as a test run. So this test
    renders a real image and asserts a page comes back — the one thing the old code could
    not do.

    Skips honestly when there is no binary, rather than passing vacuously.
    """
    if not ocr.available():
        pytest.skip("no tesseract binary on this machine")

    pytest.importorskip("PIL")
    import io

    from PIL import Image, ImageDraw, ImageFont

    font = None
    for candidate in (r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/"
                      "dejavu/DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(candidate, 34)
            break
        except OSError:
            continue

    img = Image.new("RGB", (1100, 120), "white")
    ImageDraw.Draw(img).text((30, 30), "Sum Insured (Stock): SGD 100,000",
                             fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    # The assertion that matters: this call used to raise.
    page = ocr.default_provider().ocr_image(buf.getvalue())

    assert page is not None
    assert isinstance(getattr(page, "conditioning", {}), dict), (
        "conditioning must be a real dict — the field the NameError came from")
    text = (getattr(page, "text", "") or "")
    if font is not None:
        # Only assert recognition quality where a real face was available. PIL's default
        # bitmap font is ~11px and Tesseract genuinely cannot read it, which would make
        # this a test of the fixture rather than of the engine.
        assert "100,000" in text, f"OCR ran but recovered nothing usable: {text!r}"


def test_vector_pdf_pages_are_rasterised_rather_than_reported_unreadable():
    """REGRESSION. A page drawn as vectors used to be unreadable even with OCR working.

    `extract_pdf_page_images()` only ever looked for rasters somebody had already embedded.
    Most machine-produced "scanned-looking" paperwork has none — the content is drawn — so
    the function returned `{}` and the page was reported UNREADABLE by a fully functional
    OCR engine. `13-illegible-endorsement-scan.pdf` in this repo is exactly that shape:
    zero embedded rasters, and its text recoverable at ~93% confidence once rendered.

    Also pins the status contract: `rasteriser` and `vector_pdf_rasterisation` must be
    DETECTED. The note previously stated as a fact that a rasteriser "is not installed in
    this environment", which silently became a false claim the moment it was.

    The rasteriser is PDFium (`pypdfium2`), which replaced a copyleft-licensed renderer;
    the assertion names the new engine and is otherwise the same contract.
    """
    st = ocr.status()
    assert st["vector_pdf_rasterisation"] == ocr.rasteriser_available()
    assert (st["rasteriser"] == "pdfium") == ocr.rasteriser_available()

    if not ocr.rasteriser_available():
        pytest.skip("no PDF rasteriser installed")

    import demodocs
    pdf = demodocs.path("13-illegible-endorsement-scan.pdf")
    if not pdf.exists():                            # pragma: no cover
        pytest.skip(f"demo document not present at {pdf}")
    raw = pdf.read_bytes()

    # No embedded raster: that is the precondition this fix exists for.
    from pypdf import PdfReader
    import io as _io
    reader = PdfReader(_io.BytesIO(raw))
    assert all(len(list(p.images)) == 0 for p in reader.pages), (
        "this fixture is supposed to have no embedded raster; if it gained one the test "
        "no longer exercises the rasterisation path")

    rendered = ocr.rasterise_pdf_pages(raw)
    assert rendered, "rasteriser is available but produced no page images"
    assert all(len(b) > 1000 for b in rendered.values())

    # And the combined entry point must now return something for that page.
    combined = ocr.extract_pdf_page_images(raw)
    assert combined, "extract_pdf_page_images still reports a vector page as imageless"

    if ocr.available():
        page = ocr.default_provider().ocr_image(sorted(combined.items())[0][1], page_no=1)
        text = (getattr(page, "text", "") or "")
        assert "Hock Lee" in text or "ENDORSEMENT" in text.upper(), (
            f"rasterised and OCR'd but recovered nothing identifying: {text[:200]!r}")
