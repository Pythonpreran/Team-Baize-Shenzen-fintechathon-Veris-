"""The carrier book, built from real engine output instead of invented rows.

WHAT THIS REPLACES, AND WHY IT HAD TO GO.

The carrier screens used to read from `_CARRIER_DEMO_ACCOUNTS`, six hardcoded rows with
names like "Kimly Metalworks Pte Ltd" and a `lead_reason` typed by hand. Those rows were
labelled synthetic everywhere they surfaced and carried no citations, so they were honest
rather than deceptive. They were still invented, and in a product whose entire argument is
"every number traces to a document" an invented portfolio is the one screen a hostile
reviewer can dismiss without engaging.

So this module builds the carrier view from two sources, both real:

  1. ACCOUNTS THAT ACTUALLY EXIST. Exactly the cases the engine currently holds. Today
     that is one — the case in the caller's session — and the payload says `n=1` rather
     than dressing it as a book. A book of one, honestly counted, is worth more than a
     book of sixty-one that cannot be checked.

  2. THE WORDING PORTFOLIO. Every published wording listed in `corpus/manifest.yaml`,
     aligned by the same deterministic verifier that produces findings, with the real
     threshold, the real day count and the real condition-precedent position read out of
     each document. Seven wordings, 118+ pages, four of them never hand-mapped.

WHY (2) IS THE ONE AN UNDERWRITER ACTUALLY WANTS.

"Which of my accounts drifted" needs a book we do not have. "What does my wording do to an
insured, and how does that compare with the wording the competitor across the street is
using" needs only published documents — and it is a live underwriting question with a real
answer. Chubb cuts payment below 85% adequacy; MSIG cuts from the first dollar short;
Liberty carries two provisions with different thresholds on different settlement bases. An
underwriter pricing against those three is pricing against materially different contracts,
and nothing in the market tells them so.

WHAT IS DELIBERATELY ABSENT.

No invented account names. No invented premium. No invented loss ratio. No
`internal_change`/`external_risk` score for a business nobody has documents for. Where a
field cannot be computed it is omitted rather than defaulted, because a default on a
carrier screen reads as a measurement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import align, corpus, marketmap

# The mechanisms an underwriter is being asked to compare, in the order they matter to
# an insured's recovery. Keyed to the same taxonomy the findings engine uses, so a cell
# here and a finding there cannot drift apart.
COMPARED_MECHANISMS: tuple[dict, ...] = (
    {"key": "ADEQ.PROPORTIONATE_REDUCTION",
     "label": "Under-insurance",
     "question": "At what point does the insurer start cutting the payment?"},
    {"key": "COND.UNOCCUPANCY",
     "label": "Unoccupancy",
     "question": "How many days empty before cover is affected?"},
    {"key": "COND.PREMIUM_PAYMENT_WARRANTY",
     "label": "Premium warranty",
     "question": "Does late premium terminate the policy automatically?"},
    {"key": "COND.DECLARATION_CLAUSE_GENERAL",
     "label": "Compliance condition",
     "question": "Can breaching an unrelated rule forfeit the whole claim?"},
)


@dataclass
class WordingRow:
    """One published wording, as the engine actually read it."""

    carrier: str
    key: str
    product: str
    jurisdiction: str
    published: str
    hand_read: bool
    pages: int
    proposals: int
    verified: int
    rejected: int
    collapsed: int
    mechanisms: dict = field(default_factory=dict)
    severity: Optional[str] = None
    severity_because: str = ""

    def as_dict(self) -> dict:
        return {
            "carrier": self.carrier,
            "key": self.key,
            "product": self.product,
            "jurisdiction": self.jurisdiction,
            "published": self.published or None,
            "hand_read": self.hand_read,
            "pages": self.pages,
            "alignment": {
                "proposals": self.proposals,
                "verified": self.verified,
                "rejected": self.rejected,
                "collapsed": self.collapsed,
                "rejection_rate": (round(self.rejected / self.proposals, 2)
                                   if self.proposals else None),
            },
            "mechanisms": self.mechanisms,
            "severity_for_insured": self.severity,
            "severity_because": self.severity_because or None,
        }


def _provisions_of(result: align.AlignmentResult, key: str) -> list[dict]:
    """Every bound provision for one mechanism, with the values actually read.

    `called` is the carrier's own heading for the clause, which is the point: the same
    mechanism is "Average" at Chubb, "UNDERINSURANCE" at MSIG and "Average Relief" at
    Liberty. Carrying the carrier's wording rather than ours is what lets a reader check
    the cell against the page.
    """
    out: list[dict] = []
    for a in result.by_mechanism(key):
        out.append({
            "called": getattr(a, "called", None),
            "page_no": getattr(a, "page_no", None),
            "values": dict(getattr(a, "values", {}) or {}),
            "quote": (getattr(a, "quote", "") or "")[:400],
        })
    return out


def _severity(mechs: dict, *, hand_read: bool) -> tuple[Optional[str], str]:
    """How harsh this wording is on an under-insured claimant, from its own numbers.

    An ORDERING over stated thresholds, never a quality score. A wording that cuts from
    the first dollar short is harsher on the insured than one granting an 85% cushion,
    and that is a fact about the text rather than an opinion about the carrier.
    """
    adeq = mechs.get("ADEQ.PROPORTIONATE_REDUCTION") or {}
    provisions = adeq.get("provisions") or []
    if not provisions:
        return None, ""
    # Everything below describes only the provisions the verifier BOUND. Where a wording
    # has not been hand-read, that binding is unconfirmed, so the sentence has to say so.
    # Without this the screen reported "No threshold is stated" for four Malaysian
    # wordings nobody has ever read — which asserts an absence from an unverified
    # reading, and is precisely the overclaim the rest of this product refuses.
    caveat = ("" if hand_read else
              " Unconfirmed: no person has read this wording, so this is the "
              "verifier's reading of the clause it bound, not a checked position.")

    thresholds = []
    for p in provisions:
        thr = (p.get("values") or {}).get("threshold_pct")
        thresholds.append(thr)
    # No threshold in any BOUND provision -> on this reading any shortfall bites.
    if all(t in (None, "", 0) for t in thresholds):
        return ("HARSHEST" if hand_read else "HARSHEST_UNCONFIRMED"), (
            "The bound provision states no threshold, so on this reading any shortfall "
            "reduces the payment from the first dollar." + caveat)
    numeric = []
    for t in thresholds:
        try:
            numeric.append(float(str(t).replace("%", "")))
        except (TypeError, ValueError):
            continue
    if not numeric:
        return None, ""
    if len(set(numeric)) > 1 or len(provisions) > 1:
        return ("SPLIT" if hand_read else "SPLIT_UNCONFIRMED"), (
            f"{len(provisions)} provisions on different settlement bases, so the answer "
            f"depends which basis governs the claim." + caveat)
    return ("CUSHIONED" if hand_read else "CUSHIONED_UNCONFIRMED"), (
        f"A shortfall is tolerated until adequacy falls below {numeric[0]:g}%." + caveat)


def wording_portfolio(corpus_dir: Path | str = marketmap.CORPUS) -> dict:
    """Every published wording, aligned. Real output or an empty list — never filler."""
    corpus_text = Path(corpus_dir)
    rows: list[WordingRow] = []

    for spec in marketmap.CORPUS_WORDINGS:
        # Resolved through the manifest, not joined by hand: an id that is not in the
        # manifest raises here, instead of becoming a path that merely does not exist
        # and a wording silently dropped from the portfolio.
        path = corpus.text_path_in(corpus_text, spec["corpus_id"])
        if not path.exists():
            continue
        # The page-marked text is what the committed offsets were measured against, so a
        # copy that is not the manifest's raises `CorpusDrifted` rather than being aligned.
        corpus.verify_text(spec["corpus_id"], path)
        pages = marketmap._pages(path)
        result = align.align_pages(pages, carrier=spec["key"])

        mechs: dict = {}
        for mech in COMPARED_MECHANISMS:
            provisions = _provisions_of(result, mech["key"])
            mechs[mech["key"]] = {
                "label": mech["label"],
                "question": mech["question"],
                "state": "ALIGNED" if provisions else "NOT_ALIGNED",
                "provisions": provisions,
                # NOT_ALIGNED is not "absent". The verifier did not bind a span; the
                # wording may still carry the mechanism in language this build does not
                # yet recognise. Saying "absent" would be a finding we cannot support.
                "reading": ("Read from the document" if provisions
                            else "Not located by the verifier — not evidence of absence"),
            }

        row = WordingRow(
            carrier=spec["carrier"], key=spec["key"], product=spec.get("product", ""),
            jurisdiction=spec["jurisdiction"], published=spec.get("published", ""),
            hand_read=bool(spec.get("hand_read")),
            pages=len(pages),
            proposals=result.proposals, verified=result.verified,
            rejected=result.rejected, collapsed=result.collapsed,
            mechanisms=mechs,
        )
        row.severity, row.severity_because = _severity(
            mechs, hand_read=bool(spec.get("hand_read")))
        rows.append(row)

    sg = [r for r in rows if r.jurisdiction == "SG"]
    my = [r for r in rows if r.jurisdiction != "SG"]

    return {
        "available": bool(rows),
        "wordings": [r.as_dict() for r in rows],
        "counts": {
            "wordings": len(rows),
            "singapore": len(sg),
            "other_jurisdiction": len(my),
            "hand_read": sum(1 for r in rows if r.hand_read),
            "never_hand_mapped": sum(1 for r in rows if not r.hand_read),
            "pages_read": sum(r.pages for r in rows),
        },
        "compared": [dict(m) for m in COMPARED_MECHANISMS],
        "divergence": _divergence(sg),
        "method": "DETERMINISTIC_STRUCTURAL_ALIGNMENT",
        "notice": (
            "Every row is this engine's own output over a published policy wording in "
            "this repository. Nothing here is illustrative and no account is invented. "
            "Malaysian wordings are shown to demonstrate the method generalises to "
            "documents nobody hand-mapped, and are never totalled into a Singapore "
            "position."),
    }


def _divergence(sg_rows: list[WordingRow]) -> dict:
    """Where Singapore wordings disagree. The whole argument for reading per document."""
    out: list[dict] = []
    for mech in COMPARED_MECHANISMS:
        positions: list[dict] = []
        for r in sg_rows:
            m = r.mechanisms.get(mech["key"]) or {}
            positions.append({
                "carrier": r.carrier,
                "state": m.get("state"),
                "provisions": len(m.get("provisions") or []),
                "values": [(p.get("values") or {}) for p in (m.get("provisions") or [])],
            })
        aligned = [p for p in positions if p["state"] == "ALIGNED"]
        distinct = {str(sorted((v or {}).items())) for p in aligned for v in p["values"]}
        out.append({
            "mechanism": mech["key"],
            "label": mech["label"],
            "carriers_compared": len(positions),
            "carriers_aligned": len(aligned),
            "distinct_positions": len(distinct),
            "diverges": len(distinct) > 1,
        })
    return {
        "rows": out,
        "diverging": sum(1 for r in out if r["diverges"]),
        "compared": len(out),
        "what_it_means": (
            "A mechanism that diverges cannot be hardcoded. One rule applied to every "
            "carrier would be wrong for at least one of them, in a direction that either "
            "under-reports the insured's exposure or over-reports it."),
    }


def real_accounts(cases: list[dict]) -> dict:
    """Accounts that genuinely exist, counted honestly.

    `cases` is whatever the caller can legitimately see. Today a session holds at most
    one, so this returns n<=1 and says so. The shape is a list because the moment a real
    pilot supplies a second case nothing here changes.
    """
    rows: list[dict] = []
    for case in cases or []:
        brief = (case or {}).get("brief") or {}
        if not brief:
            continue
        summary = brief.get("summary") or {}
        by_class = summary.get("by_class") or {}
        by_state = summary.get("by_state") or {}
        acc = brief.get("accounting") or {}
        rows.append({
            "business_name": brief.get("business"),
            "carrier": brief.get("carrier"),
            "case_id": brief.get("case_id"),
            "findings_total": summary.get("finding_count"),
            "by_evidence_class": by_class,
            "by_output_state": by_state,
            "abstained": summary.get("abstained"),
            "needs_authorised_review": by_state.get("REQUIRES_AUTHORISED_REVIEW", 0),
            "information_missing": by_state.get("INFORMATION_MISSING", 0),
            "clause_accounting": {
                "detected": acc.get("detected"),
                "classified": acc.get("classified"),
                "unaccounted": acc.get("unaccounted"),
                "verified_classified": acc.get("verified_classified"),
                "carrier_in_clause_map": acc.get("carrier_in_clause_map"),
            },
            "source": brief.get("source"),
            "documents_are_synthetic": bool(brief.get("synthetic")),
        })

    return {
        "available": bool(rows),
        "n": len(rows),
        "accounts": rows,
        "honest_statement": (
            f"{len(rows)} account(s). This is the real count of cases this engine holds, "
            f"not a projection. A carrier pilot over an in-force book is what turns this "
            f"into a portfolio; the per-account arithmetic below is already the arithmetic "
            f"that would run across it."
            if rows else
            "No case is loaded, so there is no account to report. This screen shows the "
            "real count rather than a placeholder."),
    }
