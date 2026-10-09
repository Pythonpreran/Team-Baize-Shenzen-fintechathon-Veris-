"""Render the cited page of a source PDF, with the quotation highlighted.

WHY THIS EXISTS. The evidence drawer could show a verbatim quote, a page number and a
pair of character offsets — and a reader who wanted to check it had to go and open the
document themselves. Showing the page next to the quote is the difference between
asserting the citation is real and demonstrating it.

WHOSE PAGES. This module renders whatever bytes it is handed; deciding WHICH documents
may be shown as pages is the API's job, because it depends on who published them. A
reader's own upload and the team's own synthetic samples are shown as pages. A third
party's publication from the corpus is not — the reader gets the cited paragraph and a
link to the publisher's copy instead (`excerpt()` below, and `source_page` in `api.py`).

WHAT IT REFUSES TO DO. The highlight is located by SEARCHING the rendered page for the
quoted string. It is never inferred from the stored character offsets, and the reason is
concrete: `char_start`/`char_end` index into `page_text.text`, which is pypdf's
extraction, while the rectangles come from PDFium's own text layer. The two disagree on
whitespace, on ligatures and on reading order, so mapping one onto the other would
produce a box that is confidently in the wrong place. A highlight nobody can trust is
worse than none, so when the string cannot be found this module says so — see
`Highlight.found` — and the caller renders the page unmarked and admits it rather than
drawing a rectangle it guessed at.

The match is reported with the METHOD that found it, so "we highlighted this" is always
qualified by how. EXACT means the full quotation was found on the page as one string.
The narrowed methods mean less than the whole quote was located, which is a weaker claim
and is labelled as one.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Optional

from . import pdfrender

# Display resolution. Lower than the 200 dpi `ocr.rasterise_pdf_pages` uses, because that
# figure is chosen for Tesseract's accuracy curve and this image is for a human eye in a
# 440px drawer. 144 dpi is exactly 2x CSS pixel density, so the page is crisp on a
# retina display without shipping a 2 MB PNG for a page nobody zoomed into.
DISPLAY_DPI = 144
FULLSCREEN_DPI = 200

# How the quotation was located, weakest last. Reported verbatim to the interface.
EXACT = "EXACT"
NORMALISED = "NORMALISED_WHITESPACE"
LEADING = "LEADING_FRAGMENT"
OFF_PAGE = "OFF_PAGE_CONTENT"
NOT_FOUND = "NOT_FOUND"

METHOD_NOTES: dict[str, str] = {
    EXACT: "The whole quotation was found on this page as a single run of text.",
    NORMALISED: ("The quotation was found after collapsing runs of whitespace. PDF "
                 "extraction disagrees with itself about spacing inside a line; the "
                 "words and their order are unchanged."),
    LEADING: ("Only the opening of the quotation could be located, so the highlight "
              "marks where it starts and not its full extent. The quoted text above is "
              "still the complete verbatim span."),
    OFF_PAGE: ("This document draws the quoted line BEYOND its own page boundary, so the "
               "text is in the file but a printer would clip it. The view below has been "
               "extended past the page edge to show it, and the area outside the page is "
               "marked. This is a defect in the source document, not in the citation — "
               "the quotation is exactly what the document contains."),
    NOT_FOUND: ("The quotation could not be located on the rendered page, so nothing is "
                "highlighted. The page is shown unmarked rather than with a rectangle "
                "drawn somewhere plausible. This is usually a page whose text is drawn "
                "as vectors or images, which the two extractors read differently."),
}

# Long quotations are the ones least likely to match as one string, because any single
# extraction difference anywhere in them defeats the search. This is how much of the
# opening is tried as a fallback.
_FRAGMENT_CHARS = 60
_MIN_FRAGMENT_CHARS = 12

#: Said whenever pages cannot be drawn because the renderer is absent. Names the package
#: so the remedy is concrete, and says what is NOT affected so nobody reads it as a
#: failed citation.
RENDERER_MISSING = ("Page images need the PDFium renderer (the pypdfium2 package), which "
                    "is not installed here. The quotation, its page number and its "
                    "offsets are unaffected — only the picture of the page is missing.")


def available() -> bool:
    """Whether a page can be rendered at all. Detected, never asserted."""
    return pdfrender.available()


@dataclass
class Highlight:
    found: bool
    method: str
    rect_count: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return {"found": self.found, "method": self.method,
                "rect_count": self.rect_count,
                "note": self.note or METHOD_NOTES.get(self.method, "")}


@dataclass
class RenderedPage:
    available: bool
    page_no: int = 0
    page_count: int = 0
    width: int = 0
    height: int = 0
    image_png: bytes = b""
    highlight: Highlight = field(
        default_factory=lambda: Highlight(False, NOT_FOUND))
    reason: str = ""

    def as_dict(self, *, include_image: bool = True) -> dict:
        out = {
            "available": self.available,
            "page_no": self.page_no,
            "page_count": self.page_count,
            "width": self.width,
            "height": self.height,
            "highlight": self.highlight.as_dict(),
            "reason": self.reason,
        }
        if include_image and self.image_png:
            out["image"] = ("data:image/png;base64,"
                            + base64.b64encode(self.image_png).decode("ascii"))
        return out


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _search(page: pdfrender.Page, quote: str) -> tuple[list, str]:
    """Locate `quote` on `page`. Returns (rects, method).

    Three attempts, weakest last, and each one is reported by name so the interface can
    say how much of the quotation the rectangles actually cover. PDFium's text layer
    includes text drawn outside the page box, so a hit can lie off the page; the caller
    checks for that separately rather than this function pretending it did not happen.
    """
    cleaned = _collapse(quote)
    if not cleaned:
        return [], NOT_FOUND

    def find(needle: str) -> list:
        try:
            return page.find(needle)
        except Exception:
            return []

    rects = find(cleaned)
    if rects:
        return rects, EXACT

    # A quotation spanning a line break is not found whole: the text layer carries the
    # break where the quotation carries a space. Fall back to the opening fragment, which
    # is usually within one line and is enough to put the reader in the right place.
    fragment = cleaned[:_FRAGMENT_CHARS]
    # Do not cut mid-word: a partial token is both less likely to match and misleading
    # about what was searched for.
    if len(cleaned) > _FRAGMENT_CHARS and " " in fragment:
        fragment = fragment.rsplit(" ", 1)[0]
    if len(fragment) >= _MIN_FRAGMENT_CHARS:
        rects = find(fragment)
        if rects:
            return rects, (EXACT if fragment == cleaned else LEADING)

    # Last attempt: the first line of the quotation as the document itself broke it.
    first_line = (quote or "").strip().splitlines()[0].strip() if (quote or "").strip() else ""
    if len(first_line) >= _MIN_FRAGMENT_CHARS and first_line != fragment:
        rects = find(first_line)
        if rects:
            return rects, NORMALISED

    return [], NOT_FOUND


def render_page(pdf_bytes: bytes, page_no: int, quote: str = "",
                dpi: int = DISPLAY_DPI) -> RenderedPage:
    """Render one 1-BASED page to PNG, highlighting `quote` where it can be found.

    `page_no` is 1-based to match `span.page_no` as stored by `ingest.ingest_pdf_bytes`,
    while PDFium indexes from 0. The conversion happens here, once, rather than at every
    call site — an off-by-one on this boundary shows the wrong page under a quotation,
    which reads as a fabricated citation.
    """
    # ORDER MATTERS. Absence of the file is a fact about the INPUT and is knowable without
    # any renderer at all, so it is answered first. Reporting "install the renderer" about
    # a citation whose bytes are simply not in the session would be advice that does not
    # help, about a dependency that is not the problem.
    if not pdf_bytes:
        return RenderedPage(
            available=False,
            reason=("The source file for this citation is not held in this session. "
                    "Uploaded bytes live in memory for the life of the case and are "
                    "dropped when the case is replaced or deleted."))
    if not available():
        return RenderedPage(available=False, reason=RENDERER_MISSING)

    try:
        with pdfrender.document(pdf_bytes) as doc:
            return _render_open(doc, page_no, quote, dpi)
    except pdfrender.PdfUnreadable as exc:
        return RenderedPage(
            available=False,
            reason=f"This document could not be opened for display: {exc}")


def _render_open(doc: pdfrender.Document, page_no: int, quote: str,
                 dpi: int) -> RenderedPage:
    count = doc.page_count
    index = int(page_no) - 1
    if index < 0 or index >= count:
        return RenderedPage(
            available=False, page_count=count,
            reason=(f"Page {page_no} is outside this document, which has "
                    f"{count} page{'s' if count != 1 else ''}."))
    page = doc.page(index)

    rects, method = _search(page, quote) if quote else ([], NOT_FOUND)

    # MEASURED, on this project's own demo schedule. `02-policy-schedule.pdf` draws part
    # of its content at y=832 on a 792pt page — below the page edge. pypdf reads the
    # content stream and returns it, so the engine stored the line and cited it correctly,
    # and PDFium's text layer finds it too — at coordinates below the bottom of the page,
    # where a render of the page box would crop it out of the picture. The citation is
    # real; the evidence would be invisible.
    #
    # So when the located text lies outside the page box, GROW THE PAGE until it comes
    # into view, and say so. OFF_PAGE states plainly that the view was extended and that
    # the source document is at fault, so an extended render is never passed off as an
    # ordinary page.
    off_page_note = ""
    if rects and page.outside(rects):
        if page.grow_to_include(rects):
            method = OFF_PAGE
        else:
            # The text is in the file but could not be brought into view. Say exactly
            # that instead of blaming extractor disagreement.
            off_page_note = (
                "The quoted line is present in this document's text but is drawn "
                "outside the printable page area, and the page could not be "
                "enlarged to bring it into view. Nothing is highlighted rather "
                "than a rectangle drawn somewhere plausible.")
            rects, method = [], NOT_FOUND

    image = page.render_png(dpi, highlights=rects)
    highlight = Highlight(
        found=bool(rects),
        method=method if rects else NOT_FOUND,
        rect_count=len(rects),
        note=off_page_note)
    if not quote:
        highlight = Highlight(
            found=False, method=NOT_FOUND, rect_count=0,
            note=("No quotation was named for this page, so nothing is "
                  "highlighted."))
    return RenderedPage(
        available=True, page_no=int(page_no), page_count=count,
        width=image.width, height=image.height, image_png=image.png,
        highlight=highlight)


# ── the excerpt: what a reader sees of somebody else's publication ──────────

#: How much of a page the excerpt may carry at most, either side of the quotation. A
#: paragraph is the unit a citation is read in; a page is a reproduction.
EXCERPT_CONTEXT_CHARS = 360


_PARAGRAPH_BREAKS = ("\r\n\r\n", "\n\n")


def excerpt(page_text: str, char_start: Optional[int], char_end: Optional[int],
            quote: str) -> dict:
    """The paragraph around a citation, cut from the page's stored text.

    Returns `{"text", "quote_start", "quote_end", "located", "cut_before", "cut_after"}`,
    where the quote offsets index into `text`. The quotation is located AT ITS STORED
    OFFSETS first — that is the claim the citation makes — and only searched for when the
    offsets do not hold it, in which case `located` says so. The excerpt is widened to the
    enclosing paragraph (a blank line) but never more than `EXCERPT_CONTEXT_CHARS` either
    side, so a page with no blank lines is not returned whole by way of a generous
    paragraph boundary. `cut_before`/`cut_after` say where the cap, rather than the
    paragraph, ended it.
    """
    text = page_text or ""
    quote = quote or ""
    start = end = None
    located = "NOT_FOUND"
    if (char_start is not None and char_end is not None
            and 0 <= char_start < char_end <= len(text)
            and quote and text[char_start:char_end] == quote):
        start, end, located = char_start, char_end, "AT_STORED_OFFSETS"
    elif quote and quote in text:
        start = text.index(quote)
        end, located = start + len(quote), "SEARCHED"
    if start is None or end is None:
        # Nothing to anchor on. The quotation itself is the excerpt, and nothing else of
        # the page is disclosed.
        return {"text": quote, "quote_start": 0, "quote_end": len(quote),
                "located": located, "cut_before": False, "cut_after": False}

    floor = max(0, start - EXCERPT_CONTEXT_CHARS)
    ceiling = min(len(text), end + EXCERPT_CONTEXT_CHARS)

    lo, cut_before = floor, floor > 0
    for brk in _PARAGRAPH_BREAKS:
        i = text.rfind(brk, floor, start)
        if i >= 0 and i + len(brk) > lo:
            lo, cut_before = i + len(brk), False
    if cut_before:
        # Capped mid-paragraph: start on a word, not inside one.
        gap = re.search(r"\s", text[lo:start])
        lo = lo + gap.end() if gap else start

    hi, cut_after = ceiling, ceiling < len(text)
    for brk in _PARAGRAPH_BREAKS:
        i = text.find(brk, end, ceiling)
        if 0 <= i < hi:
            hi, cut_after = i, False
    if cut_after:
        tail = text[end:hi]
        last_gap = max(tail.rfind(" "), tail.rfind("\n"))
        hi = end + last_gap if last_gap > 0 else end

    return {"text": text[lo:hi], "quote_start": start - lo, "quote_end": end - lo,
            "located": located, "cut_before": cut_before, "cut_after": cut_after}


def status() -> dict:
    return {
        "renderer": pdfrender.RENDERER if available() else None,
        "version": pdfrender.version(),
        "available": available(),
        "display_dpi": DISPLAY_DPI,
        "fullscreen_dpi": FULLSCREEN_DPI,
        "highlight_methods": [
            {"method": m, "note": METHOD_NOTES[m]}
            for m in (EXACT, NORMALISED, LEADING, OFF_PAGE, NOT_FOUND)
        ],
        "notice": ("The highlight is found by searching the rendered page for the "
                   "quoted string, never by mapping the stored character offsets onto "
                   "PDF coordinates — the two extractors disagree, and a confident box "
                   "in the wrong place is worse than no box. A quotation that cannot be "
                   "located is reported as NOT_FOUND and the page is shown unmarked."),
        "third_party_documents": (
            "A published document from the corpus is shown as the cited paragraph with a "
            "link to the publisher's own copy, never as a page image: it is somebody "
            "else's publication, and this product holds a reader's copy of it, not the "
            "right to reproduce it."),
    }
