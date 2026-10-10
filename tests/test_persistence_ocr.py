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

def test_persistence_status_is_honest_when_unconfigured(monkeypatch):
    """The health surface may not imply a durable store before one is configured."""
    monkeypatch.delenv("VERIS_PORTFOLIO_DB_PATH", raising=False)
    state = persistence.status()
    assert state["persistent"] is False
    assert state["backend"] == "file-sqlite"
    assert state["path_configured"] is False
    assert state["one_connection_per_request"] is True


def test_persistence_status_describes_the_configured_encrypted_store(monkeypatch, tmp_path):
    path = tmp_path / "portfolio.sqlite"
    monkeypatch.setenv("VERIS_PORTFOLIO_DB_PATH", str(path))
    monkeypatch.setenv("VERIS_MASTER_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA")
    state = persistence.status()
    assert state["persistent"] is True
    assert state["path_configured"] is True
    assert state["path"] == str(path)
    assert state["master_key_source"] == "environment"
    assert "AES-GCM" in state["encryption"]


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
