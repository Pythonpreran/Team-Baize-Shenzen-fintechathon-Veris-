"""Per-page extraction quality, and the ceiling it puts on evidence (spec §21).

WHY THIS EXISTS, AND WHY IT IS A TRUST FEATURE RATHER THAN A CAVEAT.

The demo corpus is cleaner than reality. Published research on retrieval over scanned
documents finds that a high OCR character accuracy does NOT guarantee good downstream
retrieval — structural errors break it even when the character error rate is low. Clean
printed text scores in the high nineties; annotated or handwritten pages fall to roughly
80%; complex layouts to around 60%. Rotated pages break parsers before OCR even begins.

So the honest position is not "our extraction is good". It is: **we tell you which pages we
could not read, and we refuse to base a finding on them.** That converts the weakest part of
the pipeline into the most trustworthy statement the product makes about itself.

THE RULE THAT MAKES IT MORE THAN A REPORT.

A LOW_CONFIDENCE span may never support a PROVEN finding. The confidence of the EXTRACTION
constrains the class of the EVIDENCE, exactly as authorship already does — a website is
SELF-authored so it grades DECLARED however certain we are of the words; a page we could
barely read is UNESTABLISHED however authoritative its author. Both are the same principle:
the weakest link in the chain sets the class.

That matters here specifically because page 9 of the demo wording is where LMA5401, the
cyber exclusion, lives. If that page had scanned badly, the strongest finding in the build
would be resting on characters we guessed at.

WHAT THIS MODULE DOES NOT DO.

It does not repair pages, and it does not re-run OCR. `ocr.py` owns extraction; this reads
what extraction reported and turns it into a page-level statement and a class ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .ocr import LOW_CONFIDENCE_THRESHOLD, TEXT_LAYER_MIN_CHARS

# ── page verdicts ───────────────────────────────────────────────────────────
CLEAN = "CLEAN"
UNCERTAIN = "UNCERTAIN"
UNREADABLE = "UNREADABLE"
NOT_SCANNED = "NOT_SCANNED"      # had a real text layer; OCR never needed to run

VERDICTS: tuple[str, ...] = (CLEAN, UNCERTAIN, UNREADABLE, NOT_SCANNED)

VERDICT_LABELS: dict[str, str] = {
    CLEAN: "Read cleanly",
    UNCERTAIN: "Read, but not confidently",
    UNREADABLE: "Could not be read",
    NOT_SCANNED: "Digital text, no scanning needed",
}

# The strongest evidence class a finding may reach when it rests on a page with this
# verdict. This table IS the feature: it is the difference between a quality report and a
# quality control.
CLASS_CEILING: dict[str, str] = {
    CLEAN: "PROVEN",
    NOT_SCANNED: "PROVEN",
    UNCERTAIN: "UNESTABLISHED",
    UNREADABLE: "UNESTABLISHED",
}

# A page we are confident about but which yielded almost nothing is still suspect: a
# near-empty page with high confidence usually means the layout defeated the parser rather
# than that the page was blank.
_SPARSE_CHARS = TEXT_LAYER_MIN_CHARS


@dataclass(frozen=True)
class PageQuality:
    page_no: int
    verdict: str
    confidence: Optional[float]
    text_length: int
    reason: str
    class_ceiling: str

    def as_dict(self) -> dict:
        return {
            "page_no": self.page_no,
            "verdict": self.verdict,
            "verdict_label": VERDICT_LABELS[self.verdict],
            "confidence": (round(self.confidence, 1)
                           if self.confidence is not None else None),
            "text_length": self.text_length,
            "reason": self.reason,
            "class_ceiling": self.class_ceiling,
            "may_support_proven": self.class_ceiling == "PROVEN",
        }


def assess_page(*, page_no: int, confidence: Optional[float], text_length: int,
                status: Optional[str] = None,
                had_text_layer: bool = False) -> PageQuality:
    """One page's verdict, from what extraction reported.

    `had_text_layer=True` means pypdf found real embedded text and OCR never ran, which is
    the best case and is reported as such rather than being lumped in with a good scan —
    they are different facts and an auditor should be able to tell them apart.
    """
    if had_text_layer:
        return PageQuality(
            page_no, NOT_SCANNED, confidence, text_length,
            "The page carried a digital text layer, so nothing had to be recognised.",
            CLASS_CEILING[NOT_SCANNED])

    if text_length == 0 or status == "EMPTY":
        return PageQuality(
            page_no, UNREADABLE, confidence, text_length,
            "Nothing could be extracted from this page.",
            CLASS_CEILING[UNREADABLE])

    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        return PageQuality(
            page_no, UNCERTAIN, confidence, text_length,
            (f"Recognition confidence was {confidence:.0f}%, below the "
             f"{LOW_CONFIDENCE_THRESHOLD:.0f}% threshold. Words on this page may be "
             f"wrong or missing."),
            CLASS_CEILING[UNCERTAIN])

    if status == "LOW_CONFIDENCE":
        return PageQuality(
            page_no, UNCERTAIN, confidence, text_length,
            "Extraction reported low confidence for this page.",
            CLASS_CEILING[UNCERTAIN])

    if text_length < _SPARSE_CHARS:
        return PageQuality(
            page_no, UNCERTAIN, confidence, text_length,
            (f"Only {text_length} characters came off this page. A near-empty page with "
             f"good confidence usually means the layout defeated the parser rather than "
             f"that the page was blank."),
            CLASS_CEILING[UNCERTAIN])

    return PageQuality(
        page_no, CLEAN, confidence, text_length,
        "Read cleanly.", CLASS_CEILING[CLEAN])


def report(pages: Iterable[dict], *, document_kind: str = "",
           filename: str = "") -> dict:
    """A page-level quality report for one document.

    PAGE level, not document level, and that is the whole point. "This 28-page wording
    scanned at 91%" tells an owner nothing they can act on. "Page 9 of 28 was scanned at low
    quality, and the exclusion clause on it may be incomplete" tells them exactly what to
    re-upload — and page 9 is where LMA5401 actually lives in the demo wording, so this is
    not a hypothetical.
    """
    assessed: list[PageQuality] = []
    for page in pages or []:
        assessed.append(assess_page(
            page_no=int(page.get("page_no") or 0),
            confidence=page.get("confidence"),
            text_length=int(page.get("text_length") or 0),
            status=page.get("status"),
            had_text_layer=bool(page.get("had_text_layer"))))

    total = len(assessed)
    by_verdict = {v: [p for p in assessed if p.verdict == v] for v in VERDICTS}
    readable = len(by_verdict[CLEAN]) + len(by_verdict[NOT_SCANNED])
    suspect = [p for p in assessed if p.class_ceiling != "PROVEN"]

    return {
        "filename": filename,
        "document_kind": document_kind,
        "pages_total": total,
        "pages_read_cleanly": readable,
        "pages_uncertain": len(by_verdict[UNCERTAIN]),
        "pages_unreadable": len(by_verdict[UNREADABLE]),
        "summary": (
            f"{readable} of {total} page(s) read cleanly."
            + (f" {len(suspect)} page(s) could not be read confidently, and no finding "
               f"will be graded PROVEN on them."
               if suspect else " Every page is usable as evidence.")
            if total else "No pages were assessed."),
        "pages": [p.as_dict() for p in assessed],
        "pages_needing_attention": [p.as_dict() for p in suspect],
        "class_ceiling_rule": (
            "A page we could not read confidently caps any finding resting on it at "
            "UNESTABLISHED. The confidence of the extraction constrains the class of the "
            "evidence, exactly as authorship does — the weakest link sets the class."),
        "why_page_level": (
            "A document-level score cannot be acted on. Naming the page tells you which "
            "one to re-scan, and tells you which clause might be incomplete."),
        "method": "DETERMINISTIC",
    }


def ceiling_for_pages(quality_pages: Iterable[dict],
                      page_numbers: Iterable[int]) -> str:
    """The strongest class a finding citing these pages may reach.

    Takes the WEAKEST ceiling across every page cited, because a finding is only as good as
    the worst page it rests on. A finding quoting a clean page 3 and an uncertain page 9 is
    an uncertain finding.
    """
    wanted = {int(n) for n in page_numbers or []}
    if not wanted:
        return "PROVEN"
    index = {int(p.get("page_no") or 0): str(p.get("class_ceiling") or "PROVEN")
             for p in quality_pages or []}
    ceilings = [index.get(n, "PROVEN") for n in wanted]
    return "UNESTABLISHED" if "UNESTABLISHED" in ceilings else "PROVEN"


def cap_evidence_class(current: str, ceiling: str) -> str:
    """Apply the ceiling. Never raises a class, only lowers it.

    Deliberately one-directional. A clean scan is not evidence OF anything — it only means
    we read the words correctly. So a good page can never promote a DECLARED figure to
    PROVEN; it can only fail to demote it.
    """
    order = {"PROVEN": 3, "MARKET_CONVENTION": 2, "DECLARED": 1, "UNESTABLISHED": 0}
    if ceiling not in order or current not in order:
        return current
    return current if order[current] <= order[ceiling] else ceiling


def catalogue() -> dict:
    return {
        "verdicts": [{"verdict": v, "label": VERDICT_LABELS[v],
                      "class_ceiling": CLASS_CEILING[v]} for v in VERDICTS],
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "method": "DETERMINISTIC",
    }
