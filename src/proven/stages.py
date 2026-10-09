"""Growth stages — protection that keeps up with the business (spec §13).

WHY THIS MODULE EXISTS.

The rest of the engine answers "is what you hold adequate for what you have?". It
cannot answer "what should a business like this hold at all?", because nothing in a
policy wording states that. A wording describes the cover it grants; it is silent on
the cover it does not grant, and silence is exactly where an SME's blind spot lives.

So this module does one narrow thing: it places the business on a growth ladder using
EVIDENCE ALREADY EXTRACTED FROM DOCUMENTS, and names the protections a business at
that point conventionally holds. Then it says which of those are present in the
schedule, which are absent, and which cannot be told either way.

THE HONESTY BOUNDARY, AND WHY IT IS DRAWN HERE.

Naming a protection the business does not hold is the single most dangerous thing this
product does, because it is the only output that is not derived from a document the
owner gave us. Three rules therefore hold, and each is enforced in code rather than
left to the caller's good manners:

  1. Every expectation carries a `basis` of STATUTORY or MARKET_CONVENTION, and the
     two are never merged in a count or a headline. Work injury cover for an employee
     doing manual work is law. Business interruption cover for a second site is a
     market habit. Presenting a habit with the force of law would be a lie told in
     the owner's favour, which is still a lie.

  2. `MARKET_CONVENTION` expectations may never be described as required, missing,
     non-compliant or a breach. `assert_invariant()` refuses to serialise a stage
     assessment whose language crosses that line.

  3. A stage is only ever assigned from facts whose `established` is True. A business
     is not moved up the ladder because an owner tapped a change event: an event is
     SELF-authored and, per events.py, may not create a fact. If the documents do not
     evidence the headcount, the stage says so and stops.

WHAT THIS IS NOT. It is not a quote, a recommendation to buy, or an assertion that a
gap will cause a claim to fail. It is the list of questions a business at this size
usually has to answer, with the ones this business has already answered marked off.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

# ── bases for an expectation ────────────────────────────────────────────────
#
# The distinction that keeps this module honest. STATUTORY is traceable to a named
# instrument; MARKET_CONVENTION is traceable to nothing stronger than what comparable
# businesses commonly carry, and must always be phrased as such.
STATUTORY = "STATUTORY"
MARKET_CONVENTION = "MARKET_CONVENTION"

# ── the protection vocabulary ───────────────────────────────────────────────
#
# Closed, like the event vocabulary. A protection absent from this table cannot be
# named by any stage, so a new stage cannot invent a product to sell.
PROTECTIONS: dict[str, dict[str, str]] = {
    "PROPERTY": {
        "label": "Property and assets",
        "plain": "Pays to repair or replace buildings, stock, plant and fittings.",
    },
    "PUBLIC_AND_PRODUCTS_LIABILITY": {
        "label": "Public and products liability",
        "plain": "Covers injury or damage you cause to other people or their things, "
                 "including by the products you sell.",
    },
    "WORK_INJURY": {
        "label": "Work injury compensation",
        "plain": "Covers employees injured at work. Compulsory in Singapore for all "
                 "employees doing manual work at any salary, and non-manual "
                 "employees earning $2,600 or less a month.",
    },
    "BUSINESS_INTERRUPTION": {
        "label": "Business interruption",
        "plain": "Replaces the income you lose while you cannot trade after damage "
                 "to your own premises.",
    },
    "CONTINGENT_BUSINESS_INTERRUPTION": {
        "label": "Supplier and customer dependency",
        "plain": "Replaces income lost when a supplier or customer you depend on is "
                 "damaged, even though your own premises are untouched. Normally an "
                 "extension to business interruption, so it cannot exist without it.",
    },
    "CYBER_AND_DATA": {
        "label": "Cyber and data",
        "plain": "Covers a data breach or a system attack: investigation, notifying "
                 "the people affected, restoring data, and regulatory penalties "
                 "where they are insurable.",
    },
    "GOODS_IN_TRANSIT": {
        "label": "Goods in transit",
        "plain": "Covers stock damaged or lost while it is being moved, which a "
                 "premises-based property section does not reach.",
    },
    "DIRECTORS_AND_OFFICERS": {
        "label": "Directors and officers",
        "plain": "Covers directors personally for claims about how the company was "
                 "run.",
    },
}


@dataclass(frozen=True)
class Expectation:
    """One protection a business at a given stage conventionally holds."""

    protection: str
    basis: str                  # STATUTORY | MARKET_CONVENTION
    because: str                # the observed reason, in the owner's words
    authority: Optional[str] = None   # named instrument, STATUTORY only

    def as_dict(self) -> dict:
        meta = PROTECTIONS[self.protection]
        return {
            "protection": self.protection,
            "label": meta["label"],
            "plain": meta["plain"],
            "basis": self.basis,
            "because": self.because,
            "authority": self.authority,
        }


@dataclass(frozen=True)
class Stage:
    """A rung on the ladder, and what it takes to be standing on it."""

    key: str
    ordinal: int
    label: str
    describes: str
    # Entry tests are declared as data so the ladder can be read without reading code.
    min_headcount: Optional[int] = None
    min_premises: Optional[int] = None
    requires_online_channel: bool = False

    def as_dict(self) -> dict:
        return {"key": self.key, "ordinal": self.ordinal, "label": self.label,
                "describes": self.describes}


# The ladder, lowest first. A business sits on the HIGHEST stage whose entry test is
# satisfied by established facts — never on an average of them, because a business
# with fourteen staff across two sites is not "somewhere between" one site and two.
LADDER: tuple[Stage, ...] = (
    Stage("FOUNDING", 0, "Just starting out",
          "One place, few or no employees, selling locally.",
          min_headcount=0, min_premises=0),
    Stage("ESTABLISHED", 1, "Trading steadily",
          "Employees on the books and a single place of business.",
          min_headcount=3, min_premises=1),
    Stage("SCALING", 2, "Growing",
          "More than one site, or an online channel, or a payroll into double "
          "figures.",
          min_headcount=10, min_premises=1),
    Stage("COMPLEX", 3, "Multi-site and data-handling",
          "Several sites, an online channel and a payroll large enough that a "
          "stoppage is expensive.",
          min_headcount=25, min_premises=2, requires_online_channel=True),
)
BY_KEY: dict[str, Stage] = {s.key: s for s in LADDER}

# What each stage conventionally holds. Cumulative: a stage inherits every expectation
# below it, so the ladder cannot accidentally drop a protection on the way up.
_STAGE_EXPECTS: dict[str, tuple[str, ...]] = {
    "FOUNDING": ("PROPERTY", "PUBLIC_AND_PRODUCTS_LIABILITY"),
    "ESTABLISHED": ("WORK_INJURY",),
    "SCALING": ("BUSINESS_INTERRUPTION", "GOODS_IN_TRANSIT"),
    "COMPLEX": ("CONTINGENT_BUSINESS_INTERRUPTION", "DIRECTORS_AND_OFFICERS"),
}

# Phrases that would turn a market habit into an obligation. Checked against every
# rendered string before the payload is allowed out.
_FORBIDDEN_FOR_CONVENTION: tuple[str, ...] = (
    "required", "must hold", "mandatory", "compulsory", "non-compliant",
    "in breach", "illegal", "you have to",
)

# Words in an ACRA secondary activity that evidence a channel reaching the public
# online. AUTHORITY-authored, which is why this is allowed to drive a stage at all.
_ONLINE_MARKERS: tuple[str, ...] = (
    "online", "e-commerce", "ecommerce", "internet", "web portal", "computer",
)


@dataclass
class StageAssessment:
    """The whole answer: where the business stands, and what that implies."""

    stage: Optional[Stage] = None
    evidence: list[dict] = field(default_factory=list)
    expectations: list[Expectation] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    indeterminate: list[str] = field(default_factory=list)
    next_stage: Optional[Stage] = None
    unestablished: list[str] = field(default_factory=list)

    def assert_invariant(self) -> None:
        """Refuse to serialise an assessment that overstates a market convention.

        This is a build-time guarantee rather than a review note: the phrasing lives
        in this module, so the module is where a regression would be introduced, and
        a test that reads the strings cannot be forgotten the way a convention can.
        """
        for exp in self.expectations:
            if exp.basis != MARKET_CONVENTION:
                continue
            haystack = f"{exp.because}".lower()
            for bad in _FORBIDDEN_FOR_CONVENTION:
                if bad in haystack:
                    raise AssertionError(
                        f"stages.py: expectation {exp.protection} is "
                        f"MARKET_CONVENTION but its reason says {bad!r}, which "
                        f"states an obligation the basis does not support")
            if exp.authority is not None:
                raise AssertionError(
                    f"stages.py: {exp.protection} is MARKET_CONVENTION but names "
                    f"authority {exp.authority!r}; only STATUTORY may cite one")
        for exp in self.expectations:
            if exp.basis == STATUTORY and not exp.authority:
                raise AssertionError(
                    f"stages.py: {exp.protection} claims STATUTORY basis but names "
                    f"no instrument")


def _established(facts, name: str) -> Optional[Any]:
    """Read a fact only if a document establishes it. `None` means we cannot say."""
    if facts is None:
        return None
    try:
        fact = facts.get(name)
    except AttributeError:
        return None
    return fact.value if getattr(fact, "established", False) else None


def _headcount(facts) -> tuple[Optional[int], Optional[str]]:
    """Employees on the books, preferring the authority document over the insurer's.

    The CPF contribution record is AUTHORITY-authored and reflects who is actually
    employed; the WIC schedule reflects who was declared. Where they disagree the
    larger is the business's real size, and the disagreement is itself a finding that
    compulsion.yaml already raises — so this function takes the higher of the two and
    does not re-litigate it.
    """
    cpf = _established(facts, "contributor_count")
    declared = _established(facts, "declared_headcount")
    candidates = [(int(v), src) for v, src in
                  ((cpf, "CPF contribution record"),
                   (declared, "work injury schedule")) if v is not None]
    if not candidates:
        return None, None
    return max(candidates, key=lambda pair: pair[0])


def _premises_count(facts) -> tuple[Optional[int], Optional[str]]:
    """How many places the business actually occupies.

    The tenancy is the document that establishes occupation; the schedule establishes
    only what was insured. Using the schedule here would let an unlisted site hide the
    growth that made it relevant, which is the precise failure this product exists to
    surface.
    """
    demised = _established(facts, "demised_premises")
    if isinstance(demised, (list, tuple)) and demised:
        return len(demised), "tenancy agreement"
    scheduled = _established(facts, "scheduled_premises")
    if isinstance(scheduled, (list, tuple)) and scheduled:
        return len(scheduled), "policy schedule"
    return None, None


def _online_channel(facts) -> tuple[bool, Optional[str], Optional[str]]:
    """Whether the business reaches the public through an online channel.

    Read from the ACRA secondary activity, which is AUTHORITY-authored. Returns
    (present, quote, source) so the caller can show the evidence rather than assert it.
    """
    secondary = _established(facts, "acra_secondary_activity")
    if isinstance(secondary, str) and secondary:
        low = secondary.lower()
        if any(marker in low for marker in _ONLINE_MARKERS):
            return True, secondary, "ACRA business profile"
    return False, None, None


def _stage_for(headcount: Optional[int], premises: Optional[int],
               online: bool) -> Optional[Stage]:
    """The highest rung whose entry test the evidence satisfies.

    A stage with an unmet requirement is not entered. Where a signal is unknown it is
    treated as NOT satisfying the test, so missing evidence can never inflate a stage
    and then inflate the list of things the owner is told they lack.
    """
    standing: Optional[Stage] = None
    for stage in LADDER:
        if stage.requires_online_channel and not online:
            continue
        if stage.min_headcount is not None:
            if headcount is None or headcount < stage.min_headcount:
                continue
        if stage.min_premises is not None:
            if premises is None or premises < stage.min_premises:
                continue
        standing = stage
    # SCALING is reachable by any ONE of its three signals, not all of them: a
    # single-site business that opens an online channel has genuinely changed shape.
    if standing is not None and standing.ordinal < 2:
        if online or (premises or 0) >= 2 or (headcount or 0) >= 10:
            standing = BY_KEY["SCALING"]
    return standing


def _expectations_upto(stage: Stage, *, headcount: Optional[int],
                       premises: Optional[int], online: bool,
                       online_quote: Optional[str]) -> list[Expectation]:
    """Every expectation at or below this rung, each with its observed reason."""
    out: list[Expectation] = []
    for rung in LADDER:
        if rung.ordinal > stage.ordinal:
            break
        for protection in _STAGE_EXPECTS.get(rung.key, ()):
            out.append(_expectation(protection, headcount=headcount,
                                    premises=premises, online=online,
                                    online_quote=online_quote))
    # Cyber is not a rung: it is triggered by handling customer data, which a
    # one-person online shop does as surely as a fifty-person one. Attaching it to a
    # size band would let the smallest businesses — the ones least likely to hold it —
    # never be told about it at all.
    if online:
        out.append(Expectation(
            "CYBER_AND_DATA", MARKET_CONVENTION,
            because=("Your ACRA profile records an online channel"
                     + (f' — "{online_quote}"' if online_quote else "")
                     + ". Businesses that collect customer details online commonly "
                       "carry cover for a data breach, because a standard property "
                       "and liability policy generally excludes it."),
        ))
    return out


def _expectation(protection: str, *, headcount: Optional[int],
                 premises: Optional[int], online: bool,
                 online_quote: Optional[str]) -> Expectation:
    """The reason a protection is expected, phrased from what was observed."""
    if protection == "WORK_INJURY":
        n = headcount if headcount is not None else 0
        return Expectation(
            protection, STATUTORY,
            because=(f"Your documents evidence {n} people on the payroll. Work "
                     f"injury cover is compulsory in Singapore for every employee "
                     f"doing manual work at any salary, and for non-manual "
                     f"employees earning $2,600 or less a month."),
            authority="Work Injury Compensation Act 2019 (Singapore)")
    if protection == "BUSINESS_INTERRUPTION":
        sites = f"{premises} sites" if (premises or 0) >= 2 else "your premises"
        return Expectation(
            protection, MARKET_CONVENTION,
            because=(f"With {sites} and a payroll to meet every month, a stoppage "
                     f"costs money even when the damage itself is repaired. "
                     f"Businesses at this size commonly insure the lost income as "
                     f"well as the damage."))
    if protection == "CONTINGENT_BUSINESS_INTERRUPTION":
        return Expectation(
            protection, MARKET_CONVENTION,
            because=("You depend on suppliers you do not own. Cover for income lost "
                     "when THEIR site is damaged is normally an extension to "
                     "business interruption, and is commonly added once a business "
                     "has dependencies it could not quickly replace."))
    if protection == "GOODS_IN_TRANSIT":
        return Expectation(
            protection, MARKET_CONVENTION,
            because=("Your documents describe moving goods to customers. A property "
                     "section covers stock at the insured address, so stock in a "
                     "vehicle is commonly insured separately."))
    if protection == "DIRECTORS_AND_OFFICERS":
        return Expectation(
            protection, MARKET_CONVENTION,
            because=("At this size, decisions are made by named directors who can be "
                     "pursued personally. Cover for that is commonly held once a "
                     "business has outside contracts and employees."))
    if protection == "PROPERTY":
        return Expectation(
            protection, MARKET_CONVENTION,
            because=("Every business with stock, equipment or fittings at a fixed "
                     "address commonly insures them."))
    if protection == "PUBLIC_AND_PRODUCTS_LIABILITY":
        return Expectation(
            protection, MARKET_CONVENTION,
            because=("You have customers and you make a product. Cover for harm "
                     "caused to other people or by what you sell is commonly the "
                     "first liability a business buys."))
    return Expectation(protection, MARKET_CONVENTION,
                       because="Commonly held by businesses of this shape.")


# Which operative section, if present on the schedule, evidences a protection. The
# match is on the schedule's own closed vocabulary; a protection with no entry here
# can never be reported as HELD, only as indeterminate.
_SECTION_EVIDENCE: dict[str, tuple[str, ...]] = {
    "PROPERTY": ("PROPERTY",),
    "PUBLIC_AND_PRODUCTS_LIABILITY": ("PUBLIC_AND_PRODUCTS_LIABILITY", "LIABILITY",
                                      "PUBLIC_LIABILITY"),
    "BUSINESS_INTERRUPTION": ("BUSINESS_INTERRUPTION", "BUSINESS_INTERUPTION",
                              "CONSEQUENTIAL_LOSS", "LOSS_OF_PROFITS"),
    "CYBER_AND_DATA": ("CYBER", "CYBER_AND_DATA", "DATA"),
    "GOODS_IN_TRANSIT": ("GOODS_IN_TRANSIT", "TRANSIT", "MARINE_CARGO"),
    "DIRECTORS_AND_OFFICERS": ("DIRECTORS_AND_OFFICERS", "D_AND_O",
                              "MANAGEMENT_LIABILITY"),
    # Deliberately absent: CONTINGENT_BUSINESS_INTERRUPTION and WORK_INJURY.
    # CBI is an extension inside a BI section, so a section list cannot evidence it;
    # work injury is a separate statutory policy that this schedule never contains.
}


def assess(case: dict) -> dict:
    """Place the business on the ladder and report what that implies.

    `case` is the pipeline's case dict. Only `facts` is read, and only through
    `_established`, so an unevidenced business yields an honest refusal rather than a
    stage assigned from nothing.
    """
    facts = (case or {}).get("facts")
    assessment = StageAssessment()

    headcount, headcount_src = _headcount(facts)
    premises, premises_src = _premises_count(facts)
    online, online_quote, online_src = _online_channel(facts)

    if headcount is not None:
        assessment.evidence.append({
            "signal": "People on the payroll", "value": headcount,
            "source": headcount_src, "established": True})
    else:
        assessment.unestablished.append(
            "How many people you employ — no CPF contribution record or work injury "
            "schedule was read.")
    if premises is not None:
        assessment.evidence.append({
            "signal": "Places you occupy", "value": premises,
            "source": premises_src, "established": True})
    else:
        assessment.unestablished.append(
            "How many places you occupy — no tenancy agreement or schedule of "
            "premises was read.")
    assessment.evidence.append({
        "signal": "Online sales channel",
        "value": "Yes" if online else "Not recorded",
        "source": online_src or "ACRA business profile", "established": online,
        "quote": online_quote})

    stage = _stage_for(headcount, premises, online)
    if stage is None:
        assessment.assert_invariant()
        return _result(assessment, available=False)

    assessment.stage = stage
    assessment.next_stage = next(
        (s for s in LADDER if s.ordinal == stage.ordinal + 1), None)
    assessment.expectations = _expectations_upto(
        stage, headcount=headcount, premises=premises, online=online,
        online_quote=online_quote)

    sections = _established(facts, "operative_sections") or []
    section_set = {str(s).upper() for s in sections} if isinstance(
        sections, (list, tuple)) else set()

    for exp in assessment.expectations:
        markers = _SECTION_EVIDENCE.get(exp.protection)
        if markers is None:
            # No section can evidence it either way. Saying "absent" here would be a
            # guess dressed as a finding.
            assessment.indeterminate.append(exp.protection)
        elif not section_set:
            assessment.indeterminate.append(exp.protection)
        elif section_set.intersection(markers):
            assessment.held.append(exp.protection)
        else:
            assessment.absent.append(exp.protection)

    assessment.assert_invariant()
    return _result(assessment, available=True)


def _result(a: StageAssessment, *, available: bool) -> dict:
    if not available:
        return {
            "available": False,
            "reason": ("We could not place your business on the growth ladder from "
                       "the documents read. Add a CPF contribution record or a "
                       "tenancy agreement and this fills in."),
            "evidence": a.evidence,
            "unestablished": a.unestablished,
            "ladder": [s.as_dict() for s in LADDER],
            "method": "DETERMINISTIC",
        }

    expectations = [e.as_dict() for e in a.expectations]
    statutory = [e for e in expectations if e["basis"] == STATUTORY]
    convention = [e for e in expectations if e["basis"] == MARKET_CONVENTION]
    absent_set = set(a.absent)

    return {
        "available": True,
        "stage": a.stage.as_dict() if a.stage else None,
        "next_stage": a.next_stage.as_dict() if a.next_stage else None,
        "evidence": a.evidence,
        "unestablished": a.unestablished,
        "ladder": [s.as_dict() for s in LADDER],
        "expectations": expectations,
        "held": a.held,
        "absent": a.absent,
        "indeterminate": a.indeterminate,
        "counts": {
            "expected": len(expectations),
            "held": len(a.held),
            "absent": len(a.absent),
            "indeterminate": len(a.indeterminate),
            "statutory": len(statutory),
            "market_convention": len(convention),
        },
        "absent_statutory": [e for e in statutory
                             if e["protection"] in absent_set],
        "absent_convention": [e for e in convention
                              if e["protection"] in absent_set],
        "method": "DETERMINISTIC",
        "basis_notice": (
            "STATUTORY items are traceable to a named Singapore instrument. "
            "MARKET_CONVENTION items are what comparable businesses commonly hold "
            "and are not obligations — they are questions worth answering, not "
            "failures."),
        "notice": (
            "Your stage is read from documents, not from anything you told us. "
            "Whether a protection is present is read from the operative sections on "
            "your schedule; where a schedule cannot evidence it either way we say so "
            "rather than guess. Nothing here is a quotation or an offer of cover."),
    }


def stage_of(case: dict) -> Optional[str]:
    """The stage key alone, for callers that only need to branch on it."""
    result = assess(case)
    stage = result.get("stage") or {}
    return stage.get("key")


def catalogue() -> dict:
    """The ladder and the protection vocabulary, for the UI and for the docs."""
    return {
        "ladder": [
            {**s.as_dict(),
             "adds": [{"protection": p, **PROTECTIONS[p]}
                      for p in _STAGE_EXPECTS.get(s.key, ())]}
            for s in LADDER
        ],
        "protections": [{"protection": k, **v} for k, v in PROTECTIONS.items()],
        "bases": [
            {"basis": STATUTORY,
             "means": "Traceable to a named Singapore instrument."},
            {"basis": MARKET_CONVENTION,
             "means": "Commonly held by comparable businesses. Not an obligation."},
        ],
        "method": "DETERMINISTIC",
    }
