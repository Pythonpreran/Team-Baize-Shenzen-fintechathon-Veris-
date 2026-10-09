"""Clause-level market map, computed from published wordings.

WHAT THIS IS FOR
----------------
Once mechanisms are identified structurally (align.py) rather than by a hand-built
clause map, the same engine can be run across every wording a market publishes. The
result is a comparison no party currently holds:

  * for an SME  — "your policy engages at the first dollar; four of six carriers give
                   you an 85% cushion first"
  * for a carrier — "your under-insurance clause is the market outlier. On the harsh
                   side you create disputes; on the soft side you leak margin."

WHAT IT IS NOT
--------------
This is NOT price benchmarking. Advocate Technologies and others compare premiums
against a private market dataset; that needs their data and cannot be checked by a
reader. This compares CLAUSE SEMANTICS across PUBLISHED documents, so every cell
carries a carrier, a page and a verbatim quote, and anyone can verify it from the PDF.

It is also not cross-carrier wording resale, which scope boundaries rule out. We
publish mechanism-level positions — a threshold, a day count, whether a formula is
stated — never another carrier's text beyond the quoted span that evidences the cell.

HONESTY RULES ENFORCED HERE
---------------------------
  * A cell with no verified alignment reads NOT_ALIGNED. It never reads "absent",
    because not finding a mechanism is not evidence the wording lacks it.
  * Jurisdiction travels with every carrier. Malaysian wordings are in the corpus to
    prove the method generalises and must never be totalled into a Singapore position.
  * `harshness` is an ORDERING over stated thresholds, not a quality score. A carrier
    is not "worse" for having no threshold; the insured's exposure simply begins
    sooner, which is a fact about the contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import align, corpus

#: The directory of page-marked wording text, flat and keyed by corpus id. Resolved at
#: import because `build_market_map` takes it as a default argument; a test pointing at
#: a different directory passes one explicitly.
CORPUS = corpus.text_dir()

NOT_ALIGNED = "NOT_ALIGNED"

# The published wordings the map is computed from, named by CORPUS ID. `hand_read`
# records whether a human has read the wording, because a cell derived only from
# structural alignment carries less weight than one a person has checked, and the map
# says so. The id — not the filename — is the key: the same wording has been retrieved
# from two hosts under two filenames, and an id survives that.
CORPUS_WORDINGS: tuple[dict, ...] = (
    {"carrier": "Chubb", "key": "CHUBB", "jurisdiction": "SG", "hand_read": True,
     "corpus_id": "sg-chubb-selectplus-lite-wording",
     "product": "Select+ (Lite)", "published": "2020-07"},
    {"carrier": "MSIG", "key": "MSIG", "jurisdiction": "SG", "hand_read": True,
     "corpus_id": "sg-msig-sumo-wording",
     "product": "SUMO", "published": "2024-01"},
    {"carrier": "Liberty", "key": "LIBERTY", "jurisdiction": "SG", "hand_read": True,
     "corpus_id": "sg-liberty-smecare-wording",
     "product": "SMECare", "published": "2025-05"},
    {"carrier": "AIG (MY)", "key": "MY_AIG_FIRE", "jurisdiction": "MY",
     "hand_read": False, "corpus_id": "my-aig-sme-fire-and-perils-wording",
     "product": "SME Fire & Perils", "published": ""},
    {"carrier": "QBE (MY)", "key": "MY_QBE_RETAIL", "jurisdiction": "MY",
     "hand_read": False, "corpus_id": "my-qbe-retail-package-wording",
     "product": "Retail Package", "published": ""},
    {"carrier": "QBE (MY) IAR", "key": "MY_QBE_IAR", "jurisdiction": "MY",
     "hand_read": False, "corpus_id": "my-qbe-industrial-all-risk-wording",
     "product": "Industrial All Risk", "published": ""},
    {"carrier": "AIG (MY) AR", "key": "MY_AIG_ALLRISK", "jurisdiction": "MY",
     "hand_read": False, "corpus_id": "my-aig-all-risks-and-bi-wording",
     "product": "All Risks & BI", "published": ""},
)

# Which mechanisms the map renders, and how each cell is summarised for a reader.
MAP_MECHANISMS: tuple[dict, ...] = (
    {"key": "ADEQ.PROPORTIONATE_REDUCTION",
     "row": "Under-insurance: when payment starts being cut",
     "summary": "threshold"},
    {"key": "COND.UNOCCUPANCY",
     "row": "Unoccupancy: days before cover is affected",
     "summary": "days"},
    {"key": "COND.PREMIUM_PAYMENT_WARRANTY",
     "row": "Premium payment warranty",
     "summary": "present"},
    {"key": "COND.DECLARATION_CLAUSE_GENERAL",
     "row": "Compliance made a condition precedent",
     "summary": "present"},
)


def _pages(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    pages = [p for p in raw.split("=== PAGE ") if p.strip()]
    return pages or [raw]


@dataclass
class Cell:
    carrier: str
    jurisdiction: str
    hand_read: bool
    state: str                                  # ALIGNED | NOT_ALIGNED
    summary: str                                # what a reader sees
    detail: list[dict] = field(default_factory=list)   # one entry per provision

    def as_dict(self) -> dict:
        return {
            "carrier": self.carrier,
            "jurisdiction": self.jurisdiction,
            "hand_read": self.hand_read,
            "state": self.state,
            "summary": self.summary,
            "detail": self.detail,
        }


def _threshold_summary(alignments: list) -> tuple[str, list[dict]]:
    """Summarise under-insurance position.

    A carrier may hold more than one provision on different settlement bases —
    Liberty does — so every provision is listed and the summary says how many.
    """
    detail = []
    for a in alignments:
        thr = a.values.get("threshold_pct")
        detail.append({
            "engages_at": f"below {thr}%" if thr else "first dollar short",
            "threshold_pct": thr,
            "settlement_basis": a.values.get("settlement_basis"),
            "formula_stated": bool(a.values.get("stated_formula")),
            "called": a.called,
            "page_no": a.page_no,
            "quote": a.quote,
        })
    if not detail:
        return NOT_ALIGNED, []
    if len(detail) == 1:
        d = detail[0]
        extra = " · formula stated" if d["formula_stated"] else ""
        return f"{d['engages_at']}{extra}", detail
    parts = sorted({d["engages_at"] for d in detail})
    return f"{len(detail)} provisions: " + "; ".join(parts), detail


def _days_summary(alignments: list) -> tuple[str, list[dict]]:
    detail = []
    for a in alignments:
        detail.append({
            "days": a.values.get("days"),
            "instrument": a.values.get("instrument"),
            "called": a.called,
            "page_no": a.page_no,
            "quote": a.quote,
        })
    if not detail:
        return NOT_ALIGNED, []
    days = sorted({d["days"] for d in detail if d["days"]},
                  key=lambda x: int(x) if str(x).isdigit() else 999)
    if not days:
        return "present, no day count read", detail
    if len(days) == 1:
        return f"{days[0]} days", detail
    return f"{', '.join(days)} days — varies BY SECTION", detail


def _present_summary(alignments: list) -> tuple[str, list[dict]]:
    detail = [{
        "called": a.called,
        "page_no": a.page_no,
        "values": {k: v for k, v in a.values.items() if k != "days_word"},
        "quote": a.quote,
    } for a in alignments]
    if not detail:
        return NOT_ALIGNED, []
    return "present", detail


_SUMMARISERS = {
    "threshold": _threshold_summary,
    "days": _days_summary,
    "present": _present_summary,
}


def _harshness_note(cells: list[Cell]) -> str:
    """Order the under-insurance row by when exposure begins, Singapore only.

    Deliberately narrow. This is an ordering over what the clauses state, not a
    judgement about which carrier is better, and it is computed only over wordings a
    human has read in a single jurisdiction.
    """
    sg = [c for c in cells
          if c.jurisdiction == "SG" and c.state != NOT_ALIGNED and c.hand_read]
    if not sg:
        return ""

    first_dollar = {c.carrier for c in sg
                    if any(d["threshold_pct"] is None for d in c.detail)}
    cushioned = {c.carrier for c in sg
                 if any(d["threshold_pct"] for d in c.detail)}
    # A carrier can appear in BOTH sets, and Liberty does. Listing it twice without
    # saying so reads as a contradiction, so the overlap is named explicitly — it is
    # the most consequential fact in this row, not a presentation problem.
    both = sorted(first_dollar & cushioned)
    only_first = sorted(first_dollar - cushioned)
    only_cushion = sorted(cushioned - first_dollar)

    bits = []
    if only_first:
        bits.append(
            f"{', '.join(only_first)} cut payment from the first dollar of shortfall")
    if only_cushion:
        bits.append(
            f"{', '.join(only_cushion)} state a percentage cushion first")
    if both:
        bits.append(
            f"{', '.join(both)} does both, in different provisions on different "
            f"bases of settlement — so which one governs depends on how the claim is "
            f"valued, and reading only one of them is wrong in one direction or the "
            f"other")
    return ". ".join(bits) + "." if bits else ""


@dataclass
class MarketMap:
    rows: list[dict] = field(default_factory=list)
    carriers: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    proposals: int = 0
    rejected: int = 0

    def as_dict(self) -> dict:
        return {
            "carriers": self.carriers,
            "rows": self.rows,
            "notes": self.notes,
            "proposals": self.proposals,
            "rejected": self.rejected,
            "method": (
                "Computed by structural mechanism alignment over published policy "
                "wordings. Every cell carries a page and a verbatim quote. This "
                "compares clause semantics, not premiums: there is no private "
                "dataset behind it and any cell can be checked against the source "
                "PDF."),
            "jurisdiction_warning": (
                "Malaysian wordings are included to show the method generalises to a "
                "carrier nobody has read. They are NOT Singapore market positions "
                "and are excluded from any Singapore ordering."),
            "not_aligned_meaning": (
                "NOT_ALIGNED means no span in this wording satisfied the mechanism's "
                "structural requirements. It does NOT mean the wording lacks the "
                "mechanism."),
        }


# Memoised on (corpus_dir, jurisdiction) for the same reason pipeline._COMPARE_CACHE
# exists: this is a pure function of files that do not change while the process runs,
# and it costs ~0.65s per call because it aligns seven wordings from disk. That was the
# slowest thing on the page and it was being paid again on every jurisdiction switch.
#
# Keyed by corpus_dir so a test pointing at a different directory — including the
# empty-directory degraded-mode test — cannot be served another directory's answer.
_MAP_CACHE: dict[tuple[str, Optional[str]], "MarketMap"] = {}


def build_market_map(corpus_dir: Path | str = CORPUS,
                     only_jurisdiction: Optional[str] = None) -> MarketMap:
    """Align every corpus wording and assemble the comparison.

    Memoised per (corpus, jurisdiction). The corpus is immutable published documents,
    so a second call within one process can only produce the same answer.
    """
    # Not named `corpus`: that is the module this one resolves document ids through.
    corpus_text = Path(corpus_dir)
    cache_key = (str(corpus_text.resolve() if corpus_text.exists() else corpus_text),
                 only_jurisdiction)
    cached = _MAP_CACHE.get(cache_key)
    if cached is not None:
        return cached
    mm = MarketMap()
    results: dict[str, align.AlignmentResult] = {}

    for spec in CORPUS_WORDINGS:
        if only_jurisdiction and spec["jurisdiction"] != only_jurisdiction:
            continue
        # Resolved through the manifest rather than joined by hand, so an id that is
        # not in the manifest raises instead of quietly producing a smaller map.
        path = corpus.text_path_in(corpus_text, spec["corpus_id"])
        if not path.exists():
            continue
        # Verified at read time, not only at fetch time: the page-marked text is the
        # artefact the committed offsets were measured against, so a hand-replaced or
        # half-written copy raises `CorpusDrifted` instead of being aligned and cited.
        corpus.verify_text(spec["corpus_id"], path)
        res = align.align_pages(_pages(path), carrier=spec["key"])
        results[spec["key"]] = res
        mm.proposals += res.proposals
        mm.rejected += res.rejected
        mm.carriers.append({
            "carrier": spec["carrier"],
            "key": spec["key"],
            "jurisdiction": spec["jurisdiction"],
            "product": spec["product"],
            "published": spec["published"],
            "hand_read": spec["hand_read"],
            "verified": res.verified,
            "rejected": res.rejected,
        })

    for mech in MAP_MECHANISMS:
        summariser = _SUMMARISERS[mech["summary"]]
        cells: list[Cell] = []
        for spec in CORPUS_WORDINGS:
            res = results.get(spec["key"])
            if res is None:
                continue
            alignments = res.by_mechanism(mech["key"])
            summary, detail = summariser(alignments)
            cells.append(Cell(
                carrier=spec["carrier"],
                jurisdiction=spec["jurisdiction"],
                hand_read=spec["hand_read"],
                state=NOT_ALIGNED if summary == NOT_ALIGNED else "ALIGNED",
                summary=summary,
                detail=detail,
            ))
        row = {
            "mechanism": mech["key"],
            "row": mech["row"],
            "cells": [c.as_dict() for c in cells],
        }
        if mech["summary"] == "threshold":
            note = _harshness_note(cells)
            if note:
                row["ordering_note"] = note
        mm.rows.append(row)

    mm.notes.append(
        f"{len(mm.carriers)} published wordings aligned. "
        f"{mm.proposals} candidate spans proposed, {mm.rejected} rejected by the "
        f"structural verifier. Rejections are shown because a verifier whose "
        f"refusals are hidden cannot be distinguished from one that accepts "
        f"everything.")
    _MAP_CACHE[cache_key] = mm
    return mm


def position_for(carrier_key: str, mechanism: str,
                 mm: Optional[MarketMap] = None) -> dict:
    """Where one carrier sits on one mechanism, for use inside a Review Brief.

    Returns the carrier's own cell plus the comparable cells from the same
    jurisdiction, so an SME sees its position without the map implying a
    cross-jurisdiction comparison that would be wrong.
    """
    mm = mm or build_market_map()
    row = next((r for r in mm.rows if r["mechanism"] == mechanism), None)
    if row is None:
        return {"available": False}
    own = next((c for c in row["cells"]
                if c["carrier"].upper().startswith(carrier_key.upper()[:5])), None)
    if own is None:
        return {"available": False,
                "reason": f"{carrier_key} is not in the published-wording corpus."}
    peers = [c for c in row["cells"]
             if c["jurisdiction"] == own["jurisdiction"] and c is not own
             and c["state"] == "ALIGNED"]
    return {
        "available": True,
        "mechanism": mechanism,
        "row": row["row"],
        "own": own,
        "peers": peers,
        "ordering_note": row.get("ordering_note", ""),
    }
