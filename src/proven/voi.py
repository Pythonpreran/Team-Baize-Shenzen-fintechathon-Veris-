"""Value of information — ranking the documents that would collapse uncertainty.

WHY THIS EXISTS. The engine abstains when a required operand has no document behind
it, which is correct and, on its own, useless: an owner shown eight "information
missing" cards learns that the system is honest, not what to do on Monday. This module
turns each abstention into a ranked action by asking what obtaining the document would
resolve.

THE LINE THIS MODULE DEFENDS. "Here is what it costs you not to know" must never slide
into "here is what we think the number is". So:

  * No gap ever states a value for the missing figure. The output is the size of the
    CONSEQUENCE of not knowing it, never a guess at the operand itself.
  * Every interval bound carries a basis — EVIDENCED, ASSUMED or ZERO — so a plausible
    multiple is never mistaken for a reading from a document.
  * An exposure nobody has bounded is UNBOUNDED and ranks FIRST. It gets no
    percentage, because a share of a total that excludes it is arithmetic on an
    incomplete denominator.
  * A statutory gap gets no interval at all. A missing CPF record matters, but its
    consequence is a penalty and a deadline, not a range of payouts. Inventing an
    interval would fabricate a number; calling it UNBOUNDED would push a statutory item
    above a real monetary exposure. Neither is right.
  * Nothing here changes a finding. Every operand named below remains UNESTABLISHED
    until an actual document arrives.

MEASURED DEFECT THIS FIXES. Buildings had no sum insured, so no interval could be
computed, so the gap scored 0% of total uncertainty and sorted LAST — beneath gaps
worth tens of thousands. That told the business to deal with its largest unknown
exposure after everything else.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from . import jurisdiction

# ── bases for every interval bound ──────────────────────────────────────────
# A bound is only as good as where it came from, so it always says.
BOUND_EVIDENCED = "EVIDENCED"       # read from a document in this bundle
BOUND_ASSUMED = "ASSUMED_RANGE"     # a declared multiple, not a reading
BOUND_ZERO = "ZERO"                 # the floor, true by definition
UNBOUNDED = "UNBOUNDED"             # nothing in the bundle bounds this at all

# Stock at risk can exceed its sum insured, and how far is exactly what is unknown.
# This factor is the assumption that makes the interval finite, and it is labelled
# ASSUMED wherever it appears so it can never read as evidence.
ASSUMED_STOCK_FACTOR = Decimal("2")

# Gap kinds. `MONETARY_CAPPED` is bounded above by a sum insured that IS the policy's
# maximum. `MONETARY_SCALING` is bounded only by an assumption. `STATUTORY` has a
# consequence that is not a payout range. `QUALITATIVE` cannot be priced at all.
_CAPPED = "MONETARY_CAPPED"
_SCALING = "MONETARY_SCALING"
_STATUTORY = "STATUTORY"
_QUALITATIVE = "QUALITATIVE"


@dataclass(frozen=True)
class _Source:
    """Where a missing operand's evidence would come from, and how it is priced."""

    document_needed: str
    document_kind: str
    author_class: str            # drives becomes_class, per grading.py
    kind: str                    # one of the four kinds above
    bound_fact: Optional[str]    # the fact that bounds it, if any
    weight: int                  # tie-break severity within a tier
    note: str


# Operand name -> source. An operand absent from this map produces NO gap: a new rule
# must not be able to generate advice about a document nobody named.
_OPERAND_SOURCES: dict[str, _Source] = {
    "building_value": _Source(
        document_needed="Reinstatement cost assessment",
        document_kind="REINSTATEMENT_VALUATION",
        author_class="THIRD_PARTY", kind=_CAPPED,
        bound_fact="buildings_sum_insured", weight=100,
        note=("A surveyor's reinstatement assessment is the only document that "
              "establishes what the building would cost to rebuild.")),
    "total_plant_value": _Source(
        document_needed="Complete asset register",
        document_kind="ASSET_REGISTER",
        author_class="THIRD_PARTY", kind=_CAPPED,
        bound_fact="plant_sum_insured", weight=80,
        note=("One invoice evidences one machine. Adequacy needs the total value of "
              "plant at risk, which only a full register establishes.")),
    "stock_value": _Source(
        document_needed="Management accounts or stock valuation",
        document_kind="ACCOUNTS",
        author_class="SELF", kind=_SCALING,
        bound_fact="stock_sum_insured", weight=70,
        note=("Stock at risk can exceed the sum insured, and the gap between them is "
              "what average bites on.")),
    "value_at_risk": _Source(
        document_needed="Management accounts or stock valuation",
        document_kind="ACCOUNTS",
        author_class="SELF", kind=_SCALING,
        bound_fact="stock_sum_insured", weight=69,
        note=("Under-insurance is measured against the value actually at risk, which "
              "no policy document contains.")),
    "endorsement_effect": _Source(
        document_needed="Legible copy of the endorsement",
        document_kind="ENDORSEMENT",
        author_class="INSURER", kind=_QUALITATIVE,
        bound_fact=None, weight=60,
        note=("An endorsement can amend any term in the policy, so an unreadable one "
              "leaves every finding it touches provisional. Its effect cannot be "
              "priced without reading it.")),
    "contributor_count": _Source(
        document_needed="CPF contribution record",
        document_kind="CPF_RECORD",
        author_class="AUTHORITY", kind=_STATUTORY,
        bound_fact=None, weight=50,
        note=("Work injury cover is compulsory and tracks actual headcount. The "
              "consequence of a mismatch is statutory — a penalty and a declaration "
              "deadline — not a range of payouts, so no interval is stated.")),
    "declared_headcount": _Source(
        document_needed="Work injury policy schedule",
        document_kind="WIC_SCHEDULE",
        author_class="INSURER", kind=_STATUTORY,
        bound_fact=None, weight=49,
        note=("Without the declared figure there is nothing to check the CPF record "
              "against. The consequence is statutory rather than monetary, so no "
              "interval is stated.")),
    "holder_medical": _Source(
        document_needed="Work pass holder list",
        document_kind="WORK_PASS_LIST",
        author_class="AUTHORITY", kind=_STATUTORY,
        bound_fact=None, weight=48,
        note=("Migrant worker medical minimums are a statutory obligation. The "
              "consequence is regulatory rather than a payout range, so no interval "
              "is stated.")),
    "demised_premises": _Source(
        document_needed="Tenancy or lease agreement",
        document_kind="TENANCY",
        author_class="THIRD_PARTY", kind=_QUALITATIVE,
        bound_fact=None, weight=90,
        note=("Which premises you occupy decides whether the alteration condition is "
              "engaged. Its effect is on the whole altered risk, so it cannot be "
              "reduced to an interval.")),
    "scheduled_premises": _Source(
        document_needed="Policy schedule",
        document_kind="SCHEDULE",
        author_class="INSURER", kind=_QUALITATIVE,
        bound_fact=None, weight=89,
        note=("Without the schedule there is no list of insured premises to compare "
              "the lease against.")),
    "acra_secondary": _Source(
        document_needed="ACRA business profile",
        document_kind="ACRA_PROFILE",
        author_class="AUTHORITY", kind=_QUALITATIVE,
        bound_fact=None, weight=40,
        note=("Registered activities reveal operations the policy's business "
              "description may not reflect. The consequence is a disclosure question, "
              "not a computable amount.")),
    "invoice_value": _Source(
        document_needed="Supplier invoice",
        document_kind="SUPPLIER_INVOICE",
        author_class="THIRD_PARTY", kind=_QUALITATIVE,
        bound_fact=None, weight=30,
        note=("A third-party invoice upgrades an asset value from self-declared to "
              "proven for the item it covers.")),
    "operative_sections": _Source(
        document_needed="Policy schedule",
        document_kind="SCHEDULE",
        author_class="INSURER", kind=_QUALITATIVE,
        bound_fact=None, weight=45,
        note=("The schedule's operative sections are what decide whether a section "
              "such as business interruption is in force at all.")),
}


@dataclass
class _Gap:
    operand: str
    source: _Source
    interval: Optional[dict]
    unbounded: bool
    width: Decimal

    @property
    def tier(self) -> int:
        """0 = unbounded, 1 = priced, 2 = no interval. Unbounded ranks first because a
        gap of unknown size is not a small one."""
        if self.unbounded:
            return 0
        return 1 if self.interval is not None else 2


def _fact_value(facts: Any, name: Optional[str]) -> Optional[Decimal]:
    """Read a bounding fact, tolerating both PolicyFacts and a stub.

    `PolicyFacts.get` always returns a Fact; a minimal stand-in may return None. Both
    are supported because this module duck-types its arguments on purpose — it is
    called with real facts in the pipeline and with builders in tests.
    """
    if facts is None or not name:
        return None
    try:
        fact = facts.get(name)
    except Exception:
        return None
    if fact is None or not getattr(fact, "established", False):
        return None
    value = getattr(fact, "value", None)
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    return amount if amount > 0 else None


def _decimal_str(value: Decimal) -> str:
    """Plain digits, no exponent, no trailing .00 — these strings are read by humans."""
    quantised = value.quantize(Decimal("1")) if value == value.to_integral_value() \
        else value
    return format(quantised, "f")


def _money(value: object) -> str:
    return jurisdiction.format_money(Decimal(str(value)), "SGD")


def _build_gap(operand: str, source: _Source, facts: Any) -> _Gap:
    """Price one gap, or declare it unpriceable and say which kind of unpriceable."""
    if source.kind == _STATUTORY:
        # Real consequence, wrong shape for an interval. Ranked last, never inflated.
        return _Gap(operand=operand, source=source, interval=None, unbounded=False,
                    width=Decimal(0))

    bound = _fact_value(facts, source.bound_fact)
    if bound is None:
        # Nothing in the bundle bounds this. Unknown size, not zero size.
        return _Gap(operand=operand, source=source, interval=None, unbounded=True,
                    width=Decimal(0))

    if source.kind == _CAPPED:
        # The sum insured IS the policy's ceiling, so the upper bound is evidenced.
        high, high_basis = bound, BOUND_EVIDENCED
    else:
        # The value at risk can exceed the sum insured; how far is the unknown. The
        # multiple is an assumption and is labelled as one.
        high, high_basis = bound * ASSUMED_STOCK_FACTOR, BOUND_ASSUMED

    low = Decimal(0)
    return _Gap(
        operand=operand, source=source, unbounded=False, width=high - low,
        interval={
            "low": _decimal_str(low),
            "low_basis": BOUND_ZERO,
            "high": _decimal_str(high),
            "high_basis": high_basis,
            "width": _decimal_str(high - low),
            "unit": "SGD",
        })


def rank_information_gaps(findings: Any, facts: Any = None) -> dict:
    """Rank unevidenced operands by how much uncertainty obtaining them would remove.

    `findings` is any iterable of Finding-like objects exposing `required_operands`
    with `.name` and `.span_id`. `facts` is anything with `.get(name)` returning an
    object carrying `.established` and `.value`, or None.
    """
    # One gap per operand: the same missing document advised twice is noise.
    ordered_operands: list[str] = []
    for finding in findings or []:
        for operand in getattr(finding, "required_operands", None) or []:
            name = getattr(operand, "name", None)
            if not name or getattr(operand, "span_id", None) is not None:
                continue                    # already evidenced, so not a gap
            if name in _OPERAND_SOURCES and name not in ordered_operands:
                ordered_operands.append(name)

    gaps = [_build_gap(name, _OPERAND_SOURCES[name], facts)
            for name in ordered_operands]

    # Total uncertainty counts PRICED gaps only. Adding an unbounded gap as zero would
    # understate the total; adding it as a guess would fabricate one.
    total = sum((g.width for g in gaps if g.interval is not None), Decimal(0))

    # unbounded first, then widest priced, then the unpriceable-by-nature. Operand name
    # is the final tie-break so the ranking is deterministic.
    gaps.sort(key=lambda g: (g.tier, -g.source.weight, -g.width, g.operand)
              if g.tier != 1 else (g.tier, -g.width, -g.source.weight, g.operand))

    payload: list[dict] = []
    for gap in gaps:
        source = gap.source
        if gap.unbounded:
            uncertainty = UNBOUNDED
        elif gap.interval is not None:
            uncertainty = _decimal_str(gap.width)
        else:
            uncertainty = "0"

        share: Optional[str] = None
        if gap.interval is not None and total > 0:
            share = f"{int((gap.width / total * 100).to_integral_value())}%"

        payload.append({
            "operand": gap.operand,
            "document_needed": source.document_needed,
            "document_kind": source.document_kind,
            "author_class": source.author_class,
            # Follows authorship, never usefulness. Self-authored can only ever reach
            # DECLARED — the source hierarchy is the spine of the whole product.
            "becomes_class": ("DECLARED" if source.author_class == "SELF"
                              else "PROVEN"),
            "interval": gap.interval,
            "unbounded": gap.unbounded,
            "uncertainty_removed": uncertainty,
            "share_of_total": share,
            "note": source.note,
            "still_unestablished_until_supplied": True,
        })

    unbounded_count = sum(1 for g in gaps if g.unbounded)
    return {
        "gaps": payload,
        "count": len(payload),
        "unbounded_count": unbounded_count,
        "total_uncertainty": _decimal_str(total) if total else "0",
        "recommendation": _recommendation(payload),
        "method": (
            "Each unevidenced operand is ranked by the width of the exposure it "
            "leaves open, not by any guess at its value. An exposure nothing in the "
            "bundle bounds is reported as UNBOUNDED and ranked first, because a gap of "
            "unknown size is not a small one. This is a measure of what is not known — "
            "not a prediction of what the figure will turn out to be."),
        "caveat": (
            f"Bounds carry their basis. {BOUND_EVIDENCED} means read from a document in "
            f"this bundle; {BOUND_ASSUMED} means a declared multiple rather than a "
            f"reading, and is stated so it can never be mistaken for evidence; "
            f"{BOUND_ZERO} is the floor. {UNBOUNDED} means nothing here bounds it at "
            f"all, so no interval and no percentage is shown. Statutory gaps get no "
            f"interval because their consequence is a penalty and a deadline rather "
            f"than a range of payouts. Every operand listed here will remain "
            f"UNESTABLISHED until the document actually arrives — nothing on this page "
            f"changes a finding."),
    }


def _recommendation(payload: list[dict]) -> str:
    if not payload:
        return ("No information gaps: every required operand is evidenced by a "
                "document in this bundle.")
    top = payload[0]
    # The document name is reproduced verbatim, not lower-cased: it is the actionable
    # instruction in the sentence and it has to be searchable in the output.
    document = top["document_needed"]
    if top["unbounded"]:
        return (
            f"Obtain: {document}. Nothing in the documents supplied bounds this "
            f"exposure, so its size is unknown rather than small — which is why it is "
            f"ranked above gaps that do carry a figure. Obtaining it would make "
            f"{top['operand'].replace('_', ' ')} {top['becomes_class'].lower()} rather "
            f"than unestablished.")
    if top["interval"]:
        return (
            f"Obtain: {document}. It would close the widest measured gap — up to "
            f"{_money(top['interval']['high'])} of exposure currently unestablished — and "
            f"would make {top['operand'].replace('_', ' ')} "
            f"{top['becomes_class'].lower()}.")
    return (
        f"Obtain: {document}. Its consequence is statutory rather than monetary, so "
        f"no interval is stated, but it remains the first thing to resolve.")
