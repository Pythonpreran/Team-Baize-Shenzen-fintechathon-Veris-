"""Rasterise PDF pages and locate text on them, through PDFium (`pypdfium2`).

Two callers need a picture of a PDF page: the evidence viewer (`sourceview.py`), which
shows a reader the cited page with the quotation marked, and the OCR layer (`ocr.py`),
which renders a page drawn as vectors so Tesseract has pixels to read. Both go through
this module and nothing else in the codebase imports `pypdfium2` — a test enforces that,
because the lock below only protects the calls that pass through it.

WHY PDFIUM. The renderer this replaces is distributed under a copyleft licence that would
have obliged every deployment of this repository to publish its source under the same
terms — incompatible with releasing it under Apache-2.0. PDFium is the PDF engine inside
Chromium (BSD-3-Clause / Apache-2.0) and `pypdfium2` is its binding (Apache-2.0 /
BSD-3-Clause). Both are permissive, and both are recorded in OPEN-SOURCE.md.

ONE LOCK, HELD FOR THE WHOLE LIFE OF A DOCUMENT. PDFium is not thread-safe, and that
covers loading a document, searching its text and closing it — not only rendering.
FastAPI runs synchronous routes on a thread pool, so two readers opening the evidence
drawer at the same moment would otherwise enter PDFium from two threads at once, which
is undefined behaviour inside a C library rather than a Python exception anybody could
catch. So `document()` takes `_LOCK` before the document is opened and releases it only
after the document is closed, and every page, text page and bitmap is created and closed
inside that window. The cost is that renders are serialised; one page at display
resolution takes milliseconds, and a correct queue beats a crash in native code.

Explicit closing matters for the same reason. An object PDFium allocated and Python
forgot is freed by a finaliser on whatever thread the garbage collector happens to run
on, which is outside the lock. Everything opened here is closed here.

NO IMAGE LIBRARY. The PNG is encoded with `zlib` in this file. Pillow is an optional
dependency of the OCR layer, and the evidence viewer should not quietly acquire it.

DETECTED, NOT ASSERTED. `available()` asks the import system whether `pypdfium2` is
installed. No status line anywhere states a capability this process has not checked.
"""
from __future__ import annotations

import importlib.util
import struct
import threading
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

#: Reported in status payloads. The engine, not the binding: the binding is a detail.
RENDERER = "pdfium"

#: A rectangle in PDF user space: (left, bottom, right, top), in points, y measured
#: UPWARD from the bottom of the page. PDFium reports text boxes this way and the page
#: boxes are set this way, so nothing is converted until a rectangle meets a bitmap.
Rect = tuple[float, float, float, float]

# Re-entrant so a helper that opens a document can be called by code already holding the
# lock on the same thread. A plain Lock would deadlock that thread against itself.
_LOCK = threading.RLock()

#: The amber the evidence viewer has always used for a highlight. Not the product's
#: blue: blue marks interactive affordances everywhere else, and a highlight is not
#: clickable.
HIGHLIGHT_RGB = (255, 217, 51)

# Multiply blend, precomputed per channel: white paper takes the highlight colour and
# black text stays black, which is what a highlighter pen does and what keeps the marked
# words readable. `bytes.translate` applies a table at C speed.
_BLEND = tuple(bytes((v * c) // 255 for v in range(256)) for c in HIGHLIGHT_RGB)

# How far beyond the page box a text rectangle must reach before it counts as drawn off
# the page. Glyph boxes legitimately graze the edge by a fraction of a point.
_EDGE_TOLERANCE = 0.5

#: Cap on how many occurrences of one string are marked. A short fragment that occurs
#: thirty times on a page is not locating anything.
MAX_HITS = 16


class PdfUnreadable(Exception):
    """PDFium could not open the bytes as a PDF."""


def available() -> bool:
    """Whether pages can be rendered here. Detected, never asserted."""
    return importlib.util.find_spec("pypdfium2") is not None


def version() -> Optional[str]:
    """The binding and engine versions, for status payloads. None when not installed."""
    if not available():
        return None
    try:
        from pypdfium2 import version as v
        return f"pypdfium2 {v.PYPDFIUM_INFO} (PDFium {v.PDFIUM_INFO})"
    except Exception:                                   # pragma: no cover - defensive
        return "pypdfium2"


@dataclass
class RenderedImage:
    png: bytes
    width: int
    height: int


class Page:
    """One page of an open document. Valid only inside `document()`."""

    def __init__(self, raw) -> None:
        self._raw = raw
        self._text = None

    # ── geometry ────────────────────────────────────────────────────────────

    def box(self) -> Rect:
        """The visible area: the crop box, which PDFium defaults to the media box."""
        return tuple(float(x) for x in self._raw.get_cropbox())   # type: ignore[return-value]

    def outside(self, rects: Sequence[Rect]) -> bool:
        """True when any rectangle reaches past the visible page area."""
        left, bottom, right, top = self.box()
        t = _EDGE_TOLERANCE
        return any(r[0] < left - t or r[1] < bottom - t or r[2] > right + t
                   or r[3] > top + t for r in rects)

    def grow_to_include(self, rects: Sequence[Rect], pad: float = 12.0) -> bool:
        """Enlarge the page so `rects` fall inside it. True when the page changed.

        Both boxes are set: rendering follows the crop box, and a crop box larger than the
        media box is clamped back to it. `pad` keeps the line from sitting flush against
        the new edge. This changes the in-memory copy only — the caller's bytes are never
        written back.
        """
        if not rects:
            return False
        left, bottom, right, top = self.box()
        new = (min([left] + [r[0] - pad for r in rects if r[0] < left]),
               min([bottom] + [r[1] - pad for r in rects if r[1] < bottom]),
               max([right] + [r[2] + pad for r in rects if r[2] > right]),
               max([top] + [r[3] + pad for r in rects if r[3] > top]))
        if new == (left, bottom, right, top):
            return False
        try:
            self._raw.set_mediabox(*new)
            self._raw.set_cropbox(*new)
        except Exception:
            # A page whose boxes cannot be rewritten is reported, never guessed at.
            return False
        return True

    # ── text ────────────────────────────────────────────────────────────────

    def _textpage(self):
        if self._text is None:
            self._text = self._raw.get_textpage()
        return self._text

    def find(self, needle: str, max_hits: int = MAX_HITS) -> list[Rect]:
        """Every rectangle covering an occurrence of `needle`, case-insensitively.

        One rectangle per line segment of each match, in PDF user space. Case-insensitive
        because a quotation stored from one extraction and searched in another can differ
        in case on a small-capitals heading, and the words are what is being located.
        """
        if not needle:
            return []
        textpage = self._textpage()
        searcher = textpage.search(needle, match_case=False, match_whole_word=False)
        rects: list[Rect] = []
        try:
            for _ in range(max_hits):
                hit = searcher.get_next()
                if not hit:
                    break
                index, count = hit
                for i in range(textpage.count_rects(index, count)):
                    rects.append(tuple(float(x) for x in textpage.get_rect(i)))
        finally:
            searcher.close()
        return rects

    # ── pixels ──────────────────────────────────────────────────────────────

    def render_png(self, dpi: float, highlights: Sequence[Rect] = (),
                   grayscale: bool = False) -> RenderedImage:
        """Rasterise the visible area at `dpi`, marking `highlights`, as PNG bytes."""
        scale = max(0.25, float(dpi) / 72.0)
        bitmap = self._raw.render(scale=scale, grayscale=grayscale, rev_byteorder=True)
        try:
            width, height = int(bitmap.width), int(bitmap.height)
            stride, channels = int(bitmap.stride), int(bitmap.n_channels)
            pixels = bytearray(memoryview(bitmap.buffer).cast("B"))
            if highlights and channels >= 3:
                convert = bitmap.get_posconv(self._raw)
                for rect in highlights:
                    _blend_rect(pixels, stride, channels, width, height,
                                _device_box(convert, rect))
        finally:
            bitmap.close()
        return RenderedImage(encode_png(pixels, width, height, stride, channels),
                             width, height)

    def close(self) -> None:
        if self._text is not None:
            try:
                self._text.close()
            finally:
                self._text = None
        self._raw.close()


class Document:
    """An open PDF. Valid only inside `document()`."""

    def __init__(self, raw) -> None:
        self._raw = raw
        self._pages: list[Page] = []

    @property
    def page_count(self) -> int:
        return len(self._raw)

    def page(self, index: int) -> Page:
        """The page at 0-based `index`. Closed with the document."""
        page = Page(self._raw[index])
        self._pages.append(page)
        return page

    def _close(self) -> None:
        while self._pages:
            try:
                self._pages.pop().close()
            except Exception:                           # pragma: no cover - defensive
                pass
        self._raw.close()


@contextmanager
def document(pdf_bytes: bytes) -> Iterator[Document]:
    """Open `pdf_bytes` with the PDFium lock held until the document is closed.

    Raises `PdfUnreadable` when the bytes are not a PDF PDFium can open, and
    `RuntimeError` when PDFium is not installed — callers check `available()` first and
    report the absence in their own words.
    """
    if not available():
        raise RuntimeError("pypdfium2 is not installed")
    import pypdfium2 as pdfium

    with _LOCK:
        try:
            raw = pdfium.PdfDocument(bytes(pdf_bytes))
        except Exception as exc:
            raise PdfUnreadable(str(exc)) from exc
        doc = Document(raw)
        try:
            yield doc
        finally:
            doc._close()


def rasterise(pdf_bytes: bytes, only_pages: Optional[set[int]] = None,
              dpi: int = 200, grayscale: bool = True) -> dict[int, bytes]:
    """`{0-based page index: PNG bytes}` for the requested pages. `{}` when unavailable.

    Never raises: a page that fails to render is left out, and an unopenable file
    yields nothing, so the OCR layer reports those pages unreadable rather than crash.
    Grayscale by default because that is what OCR wants and it is a third of the bytes.
    """
    if not available() or not pdf_bytes:
        return {}
    out: dict[int, bytes] = {}
    try:
        with document(pdf_bytes) as doc:
            for index in range(doc.page_count):
                if only_pages is not None and index not in only_pages:
                    continue
                try:
                    out[index] = doc.page(index).render_png(dpi, grayscale=grayscale).png
                except Exception:
                    continue
    except Exception:
        return out
    return out


# ── bitmap helpers ──────────────────────────────────────────────────────────

def _device_box(convert, rect: Rect) -> tuple[int, int, int, int]:
    """A page-space rectangle as a pixel box (x0, y0, x1, y1), whatever the rotation."""
    left, bottom, right, top = rect
    points = [convert.to_bitmap(x, y) for x, y in
              ((left, bottom), (left, top), (right, bottom), (right, top))]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # One pixel of air around the glyph boxes, so the mark does not clip descenders.
    return min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1


def _blend_rect(pixels: bytearray, stride: int, channels: int, width: int, height: int,
                box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    x0, x1 = max(0, min(width, x0)), max(0, min(width, x1))
    y0, y1 = max(0, min(height, y0)), max(0, min(height, y1))
    if x0 >= x1 or y0 >= y1:
        return
    for y in range(y0, y1):
        start = y * stride + x0 * channels
        end = y * stride + x1 * channels
        segment = pixels[start:end]
        for c in range(3):
            segment[c::channels] = segment[c::channels].translate(_BLEND[c])
        pixels[start:end] = segment


def encode_png(pixels: bytes | bytearray, width: int, height: int, stride: int,
               channels: int) -> bytes:
    """Encode raw 8-bit rows as a PNG. 1 channel is gray, 3 is RGB, 4 is RGB plus padding.

    A fourth channel from PDFium is padding unless an alpha format was asked for, and none
    is, so it is dropped rather than written as transparency.
    """
    if channels not in (1, 3, 4):
        raise ValueError(f"cannot encode a {channels}-channel bitmap")
    out_channels = 1 if channels == 1 else 3
    row_bytes = width * channels
    raw = bytearray()
    for y in range(height):
        row = pixels[y * stride: y * stride + row_bytes]
        if channels == 4:
            rgb = bytearray(width * 3)
            rgb[0::3], rgb[1::3], rgb[2::3] = row[0::4], row[1::4], row[2::4]
            row = rgb
        raw += b"\x00"                                   # filter type: none
        raw += row
    color_type = 0 if out_channels == 1 else 2

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
