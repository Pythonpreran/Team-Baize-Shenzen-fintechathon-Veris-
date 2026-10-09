"""PDF rendering through PDFium: correct pixels, honest highlights, one lock.

`pdfrender.py` replaced a copyleft-licensed renderer. These tests pin what the evidence
viewer and the OCR layer rely on, against synthetic PDFs built in the test so nothing
here needs the corpus:

  * a page renders to a real PNG of the right size, in colour for a reader and in
    grayscale for OCR;
  * text is LOCATED on the page, and the highlight lands where the text is — not
    somewhere plausible;
  * text drawn beyond the page edge is detected, and the page can be grown to show it;
  * PDFium is entered by one thread at a time, and only through this module.
"""
from __future__ import annotations

import io
import re
import threading
from pathlib import Path

import pytest

import pdfbuild
from proven import ocr, pdfrender

pytestmark = pytest.mark.skipif(
    not pdfrender.available(), reason="the PDFium renderer (pypdfium2) is not installed")

SRC = Path(__file__).resolve().parents[1] / "src" / "proven"
LINES = ["POLICY SCHEDULE", "Insured Name: Example Household", "Deductible SGD 5,000",
         "Room charges are limited to one per cent of the sum insured per day."]


def _pdf(lines=LINES, **kw) -> bytes:
    return pdfbuild.build_pdf([lines], **kw)


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _png_colour_type(png: bytes) -> int:
    return png[25]


# ── rendering ────────────────────────────────────────────────────────────────

def test_a_page_renders_to_a_png_of_the_page_size_at_the_requested_resolution():
    with pdfrender.document(_pdf()) as doc:
        assert doc.page_count == 1
        image = doc.page(0).render_png(144)
    width, height = _png_size(image.png)
    # 612 x 792 pt at 144 dpi is 1224 x 1584 px.
    assert (width, height) == (image.width, image.height) == (1224, 1584)
    assert _png_colour_type(image.png) == 2, "a reader's page is RGB"


def test_grayscale_rendering_is_what_ocr_gets():
    rendered = ocr.rasterise_pdf_pages(_pdf())
    assert set(rendered) == {0}
    assert _png_colour_type(rendered[0]) == 0, "OCR input should be single-channel gray"
    assert _png_size(rendered[0]) == (1700, 2200)   # 200 dpi


def test_ocr_rasterises_a_synthetic_pdf_through_pdfium():
    """A page drawn as vectors has no embedded raster; PDFium gives OCR something to read.

    The rasterisation half needs no OCR engine and always runs. Recognition is a separate
    test below, because it needs the tesseract binary.
    """
    st = ocr.status()
    assert st["rasteriser"] == pdfrender.RENDERER
    assert st["vector_pdf_rasterisation"] is True
    pages = ocr.extract_pdf_page_images(_pdf())
    assert set(pages) == {0}, "the vector page produced nothing for OCR to read"
    assert len(pages[0]) > 1000


def test_ocr_reads_text_back_from_a_pdfium_render():
    if not ocr.available():
        pytest.skip("no tesseract binary on this machine; rasterisation is tested above")
    png = ocr.rasterise_pdf_pages(_pdf())[0]
    page = ocr.default_provider().ocr_image(png, page_no=1)
    assert "SCHEDULE" in page.text.upper()


def test_only_the_requested_pages_are_rasterised():
    data = pdfbuild.build_pdf([["page one"], ["page two"], ["page three"]])
    assert set(pdfrender.rasterise(data, only_pages={1})) == {1}
    assert set(pdfrender.rasterise(data)) == {0, 1, 2}


def test_bytes_that_are_not_a_pdf_are_refused_by_name_and_rasterise_to_nothing():
    with pytest.raises(pdfrender.PdfUnreadable):
        with pdfrender.document(b"this is not a pdf"):
            pass
    assert pdfrender.rasterise(b"this is not a pdf") == {}
    assert pdfrender.rasterise(b"") == {}


# ── locating text, and marking it ────────────────────────────────────────────

def test_text_is_located_case_insensitively_and_returned_in_page_space():
    with pdfrender.document(_pdf()) as doc:
        page = doc.page(0)
        exact = page.find("Deductible SGD 5,000")
        loose = page.find("deductible sgd 5,000")
        missing = page.find("a phrase that is not on this page")
        box = page.box()
    assert exact and exact == loose
    assert missing == []
    left, bottom, right, top = exact[0]
    assert box[0] <= left < right <= box[2] and box[1] <= bottom < top <= box[3]


def test_the_highlight_lands_on_the_located_text_and_nowhere_else():
    """Measured on the pixels: the marked rows are amber, and an unmarked render of the
    same page has no amber at all."""
    with pdfrender.document(_pdf()) as doc:
        page = doc.page(0)
        rects = page.find("Deductible SGD 5,000")
        marked = page.render_png(72, highlights=rects)
        plain = page.render_png(72)
    pil = pytest.importorskip("PIL.Image")
    img = pil.open(io.BytesIO(marked.png)).convert("RGB")
    bare = pil.open(io.BytesIO(plain.png)).convert("RGB")

    def amber(px) -> bool:
        r, g, b = px
        return r > 200 and 150 < g < 235 and b < 120

    left, bottom, right, top = rects[0]
    # 72 dpi: one point is one pixel; y flips because PDF space measures from the bottom.
    cx, cy = int((left + right) / 2), int(792 - (bottom + top) / 2)
    row = [img.getpixel((x, cy)) for x in range(int(left) + 1, int(right) - 1)]
    assert any(amber(px) for px in row), "no highlight where the text is"
    assert not amber(img.getpixel((cx, 20))), "highlight bled to the top of the page"
    assert not any(amber(bare.getpixel((x, cy))) for x in range(int(left), int(right)))


# ── text beyond the page edge ────────────────────────────────────────────────

def test_text_drawn_below_the_page_is_detected_and_the_page_can_grow_to_show_it():
    """Forty lines from a 760pt start at 14pt leading run off the bottom of a 792pt page —
    the shape of the real demo schedule that motivated this."""
    lines = [f"line {i:02d}" for i in range(60)]
    with pdfrender.document(pdfbuild.build_pdf([lines])) as doc:
        page = doc.page(0)
        inside = page.find("line 01")
        below = page.find("line 59")
        assert inside and not page.outside(inside)
        assert below and page.outside(below)
        before = page.box()
        assert page.grow_to_include(below) is True
        after = page.box()
        assert after[1] <= below[0][1] < before[1], "the page did not grow downward"
        assert not page.outside(below), "the grown page still crops the line"
        taller = page.render_png(72)
    assert taller.height > 792


def test_growing_a_page_for_text_already_inside_it_changes_nothing():
    with pdfrender.document(_pdf()) as doc:
        page = doc.page(0)
        assert page.grow_to_include(page.find("POLICY SCHEDULE")) is False
        assert page.grow_to_include([]) is False


# ── one lock, one module ─────────────────────────────────────────────────────

def test_the_lock_is_held_for_the_whole_life_of_a_document():
    """PDFium is not thread-safe, so no other thread may enter it while a document is
    open — not only while a page is being rasterised."""
    seen: list[bool] = []

    def probe() -> None:
        got = pdfrender._LOCK.acquire(blocking=False)
        seen.append(got)
        if got:
            pdfrender._LOCK.release()

    with pdfrender.document(_pdf()) as doc:
        doc.page(0).find("POLICY")
        t = threading.Thread(target=probe)
        t.start()
        t.join()
    assert seen == [False], "another thread could enter PDFium while a document was open"

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    assert seen == [False, True], "the lock was not released when the document closed"


def test_concurrent_renders_are_serialised_and_all_correct():
    """Eight threads, the way a thread pool would call it. Every render must succeed and
    produce the same bytes; a race inside PDFium shows up as a crash or as garbage."""
    data = _pdf()
    results: list[bytes] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            with pdfrender.document(data) as doc:
                page = doc.page(0)
                results.append(page.render_png(72, highlights=page.find("Deductible")).png)
        except BaseException as exc:          # pragma: no cover - the failure is the point
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(results) == 8 and len(set(results)) == 1


def test_no_module_but_pdfrender_imports_pdfium():
    """The lock only protects calls that pass through it."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "pdfrender.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(import|from)\s+pypdfium2", text, re.MULTILINE):
            offenders.append(path.name)
    assert offenders == [], f"PDFium imported outside pdfrender.py: {offenders}"


def test_availability_is_detected_and_the_version_is_reported():
    assert pdfrender.available() is True
    assert "pypdfium2" in (pdfrender.version() or "")


# ── the PNG encoder ──────────────────────────────────────────────────────────

def test_the_png_encoder_round_trips_through_an_independent_decoder():
    pil = pytest.importorskip("PIL.Image")
    width, height = 3, 2
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255,
                 10, 20, 30, 40, 50, 60, 70, 80, 90])
    img = pil.open(io.BytesIO(pdfrender.encode_png(rgb, width, height, width * 3, 3)))
    assert img.size == (3, 2)
    assert img.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
    assert img.convert("RGB").getpixel((2, 1)) == (70, 80, 90)

    # A four-channel bitmap's fourth byte is padding, and must not become transparency.
    rgbx = bytes([1, 2, 3, 0, 4, 5, 6, 0])
    img = pil.open(io.BytesIO(pdfrender.encode_png(rgbx, 2, 1, 8, 4)))
    assert img.mode == "RGB" and img.getpixel((1, 0)) == (4, 5, 6)

    with pytest.raises(ValueError):
        pdfrender.encode_png(b"\x00" * 4, 1, 1, 2, 2)
