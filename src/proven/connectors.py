"""Business data connectors — the "real-time" half of the problem statement.

THE HONEST FRAMING, STATED FIRST. There is exactly one connector here and it is a
SIMULATED read-only accounting feed. It is labelled `simulated: true` in every
response, the UI renders that label, and no part of this module claims to hold an
OAuth token or to have contacted an accounting package or a bank.

WHY IT IS STILL WORTH BUILDING. The product's claim is that an SME's exposure moves
when the business moves, while the documents evidencing its cover stay still. Until
now the only way to express that was a button the owner pressed themselves, which
demonstrates nothing — if the owner already knows they bought a freezer, they do not
need software to tell them. A derived signal shows the actual mechanism: a fact
arrives from a system the owner did not type into, the engine re-derives exposure, and
the protection score moves. That is the loop the problem statement describes, and this
is the smallest honest way to show it works end to end.

WHAT A REAL CONNECTOR WOULD ADD, and what it would not. A live accounting integration
would replace `_FEED` with an HTTP call and an OAuth token. It would not change the
architecture below it: the signal still becomes a document-backed fact, still grades
by authorship, and still cannot raise a finding's evidence class on its own. So the
integration is a DOCUMENTED SEAM, not a built feature — and saying that is more
defensible than a fake OAuth screen. (An earlier build carried a live connector to one
vendor's API; it was removed rather than shipped half-supported, and the known-risk list
in SECURITY.md records the gap.)

AUTHORSHIP, for whoever builds the seam. An operand is graded by WHO AUTHORED IT, not by
whose API served it. A business that keys its own stock count into an accounting package
has authored that number as surely as if it typed it into a spreadsheet, so a stock or
headcount figure read from the business's own books is SELF; only a document somebody
else issued — a supplier's bill — is THIRD_PARTY. Serving a figure over somebody else's
API does not launder it.

EVIDENCE DISCIPLINE. A connector signal is THIRD_PARTY authored (an accounting system
is not the owner's own assertion), but it is not a document, so it cannot by itself
produce a PROVEN finding. It raises the question and names the document that would
answer it. That boundary is the whole reason the grading scheme survives contact with
live data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

SIMULATED_NOTICE = (
    "SIMULATED read-only feed. This build does not connect to a live accounting "
    "system. The data below is fixed demonstration data, and it is labelled as such "
    "everywhere it appears."
)


@dataclass(frozen=True)
class Signal:
    """One derived business-change signal."""

    id: str
    connector: str
    detected_on: str                 # ISO date
    kind: str                        # ASSET_ACQUIRED | HEADCOUNT | REVENUE_CHANNEL ...
    label: str
    amount_sgd: Optional[Decimal]
    implies_event: Optional[str]     # a change-event code from events.py
    affects: tuple[str, ...]         # fact names whose value this moves
    asks_for: tuple[str, ...]        # document kinds that would evidence it
    author_class: str = "THIRD_PARTY"
    # Set on minuted decisions only. `lead_days` is how far ahead of the corresponding
    # transactional signal the intention was recorded; `anticipates` names that signal.
    # Both exist so the interface can show the value of early warning without letting a
    # note pass for evidence of the thing it anticipated.
    lead_days: Optional[int] = None
    anticipates: Optional[str] = None

    @property
    def grades_as(self) -> str:
        """The strongest evidence class a finding resting on this signal could reach.

        Never PROVEN either way: a signal is not a document. A third-party feed is worth
        more than the owner's own minutes, and neither is worth a citation.
        """
        return "DECLARED" if self.author_class == "SELF" else "THIRD_PARTY"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "connector": self.connector,
            "detected_on": self.detected_on,
            "kind": self.kind,
            "label": self.label,
            "amount_sgd": (format(self.amount_sgd, "f")
                           if self.amount_sgd is not None else None),
            "implies_event": self.implies_event,
            "affects": list(self.affects),
            "asks_for": list(self.asks_for),
            "author_class": self.author_class,
            "grades_as": self.grades_as,
            "source_type": "MINUTED_DECISION" if self.author_class == "SELF"
                           else "TRANSACTIONAL",
            "lead_days": self.lead_days,
            "anticipates": self.anticipates,
            "simulated": True,
        }


# The demonstration feed. Ordered chronologically. Chosen to line up with the Hock Lee
# persona so the connector and the fixtures tell one story rather than two.
_FEED: tuple[Signal, ...] = (
    Signal(
        id="SIG-001", connector="Accounting feed (simulated)",
        detected_on="2026-03-14", kind="ASSET_ACQUIRED",
        label="Blast freezer, 8-tray — capital purchase posted to fixed assets",
        amount_sgd=Decimal("62000"), implies_event="BOUGHT_COSTLY_EQUIPMENT",
        affects=("plant_value_at_risk",),
        asks_for=("SUPPLIER_INVOICE", "ASSET_REGISTER"),
    ),
    Signal(
        id="SIG-002", connector="Accounting feed (simulated)",
        detected_on="2026-04-02", kind="HEADCOUNT",
        label="Payroll headcount rose from 11 to 14",
        amount_sgd=None, implies_event="HEADCOUNT_CHANGE",
        affects=("contributor_count",),
        asks_for=("CPF_RECORD", "WIC_SCHEDULE"),
    ),
    Signal(
        id="SIG-003", connector="Accounting feed (simulated)",
        detected_on="2026-05-20", kind="INVENTORY",
        label="Closing inventory rose to S$150,000 (from S$96,000)",
        amount_sgd=Decimal("150000"), implies_event="BOUGHT_COSTLY_EQUIPMENT",
        affects=("stock_value",),
        asks_for=("ACCOUNTS",),
    ),
    Signal(
        id="SIG-004", connector="Accounting feed (simulated)",
        detected_on="2026-06-01", kind="PREMISES",
        label="New rent line: Unit 03-12, 21 Tai Seng Avenue",
        amount_sgd=None, implies_event="OPENED_OR_LEASED_LOCATION",
        affects=("scheduled_premises",),
        asks_for=("TENANCY", "SCHEDULE", "ENDORSEMENT"),
    ),
    Signal(
        id="SIG-005", connector="Accounting feed (simulated)",
        detected_on="2026-06-18", kind="REVENUE_CHANNEL",
        label="Online payment settlements began (new merchant account)",
        amount_sgd=None, implies_event="STARTED_SELLING_ONLINE",
        affects=("business_description",),
        asks_for=("ACRA_PROFILE", "SCHEDULE"),
    ),
)

# ── the second source: minuted decisions ─────────────────────────────────────
# PORTED from the parallel VERIS build, where it was the best idea in that tree.
#
# WHY IT IS WORTH A SEPARATE SOURCE. Every signal above is a consequence: the freezer is
# already bought, the rent line is already posted, the settlements have already started.
# By the time an accounting system knows, the exposure has existed for weeks. A minuted
# decision is the same change observed EARLIER — "lease signed, handover June 1" precedes
# the first rent line by a month, and that month is the whole value of the product.
#
# THE EVIDENCE CEILING, which is stricter than the accounting feed's. Minutes are authored
# by the business about itself, so they grade DECLARED, not THIRD_PARTY. A resolution to
# do something is also not proof it was done. So a note may raise a question and name the
# document that would settle it, and it may never move a figure. `lead_days` is carried so
# the interface can show what the early warning was worth without implying the note is
# evidence of the outcome.
_NOTES: tuple[Signal, ...] = (
    Signal(
        id="NOTE-001", connector="Meeting notes (simulated)",
        detected_on="2026-05-10", kind="PREMISES",
        label=("Management meeting: approved taking Unit 03-12 for additional cold "
               "storage. Lease signed, handover 1 June."),
        amount_sgd=None, implies_event="OPENED_OR_LEASED_LOCATION",
        affects=("scheduled_premises",),
        asks_for=("TENANCY", "SCHEDULE"),
        author_class="SELF", lead_days=22, anticipates="SIG-004",
    ),
    Signal(
        id="NOTE-002", connector="Meeting notes (simulated)",
        detected_on="2026-05-02", kind="ASSET_ACQUIRED",
        label=("Capex approved: blast freezer for the new cold room, quote accepted "
               "at S$62,000."),
        amount_sgd=None, implies_event="BOUGHT_COSTLY_EQUIPMENT",
        affects=("plant_value_at_risk",),
        asks_for=("SUPPLIER_INVOICE",),
        author_class="SELF", lead_days=41, anticipates="SIG-001",
    ),
    Signal(
        id="NOTE-003", connector="Meeting notes (simulated)",
        detected_on="2026-06-04", kind="REVENUE_CHANNEL",
        label=("Sales review: online storefront to go live mid-June; card settlements "
               "via new merchant account."),
        amount_sgd=None, implies_event="STARTED_SELLING_ONLINE",
        affects=("business_description",),
        asks_for=("ACRA_PROFILE",),
        author_class="SELF", lead_days=14, anticipates="SIG-005",
    ),
)

ALL_SIGNALS: tuple[Signal, ...] = _FEED + _NOTES

BY_ID: dict[str, Signal] = {s.id: s for s in ALL_SIGNALS}


def catalogue() -> dict:
    """The connector list for the UI. One connected, two named as unavailable.

    The unavailable ones are listed rather than hidden so the product is explicit
    about the boundary of what it does, instead of implying a full integration suite
    behind a single working demo.
    """
    return {
        "connectors": [
            {
                "id": "accounting-demo",
                "name": "Accounting feed",
                "status": "CONNECTED",
                "simulated": True,
                "detail": "Fixed demonstration data. Not a live integration.",
                "signal_count": len(_FEED),
            },
            {
                "id": "meeting-notes-demo",
                "name": "Meeting notes",
                "status": "CONNECTED",
                "simulated": True,
                "detail": ("Minuted decisions. Owner-authored, so signals from here grade "
                           "DECLARED and can never move a figure — but they arrive weeks "
                           "before the transaction does."),
                "signal_count": len(_NOTES),
            },
            {
                "id": "pos",
                "name": "Point of sale",
                "status": "NOT_CONNECTED",
                "simulated": True,
                "detail": "Not implemented in this build.",
                "signal_count": 0,
            },
            {
                "id": "payroll",
                "name": "Payroll",
                "status": "NOT_CONNECTED",
                "simulated": True,
                "detail": "Not implemented in this build.",
                "signal_count": 0,
            },
        ],
        "notice": SIMULATED_NOTICE,
    }


def signals(since: Optional[str] = None, include_notes: bool = True) -> list[dict]:
    """Every signal, optionally those on or after an ISO date.

    `include_notes=False` returns transactional signals only, which is what a caller that
    wants to treat feeds and minutes differently should ask for.
    """
    items = list(ALL_SIGNALS if include_notes else _FEED)
    items.sort(key=lambda s: s.detected_on)
    if since:
        try:
            cutoff = date.fromisoformat(since)
            items = [s for s in items
                     if date.fromisoformat(s.detected_on) >= cutoff]
        except ValueError:
            pass
    return [s.as_dict() for s in items]


#: What a real accounting integration would have to supply, stated so the seam is a
#: specification rather than a gap. Served by the connected-systems screen.
ACCOUNTING_SEAM = {
    "status": "DOCUMENTED_SEAM",
    "built": False,
    "summary": ("A live accounting integration is a documented seam, not a built "
                "feature. The feed on this screen is fixed demonstration data."),
    "would_replace": "the fixed demonstration feed (`_FEED` in connectors.py)",
    "would_not_change": [
        "a signal raises a question and names the document that would settle it",
        "a signal never establishes a value or raises a finding's evidence class",
        "a figure is graded by who authored it, not by whose API served it",
    ],
    "rule": ("`simulated` may be false only on a row that came back from a successful "
             "call to the live system, never because a connection is configured."),
}


def signal(signal_id: str) -> Optional[Signal]:
    return BY_ID.get(signal_id)


def derived_events(signal_ids: list[str]) -> list[str]:
    """Change-event codes implied by these signals.

    This is the point of the connector: the owner did not tap the event, the feed
    implied it. The event still carries no evidential weight of its own — it selects
    which findings are relevant and names the documents that would settle them.
    """
    out: list[str] = []
    for sid in signal_ids or []:
        sig = BY_ID.get(sid)
        if sig and sig.implies_event and sig.implies_event not in out:
            out.append(sig.implies_event)
    return out


def requested_documents(signal_ids: list[str]) -> list[str]:
    out: list[str] = []
    for sid in signal_ids or []:
        sig = BY_ID.get(sid)
        if not sig:
            continue
        for kind in sig.asks_for:
            if kind not in out:
                out.append(kind)
    return out


def timeline() -> dict:
    """The business-change timeline: connector signals plus what each one asks for.

    Ordered by date, so the minuted decision appears BEFORE the transaction it anticipated.
    That ordering is the argument for reading minutes at all.

    Every row is the labelled fixture. `live` is kept in the envelope, reporting nothing
    live, so a screen that reads it says so in the same words it would use if a live
    integration were ever built behind the seam.
    """
    rows = sorted(ALL_SIGNALS, key=lambda s: s.detected_on)
    lead = [s for s in rows if s.lead_days]
    fixture_events = [
        {
            **sig.as_dict(),
            "asks_for_labels": [k.replace("_", " ").title() for k in sig.asks_for],
        }
        for sig in rows
    ]
    return {
        "events": fixture_events,
        "live": {
            "any_live": False,
            "live_count": 0,
            "fixture_count": len(fixture_events),
            "mode": "FIXTURE",
        },
        "accounting_integration": ACCOUNTING_SEAM,
        "earliest_warning_days": max((s.lead_days or 0) for s in rows) if lead else 0,
        "notice": SIMULATED_NOTICE,
        "explanation": (
            "Each row is a business change derived from a data feed rather than typed "
            "in by the owner. A signal raises a question and names the document that "
            "would answer it. It never establishes a value on its own — an accounting "
            "line is not a policy document, so it cannot make a finding proven."
        ),
        "two_sources": (
            "Minuted decisions are owner-authored and grade DECLARED, so they can never "
            "move a figure. They matter because they arrive first: the lease was minuted "
            "on 10 May and the rent line posted on 1 June, so the premises question was "
            "answerable 22 days before any transaction revealed it."
        ),
    }
