"""Compounding — the gap that widens while nothing happens (spec §17).

WHY THIS MODULE EXISTS.

"As businesses grow, those gaps quietly compound." Every other screen in this product
is a snapshot: here is your position today. A snapshot cannot show compounding, because
compounding is a shape over time — and the shape is what makes the problem feel
inevitable rather than accidental.

What this module does is narrow and entirely arithmetic. It builds two ordered series
from dated documents:

  * things that changed in the BUSINESS — a lease commencing, an invoice for new
    equipment, accounts showing more stock than last time;
  * things that changed in the POLICY — an endorsement, a schedule amendment.

Then it matches them. A business change with a policy change after it is a closed loop.
A business change with nothing after it is an open one, and the module reports how many
days it has been open. That number is the compounding: not a projection, not a risk
score, just the count of days between a business becoming different and its policy
being told.

WHY THE DAY COUNT IS THE RIGHT MEASURE, AND NOT A PROJECTED LOSS.

The tempting output here is money: "this gap has cost you S$X in unrecovered
exposure." That figure would be fabricated. Exposure is not incurred continuously —
it crystallises only if a loss happens, and we do not know whether one will. Days
open, by contrast, is a fact: it is the difference between two dates in two documents.
It conveys the same urgency without asserting anything we cannot evidence.

`assert_invariant()` therefore refuses to serialise any event carrying a monetary
"cost of delay", and the module has no field for one.

WHAT AN OPEN LOOP IS NOT. It is not a breach, a lapse, or a prediction that a claim
will fail. Several of these gaps have entirely innocent explanations — an endorsement
we were not given, a cover note issued verbally. The output says so, every time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from . import jurisdiction


def _money(value: object) -> str:
    return jurisdiction.format_money(Decimal(str(value)), "SGD")

# ── what kind of change this is ─────────────────────────────────────────────
BUSINESS_CHANGE = "BUSINESS_CHANGE"
POLICY_CHANGE = "POLICY_CHANGE"

# ── whether the loop closed ─────────────────────────────────────────────────
CLOSED = "CLOSED"              # a policy change followed it
OPEN = "OPEN"                  # nothing followed it
NOT_APPLICABLE = "NOT_APPLICABLE"  # a policy change needs no response

_STATUS_LABELS: dict[str, str] = {
    CLOSED: "Policy updated",
    OPEN: "Policy not updated",
    NOT_APPLICABLE: "Policy change",
}

# Phrases that would turn an open loop into an accusation.
_FORBIDDEN: tuple[str, ...] = (
    "breach", "lapsed", "void", "invalid", "will be declined", "not covered at all",
)


@dataclass(frozen=True)
class Change:
    """One dated thing that happened, and what it did or did not trigger."""

    on: date
    kind: str                     # BUSINESS_CHANGE | POLICY_CHANGE
    label: str
    detail: str
    document_kind: str
    author_class: str
    evidence_class: str
    status: str = OPEN
    closed_by: Optional[str] = None
    closed_on: Optional[date] = None
    days_open: Optional[int] = None
    what_would_close_it: Optional[str] = None
    interest: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "on": self.on.isoformat(),
            "kind": self.kind,
            "label": self.label,
            "detail": self.detail,
            "document_kind": self.document_kind,
            "author_class": self.author_class,
            "evidence_class": self.evidence_class,
            "status": self.status,
            "status_label": _STATUS_LABELS[self.status],
            "closed_by": self.closed_by,
            "closed_on": self.closed_on.isoformat() if self.closed_on else None,
            "days_open": self.days_open,
            "what_would_close_it": self.what_would_close_it,
            "interest": self.interest,
        }


@dataclass
class CompoundingReport:
    changes: list[Change] = field(default_factory=list)
    as_at: Optional[date] = None
    unestablished: list[str] = field(default_factory=list)

    def assert_invariant(self) -> None:
        blob = " ".join(f"{c.label} {c.detail} {c.what_would_close_it or ''}"
                        for c in self.changes).lower()
        for bad in _FORBIDDEN:
            if bad in blob:
                raise AssertionError(
                    f"compounding.py: emitted adjudicating phrase {bad!r}; an open "
                    f"loop is a timing observation, not a coverage decision")
        for c in self.changes:
            if c.status == OPEN and c.days_open is None:
                raise AssertionError(
                    f"compounding.py: {c.label!r} is OPEN but carries no day count, "
                    f"so nothing is being measured")
            if c.status == OPEN and not c.what_would_close_it:
                raise AssertionError(
                    f"compounding.py: {c.label!r} is OPEN but names no way to close "
                    f"it, which makes it an alarm rather than an action")


def _established(facts, name: str) -> Optional[Any]:
    if facts is None:
        return None
    try:
        fact = facts.get(name)
    except AttributeError:
        return None
    return fact.value if getattr(fact, "established", False) else None


def _fact_meta(facts, name: str) -> tuple[str, str]:
    if facts is None:
        return "UNKNOWN", "SELF"
    try:
        fact = facts.get(name)
    except AttributeError:
        return "UNKNOWN", "SELF"
    doc = getattr(fact, "doc", None)
    return (getattr(doc, "kind", "UNKNOWN") if doc else "UNKNOWN",
            getattr(fact, "author_class", "SELF"))


_GRADES_AS: dict[str, str] = {
    "AUTHORITY": "PROVEN", "THIRD_PARTY": "PROVEN", "INSURER": "PROVEN",
    "SELF": "DECLARED",
}


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _policy_changes(store) -> list[Change]:
    """Endorsements, in date order. These are the business's policy responses."""
    out: list[Change] = []
    if store is None:
        return out
    try:
        docs = store.all("ENDORSEMENT")
    except AttributeError:
        return out
    for doc in docs or []:
        data = getattr(doc, "data", None) or {}
        when = _as_date(data.get("effective_date"))
        if when is None:
            continue
        ref = str(data.get("endorsement_ref") or "Endorsement")
        effect = str(data.get("effect") or data.get("title") or "").strip()
        illegible = "page_2_extraction" in data and "UNREADABLE" in str(
            data.get("page_2_extraction", ""))
        out.append(Change(
            on=when, kind=POLICY_CHANGE,
            label=f"{ref} issued",
            detail=(effect if effect and not illegible else
                    "We could not read what this endorsement changed, so we are not "
                    "counting it as closing anything."),
            document_kind="ENDORSEMENT",
            author_class=getattr(doc, "author_class", "INSURER"),
            evidence_class=("UNESTABLISHED" if illegible else "PROVEN"),
            status=NOT_APPLICABLE,
            interest=_interest_of(effect)))
    return sorted(out, key=lambda c: c.on)


def _interest_of(effect: str) -> Optional[str]:
    """Which insured interest an endorsement moved, from its own words."""
    low = (effect or "").lower()
    if "plant" in low or "machinery" in low or "equipment" in low:
        return "plant"
    if "stock" in low or "inventory" in low:
        return "stock"
    if "premises" in low or "location" in low or "unit" in low:
        return "premises"
    if "building" in low:
        return "buildings"
    return None


def _business_changes(facts, store) -> tuple[list[Change], list[str]]:
    """Dated evidence that the business became different."""
    out: list[Change] = []
    gaps: list[str] = []

    # A second unit on the lease. The strongest finding in the corpus, and it has a
    # commencement date, which is what lets it be placed on a timeline at all.
    if store is not None:
        try:
            tenancies = store.all("TENANCY")
        except AttributeError:
            tenancies = []
        for doc in tenancies or []:
            data = getattr(doc, "data", None) or {}
            demised = [str(x).strip() for x in (data.get("demised_premises") or [])]
            commencement = None
            for key, value in data.items():
                if str(key).startswith("commencement") and _as_date(value):
                    commencement = _as_date(value)
                    break
            if commencement and len(demised) > 1:
                out.append(Change(
                    on=commencement, kind=BUSINESS_CHANGE,
                    label="You took on another unit",
                    detail=(f"Your tenancy covers {len(demised)} units: "
                            f"{'; '.join(demised)}."),
                    document_kind="TENANCY",
                    author_class=getattr(doc, "author_class", "THIRD_PARTY"),
                    evidence_class="PROVEN",
                    what_would_close_it=(
                        "An endorsement adding the second unit to your schedule, or "
                        "the endorsement you already hold if we have not seen it."),
                    interest="premises"))

        # Equipment bought. Third-party invoice, so PROVEN, and it carries a date.
        try:
            invoices = store.all("SUPPLIER_INVOICE")
        except AttributeError:
            invoices = []
        for doc in invoices or []:
            data = getattr(doc, "data", None) or {}
            when = _as_date(data.get("invoice_date"))
            amount = data.get("amount_sgd")
            if when is None or amount is None:
                continue
            item = str(data.get("item") or "equipment")
            out.append(Change(
                on=when, kind=BUSINESS_CHANGE,
                label="You bought equipment",
                detail=(f"{item} at {_money(amount)}. New plant raises "
                        f"the value at risk."),
                document_kind="SUPPLIER_INVOICE",
                author_class=getattr(doc, "author_class", "THIRD_PARTY"),
                evidence_class="PROVEN",
                what_would_close_it=(
                    "An endorsement increasing the plant and machinery sum insured."),
                interest="plant"))

    # Stock above the sum insured, dated by the accounts it came from.
    stock_value = _established(facts, "stock_value")
    stock_si = _established(facts, "stock_sum_insured")
    if stock_value is not None and stock_si is not None:
        try:
            over = Decimal(str(stock_value)) > Decimal(str(stock_si))
        except Exception:
            over = False
        if over:
            kind, author = _fact_meta(facts, "stock_value")
            when = _accounts_date(store) or _established(facts, "inception")
            when = _as_date(when)
            if when is not None:
                out.append(Change(
                    on=when, kind=BUSINESS_CHANGE,
                    label="Your stock outgrew its sum insured",
                    detail=(f"Accounts show {_money(stock_value)} of "
                            f"stock against {_money(stock_si)} insured."),
                    document_kind=kind, author_class=author,
                    evidence_class=_GRADES_AS.get(author, "DECLARED"),
                    what_would_close_it=(
                        "An endorsement raising the stock sum insured to what you "
                        "actually carry."),
                    interest="stock"))
    else:
        gaps.append("When your stock last changed — no dated management accounts "
                    "were read.")

    # An online channel appearing on the register. ACRA gives no date for the change,
    # so it is placed at policy inception with that limitation stated rather than
    # invented — a fabricated date would corrupt every day count on the screen.
    secondary = _established(facts, "acra_secondary_activity")
    if isinstance(secondary, str) and secondary:
        inception = _as_date(_established(facts, "inception"))
        if inception is not None:
            kind, author = _fact_meta(facts, "acra_secondary_activity")
            out.append(Change(
                on=inception, kind=BUSINESS_CHANGE,
                label="You added an activity on the register",
                detail=(f'ACRA records a secondary activity: "{secondary}". Your '
                        f"schedule's business description does not mention it. We do "
                        f"not know the date this was filed, so it is shown at policy "
                        f"inception."),
                document_kind=kind, author_class=author,
                evidence_class=_GRADES_AS.get(author, "DECLARED"),
                what_would_close_it=(
                    "A schedule amendment updating your business description, and a "
                    "conversation about whether the new activity needs different "
                    "cover."),
                interest="premises"))

    return sorted(out, key=lambda c: c.on), gaps


def _accounts_date(store) -> Optional[date]:
    """The 'as at' date on the management accounts, if the document states one."""
    if store is None:
        return None
    try:
        docs = store.all("ACCOUNTS")
    except AttributeError:
        return None
    for doc in docs or []:
        data = getattr(doc, "data", None) or {}
        title = str(data.get("title") or "")
        for token in title.replace(",", " ").split():
            when = _as_date(token)
            if when:
                return when
    return None


def _match(business: list[Change], policy: list[Change],
           as_at: date) -> list[Change]:
    """Close a business change if a policy change for the same interest followed it.

    Matching is on interest AND ordering: an endorsement that predates the change it
    would have to cover cannot have closed it. This is what stops the plant endorsement
    from appearing to answer the lease, which would be the flattering reading rather
    than the true one.
    """
    out: list[Change] = []
    for change in business:
        closer = next(
            (p for p in policy
             if p.interest and p.interest == change.interest
             and p.on >= change.on
             and p.evidence_class != "UNESTABLISHED"),
            None)
        if closer is not None:
            out.append(Change(
                **{**change.__dict__,
                   "status": CLOSED,
                   "closed_by": closer.label,
                   "closed_on": closer.on,
                   "days_open": (closer.on - change.on).days,
                   "what_would_close_it": None}))
        else:
            out.append(Change(
                **{**change.__dict__,
                   "status": OPEN,
                   "days_open": max(0, (as_at - change.on).days)}))
    return out


def assess(case: dict, *, as_at: Optional[date] = None) -> dict:
    """Build the timeline and measure how long each gap has been open."""
    facts = (case or {}).get("facts")
    store = (case or {}).get("store")
    report = CompoundingReport()

    policy = _policy_changes(store)
    business, gaps = _business_changes(facts, store)
    report.unestablished = gaps

    # The clock. Prefer the latest date any document evidences, so the screen is
    # reproducible: pinning it to today would make every number change overnight and
    # make the demo unrepeatable.
    dated = [c.on for c in (business + policy)]
    report.as_at = as_at or (max(dated) if dated else None)
    if report.as_at is None:
        report.assert_invariant()
        return {
            "available": False,
            "reason": ("We could not build a timeline: none of the documents we read "
                       "carry dates we could use."),
            "unestablished": report.unestablished,
            "method": "DETERMINISTIC",
        }

    matched = _match(business, policy, report.as_at)
    report.changes = sorted(matched + policy, key=lambda c: c.on)
    report.assert_invariant()

    open_loops = [c for c in matched if c.status == OPEN]
    closed_loops = [c for c in matched if c.status == CLOSED]
    longest = max((c.days_open or 0 for c in open_loops), default=0)

    return {
        "available": True,
        "as_at": report.as_at.isoformat(),
        "as_at_basis": ("The latest date evidenced by your documents, not today's "
                        "date, so this screen reads the same every time you open it."),
        "timeline": [c.as_dict() for c in report.changes],
        "counts": {
            "business_changes": len(matched),
            "policy_changes": len(policy),
            "open": len(open_loops),
            "closed": len(closed_loops),
        },
        "longest_open_days": longest,
        "headline": _headline(len(matched), len(closed_loops), longest),
        "open_loops": [c.as_dict() for c in open_loops],
        "compounding_statement": (
            f"{len(open_loops)} change(s) in your business have not been followed by "
            f"a change in your policy. The oldest has been open for {longest} days. "
            f"Each one on its own is small; they do not cancel out, and a loss "
            f"engages all of them at once."),
        "why_days_not_dollars": (
            "We report days open rather than a running cost. Exposure is not incurred "
            "day by day — it crystallises only if something happens — so a dollar "
            "figure here would be invented. The day count is a fact: it is the "
            "distance between two dates in two of your own documents."),
        "innocent_explanations": [
            "You may hold an endorsement we were not given.",
            "Your adviser may have arranged cover we cannot see.",
            "A change may have been notified verbally and not yet documented.",
        ],
        "not_an_adjudication": (
            "An open loop is a timing observation, not a decision about whether a "
            "claim would be paid. Only your insurer can decide that, and nothing "
            "here says otherwise."),
        "unestablished": report.unestablished,
        "method": "DETERMINISTIC",
    }


def _headline(total: int, closed: int, longest: int) -> str:
    if total == 0:
        return "No dated business changes were found in your documents."
    if closed == total:
        return (f"All {total} change(s) in your business were followed by a policy "
                f"update. That is the pattern you want.")
    return (f"{total} change(s) in your business. {closed} followed by a policy "
            f"update. The oldest gap has been open {longest} days.")
