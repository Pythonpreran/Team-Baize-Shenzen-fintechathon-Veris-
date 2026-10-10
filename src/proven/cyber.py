"""Cyber and data exposure — the gap that is written down and never read (spec §14).

WHY THIS MODULE EXISTS.

`events.py` has carried this sentence since the first build: "Cyber and data exposure
is usually excluded rather than absent, and the exclusion is what has to be read." The
event existed. The reading did not. `score.py` declared a `digital` dimension with an
empty rule tuple, so the dimension always reported `assessed: false` — and because the
overall score is the mean of ASSESSED dimensions only, an entire risk class was
excluded from the number the owner is shown. The score looked better for the absence
of the check. That is the product enacting the very problem it exists to solve.

WHAT MAKES THIS DETERMINISTIC RATHER THAN A GUESS.

A cyber gap is usually presented as a probability: how likely is a breach, how much
might it cost. This module refuses that framing entirely, because it cannot be
evidenced from documents and because the answer would be a model's opinion.

Instead it asks three questions that documents CAN answer:

  1. Does the business demonstrably hold personal data? Read from AUTHORITY-authored
     documents — an ACRA profile recording an online channel, a CPF record proving
     employees exist. Holding personal data is what engages the PDPA; how much data,
     or how attractive a target it is, is not asked because it cannot be established.

  2. Does the policy respond to a data loss? Read from the schedule's operative
     sections and, where present, from the exclusion in the wording itself — quoted
     verbatim at a page number. An exclusion authored by the insurer is the strongest
     evidence in the system: it is the carrier stating in its own document that this
     is not covered.

  3. What obligations attach regardless of cover? The PDPA's notification duty and
     its financial penalty ceiling are statute. They are cited, not computed.

The finding is therefore the intersection of two documented facts — data is held, and
the policy says it does not respond — and not a prediction of anything.

WHAT THIS MODULE MUST NEVER DO. It must not state that a breach is likely, estimate a
penalty, or present the statutory ceiling as an expected loss. `assert_invariant()`
enforces the first two; the third is enforced by never putting the ceiling in a field
whose name implies exposure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from . import jurisdiction

# ── the data-protection regime, from the jurisdiction pack ──────────────────
#
# Figures are statute, not estimates, and each carries its instrument so a reviewer can
# check it. They used to be constants here, which stated Singapore's law about a business
# from anywhere. They now live in the pack the case is read under — for Singapore,
# jurisdictions/SG.yaml, each figure quoted from the Act or the Regulations — and the
# sentences below are filled from it. A pack with no data-protection regime, or one whose
# figures are not all verified for owner-facing use, gets no statement of the law at all.


@dataclass(frozen=True)
class Regime:
    """The figures the screen states, each from a verified pack entry."""

    label: str              # Personal Data Protection Act 2012 (Singapore)
    short_title: str        # PDPA
    regulator: str          # PDPC
    country: str            # Singapore
    currency: "jurisdiction.Currency"
    ceiling: Decimal
    trigger: Decimal
    turnover_share: str
    effective_from: str     # ISO date, as the prior build displayed it
    cited_as: str
    scale: int
    notify_days: int
    # Quote, link and status of every entry above, shown beside the statement (rule 17).
    sources: tuple = ()

    def money(self, amount: Decimal) -> str:
        return self.currency.format(amount)


def regime(pack: "jurisdiction.Pack") -> Optional[Regime]:
    """The data-protection regime the pack verifies, or None. All or nothing: a ceiling
    stated without the notification duty beside it would be half a statement of the law."""
    dp = pack.data_protection
    if dp is None or not dp.owner_facing or pack.currency is None:
        return None
    v = dp.values
    try:
        penalty = pack.entry(v["penalty"])
        notification = pack.entry(v["notification"])
        scale = pack.entry(v["significant_scale"])
        if not all(e.owner_facing for e in (penalty, notification, scale)):
            return None
        return Regime(
            label=str(v["label"]), short_title=str(v["short_title"]),
            regulator=str(v["regulator_short"]), country=pack.name,
            currency=pack.currency,
            ceiling=Decimal(str(penalty.values["penalty_ceiling"])),
            trigger=Decimal(str(penalty.values["turnover_trigger"])),
            turnover_share=str(penalty.values["turnover_share"]),
            effective_from=str(penalty.values["effective_from"]),
            cited_as=str(penalty.values["cited_as"]),
            scale=int(scale.values["affected_individuals"]),
            notify_days=int(notification.values["days"]),
            sources=tuple(e.provenance() for e in (dp, penalty, notification, scale)))
    except (KeyError, TypeError, ValueError, jurisdiction.PackError):
        return None

# ── cover states ────────────────────────────────────────────────────────────
COVER_PRESENT = "PRESENT"            # a cyber section is on the schedule
COVER_EXCLUDED = "EXCLUDED"          # the wording excludes it, and we can quote that
COVER_ABSENT = "ABSENT"              # no section, and no exclusion located
COVER_INDETERMINATE = "INDETERMINATE"  # no schedule sections read at all

_COVER_LABELS: dict[str, str] = {
    COVER_PRESENT: "A cyber section is on your schedule",
    COVER_EXCLUDED: "Your policy excludes it in writing",
    COVER_ABSENT: "No cyber section on your schedule",
    COVER_INDETERMINATE: "We could not read your operative sections",
}

# Section names that would evidence cyber cover.
_CYBER_SECTIONS: tuple[str, ...] = ("CYBER", "CYBER_AND_DATA", "DATA",
                                    "CYBER_LIABILITY", "DATA_PROTECTION")

# Headings that mark a cyber or electronic-data exclusion in a wording. Same
# vocabulary as taxonomy/classification.yaml EXCL.CYBER, kept in step deliberately:
# the classifier finds the provision, this module reads it.
_EXCLUSION_MARKERS: tuple[str, ...] = (
    "cyber", "electronic data", "data exclusion", "electronic means",
    "computer virus", "hacking", "denial of service",
)

# Phrases this module may never emit. Enforced, not documented.
_FORBIDDEN: tuple[str, ...] = (
    "likely to be breached", "will be attacked", "probability of a breach",
    "expected penalty", "you will be fined", "estimated fine",
)

_ONLINE_MARKERS: tuple[str, ...] = (
    "online", "e-commerce", "ecommerce", "internet", "web portal",
)


@dataclass(frozen=True)
class DataHolding:
    """One evidenced reason the business is inside the PDPA's scope."""

    subject: str            # whose data
    basis: str              # what the document shows
    document_kind: str
    author_class: str
    evidence_class: str     # PROVEN | DECLARED | UNESTABLISHED
    quote: Optional[str] = None
    page_no: Optional[int] = None
    count: Optional[int] = None

    def as_dict(self) -> dict:
        return {"subject": self.subject, "basis": self.basis,
                "document_kind": self.document_kind,
                "author_class": self.author_class,
                "evidence_class": self.evidence_class,
                "quote": self.quote, "page_no": self.page_no,
                "count": self.count}


@dataclass(frozen=True)
class CostHead:
    """A cost a data breach creates, and what in the policy responds to it.

    `responds` is deliberately a tri-state string rather than a boolean: "NOTHING" is
    a finding, "UNKNOWN" is an abstention, and collapsing them would turn an
    unanswered question into a confident negative.
    """

    head: str
    plain: str
    responds: str           # NOTHING | UNKNOWN | SECTION name
    why: str

    def as_dict(self) -> dict:
        return {"head": self.head, "plain": self.plain,
                "responds": self.responds, "why": self.why}


@dataclass
class CyberAssessment:
    holdings: list[DataHolding] = field(default_factory=list)
    cover_state: str = COVER_INDETERMINATE
    exclusion_quote: Optional[str] = None
    exclusion_page: Optional[int] = None
    exclusion_document_kind: Optional[str] = None
    cost_heads: list[CostHead] = field(default_factory=list)
    unestablished: list[str] = field(default_factory=list)

    def assert_invariant(self, *, gap_reported: bool = True) -> None:
        """No probability, no predicted penalty, and no gap without both halves.

        `gap_reported=False` is for the abstention path in `assess()`, which returns
        `available: False` and renders no gap at all.

        Why that parameter has to exist. The intersection rule below is a statement
        about what may appear ON SCREEN, not about what may exist in memory. The
        abstention path legitimately holds an EXCLUDED cover state with no holdings —
        that is precisely the situation it was written to handle gracefully — so
        running the rule there made the graceful path unreachable and turned a
        correct abstention into a 500. It fired on every wording carrying a cyber
        exclusion, which is the common case rather than an edge one: Chubb Select+
        carries LMA5401 at cl 4.3, so uploading that wording without an ACRA profile
        or CPF record crashed /api/cyber and, through it, /api/gap-summary and the
        overview that composes over it.
        """
        blob = " ".join(
            [h.basis for h in self.holdings]
            + [c.why for c in self.cost_heads]
            + [self.exclusion_quote or ""]
        ).lower()
        for bad in _FORBIDDEN:
            if bad in blob:
                raise AssertionError(
                    f"cyber.py: emitted forbidden predictive phrase {bad!r}")
        # A gap is the INTERSECTION of held data and absent cover. Reporting one
        # without the other would be either an alarm with no exposure behind it or an
        # exposure with no gap behind it, and both read as noise.
        if (gap_reported
                and self.cover_state in (COVER_EXCLUDED, COVER_ABSENT)
                and not self.holdings):
            raise AssertionError(
                "cyber.py: cover reported as unavailable but no data holding was "
                "established; that is not a gap and must not be presented as one")


def _established(facts, name: str) -> Optional[Any]:
    if facts is None:
        return None
    try:
        fact = facts.get(name)
    except AttributeError:
        return None
    return fact.value if getattr(fact, "established", False) else None


def _fact_meta(facts, name: str) -> tuple[Optional[str], Optional[str]]:
    """(document_kind, author_class) for an established fact, else (None, None)."""
    if facts is None:
        return None, None
    try:
        fact = facts.get(name)
    except AttributeError:
        return None, None
    if not getattr(fact, "established", False):
        return None, None
    doc = getattr(fact, "doc", None)
    return (getattr(doc, "kind", None) if doc else None,
            getattr(fact, "author_class", None))


# author_class -> evidence_class, mirroring grading.py. Duplicated as a mapping rather
# than imported because grading.py grades FINDINGS, and a data holding is not a
# finding; sharing the function would couple two things that must be free to diverge.
_GRADES_AS: dict[str, str] = {
    "AUTHORITY": "PROVEN", "THIRD_PARTY": "PROVEN", "INSURER": "PROVEN",
    "SELF": "DECLARED",
}


def _holdings(facts, law: Optional[Regime] = None,
              pack: Optional["jurisdiction.Pack"] = None
              ) -> tuple[list[DataHolding], list[str]]:
    """Every evidenced reason a data-protection law applies to this business."""
    out: list[DataHolding] = []
    gaps: list[str] = []
    under = f" under the {law.short_title}" if law is not None else ""
    # The records named as missing are the country's own; a pack that does not define
    # them gets the same sentence without the name.
    register_named = pack is not None and pack.lists("ACRA_PROFILE")
    payroll_named = pack is not None and pack.lists("CPF_RECORD")

    secondary = _established(facts, "acra_secondary_activity")
    if isinstance(secondary, str) and any(
            m in secondary.lower() for m in _ONLINE_MARKERS):
        kind, author = _fact_meta(facts, "acra_secondary_activity")
        out.append(DataHolding(
            subject="Customers",
            basis=("Your ACRA business profile records an online sales channel. "
                   "Selling online means collecting customer names, contact details "
                   f"and delivery addresses, which are personal data{under}."),
            document_kind=kind or "ACRA_PROFILE",
            author_class=author or "AUTHORITY",
            evidence_class=_GRADES_AS.get(author or "AUTHORITY", "DECLARED"),
            quote=secondary))
    else:
        gaps.append(
            "Whether you sell online or collect customer details — no ACRA profile "
            "recording a retail or online activity was read." if register_named else
            "Whether you sell online or collect customer details — no document "
            "recording a retail or online activity was read.")

    headcount = _established(facts, "contributor_count")
    if headcount is not None:
        kind, author = _fact_meta(facts, "contributor_count")
        n = int(headcount)
        out.append(DataHolding(
            subject="Employees",
            basis=(f"Your CPF contribution record evidences {n} employees. Payroll, "
                   f"NRIC and bank details are personal data, and an employer holds "
                   f"them whether or not it sells anything online."),
            document_kind=kind or "CPF_RECORD",
            author_class=author or "AUTHORITY",
            evidence_class=_GRADES_AS.get(author or "AUTHORITY", "DECLARED"),
            quote=f"contributor_count: {n}", count=n))
    else:
        gaps.append(
            "How many employees' records you hold — no CPF contribution record was "
            "read." if payroll_named else
            "How many employees' records you hold — no payroll or contribution record "
            "was read.")

    return out, gaps


def _find_exclusion(store) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """Locate a cyber or electronic-data exclusion and quote it VERBATIM.

    Returns (quote, page_no, document_kind). The quote is a slice of the page text as
    it actually appears, never a paraphrase, because the whole force of this finding is
    that the carrier said it in its own words. If nothing is located we return None and
    the caller reports ABSENT rather than EXCLUDED — inferring an exclusion we could
    not find would be the same class of error as inventing cover.
    """
    if store is None:
        return None, None, None
    for kind in ("BASE_WORDING", "ENDORSEMENT", "SCHEDULE"):
        try:
            docs = store.all(kind)
        except AttributeError:
            docs = []
        for doc in docs or []:
            for page_no, text in enumerate(getattr(doc, "pages", None) or [],
                                           start=1):
                if not text:
                    continue
                low = text.lower()
                for marker in _EXCLUSION_MARKERS:
                    idx = low.find(marker)
                    if idx < 0:
                        continue
                    # Only treat it as an exclusion if the surrounding text says so.
                    window = low[max(0, idx - 400): idx + 400]
                    if not any(w in window for w in
                               ("exclud", "not cover", "no liability",
                                "shall not apply", "does not insure")):
                        continue
                    quote = _sentence_around(text, idx)
                    if quote:
                        return quote, page_no, kind
    return None, None, None


def _sentence_around(text: str, idx: int) -> Optional[str]:
    """The sentence containing `idx`, trimmed, as it appears in the document."""
    start = max(0, idx - 600)
    end = min(len(text), idx + 600)
    window = text[start:end]
    pos = idx - start
    left = max((window.rfind(p, 0, pos) for p in (". ", ".\n", ";\n", "\n\n")),
               default=-1)
    right_candidates = [window.find(p, pos) for p in (". ", ".\n", ";\n")]
    right = min((r for r in right_candidates if r >= 0), default=-1)
    begin = left + 2 if left >= 0 else 0
    finish = right + 1 if right >= 0 else len(window)
    quote = " ".join(window[begin:finish].split())
    return quote[:600] if len(quote) >= 20 else None


def _cover_state(facts, store) -> tuple[str, Optional[str], Optional[int],
                                        Optional[str]]:
    sections = _established(facts, "operative_sections")
    if isinstance(sections, (list, tuple)) and sections:
        upper = {str(s).upper() for s in sections}
        if upper.intersection(_CYBER_SECTIONS):
            return COVER_PRESENT, None, None, None
        quote, page, kind = _find_exclusion(store)
        if quote:
            return COVER_EXCLUDED, quote, page, kind
        return COVER_ABSENT, None, None, None
    quote, page, kind = _find_exclusion(store)
    if quote:
        return COVER_EXCLUDED, quote, page, kind
    return COVER_INDETERMINATE, None, None, None


def _cost_heads(facts, cover_state: str, law: Regime) -> list[CostHead]:
    """The bills a data breach generates, and what in this policy meets each one.

    This is the heart of the screen. It is not a loss estimate: no figure appears.
    It is a list of cost heads with the section that responds, and the honest answer
    for a property-and-liability policy is that almost nothing does.
    """
    sections = _established(facts, "operative_sections")
    upper = {str(s).upper() for s in sections} if isinstance(
        sections, (list, tuple)) else set()
    has_liability = bool(upper.intersection(
        ("PUBLIC_AND_PRODUCTS_LIABILITY", "LIABILITY", "PUBLIC_LIABILITY")))
    has_bi = bool(upper.intersection(
        ("BUSINESS_INTERRUPTION", "CONSEQUENTIAL_LOSS", "LOSS_OF_PROFITS")))
    cyber_ok = cover_state == COVER_PRESENT
    unknown = cover_state == COVER_INDETERMINATE

    def state(responds_if_cyber: bool, otherwise: str, why: str) -> tuple[str, str]:
        if unknown:
            return "UNKNOWN", ("We could not read your operative sections, so we "
                               "cannot say what responds to this.")
        if cyber_ok and responds_if_cyber:
            return "CYBER", "Your schedule shows a cyber section, which is where "\
                            "this cost normally sits."
        return otherwise, why

    heads: list[CostHead] = []

    r, w = state(True, "NOTHING",
                 "A property section responds to physical damage to insured "
                 "property. Data is not physical property, so there is nothing for "
                 "it to attach to.")
    heads.append(CostHead(
        "Finding out what happened",
        "A forensic investigation to establish which records were taken and how.",
        r, w))

    r, w = state(True, "NOTHING",
                 "Notification is a statutory duty owed to the regulator and to "
                 "individuals. It is a compliance cost, not damage to property, so "
                 "no property or liability section reaches it.")
    heads.append(CostHead(
        "Telling the regulator and your customers",
        f"The {law.short_title} requires notifying the {law.regulator} within "
        f"{law.notify_days} days "
        f"of assessing a breach as notifiable, and notifying affected individuals "
        f"where significant harm is likely.",
        r, w))

    r, w = state(True, "NOTHING",
                 "Rebuilding data is not repair of insured property. The property "
                 "section measures loss by the cost of reinstating physical items.")
    heads.append(CostHead(
        "Getting your systems and records back",
        "Restoring data from backups, rebuilding systems, and paying for the time "
        "it takes.",
        r, w))

    r, w = state(True, "NOTHING",
                 "A financial penalty is imposed on the business for its own "
                 "conduct. Liability cover responds to what you owe someone else, "
                 "and penalties are commonly excluded or uninsurable in part.")
    heads.append(CostHead(
        f"A {law.regulator} financial penalty",
        "The Commission may impose a financial penalty where it finds the "
        "Protection Obligation was breached.",
        r, w))

    if has_bi:
        heads.append(CostHead(
            "Income lost while you are down",
            "Trading stops or slows while systems are rebuilt.",
            "BUSINESS_INTERRUPTION",
            "You hold a business interruption section. Whether it responds to a "
            "cyber event with no physical damage depends on its trigger wording, "
            "which a licensed adviser should read."))
    else:
        r, w = state(True, "NOTHING",
                     "You hold no business interruption section at all, so income "
                     "lost during downtime is not insured from any cause — cyber or "
                     "otherwise.")
        heads.append(CostHead(
            "Income lost while you are down",
            "Trading stops or slows while systems are rebuilt.", r, w))

    if has_liability:
        heads.append(CostHead(
            "Claims from the people whose data was taken",
            "Individuals affected by a breach may bring a claim.",
            "REVIEW",
            "You hold a public and products liability section. It responds to injury "
            "and physical damage; whether it reaches a pure data loss depends on its "
            "definitions and any cyber exclusion, which needs a licensed adviser."))
    else:
        r, w = state(True, "NOTHING",
                     "No liability section was read on your schedule.")
        heads.append(CostHead(
            "Claims from the people whose data was taken",
            "Individuals affected by a breach may bring a claim.", r, w))

    return heads


def assess(case: dict) -> dict:
    """The cyber and data position, from documents only."""
    facts = (case or {}).get("facts")
    store = (case or {}).get("store")
    pack = jurisdiction.pack_of(case)
    law = regime(pack)
    a = CyberAssessment()

    a.holdings, a.unestablished = _holdings(facts, law, pack)
    (a.cover_state, a.exclusion_quote, a.exclusion_page,
     a.exclusion_document_kind) = _cover_state(facts, store)

    # No evidenced data holding means no gap to report. Abstain rather than alarm.
    # The language half of the invariant still applies; the gap-intersection half does
    # not, because nothing below is rendered as a gap. See assert_invariant().
    if not a.holdings:
        a.cost_heads = []
        a.assert_invariant(gap_reported=False)
        # The documents worth adding are a property of the country: the two named here
        # are Singapore's, so they are named only under a pack that lists both.
        if pack.kind("ACRA_PROFILE") and pack.kind("CPF_RECORD"):
            add = (" Add your ACRA business profile or a CPF contribution record and "
                   "this screen fills in.")
        else:
            add = (" These documents are not read under a jurisdiction pack that "
                   "names the records which would establish it, so none is asked for.")
        return {
            "available": False,
            "reason": ("We could not establish from your documents that you hold "
                       "personal data, so we are not going to tell you that you have "
                       "a cyber gap." + add),
            "unestablished": a.unestablished,
            "method": "DETERMINISTIC",
        }

    # Data is held, but whether a data-protection law applies — and what it requires —
    # depends on the country. Without a verified regime there is nothing honest to say
    # about the law, so the screen says that instead of borrowing one.
    if law is None:
        a.cost_heads = []
        a.assert_invariant(gap_reported=False)
        return {
            "available": False,
            "reason": ("Your documents show you hold personal data. Which data-protection "
                       "law applies to it, and what that law requires, depends on the "
                       "country — and these documents are not read under a jurisdiction "
                       "pack that states a data-protection regime. So this screen makes "
                       "no statement about the law, its penalties or its deadlines."),
            "unestablished": a.unestablished,
            "jurisdiction_pack": pack.code,
            "method": "DETERMINISTIC",
        }

    a.cost_heads = _cost_heads(facts, a.cover_state, law)
    a.assert_invariant()

    uncovered = [c for c in a.cost_heads if c.responds == "NOTHING"]
    needs_review = [c for c in a.cost_heads if c.responds in ("REVIEW", "UNKNOWN")]
    gap_open = a.cover_state in (COVER_EXCLUDED, COVER_ABSENT)

    return {
        "available": True,
        "cover_state": a.cover_state,
        "cover_label": _COVER_LABELS[a.cover_state],
        "gap_open": gap_open,
        "evidence_class": ("PROVEN" if a.cover_state == COVER_EXCLUDED
                           else "DECLARED" if gap_open else "PROVEN"),
        "holdings": [h.as_dict() for h in a.holdings],
        "exclusion": ({
            "quote": a.exclusion_quote,
            "page_no": a.exclusion_page,
            "document_kind": a.exclusion_document_kind,
            "author_class": "INSURER",
            "evidence_class": "PROVEN",
            "why_it_matters": ("This is your insurer, in its own document, saying "
                              "this is not covered. It is the strongest kind of "
                              "evidence we can show you — there is nothing to "
                              "argue about."),
        } if a.exclusion_quote else None),
        "cost_heads": [c.as_dict() for c in a.cost_heads],
        "uncovered_count": len(uncovered),
        "review_count": len(needs_review),
        "statutory": {
            "regime": law.label,
            # The key keeps its old name because the screen reads it; the amount is in
            # the pack's currency, which for this regime is SGD.
            "penalty_ceiling_sgd": str(law.ceiling),
            "penalty_ceiling_basis": (
                f"From {law.effective_from} the maximum financial penalty "
                f"is {law.money(law.ceiling)}, or "
                f"{law.turnover_share} where that turnover exceeds "
                f"{law.money(law.trigger)}, whichever is higher."),
            "which_branch_applies": (
                f"We have not established your annual turnover in {law.country} from "
                f"your documents, so we cannot tell you which branch applies. For a "
                f"business under the {law.currency.millions(law.trigger)} threshold "
                f"the ceiling is {law.money(law.ceiling)}."),
            "notifiable_scale": law.scale,
            "notification_duty": (
                f"A breach must be notified to the {law.regulator} if it is likely to "
                f"cause significant harm to affected individuals, or affects at least "
                f"{law.scale} individuals. Notification to the {law.regulator} is "
                f"due within {law.notify_days} calendar days of assessing it "
                f"as notifiable."),
            "not_a_prediction": (
                f"This is the statutory ceiling, not an expected cost. The "
                f"{law.regulator} assesses harm and culpability case by case. We do not "
                f"predict penalties and this figure must not be read as one."),
            "authority": law.cited_as,
            "pack_sources": list(law.sources),
        },
        "unestablished": a.unestablished,
        "method": "DETERMINISTIC",
        "notice": (
            "Everything above is read from your own documents. We do not scan your "
            "network, estimate how likely a breach is, or price one. What we can "
            "show you is that you hold personal data and what your policy says about "
            "responding to its loss."),
    }


def as_external_signals(assessment: dict) -> list[dict]:
    """Feed the orchestrator, in the shape sentinel/discovery already use."""
    if not (assessment or {}).get("available") or not assessment.get("gap_open"):
        return []
    state = assessment.get("cover_state")
    return [{
        "id": "CYBER-GAP",
        "signal_type": "POTENTIAL_EXPOSURE",
        "label": "Personal data held; policy does not respond to its loss",
        "severity": "ELEVATED" if state == COVER_EXCLUDED else "MODERATE",
        "insured_interests": ["digital"],
        "asserts": ("Personal data holding is evidenced and the policy either "
                    "excludes or omits cover for its loss."),
        "simulated": False,
        "source": {"name": "Your own documents", "category": "POLICY",
                   "mode": "DOCUMENT"},
    }]
