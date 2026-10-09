"""THE ACTION LOOP — the half of the product that changes something.

WHY THIS MODULE EXISTS. Everything upstream of here detects. `connectors.py` sees the
business move, `sentinel.py` sees the environment move, `align.py` and `rules.py` prove
what the policy would pay, `orchestrator.py` decides what deserves attention. And then,
until this module, nothing happened. The owner received a very well evidenced document
about a S$50,000 shortfall and the shortfall stayed exactly where it was.

That is the gap the team's EUREKA note called "fix it before step 5", and it is the
objection the category loses on: detection is not the bottleneck. An SME that knows it is
under-insured and does nothing is in the same position as one that never knew, except
crosser. So a finding has to be able to become a request, a request has to be able to
reach a human who can price it, and the system has to be able to say afterwards how much
exposure actually closed.

WHAT AN ACTION IS. A durable record that some specific mismatch should be corrected, with
the evidence that justified it attached, moving through states a human drives. It is NOT
an instruction to buy anything, and it is not a decision that cover is needed — those are
a licensed intermediary's calls, and the state machine below is deliberately shaped so
that the system can prepare and route a request but only a human can advance it.

THE RULE ABOUT MONEY, and it is the whole reason this module can be trusted. An action
carries the amount of exposure it would close, because that is arithmetic over figures
already evidenced elsewhere: a documented limit subtracted from a documented value. It
carries NO premium. Not an estimate, not a range, not "typically around". Pricing a risk
requires a rating engine, an appetite, and an underwriter, and this system has none of the
three. `premium_basis` is therefore a permanent `NOT_QUOTED` with a stated reason, and
`test_actions.py` fails the build if any premium-shaped field acquires a number.

Inventing an indicative premium would be the single fastest way to destroy the credibility
that verbatim citation and abstention were built to earn.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

# ── what kind of correction is being asked for ───────────────────────────────
# A closed vocabulary. Each one names a change to the CONTRACT, because that is the only
# thing that closes a protection gap — a report does not.

LIMIT_INCREASE = "LIMIT_INCREASE_REQUEST"
SCHEDULE_AMENDMENT = "SCHEDULE_AMENDMENT_REQUEST"
DECLARATION_UPDATE = "DECLARATION_UPDATE_REQUEST"
COVER_CONFIRMATION = "COVER_CONFIRMATION_REQUEST"
DOCUMENT_REQUEST = "DOCUMENT_REQUEST"

ACTION_TYPES: tuple[str, ...] = (LIMIT_INCREASE, SCHEDULE_AMENDMENT, DECLARATION_UPDATE,
                                 COVER_CONFIRMATION, DOCUMENT_REQUEST)

ACTION_LABELS: dict[str, str] = {
    LIMIT_INCREASE: "Request a limit increase",
    SCHEDULE_AMENDMENT: "Request a schedule amendment",
    DECLARATION_UPDATE: "Update a declaration",
    COVER_CONFIRMATION: "Ask whether cover exists",
    DOCUMENT_REQUEST: "Provide a document",
}

# What each type is asking the adviser to actually do. Written for the adviser, not for us.
ACTION_ASKS: dict[str, str] = {
    LIMIT_INCREASE: ("Obtain terms for an increased sum insured on the interest named "
                     "below, or advise that the current limit is appropriate."),
    SCHEDULE_AMENDMENT: ("Add or amend the scheduled item named below, or advise that no "
                         "amendment is required."),
    DECLARATION_UPDATE: ("Submit the corrected declaration to the insurer, or advise that "
                         "the existing declaration remains accurate."),
    COVER_CONFIRMATION: ("Confirm whether the activity named below is covered under the "
                         "current contract, and on what terms."),
    DOCUMENT_REQUEST: ("Obtain the document named below from the client so the position "
                       "can be established rather than estimated."),
}

# ── the state machine ────────────────────────────────────────────────────────
# States are things a PERSON does, with one exception (DRAFTED, which the engine does).
# That asymmetry is the point: the system can prepare a request and it can carry it, but
# it cannot advance it, price it, or close it.

DRAFTED = "DRAFTED"                                # engine prepared it
SENT_TO_ADVISER = "SENT_TO_ADVISER"                # owner released it (consent gated)
ADVISER_REVIEWING = "ADVISER_REVIEWING"            # adviser picked it up
INFORMATION_REQUESTED = "INFORMATION_REQUESTED"    # adviser needs something first
QUOTE_RECEIVED = "QUOTE_RECEIVED"                  # terms came back from the market
BOUND = "BOUND"                                    # the contract actually changed
DECLINED = "DECLINED"                              # market or client said no
WITHDRAWN = "WITHDRAWN"                            # owner pulled it
NOT_REQUIRED = "NOT_REQUIRED"                      # adviser judged it unnecessary

STATES: tuple[str, ...] = (DRAFTED, SENT_TO_ADVISER, ADVISER_REVIEWING,
                           INFORMATION_REQUESTED, QUOTE_RECEIVED, BOUND, DECLINED,
                           WITHDRAWN, NOT_REQUIRED)

TERMINAL: frozenset[str] = frozenset({BOUND, DECLINED, WITHDRAWN, NOT_REQUIRED})

# Only BOUND closes exposure. DECLINED and NOT_REQUIRED are legitimate endings, and
# counting them as closed would let the product report success for having been ignored.
CLOSES_EXPOSURE: frozenset[str] = frozenset({BOUND})

# Who may make each move. The engine appears exactly once.
TRANSITIONS: dict[str, tuple[tuple[str, str], ...]] = {
    DRAFTED: ((SENT_TO_ADVISER, "owner"), (WITHDRAWN, "owner")),
    SENT_TO_ADVISER: ((ADVISER_REVIEWING, "adviser"), (WITHDRAWN, "owner")),
    ADVISER_REVIEWING: ((INFORMATION_REQUESTED, "adviser"), (QUOTE_RECEIVED, "adviser"),
                        (NOT_REQUIRED, "adviser"), (DECLINED, "adviser"),
                        (WITHDRAWN, "owner")),
    INFORMATION_REQUESTED: ((ADVISER_REVIEWING, "adviser"), (WITHDRAWN, "owner")),
    QUOTE_RECEIVED: ((BOUND, "adviser"), (DECLINED, "adviser"), (WITHDRAWN, "owner")),
    BOUND: (),
    DECLINED: (),
    WITHDRAWN: (),
    NOT_REQUIRED: (),
}

STATE_LABELS: dict[str, str] = {
    DRAFTED: "Prepared — not yet sent",
    SENT_TO_ADVISER: "Sent to your adviser",
    ADVISER_REVIEWING: "Adviser is reviewing",
    INFORMATION_REQUESTED: "Adviser needs more information",
    QUOTE_RECEIVED: "Terms received",
    BOUND: "Cover amended",
    DECLINED: "Not taken up",
    WITHDRAWN: "Withdrawn",
    NOT_REQUIRED: "Adviser advised no change needed",
}

# The permanent answer on pricing.
NOT_QUOTED = "NOT_QUOTED"
PREMIUM_NOTICE = (
    "No premium is shown. Pricing a risk requires a rating engine, an underwriting "
    "appetite and an authorised underwriter, and this system has none of them. The "
    "request carries the exposure it would close and the evidence for it; the carrier or "
    "broker prices it."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── the record ───────────────────────────────────────────────────────────────

@dataclass
class Action:
    """One requested correction, and the evidence that justified asking."""

    id: str
    case_id: str
    action_type: str
    interest: str                       # stock | plant | premises | workforce | ...
    title: str
    state: str = DRAFTED
    # The arithmetic. Both operands come from figures already evidenced upstream; this
    # module performs a subtraction and nothing else.
    documented_value: Optional[str] = None    # Decimal as str — what the policy records
    observed_value: Optional[str] = None      # Decimal as str — what the evidence shows
    exposure_sgd: Optional[str] = None        # Decimal as str — the difference
    exposure_basis: str = ""                  # how the difference was arrived at
    # Interests measured in people, not dollars. Kept separate so a headcount gap is
    # visible and actionable without being converted into a money figure it cannot support.
    count_documented: Optional[int] = None
    count_observed: Optional[int] = None
    count_basis: str = ""
    # Provenance. An action with no evidence should not be raisable.
    source_rule_id: Optional[str] = None
    source_decision: Optional[str] = None
    evidence_class: Optional[str] = None
    policy_refs: list[dict] = field(default_factory=list)
    signal_refs: list[dict] = field(default_factory=list)
    documents_requested: list[str] = field(default_factory=list)
    # Pricing, permanently absent.
    premium_basis: str = NOT_QUOTED
    # Lifecycle.
    history: list[dict] = field(default_factory=list)
    adviser_note: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # ── invariants ──────────────────────────────────────────────────────────
    def assert_invariant(self) -> None:
        """Refuse to exist in a state that would mislead.

        Every one of these was a real way to get this wrong:
          * an action with no evidence is an opinion with a button
          * an exposure figure with no basis is a number nobody can check
          * a closed exposure on a non-BOUND action reports success for inaction
          * a premium is not ours to state
        """
        if self.action_type not in ACTION_TYPES:
            raise ValueError(f"unknown action_type {self.action_type!r}")
        if self.state not in STATES:
            raise ValueError(f"unknown state {self.state!r}")
        if self.premium_basis != NOT_QUOTED:
            raise ValueError("premium_basis must remain NOT_QUOTED")
        if not (self.policy_refs or self.signal_refs or self.documents_requested):
            raise ValueError(
                "an action must carry evidence: a policy reference, a signal, or a named "
                "document. Raising one from nothing is how a report becomes a guess.")
        if self.exposure_sgd is not None and not self.exposure_basis:
            raise ValueError("an exposure figure without a stated basis is unusable")
        if self.exposure_sgd is not None:
            if Decimal(self.exposure_sgd) < 0:
                raise ValueError("exposure cannot be negative")

    @property
    def closes_exposure(self) -> bool:
        return self.state in CLOSES_EXPOSURE and self.exposure_sgd is not None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def allowed_moves(self) -> list[dict]:
        return [{"to": to, "by": by, "label": STATE_LABELS[to]}
                for to, by in TRANSITIONS[self.state]]

    def as_dict(self) -> dict:
        self.assert_invariant()
        return {
            "id": self.id,
            "case_id": self.case_id,
            "action_type": self.action_type,
            "action_label": ACTION_LABELS[self.action_type],
            "asks": ACTION_ASKS[self.action_type],
            "interest": self.interest,
            "title": self.title,
            "state": self.state,
            "state_label": STATE_LABELS[self.state],
            "is_terminal": self.is_terminal,
            "closes_exposure": self.closes_exposure,
            "allowed_moves": self.allowed_moves(),
            "documented_value": self.documented_value,
            "observed_value": self.observed_value,
            "exposure_sgd": self.exposure_sgd,
            "exposure_basis": self.exposure_basis,
            "count_documented": self.count_documented,
            "count_observed": self.count_observed,
            "count_basis": self.count_basis,
            "is_quantified": self.exposure_sgd is not None,
            "source_rule_id": self.source_rule_id,
            "source_decision": self.source_decision,
            "evidence_class": self.evidence_class,
            "policy_refs": self.policy_refs,
            "signal_refs": self.signal_refs,
            "documents_requested": self.documents_requested,
            "premium_basis": self.premium_basis,
            "premium_notice": PREMIUM_NOTICE,
            "adviser_note": self.adviser_note,
            "history": self.history,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ── persistence ──────────────────────────────────────────────────────────────
# A separate table rather than a column on `finding`, because an action outlives the
# finding that suggested it: the finding is recomputed on every run from the documents,
# and a request a human has already sent must not vanish when the pipeline re-runs.

SCHEMA = """
CREATE TABLE IF NOT EXISTS action (
  id                TEXT PRIMARY KEY,
  case_id           TEXT NOT NULL,
  action_type       TEXT NOT NULL,
  interest          TEXT NOT NULL,
  title             TEXT NOT NULL,
  state             TEXT NOT NULL,
  documented_value  TEXT,
  observed_value    TEXT,
  exposure_sgd      TEXT,
  exposure_basis    TEXT NOT NULL DEFAULT '',
  count_documented  INTEGER,
  count_observed    INTEGER,
  count_basis       TEXT NOT NULL DEFAULT '',
  source_rule_id    TEXT,
  source_decision   TEXT,
  evidence_class    TEXT,
  policy_refs       TEXT NOT NULL DEFAULT '[]',
  signal_refs       TEXT NOT NULL DEFAULT '[]',
  documents_requested TEXT NOT NULL DEFAULT '[]',
  adviser_note      TEXT NOT NULL DEFAULT '',
  history           TEXT NOT NULL DEFAULT '[]',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS action_case ON action(case_id);
"""
# Deliberately absent: any premium, price, rate or quoted_amount column. There is nothing
# to roll up, which is the structural version of the promise in PREMIUM_NOTICE.


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _row_to_action(r: sqlite3.Row) -> Action:
    return Action(
        id=r["id"], case_id=r["case_id"], action_type=r["action_type"],
        interest=r["interest"], title=r["title"], state=r["state"],
        documented_value=r["documented_value"], observed_value=r["observed_value"],
        exposure_sgd=r["exposure_sgd"], exposure_basis=r["exposure_basis"] or "",
        count_documented=r["count_documented"], count_observed=r["count_observed"],
        count_basis=r["count_basis"] or "",
        source_rule_id=r["source_rule_id"], source_decision=r["source_decision"],
        evidence_class=r["evidence_class"],
        policy_refs=json.loads(r["policy_refs"]),
        signal_refs=json.loads(r["signal_refs"]),
        documents_requested=json.loads(r["documents_requested"]),
        adviser_note=r["adviser_note"] or "",
        history=json.loads(r["history"]),
        created_at=r["created_at"], updated_at=r["updated_at"])


def _persist(conn: sqlite3.Connection, a: Action) -> None:
    a.assert_invariant()
    conn.execute(
        "INSERT OR REPLACE INTO action (id, case_id, action_type, interest, title, state,"
        " documented_value, observed_value, exposure_sgd, exposure_basis,"
        " count_documented, count_observed, count_basis, source_rule_id,"
        " source_decision, evidence_class, policy_refs, signal_refs,"
        " documents_requested, adviser_note, history, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (a.id, a.case_id, a.action_type, a.interest, a.title, a.state,
         a.documented_value, a.observed_value, a.exposure_sgd, a.exposure_basis,
         a.count_documented, a.count_observed, a.count_basis,
         a.source_rule_id, a.source_decision, a.evidence_class,
         json.dumps(a.policy_refs), json.dumps(a.signal_refs),
         json.dumps(a.documents_requested), a.adviser_note, json.dumps(a.history),
         a.created_at, a.updated_at))
    conn.commit()


def get(conn: sqlite3.Connection, action_id: str) -> Optional[Action]:
    r = conn.execute("SELECT * FROM action WHERE id=?", (action_id,)).fetchone()
    return _row_to_action(r) if r else None


def for_case(conn: sqlite3.Connection, case_id: str) -> list[Action]:
    rows = conn.execute(
        "SELECT * FROM action WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()
    return [_row_to_action(r) for r in rows]


def delete_for_case(conn: sqlite3.Connection, case_id: str) -> int:
    """Case deletion must reach here too.

    MEASURED NOTE — the base build shipped a defect where `DELETE /api/case` cleared the
    brief but left three other views serving the deleted business's data. An action list
    is the most sensitive version of that mistake, because it names a business and a
    figure. So deletion is total and `test_actions.py` asserts the list is empty after.
    """
    n = conn.execute("SELECT COUNT(*) c FROM action WHERE case_id=?",
                     (case_id,)).fetchone()["c"]
    conn.execute("DELETE FROM action WHERE case_id=?", (case_id,))
    conn.commit()
    return int(n)


# ── raising an action from a finding ─────────────────────────────────────────

# Which rule justifies which correction, and on which insured interest. A rule that is not
# in this map cannot raise an action — the mapping is the review step, and leaving it out
# is how a new rule fails safe rather than inventing a request nobody designed.
_RULE_TO_ACTION: dict[str, tuple[str, str]] = {
    "AVERAGE-ADEQUACY-BELOW-THRESHOLD": (LIMIT_INCREASE, "stock"),
    "PLANT-ADEQUACY-UNESTABLISHED": (LIMIT_INCREASE, "plant"),
    "BUILDINGS-VALUE-UNESTABLISHED": (LIMIT_INCREASE, "buildings"),
    "ALTERATION-PREMISES-NOT-ON-SCHEDULE": (SCHEDULE_AMENDMENT, "premises"),
    "SUBLIMIT-CONFLICTING-ENDORSEMENTS": (SCHEDULE_AMENDMENT, "stock"),
    "WICA-HEADCOUNT-UNDERDECLARED": (DECLARATION_UPDATE, "workforce"),
    "WORKPASS-MEDICAL-BELOW-MINIMUM": (DECLARATION_UPDATE, "workforce"),
    "ACTIVITY-SSIC-NOT-DECLARED": (DECLARATION_UPDATE, "premises"),
    "BUSINESS-INTERRUPTION-NOT-CONFIRMED": (COVER_CONFIRMATION, "business_interruption"),
    "THEFT-FORCIBLE-ENTRY-NOT-WRITTEN-BACK": (COVER_CONFIRMATION, "stock"),
}

# When an interest has no established figure, this is the document that would establish it.
# Naming it is what makes an UNESTABLISHED finding actionable instead of a dead end — and
# it is also what gives the action its evidence, so the invariant is satisfied honestly
# rather than by relaxing it.
_ESTABLISHING_DOCUMENT: dict[str, str] = {
    "stock": "ACCOUNTS",
    "plant": "ASSET_REGISTER",
    "buildings": "VALUATION_REPORT",
    "premises": "TENANCY",
    "workforce": "CPF_RECORD",
    "business_interruption": "ACCOUNTS",
}


def action_type_for_rule(rule_id: str) -> Optional[tuple[str, str]]:
    return _RULE_TO_ACTION.get(rule_id)


# Where the two figures for an exposure calculation come from, per insured interest.
# NAMES ARE EXTRACTED FACTS, not finding detail — a fact carries its own document kind and
# author class, so the subtraction inherits real provenance instead of a rule's summary.
#
# Only `stock` has both halves in this build, and that is the honest state of it:
#   stock      documented S$100,000 (SCHEDULE, insurer)  vs  observed S$150,000 (ACCOUNTS)
#   plant      documented S$180,000 (SCHEDULE, insurer)  vs  NOTHING totals current plant.
#              `invoice_equipment_value` is ONE freezer at S$62,000, so subtracting it from
#              the sum insured would produce a confident, wrong, negative number. The
#              asset register is named as the missing document instead.
#   buildings  neither figure is established.
_INTEREST_MONEY: dict[str, tuple[Optional[str], Optional[str]]] = {
    "stock": ("stock_sum_insured", "stock_value"),
    "plant": ("plant_sum_insured", None),
    "buildings": (None, None),
}

# Interests measured in people rather than dollars. A headcount gap is real and actionable
# and has no exposure figure, so it travels in its own fields and `exposure_sgd` stays None.
_INTEREST_COUNT: dict[str, tuple[str, str]] = {
    "workforce": ("declared_headcount", "contributor_count"),
}


def _fact_value(facts: dict, name: Optional[str]):
    """A fact's value plus its provenance, or (None, None, None) when absent."""
    if not name:
        return None, None, None
    f = (facts or {}).get(name)
    if not isinstance(f, dict) or f.get("value") in (None, ""):
        return None, None, None
    return f.get("value"), f.get("document_kind"), f.get("author_class")


def _exposure(documented: Optional[Decimal], observed: Optional[Decimal],
              interest: str = "") -> tuple[Optional[str], str]:
    """The only arithmetic in this module.

    Returns (exposure, basis). Both inputs must be evidenced elsewhere; this subtracts.
    A missing input yields no figure and a basis that says why — the same refusal the rest
    of the engine makes, applied to the number a judge is most likely to want.
    """
    # Distinguish "this interest is not measured in money" from "the figures are missing".
    # Reporting a headcount or a scheduled address as an unestablished sum insured is a
    # wrong explanation of a correct abstention, which is its own kind of misleading.
    if interest in _INTEREST_COUNT:
        return None, ("This interest is measured in people, not in a sum insured, so no "
                      "exposure figure applies. The counts and their sources are below.")
    if interest not in _INTEREST_MONEY:
        return None, ("This correction is not a shortfall against a sum insured, so no "
                      "exposure figure applies. What is requested is named below.")
    if documented is None and observed is None:
        return None, ("Neither the documented limit nor the current value is established "
                      "for this interest, so no exposure figure is computed.")
    if documented is None:
        return None, ("No documented limit is established for this interest, so the "
                      "shortfall cannot be computed. The observed value is shown alone.")
    if observed is None:
        return None, ("The current value is not established by any document, so the "
                      "shortfall cannot be computed. Requesting the document that would "
                      "establish it is the action.")
    diff = observed - documented
    if diff <= 0:
        return "0", (f"Observed value S${observed:,.0f} does not exceed the documented "
                     f"limit S${documented:,.0f}. No shortfall on these figures.")
    return str(diff), (f"Observed value S${observed:,.0f} less documented limit "
                       f"S${documented:,.0f} = S${diff:,.0f} above the documented limit. "
                       f"Both figures are cited above; this is the subtraction.")


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def raise_from_finding(
    conn: sqlite3.Connection, case_id: str, finding: dict,
    facts: Optional[dict] = None, decision: Optional[str] = None,
    signal_refs: Optional[list[dict]] = None,
) -> Optional[Action]:
    """Prepare (do not send) an action for one finding.

    Returns None when the finding has no mapped correction. Refusing to raise is the
    common case and it is correct: most findings are information, not requests.
    """
    rule_id = finding.get("rule_id") or ""
    mapped = _RULE_TO_ACTION.get(rule_id)
    if mapped is None:
        return None
    action_type, interest = mapped
    facts = facts or {}
    detail = finding.get("detail") or {}

    # ── the money, from facts ────────────────────────────────────────────────
    doc_name, obs_name = _INTEREST_MONEY.get(interest, (None, None))
    doc_raw, doc_kind, doc_author = _fact_value(facts, doc_name)
    obs_raw, obs_kind, obs_author = _fact_value(facts, obs_name)
    documented, observed = _dec(doc_raw), _dec(obs_raw)
    exposure, basis = _exposure(documented, observed, interest)

    # Name the provenance of each side, because a subtraction is only as good as its
    # operands and the reader should not have to go looking for them.
    if documented is not None and doc_kind:
        basis += f" Documented limit from {doc_kind} ({doc_author})."
    if observed is not None and obs_kind:
        basis += f" Current value from {obs_kind} ({obs_author})."

    # ── the counts, for interests measured in people ─────────────────────────
    count_documented = count_observed = None
    count_basis = ""
    cd_name, co_name = _INTEREST_COUNT.get(interest, (None, None))
    if cd_name:
        cd_raw, cd_kind, cd_author = _fact_value(facts, cd_name)
        co_raw, co_kind, co_author = _fact_value(facts, co_name)
        if cd_raw is not None and co_raw is not None:
            count_documented, count_observed = int(cd_raw), int(co_raw)
            gap = count_observed - count_documented
            count_basis = (
                f"{count_observed} on record with {co_kind} ({co_author}) against "
                f"{count_documented} declared on {cd_kind} ({cd_author}) — a difference of "
                f"{gap}. This is a count, not a sum insured, so no exposure figure follows "
                f"from it.")

    policy_refs = [
        {"clause_ref": s.get("clause_ref"), "page_no": s.get("page_no"),
         "document": s.get("document"), "quote": (s.get("quote") or "")[:240]}
        for s in (finding.get("spans") or [])
    ]
    # Documents to obtain: whatever the rule named, plus the fact that would let the
    # subtraction happen at all when the observed side is missing.
    docs = list(detail.get("missing_documents") or ())
    for item in detail.get("missing_from_schedule") or ():
        ref = f"SCHEDULE (to include: {item})"
        if ref not in docs:
            docs.append(ref)
    # A money interest with no current value cannot be quantified, so the action IS the
    # request for the document that would quantify it. This is also what supplies the
    # evidence the invariant requires — a finding with no spans is exactly the case where
    # naming the missing document is the only honest thing to hold.
    if interest in _INTEREST_MONEY and observed is None:
        want = _ESTABLISHING_DOCUMENT.get(interest)
        if want and want not in docs:
            docs.append(want)
    if not (policy_refs or docs):
        # Nothing cites this and nothing would settle it. Raising a request from that is
        # an opinion with a button on it, so decline to raise one.
        return None

    a = Action(
        id="ACT-" + uuid.uuid4().hex[:10].upper(),
        case_id=case_id,
        action_type=action_type,
        interest=interest,
        title=finding.get("headline") or ACTION_LABELS[action_type],
        state=DRAFTED,
        documented_value=str(documented) if documented is not None else None,
        observed_value=str(observed) if observed is not None else None,
        exposure_sgd=exposure,
        exposure_basis=basis,
        count_documented=count_documented,
        count_observed=count_observed,
        count_basis=count_basis,
        source_rule_id=rule_id,
        source_decision=decision,
        evidence_class=finding.get("class") or finding.get("evidence_class"),
        policy_refs=policy_refs,
        signal_refs=list(signal_refs or ()),
        documents_requested=docs,
        history=[{"at": _now(), "to": DRAFTED, "by": "engine",
                  "note": "Prepared from the finding named above. Not sent."}],
    )
    _persist(conn, a)
    return a


def raise_for_case(conn: sqlite3.Connection, case_id: str, findings: Iterable[dict],
                   facts: Optional[dict] = None,
                   decisions: Optional[list[dict]] = None) -> list[Action]:
    """Prepare actions for every finding that maps to one, skipping duplicates.

    Idempotent by (case_id, source_rule_id, interest): re-running the pipeline must not
    produce a second copy of a request the adviser is already working on.
    """
    existing = {(a.source_rule_id, a.interest) for a in for_case(conn, case_id)}
    by_interest: dict[str, str] = {}
    for d in (decisions or ()):
        by_interest[d.get("interest")] = d.get("decision")

    out: list[Action] = []
    for f in findings:
        mapped = _RULE_TO_ACTION.get(f.get("rule_id") or "")
        if mapped is None or (f.get("rule_id"), mapped[1]) in existing:
            continue
        # Per-finding isolation. One finding that cannot support a request must not cost
        # the user the other five — a batch that fails whole is how a single edge case
        # silently removes the entire action loop from a demo.
        try:
            a = raise_from_finding(conn, case_id, f, facts=facts,
                                   decision=by_interest.get(mapped[1]))
        except ValueError:
            continue
        if a is not None:
            out.append(a)
            existing.add((a.source_rule_id, a.interest))
    return out


# ── advancing an action ──────────────────────────────────────────────────────

class TransitionRefused(Exception):
    """A move the state machine does not permit, or not by that actor."""

    def __init__(self, message: str, code: str = "TRANSITION_REFUSED"):
        super().__init__(message)
        self.code = code


def advance(conn: sqlite3.Connection, action_id: str, to_state: str, by: str,
            note: str = "") -> Action:
    """Move an action, or refuse and say why.

    `by` is checked against the transition table, not merely recorded. An owner cannot
    mark their own request BOUND — that is an adviser confirming the contract changed, and
    letting the owner assert it would make the exposure-closed figure self-reported.
    """
    a = get(conn, action_id)
    if a is None:
        raise TransitionRefused(f"No action {action_id!r}.", "ACTION_NOT_FOUND")
    if to_state not in STATES:
        raise TransitionRefused(f"Unknown state {to_state!r}.", "UNKNOWN_STATE")
    permitted = {t: actor for t, actor in TRANSITIONS[a.state]}
    if to_state not in permitted:
        raise TransitionRefused(
            f"{a.state} cannot move to {to_state}. Permitted: "
            f"{sorted(permitted) or 'none — this action is closed'}.")
    if permitted[to_state] != by:
        raise TransitionRefused(
            f"Only the {permitted[to_state]} may move this action to {to_state}; "
            f"the request came from the {by}.", "WRONG_ACTOR")

    a.state = to_state
    a.updated_at = _now()
    if note:
        a.adviser_note = note if by == "adviser" else a.adviser_note
    a.history.append({"at": a.updated_at, "to": to_state, "by": by, "note": note})
    _persist(conn, a)
    return a


# ── the outcome metric ───────────────────────────────────────────────────────

def outcomes(conn: sqlite3.Connection, case_id: str,
             findings_count: int = 0) -> dict:
    """The only number that shows the loop closed.

    Exposure is counted as closed ONLY on BOUND. `pending` is reported separately rather
    than added in, because a request in flight has closed nothing, and a product that
    blurs the two is measuring its own activity instead of its effect.

    MEASURED DEFECT — exposure is counted ONCE PER INSURED INTEREST, not once per action.
    Two rules legitimately raise two different corrections against the same stock
    shortfall: AVERAGE-ADEQUACY asks for a limit increase, SUBLIMIT-CONFLICTING-ENDORSEMENTS
    asks for a schedule amendment. Both carry the same S$50,000, because it is the same
    S$50,000. Summing the actions reported S$100,000 of exposure on a business with
    S$50,000 of it — a doubling, in the direction that flatters us, on the one figure the
    whole loop exists to produce. Grouping by interest is the fix; the per-interest
    breakdown is returned so the arithmetic can be checked by hand.
    """
    acts = for_case(conn, case_id)
    by_state: dict[str, int] = {}
    # interest -> {"amount": Decimal, "closed": bool, "in_flight": bool, "actions": [...]}
    interests: dict[str, dict] = {}

    for a in acts:
        by_state[a.state] = by_state.get(a.state, 0) + 1
        if a.exposure_sgd is None:
            continue
        amount = Decimal(a.exposure_sgd)
        slot = interests.setdefault(
            a.interest, {"amount": amount, "closed": False, "in_flight": False,
                         "actions": []})
        # If two actions on one interest disagree on the figure, keep the larger and say
        # so. They should agree — they read the same facts — but silently trusting the
        # first one seen would hide a real inconsistency.
        if amount > slot["amount"]:
            slot["amount"] = amount
        slot["actions"].append({"id": a.id, "action_type": a.action_type,
                                "state": a.state, "exposure_sgd": a.exposure_sgd})
        if a.state in CLOSES_EXPOSURE:
            slot["closed"] = True
        elif a.state != DRAFTED and not a.is_terminal:
            # DRAFTED is prepared, not pending: nobody has been asked anything yet.
            # Counting it as in flight would let the product report momentum it has not
            # earned — the figure would move the moment the engine ran, not when a human
            # did something.
            slot["in_flight"] = True

    closed = sum((v["amount"] for v in interests.values() if v["closed"]), Decimal(0))
    pending = sum((v["amount"] for v in interests.values()
                   if v["in_flight"] and not v["closed"]), Decimal(0))
    identified = sum((v["amount"] for v in interests.values()), Decimal(0))

    unquantified = sum(1 for a in acts if a.exposure_sgd is None)
    multi = {k: v for k, v in interests.items() if len(v["actions"]) > 1}

    return {
        "gaps_detected": findings_count,
        "actions_raised": len(acts),
        "actions_drafted": by_state.get(DRAFTED, 0),
        "actions_in_flight": sum(1 for a in acts
                                 if a.state != DRAFTED and not a.is_terminal),
        "actions_resolved": sum(1 for a in acts if a.is_terminal),
        "actions_bound": by_state.get(BOUND, 0),
        "by_state": by_state,
        "exposure_identified_sgd": str(identified),
        "exposure_closed_sgd": str(closed),
        "exposure_pending_sgd": str(pending),
        "exposure_by_interest": {
            k: {"amount_sgd": str(v["amount"]),
                "status": ("CLOSED" if v["closed"]
                           else "IN_FLIGHT" if v["in_flight"] else "NOT_PROGRESSED"),
                "actions": v["actions"]}
            for k, v in sorted(interests.items())
        },
        "actions_without_exposure_figure": unquantified,
        "premium_basis": NOT_QUOTED,
        "counting_basis": (
            "Exposure is counted once per insured interest. Two corrections against the "
            "same shortfall are two requests, not two shortfalls."),
        "notes": [
            "Exposure is counted as closed only when an adviser records the contract as "
            "amended (BOUND). A request in flight has closed nothing.",
            "DECLINED and NOT_REQUIRED are legitimate endings and are not counted as "
            "closed exposure.",
            PREMIUM_NOTICE,
        ] + ([
            f"{unquantified} action(s) carry no exposure figure: the interest is not "
            f"measured in a sum insured, or a required value is not established by any "
            f"document. They are listed, not estimated."
        ] if unquantified else []) + ([
            f"{len(multi)} insured interest(s) carry more than one requested correction "
            f"({', '.join(sorted(multi))}). Counted once each."
        ] if multi else []),
    }
