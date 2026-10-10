"""Carrier and document-kind detection for uploaded bundles.

Deterministic and evidence-bearing. Every detection returns the matched phrase and
the page it was found on, so a reviewer can check the decision the same way they
check a finding. No model, and no silent default.

Two rules that are load-bearing rather than stylistic:

  1. `author_class` is NEVER caller-supplied. It derives from the detected kind AND
     the jurisdiction pack that applies (who authors a CPF record is a fact about
     Singapore, so it lives in jurisdictions/SG.yaml), because author_class derives
     evidence_class (grading.py) and a caller who could assert "AUTHORITY" could
     manufacture a PROVEN finding out of a self-written spreadsheet. An undetectable
     kind, or a kind the applicable pack does not list, falls back to SELF — the
     weakest class — so an unknown document can only ever weaken a finding.

  2. An unrecognised carrier is reported as UNKNOWN, not guessed. Classification
     then degrades to the heading-keyword path and the accounting header says so.
     Guessing CHUBB would import Chubb's 85% average threshold into a wording that
     may state no threshold at all — a wrong answer in the direction that costs the
     business money at claim time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import jurisdiction

UNKNOWN_CARRIER = "UNKNOWN"

# Distinctive carrier markers. Ordered most to least specific within each carrier;
# a marker must be specific enough that it cannot appear in a rival's wording.
CARRIER_MARKERS: dict[str, tuple[str, ...]] = {
    "CHUBB": ("chubb select+", "chubb insurance singapore", "chubb select",
              "chubb"),
    "MSIG": ("msig sumo", "msig insurance (singapore)", "msig"),
    "LIBERTY": ("liberty smecare", "liberty insurance pte", "liberty insurance",
                "smecare"),
    "AIG": ("aig asia pacific insurance", "aig singapore", "american home assurance"),
    "QBE": ("qbe insurance (singapore)", "qbe insurance"),
    "GREAT_EASTERN": ("great eastern general insurance", "bizprotect",
                      "bizsupreme"),
    "ALLIED_WORLD": ("allied world assurance",),
    # The fictional sandbox insurer that the carrier workspace and the sandbox
    # counterparty speak for. A real insurer is listed above only because the corpus
    # holds that insurer's own published wording.
    "LOTUS_GENERAL": ("lotus general (sandbox)", "lotus general"),
}

# Document-kind markers. A schedule and a base wording read very differently, and
# getting this wrong changes which rules can run at all.
# Phrases that identify a SPECIFIC kind so strongly that a more generic kind must
# not outrank them, however many generic markers it also matches.
#
# The case that forced this: a Work Injury Compensation policy schedule is a real,
# common document and its own title contains the words "policy schedule". It
# therefore scored higher as a property SCHEDULE (5) than as a WIC_SCHEDULE (3),
# was ingested as the policy schedule, and displaced the real one — so the work
# injury headcount finding reported its own source document as "not provided", and
# an unrelated finding quoted "WORK INJURY" as though it came from the schedule.
# Wrong document, wrong quote, and the quote is the thing this product sells.
#
# Generic markers cannot settle a document's identity when a specific phrase names
# it outright. These win.
DECISIVE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("WIC_SCHEDULE", ("work injury compensation", "workmen's compensation",
                      "workmens compensation", "wica")),
    ("CPF_RECORD", ("central provident fund", "cpf contribution")),
    ("WORK_PASS_LIST", ("work permit holders", "work permit no",
                        "s pass holders")),
    ("ACRA_PROFILE", ("accounting and corporate regulatory authority",
                      "business profile extract")),
    ("TENANCY", ("tenancy agreement", "lease agreement", "demised premises")),
    ("SUPPLIER_INVOICE", ("tax invoice", "invoice no")),
    ("PROPOSAL", ("proposal form", "declaration by proposer")),
)

KIND_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SCHEDULE", ("policy schedule", "schedule of insurance", "sum insured",
                  "period of insurance", "the schedule", "insured name",
                  "policy number")),
    ("ENDORSEMENT", ("endorsement no", "endorsement number", "this endorsement",
                     "memorandum of endorsement", "it is hereby endorsed")),
    ("BASE_WORDING", ("policy wording", "general conditions", "general exclusions",
                      "operative clause", "definitions", "we agree to indemnify",
                      "insuring clause")),
    ("PROPOSAL", ("proposal form", "application form", "declaration by proposer")),
    ("CPF_RECORD", ("central provident fund", "cpf contribution", "cpf board")),
    ("WIC_SCHEDULE", ("work injury compensation", "wica", "workmen's compensation")),
    ("WORK_PASS_LIST", ("work permit", "s pass", "employment pass")),
    ("ACRA_PROFILE", ("acra", "business profile", "unique entity number", "ssic")),
    ("TENANCY", ("tenancy agreement", "lease agreement", "demised premises",
                 "permitted use", "landlord")),
    ("ACCOUNTS", ("balance sheet", "management accounts", "statement of financial "
                  "position", "inventory", "trade receivables")),
    ("SUPPLIER_INVOICE", ("invoice no", "tax invoice", "invoice number")),
)

# author_class is DERIVED from kind, by the jurisdiction pack that applies: each pack's
# `document_kinds` block is the table that used to sit here. Never widen one without
# reading grading.py — every entry is a claim about who had an independent reason to get
# the document right, and the loader refuses a country pack that re-grades a kind the
# GENERIC pack shares.

# A kind the owner may choose in the UI. Anything else is detected only.
DECLARABLE_KINDS: tuple[str, ...] = (
    "BASE_WORDING", "SCHEDULE", "ENDORSEMENT", "CPF_RECORD", "WIC_SCHEDULE",
    "WORK_PASS_LIST", "ACRA_PROFILE", "TENANCY", "ACCOUNTS", "SUPPLIER_INVOICE",
    "PROPOSAL",
)

# Weighted so a wording's incidental "sum insured" mention cannot outrank a
# schedule's structural markers. Position matters: the first two pages of a document
# identify it, page 20 does not.
_FRONT_PAGES = 3


@dataclass
class Detection:
    """What was detected, and the evidence for it."""

    value: str
    confidence: str                      # HIGH | LOW | NONE
    matched: Optional[str] = None
    page_no: Optional[int] = None
    considered: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"value": self.value, "confidence": self.confidence,
                "matched": self.matched, "page_no": self.page_no,
                "considered": self.considered}


def _score(pages: list[str], markers: tuple[str, ...],
           front_weight: int = 3) -> tuple[int, Optional[str], Optional[int]]:
    """Count marker hits, weighting the identifying front pages more heavily.
    Returns (score, first matched marker, page it matched on)."""
    total = 0
    first_match: Optional[str] = None
    first_page: Optional[int] = None
    for page_no, text in enumerate(pages, start=1):
        low = text.lower()
        weight = front_weight if page_no <= _FRONT_PAGES else 1
        for marker in markers:
            if marker in low:
                total += weight
                if first_match is None:
                    first_match, first_page = marker, page_no
    return total, first_match, first_page


def detect_carrier(pages: list[str]) -> Detection:
    """Identify the carrier from the wording text. UNKNOWN is a legitimate answer
    and must stay legitimate: it is what stops a Chubb threshold being applied to a
    wording nobody has read."""
    scores: dict[str, int] = {}
    best: tuple[int, Optional[str], Optional[int], Optional[str]] = (0, None, None,
                                                                    None)
    for carrier, markers in CARRIER_MARKERS.items():
        score, matched, page_no = _score(pages, markers)
        if score:
            scores[carrier] = score
        if score > best[0]:
            best = (score, matched, page_no, carrier)

    if best[3] is None:
        return Detection(UNKNOWN_CARRIER, "NONE", considered=scores)
    # A tie between two carriers is not a detection. Two carrier names in one file
    # usually means a comparison document or a broker slip.
    top = sorted(scores.values(), reverse=True)
    confidence = "HIGH" if (len(top) == 1 or top[0] > top[1]) and best[0] >= 3 \
        else "LOW"
    return Detection(best[3], confidence, best[1], best[2], scores)


def _decisive_kind(pages: list[str]) -> Optional[tuple[str, str, int]]:
    """A phrase that names the document outright beats any amount of generic
    scoring. Returns (kind, matched phrase, page) or None."""
    for kind, markers in DECISIVE_MARKERS:
        score, matched, page_no = _score(pages, markers)
        if score:
            return kind, matched or "", page_no or 1
    return None


def detect_kind(pages: list[str], filename: str = "") -> Detection:
    """Identify the document kind. Filename hints are used only to break a tie —
    a filename is owner-authored and cannot outrank the document's own text."""
    decisive = _decisive_kind(pages)

    scores: dict[str, int] = {}
    best: tuple[int, Optional[str], Optional[int], Optional[str]] = (0, None, None,
                                                                    None)
    for kind, markers in KIND_MARKERS:
        score, matched, page_no = _score(pages, markers)
        # A base wording is long; a schedule is short. Length is real evidence.
        if kind == "BASE_WORDING" and len(pages) >= 10:
            score += 4
        if kind == "SCHEDULE" and len(pages) <= 6:
            score += 2
        if score:
            scores[kind] = score
        if score > best[0]:
            best = (score, matched, page_no, kind)

    # A decisive phrase settles it before generic scores are compared.
    if decisive is not None:
        kind, matched, page_no = decisive
        return Detection(kind, "HIGH", matched, page_no, scores)

    name = (filename or "").lower()
    hint = next((k for k in DECLARABLE_KINDS
                 if k.lower().replace("_", "") in re.sub(r"[^a-z]", "", name)), None)
    if best[3] is None:
        if hint:
            return Detection(hint, "LOW", matched=f"filename:{name}",
                             considered=scores)
        return Detection("UNKNOWN", "NONE", considered=scores)

    top = sorted(scores.values(), reverse=True)
    confidence = "HIGH" if best[0] >= 6 and (len(top) == 1 or top[0] > top[1]) \
        else "LOW"
    if confidence == "LOW" and hint and hint in scores:
        best = (scores[hint], f"filename:{name}", 1, hint)
    return Detection(best[3], confidence, best[1], best[2], scores)


def author_class_for(kind: str,
                     pack: Optional["jurisdiction.Pack"] = None) -> str:
    """The evidence-authorship class for a detected kind. Unknown -> SELF, the
    weakest class, so an unidentified document can never strengthen a finding.

    With a pack — which is how the pipeline always calls it — that pack's answer. With
    none (the upload screen's list of kinds, which has no case yet), the answer every
    pack that lists the kind agrees on, and SELF where they disagree."""
    return jurisdiction.author_class_for(kind, pack)
