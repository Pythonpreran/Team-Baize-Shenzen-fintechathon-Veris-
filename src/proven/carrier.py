"""The carrier workspace: an underwriter's questions, not a dashboard (spec §18).

WHAT WAS WRONG WITH THE CARRIER VIEW.

Five read-only screens over two endpoints. An underwriter could look at a portfolio and
could not do anything with it — no way to open an account, ask a question, request a
document, or record a decision. It was a slide, not a workspace.

THE BOUNDARY PROBLEM, AND HOW IT IS RESOLVED HERE.

The task brief asks for "case detail — findings, evidence, document trace". The
architecture says a carrier never sees one business's documents, and the AI/retrieval
routes were just locked down to enforce exactly that. Those two requirements look
incompatible, and the naive fix — a privileged carrier read path — would undo the
security fix and the product's central claim with it.

They are reconciled by noticing that "carrier" and "no access" were never the same
statement. What a carrier may not do is read a case NOBODY DISCLOSED TO THEM. So:

  * DEMO PORTFOLIO ACCOUNTS  synthetic rows with no documents behind them. Detail here
                             is synthetic too, labelled, and carries no citations —
                             because inventing a page reference in a product about
                             citation fidelity is the one unforgivable move.
  * THE LIVE CASE            visible only when the owner granted a disclosure NAMING
                             the carrier. Same gate, same `pipeline.granted_fields`,
                             same audit row. Consent is the access path for a carrier
                             exactly as it is for an adviser.

So the carrier gets a real workspace and the boundary gets stronger rather than weaker:
consent now governs three roles instead of two.

WHAT THIS MODULE STILL REFUSES.

  * No premium figures. `actions.py` forbids them structurally and this module holds to
    the same rule — an underwriting workspace that quotes is a rating engine, and this
    is not one.
  * No SME risk score used for underwriting. The Protection Pulse score exists to tell
    an owner where they stand. Repurposing it as an acceptance signal would turn a
    diagnostic the owner is meant to trust into a thing used against them.
  * No decision. The states below record what a human decided; nothing computes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

# ── decision vocabulary (A4) ────────────────────────────────────────────────
#
# Closed, and deliberately short of anything adjudicating. An underwriter records that
# they looked, and what they concluded about the ACCOUNT — never a verdict on a claim.
REVIEWED = "REVIEWED"
ACCEPTED_AS_IS = "ACCEPTED_AS_IS"
NEEDS_ENDORSEMENT = "NEEDS_ENDORSEMENT"
INFORMATION_REQUESTED = "INFORMATION_REQUESTED"
REFERRED_TO_ADVISER = "REFERRED_TO_ADVISER"

DECISIONS: tuple[str, ...] = (REVIEWED, ACCEPTED_AS_IS, NEEDS_ENDORSEMENT,
                              INFORMATION_REQUESTED, REFERRED_TO_ADVISER)

DECISION_LABELS: dict[str, str] = {
    REVIEWED: "Reviewed, no action",
    ACCEPTED_AS_IS: "Accepted as it stands",
    NEEDS_ENDORSEMENT: "Needs an endorsement",
    INFORMATION_REQUESTED: "Information requested",
    REFERRED_TO_ADVISER: "Referred to the adviser",
}

# Language an underwriter's note may not contain. The same rule the rest of the engine
# obeys: this product does not adjudicate, and a free-text field is exactly where that
# discipline would otherwise leak.
_FORBIDDEN_IN_NOTE: tuple[str, ...] = (
    "claim will fail", "claim would fail", "not covered", "no cover",
    "void", "repudiate", "decline the claim", "breach of warranty",
)

MAX_NOTE = 600


@dataclass(frozen=True)
class Decision:
    """One append-only record that a named human looked at an account and concluded."""

    id: str
    account_id: str
    decision: str
    note: str
    by: str
    organisation: str
    at: str

    def as_dict(self) -> dict:
        return {"id": self.id, "account_id": self.account_id,
                "decision": self.decision,
                "decision_label": DECISION_LABELS.get(self.decision, self.decision),
                "note": self.note, "by": self.by,
                "organisation": self.organisation, "at": self.at}


class DecisionLog:
    """Append-only, in memory, bounded.

    Append-only is the point: an underwriting file that can be edited after the fact is
    not a record, and the value of this surface is that it feeds `/api/record` as
    evidence somebody looked. There is deliberately no update or delete.

    In memory because the carrier portfolio itself is synthetic demonstration data — a
    durable table for decisions about fictional accounts would imply a persistence
    guarantee the rest of that screen does not have. The SME-side equivalent,
    `actions.py`, IS durable, because those decisions are about a real case.
    """

    MAX = 500

    def __init__(self) -> None:
        self._rows: list[Decision] = []
        self._seq = 0

    def add(self, *, account_id: str, decision: str, note: str, by: str,
            organisation: str) -> Decision:
        if decision not in DECISIONS:
            raise ValueError(f"Unknown decision {decision!r}")
        clean = " ".join(str(note or "").split())[:MAX_NOTE]
        low = clean.lower()
        for bad in _FORBIDDEN_IN_NOTE:
            if bad in low:
                raise ValueError(
                    f"An underwriting note may not state a claim outcome "
                    f"({bad!r}). Record what you observed and what you decided "
                    f"about the account.")
        self._seq += 1
        row = Decision(
            id=f"dec-{self._seq:05d}", account_id=str(account_id),
            decision=decision, note=clean, by=by, organisation=organisation,
            at=datetime.now(timezone.utc).isoformat())
        self._rows.append(row)
        if len(self._rows) > self.MAX:
            del self._rows[0]
        return row

    def for_account(self, account_id: str) -> list[dict]:
        return [r.as_dict() for r in self._rows
                if r.account_id == str(account_id)]

    def all(self, limit: int = 100) -> list[dict]:
        return [r.as_dict() for r in reversed(self._rows[-limit:])]

    def count(self) -> int:
        return len(self._rows)

    def latest_for(self, account_id: str) -> Optional[dict]:
        rows = self.for_account(account_id)
        return rows[-1] if rows else None

    def clear(self) -> None:
        self._rows.clear()
        self._seq = 0


LOG = DecisionLog()


# ── renewal triage (B4) ─────────────────────────────────────────────────────
#
# Deterministic ranking. The model, where one is used at all, writes the one-line reason
# AFTER this has decided the order — it never influences the order, because a queue an
# underwriter works top-down is a consequential decision.

# What raises attention, and by how much. Declared as a table so the ranking can be
# argued with rather than reverse-engineered from control flow. These are product
# judgements, not measurements, and the payload says so.
_TRIAGE_WEIGHTS: dict[str, int] = {
    "priority_high": 40,
    "priority_medium": 15,
    "internal_change_high": 25,
    "external_risk_high": 20,
    "external_risk_med": 8,
    "unestablished_each": 5,
    "no_adviser": 12,
}
_UNESTABLISHED_CAP = 20


def triage(accounts: list[dict]) -> dict:
    """Rank the book by attention, from fields the carrier already owns.

    Every input is a book-level field on the carrier's own account row — priority,
    internal_change, external_risk, unestablished_count, adviser. None of it is read
    from an SME's documents, so this ranking needs no consent and crosses no boundary.
    """
    ranked: list[dict] = []
    for a in accounts or []:
        score = 0
        why: list[str] = []
        priority = str(a.get("priority") or "").upper()
        if priority == "HIGH":
            score += _TRIAGE_WEIGHTS["priority_high"]
            why.append("flagged high priority on your book")
        elif priority == "MEDIUM":
            score += _TRIAGE_WEIGHTS["priority_medium"]
            why.append("flagged medium priority")
        if str(a.get("internal_change") or "").upper() == "HIGH":
            score += _TRIAGE_WEIGHTS["internal_change_high"]
            why.append("the business changed materially since it was last looked at")
        ext = str(a.get("external_risk") or "").upper()
        if ext == "HIGH":
            score += _TRIAGE_WEIGHTS["external_risk_high"]
            why.append("conditions around the premises worsened")
        elif ext in ("MED", "MEDIUM", "ELEVATED"):
            score += _TRIAGE_WEIGHTS["external_risk_med"]
            why.append("conditions around the premises are elevated")
        unestablished = int(a.get("unestablished_count") or 0)
        if unestablished:
            score += min(unestablished * _TRIAGE_WEIGHTS["unestablished_each"],
                         _UNESTABLISHED_CAP)
            why.append(f"{unestablished} item(s) still unevidenced")
        if not a.get("adviser"):
            score += _TRIAGE_WEIGHTS["no_adviser"]
            why.append("no adviser is assigned")

        ranked.append({
            "account_id": a.get("account_id"),
            "business_name": a.get("business_name"),
            "industry": a.get("industry"),
            "attention": score,
            "reasons": why,
            "reason_line": _reason_line(why),
            "last_reviewed": a.get("last_reviewed"),
            "state": a.get("state"),
            "adviser": a.get("adviser"),
            "decision": (LOG.latest_for(str(a.get("account_id"))) or {}).get(
                "decision_label"),
        })

    # Sort by attention, then by name so equal scores have a stable, explicable order
    # rather than whatever order the source list happened to be in.
    ranked.sort(key=lambda r: (-r["attention"], str(r["business_name"] or "")))
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i

    return {
        "ranked": ranked,
        "count": len(ranked),
        "weights": dict(_TRIAGE_WEIGHTS),
        "method": "DETERMINISTIC",
        "basis": ("Ranked from book-level fields the carrier already holds — priority, "
                  "internal change, external risk, unevidenced items and whether an "
                  "adviser is assigned. No SME document is read and no model "
                  "influences the order."),
        "weights_are_judgements": (
            "The weights above are product judgements, not measurements. They are "
            "declared in one table so they can be argued with rather than inferred."),
        "not_a_rating": (
            "This is a work queue, not a rating or an acceptance signal. It carries no "
            "premium and must not be used as one."),
    }


def _reason_line(why: list[str]) -> str:
    if not why:
        return "Nothing on your book flags this account."
    if len(why) == 1:
        return "Because " + why[0] + "."
    return "Because " + ", ".join(why[:-1]) + ", and " + why[-1] + "."


# ── systemic patterns (B5) ──────────────────────────────────────────────────
#
# Rules find the pattern; prose describes it. A "pattern" here is a plain count over the
# book with a threshold, because that is what can be defended when an underwriter asks
# how the number was reached.

_PATTERN_MIN_SHARE = 0.30      # a third of the book before it is called a pattern
_PATTERN_MIN_COUNT = 2


def patterns(accounts: list[dict]) -> dict:
    """Concentrations across the book, counted rather than inferred."""
    rows = list(accounts or [])
    total = len(rows)
    found: list[dict] = []

    def add(kind: str, label: str, matched: list[dict], reading: str) -> None:
        if len(matched) < _PATTERN_MIN_COUNT:
            return
        share = len(matched) / total if total else 0
        if share < _PATTERN_MIN_SHARE:
            return
        found.append({
            "kind": kind, "label": label,
            "count": len(matched), "of": total,
            "share_pct": round(share * 100),
            "accounts": [m.get("business_name") for m in matched],
            "reading": reading,
        })

    by_reason: dict[str, list[dict]] = {}
    for a in rows:
        by_reason.setdefault(str(a.get("lead_reason") or "Unspecified"), []).append(a)
    for reason, matched in sorted(by_reason.items(),
                                 key=lambda kv: -len(kv[1])):
        add("LEAD_REASON", f"{reason}", matched,
            f"{len(matched)} of {total} accounts surfaced for the same reason. That is "
            f"a wording or an appetite question, not {len(matched)} separate cases.")

    by_industry: dict[str, list[dict]] = {}
    for a in rows:
        by_industry.setdefault(str(a.get("industry") or "Unspecified"), []).append(a)
    for industry, matched in sorted(by_industry.items(),
                                   key=lambda kv: -len(kv[1])):
        add("INDUSTRY", f"{industry}", matched,
            f"{len(matched)} of {total} accounts sit in one industry. A peril that "
            f"lands on that trade lands on all of them at once.")

    unassigned = [a for a in rows if not a.get("adviser")]
    add("NO_ADVISER", "No adviser assigned", unassigned,
        f"{len(unassigned)} of {total} accounts have nobody assigned, so nothing "
        f"routes the findings to a person who can act on them.")

    return {
        "patterns": found,
        "count": len(found),
        "thresholds": {"minimum_share_pct": int(_PATTERN_MIN_SHARE * 100),
                       "minimum_count": _PATTERN_MIN_COUNT},
        "method": "DETERMINISTIC",
        "basis": ("Each pattern is a count over the book against a stated threshold. "
                  "Nothing is inferred and nothing is predicted."),
        "notice": ("Book-level only. No individual account's documents were read to "
                   "produce this."),
    }


# ── undisclosed facts inbox (A2) ────────────────────────────────────────────

def undisclosed_inbox(accounts: list[dict]) -> dict:
    """What the book says needs an underwriter's attention, and what it may not say.

    Reuses the existing shape of a change feed rather than adding a second
    change-detection engine: the rows are derived from the carrier's own account fields,
    and the honest limitation — that a carrier cannot see WHAT changed inside a business
    without a disclosure — is stated rather than papered over with invented detail.
    """
    items: list[dict] = []
    for a in accounts or []:
        if str(a.get("internal_change") or "").upper() == "HIGH":
            items.append({
                "account_id": a.get("account_id"),
                "business_name": a.get("business_name"),
                "kind": "INTERNAL_CHANGE",
                "headline": "This business changed materially since its last review",
                "what_we_can_say": (
                    "Your own book flags a high internal change on this account."),
                "what_we_cannot_say": (
                    "What changed. That sits in the business's documents, and reading "
                    "them needs a disclosure naming you."),
                "next_step": "Request a document, or refer it to the assigned adviser.",
                "adviser": a.get("adviser"),
            })
        if int(a.get("unestablished_count") or 0) >= 2:
            items.append({
                "account_id": a.get("account_id"),
                "business_name": a.get("business_name"),
                "kind": "UNEVIDENCED",
                "headline": (f"{a.get('unestablished_count')} items on this account "
                             f"have no document behind them"),
                "what_we_can_say": (
                    "The count is on your book. Unevidenced is not the same as wrong — "
                    "it means nobody has established it either way."),
                "what_we_cannot_say": (
                    "Which items, or what they would show. That needs a disclosure."),
                "next_step": "Request the specific document from the business.",
                "adviser": a.get("adviser"),
            })
    return {
        "items": items,
        "count": len(items),
        "method": "DETERMINISTIC",
        "notice": ("Every row separates what your book evidences from what it cannot. "
                   "A carrier sees counts and flags; the documents behind them require "
                   "the business to disclose them to you by name."),
    }


def catalogue() -> dict:
    """The decision vocabulary, for the UI and the docs."""
    return {
        "decisions": [{"decision": d, "label": DECISION_LABELS[d]} for d in DECISIONS],
        "note_limit": MAX_NOTE,
        "append_only": True,
        "forbidden_note_language": list(_FORBIDDEN_IN_NOTE),
        "notice": ("Decisions are append-only. An underwriting file that can be edited "
                   "after the fact is not a record."),
        "method": "DETERMINISTIC",
    }
