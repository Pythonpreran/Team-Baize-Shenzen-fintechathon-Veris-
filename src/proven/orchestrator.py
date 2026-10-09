"""VERIS ORCHESTRATOR — where the two agents meet, and neither one wins by being louder.

THE PROBLEM THIS SOLVES. Two agents now produce output about the same business.
`connectors.py` (Connect) says "your closing inventory rose to S$150,000". `sentinel.py`
(Sentinel) says "the published equipment cost index rose 8.4%". Both are true. Neither is
a finding. And the tempting thing to do — hand both to a model and ask what it means — is
precisely how you get S$150,000 x 1.084 rendered as a sum insured, a number with no basis
in any document, on a screen that leads to money.

So this module is deterministic. The agents PROPOSE; this decides. Same architecture as
`ai.py`, applied one level up.

THE FOUR PRECEDENCE RULES, and they are the whole module:

  1. CURRENT STATE COMES FROM DOCUMENTS AND INTERNAL RECORDS. NEVER FROM EXTERNAL DATA.
     A cost index cannot restate what your plant is worth. A flood registry cannot
     restate where you trade. External data has no access to facts about one business,
     so it is structurally unable to establish one, and `sentinel.py` enforces that
     ceiling at construction.

  2. EXTERNAL DATA SETS FORWARD RISK, AND FORWARD RISK NEVER EDITS A CURRENT VALUE.
     It travels in its own field (`risk_state`) so it can be shown next to the current
     position without being folded into it. Two numbers, side by side, is the honest
     rendering; one merged number is not.

  3. AGREEMENT RAISES PRIORITY. IT DOES NOT RAISE CERTAINTY.
     When a business change and an environmental condition land on the same insured
     interest, that is worth a human's attention sooner. It is not extra evidence, and
     the evidence class does not move.

  4. THE REASON IS PART OF THE OUTPUT.
     Every decision carries `precedence_reason` in plain words. A conflict resolved
     without saying why is indistinguishable from a coin toss, and an adviser being asked
     to act on it deserves better.

WHAT IT MAY NOT SAY. The decision vocabulary is a closed set and every value in it is
about attention, not adjudication: MONITOR, WATCH, REVIEW, ACTION_REQUIRED,
UNESTABLISHED, POTENTIAL_INCIDENT. There is no BREACH, no VIOLATION, no claim outcome —
not as a string, not as an enum, not as a column. `test_orchestrator.py` greps this module
for that vocabulary and fails the build if it appears.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

from . import sentinel

# ── decision vocabulary: attention, never adjudication ──────────────────────

MONITOR = "MONITOR"                        # nothing to do
WATCH = "WATCH"                            # real signal, keep looking
REVIEW = "REVIEW"                          # a human should look
ACTION_REQUIRED = "ACTION_REQUIRED"        # documented mismatch
UNESTABLISHED = "UNESTABLISHED"            # not enough evidence to say
POTENTIAL_INCIDENT = "POTENTIAL_INCIDENT"  # environment suggests it; nothing confirms it

DECISIONS: tuple[str, ...] = (MONITOR, WATCH, REVIEW, ACTION_REQUIRED,
                              UNESTABLISHED, POTENTIAL_INCIDENT)

DECISION_ORDER = {d: i for i, d in enumerate(
    (MONITOR, WATCH, UNESTABLISHED, POTENTIAL_INCIDENT, REVIEW, ACTION_REQUIRED))}

DECISION_LABELS: dict[str, str] = {
    MONITOR: "Monitoring — no action required",
    WATCH: "Worth watching",
    REVIEW: "Review recommended",
    ACTION_REQUIRED: "Action recommended",
    UNESTABLISHED: "Not established — evidence missing",
    POTENTIAL_INCIDENT: "Possible impact — confirmation required",
}

PRIORITY_ORDER: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")

# How stale an internal record may be before an overlapping external signal is reported
# as the more current information. 90 days is a judgement, declared here in one place so
# it can be argued with rather than discovered in a branch.
STALE_AFTER_DAYS = 90

# Insured interests the orchestrator reasons about. Both agents tag their output with
# these, which is what lets an overlap be detected by set intersection rather than by
# asking a model whether two sentences are about the same thing.
INTERESTS: tuple[str, ...] = ("stock", "plant", "buildings", "premises",
                              "workforce", "business_interruption")

# Which internal signal kinds bear on which insured interest.
_INTERNAL_INTEREST: dict[str, tuple[str, ...]] = {
    "ASSET_ACQUIRED": ("plant",),
    "INVENTORY": ("stock",),
    "HEADCOUNT": ("workforce",),
    "PREMISES": ("premises", "buildings"),
    "REVENUE_CHANNEL": ("business_interruption",),
}

# Which rule ids speak to which insured interest, so a decision can cite the policy
# findings that already exist rather than inventing a parallel analysis.
_RULE_INTEREST: dict[str, tuple[str, ...]] = {
    "AVERAGE-ADEQUACY-BELOW-THRESHOLD": ("stock",),
    "PLANT-ADEQUACY-UNESTABLISHED": ("plant",),
    "BUILDINGS-VALUE-UNESTABLISHED": ("buildings",),
    "ALTERATION-PREMISES-NOT-ON-SCHEDULE": ("premises",),
    "WICA-HEADCOUNT-UNDERDECLARED": ("workforce",),
    "WORKPASS-MEDICAL-BELOW-MINIMUM": ("workforce",),
    "BUSINESS-INTERRUPTION-NOT-CONFIRMED": ("business_interruption",),
    "SUBLIMIT-CONFLICTING-ENDORSEMENTS": ("stock",),
    "UNOCCUPANCY-WATER-EXCLUSION": ("buildings", "stock"),
}

# Finding states that represent a documented problem rather than an open question.
_DOCUMENTED_STATES = frozenset({"DOCUMENTED", "REQUIRES_AUTHORISED_REVIEW"})
_OPEN_STATES = frozenset({"INFORMATION_MISSING", "NOT_CONFIRMED"})


@dataclass
class Decision:
    """One reconciled position on one insured interest."""

    interest: str
    decision: str
    priority: str
    current_state: str
    risk_state: str
    precedence_reason: str
    evidence_class: Optional[str] = None
    internal_refs: list[dict] = field(default_factory=list)
    external_refs: list[dict] = field(default_factory=list)
    policy_refs: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    overlap: bool = False
    stale_internal: bool = False

    def as_dict(self) -> dict:
        return {
            "interest": self.interest,
            "interest_label": self.interest.replace("_", " ").title(),
            "decision": self.decision,
            "decision_label": DECISION_LABELS[self.decision],
            "priority": self.priority,
            # Two fields, never one. Folding forward risk into the current position is
            # the specific error this whole module exists to prevent.
            "current_state": self.current_state,
            "risk_state": self.risk_state,
            "precedence_reason": self.precedence_reason,
            "evidence_class": self.evidence_class,
            "internal_refs": self.internal_refs,
            "external_refs": self.external_refs,
            "policy_refs": self.policy_refs,
            "actions": self.actions,
            "overlap": self.overlap,
            "stale_internal": self.stale_internal,
        }


def _interests_for_internal(signal: dict) -> tuple[str, ...]:
    return _INTERNAL_INTEREST.get(str(signal.get("kind", "")).upper(), ())


def _interests_for_rule(rule_id: str) -> tuple[str, ...]:
    return _RULE_INTEREST.get(rule_id, ())


def _is_stale(iso_date: Optional[str], *, today: Optional[date] = None) -> bool:
    if not iso_date:
        return False
    try:
        observed = date.fromisoformat(iso_date)
    except (ValueError, TypeError):
        return False
    reference = today or date.today()
    return observed < reference - timedelta(days=STALE_AFTER_DAYS)


def _worst(values: Iterable[str], order: tuple[str, ...], default: str) -> str:
    best = default
    for value in values:
        if value in order and order.index(value) > order.index(best):
            best = value
    return best


def evaluate(*, internal_signals: Optional[list[dict]] = None,
             external_signals: Optional[list[dict]] = None,
             findings: Optional[list[dict]] = None,
             today: Optional[date] = None) -> dict:
    """Reconcile both agents against the policy findings the engine already produced.

    Returns one Decision per insured interest that any input touches. Interests nobody
    mentioned are omitted rather than reported as MONITOR: "we did not look" and "we
    looked and it is fine" are different statements, and the engine only earns the second
    one where it actually looked.
    """
    internal_signals = list(internal_signals or [])
    external_signals = list(external_signals or [])
    findings = list(findings or [])

    # Bucket every input by insured interest.
    internal_by: dict[str, list[dict]] = {}
    for signal in internal_signals:
        for interest in _interests_for_internal(signal):
            internal_by.setdefault(interest, []).append(signal)

    external_by: dict[str, list[dict]] = {}
    for signal in external_signals:
        for interest in signal.get("insured_interests") or ():
            if interest in INTERESTS:
                external_by.setdefault(interest, []).append(signal)

    findings_by: dict[str, list[dict]] = {}
    for finding in findings:
        for interest in _interests_for_rule(finding.get("rule_id", "")):
            findings_by.setdefault(interest, []).append(finding)

    touched = sorted(set(internal_by) | set(external_by) | set(findings_by),
                     key=lambda i: INTERESTS.index(i) if i in INTERESTS else 99)

    decisions = [
        _decide(interest,
                internal=internal_by.get(interest, []),
                external=external_by.get(interest, []),
                policy=findings_by.get(interest, []),
                today=today)
        for interest in touched
    ]
    decisions.sort(key=lambda d: (-DECISION_ORDER[d.decision],
                                  -PRIORITY_ORDER.index(d.priority)))

    overlaps = [d for d in decisions if d.overlap]
    return {
        "decisions": [d.as_dict() for d in decisions],
        "count": len(decisions),
        "overlap_count": len(overlaps),
        "overlap_interests": [d.interest for d in overlaps],
        "highest": decisions[0].as_dict() if decisions else None,
        "by_decision": {d: sum(1 for x in decisions if x.decision == d)
                        for d in DECISIONS},
        "inputs": {
            "internal_signals": len(internal_signals),
            "external_signals": len(external_signals),
            "policy_findings": len(findings),
            "interests_touched": touched,
        },
        "method": "DETERMINISTIC",
        "notice": (
            "Reconciled by fixed precedence rules, not by a model. Current position is "
            "taken only from documents and internal records; external data sets forward "
            "risk and never edits a current value. Where both agents land on the same "
            "insured interest, priority rises but the evidence class does not — "
            "agreement is a reason to look sooner, not stronger proof."),
        "vocabulary": {
            "decisions": list(DECISIONS),
            "deliberately_absent": ["BREACH", "VIOLATION", "CLAIM_APPROVED",
                                    "CLAIM_DENIED", "COVERAGE_GUARANTEED"],
        },
    }


def _decide(interest: str, *, internal: list[dict], external: list[dict],
            policy: list[dict], today: Optional[date]) -> Decision:
    """The precedence rules, applied to one insured interest."""
    has_internal, has_external, has_policy = bool(internal), bool(external), bool(policy)
    overlap = has_internal and has_external

    internal_refs = [{
        "id": s.get("id"), "kind": s.get("kind"), "label": s.get("label"),
        "detected_on": s.get("detected_on"), "amount_sgd": s.get("amount_sgd"),
        "source": s.get("connector"), "author_class": s.get("author_class"),
        "agent": "connect",
    } for s in internal]

    external_refs = [{
        "id": s.get("id"), "signal_type": s.get("signal_type"),
        "label": s.get("label"), "severity": s.get("severity"),
        "horizon": s.get("horizon"), "observed_on": s.get("observed_on"),
        "claim_scope": s.get("claim_scope"),
        "source": (s.get("source") or {}).get("name"),
        "simulated": s.get("simulated", True), "agent": "sentinel",
    } for s in external]

    policy_refs = [{
        "rule_id": f.get("rule_id"), "headline": f.get("headline"),
        "state": f.get("state"), "class": f.get("class"),
    } for f in policy]

    # ── RULE 1: current position, from documents and internal records only ──
    documented = [f for f in policy if f.get("state") in _DOCUMENTED_STATES]
    open_items = [f for f in policy if f.get("state") in _OPEN_STATES]

    if documented:
        current_state = (f"{len(documented)} documented finding(s) on {interest}, "
                         f"evidenced by the policy documents on record.")
        evidence_class = _worst(
            (f.get("class", "") for f in documented),
            ("UNESTABLISHED", "MARKET_CONVENTION", "DECLARED", "PROVEN"),
            "UNESTABLISHED")
    elif open_items:
        current_state = (f"{len(open_items)} item(s) on {interest} could not be "
                         f"established from the documents provided.")
        evidence_class = "UNESTABLISHED"
    elif has_internal:
        current_state = (f"A business record reports a change affecting {interest}. "
                         f"No policy document on record settles what it means.")
        evidence_class = None
    else:
        current_state = f"Nothing on record establishes a current position on {interest}."
        evidence_class = None

    # ── RULE 2: forward risk travels separately ────────────────────────────
    stale_internal = has_internal and all(
        _is_stale(s.get("detected_on"), today=today) for s in internal)

    if has_external:
        worst = max(external, key=lambda s: s.get("severity_rank", 0))
        horizons = sorted({s.get("horizon") for s in external if s.get("horizon")})
        risk_state = (f"{worst.get('severity')} — {worst.get('label')} "
                      f"({', '.join(horizons).lower()}). External condition only; it "
                      f"asserts nothing about this business.")
        external_severity = worst.get("severity", "LOW")
    else:
        risk_state = "No external signal matched this insured interest."
        external_severity = "LOW"

    # ── RULE 3 + 4: the decision, and the reason for it ────────────────────
    if documented and overlap:
        decision = ACTION_REQUIRED
        priority = "HIGH"
        reason = (
            "Both agents landed on this insured interest AND the documents already "
            "evidence a shortfall. The current position is taken from the documents; "
            f"the external signal ({external_severity}) is retained as forward risk and "
            "has not been folded into any figure. Agreement between the two agents "
            "raised the priority, not the evidence class — it is a reason to look "
            "sooner, not stronger proof.")
    elif documented:
        decision = ACTION_REQUIRED
        priority = "HIGH" if evidence_class == "PROVEN" else "MEDIUM"
        reason = ("Established from the documents on record. No external signal was "
                  "needed to reach it and none was used.")
    elif overlap and open_items:
        decision = REVIEW
        priority = "HIGH"
        reason = (
            "A business change and an external condition both bear on this insured "
            "interest, but the documents do not establish the current position, so no "
            "figure can be computed. Priority is raised because two independent signals "
            "agree; the state stays unestablished because agreement is not evidence.")
    elif overlap:
        decision = REVIEW
        priority = "MEDIUM"
        reason = (
            "A business change and an external condition coincide on this insured "
            "interest. Neither establishes a consequence on its own: an internal record "
            "is not a policy document, and an external feed cannot speak to one "
            "business. Worth a human read.")
    elif has_external and not has_internal and not has_policy:
        # The rule that stops the most common overclaim in the category.
        decision = POTENTIAL_INCIDENT if external_severity in {"ELEVATED", "HIGH"} \
            else WATCH
        priority = "MEDIUM" if external_severity in {"ELEVATED", "HIGH"} else "LOW"
        reason = (
            f"An external signal ({external_severity}) touches this insured interest, "
            "and nothing business-specific confirms any effect. Reported as a condition "
            "requiring confirmation rather than as something that has happened — a "
            "forecast is not an event, and a hazard designation is not a loss.")
    elif open_items:
        decision = UNESTABLISHED
        priority = "MEDIUM"
        reason = ("A required input has no document behind it, so the engine will not "
                  "estimate the figure. What is missing is named rather than filled in.")
    elif has_internal:
        decision = REVIEW
        priority = "MEDIUM"
        reason = ("A business record reports a change here and no policy document on "
                  "record addresses it. An internal record raises the question; only a "
                  "document settles it.")
    else:
        decision = MONITOR
        priority = "LOW"
        reason = "Nothing on record and no external signal. Monitoring."

    # Staleness modifies the reason rather than the decision: an old internal record does
    # not become wrong, it becomes less current, and saying so is the useful part.
    if stale_internal and has_external:
        reason += (f" The internal record here is older than {STALE_AFTER_DAYS} days, "
                   f"so the external signal is currently the more recent information — "
                   f"which does not make it evidence about this business.")

    return Decision(
        interest=interest, decision=decision, priority=priority,
        current_state=current_state, risk_state=risk_state,
        precedence_reason=reason, evidence_class=evidence_class,
        internal_refs=internal_refs, external_refs=external_refs,
        policy_refs=policy_refs, overlap=overlap, stale_internal=stale_internal,
        actions=_actions_for(interest, decision, internal, external, policy),
    )


def _actions_for(interest: str, decision: str, internal: list[dict],
                 external: list[dict], policy: list[dict]) -> list[dict]:
    """What to do next. Documents to obtain and humans to involve — never a coverage
    instruction, which is a licensed adviser's call."""
    actions: list[dict] = []

    wanted: list[str] = []
    for signal in internal:
        for kind in signal.get("asks_for") or ():
            if kind not in wanted:
                wanted.append(kind)
    for finding in policy:
        for kind in (finding.get("detail") or {}).get("missing_documents") or ():
            if kind not in wanted:
                wanted.append(kind)
    if wanted:
        actions.append({
            "type": "PROVIDE_DOCUMENT", "documents": wanted,
            "label": "Provide the document that would settle this",
            "detail": ("These are named because they are the inputs the rule needs. "
                       "Nothing is estimated in their absence."),
        })

    if decision in {REVIEW, ACTION_REQUIRED}:
        actions.append({
            "type": "AUTHORISED_REVIEW", "label": "Send to an authorised adviser",
            "detail": ("The engine locates, quotes and computes. Whether cover responds "
                       "is a licensed adviser's determination, not this system's."),
        })
    if decision == POTENTIAL_INCIDENT:
        actions.append({
            "type": "CONFIRM_STATUS", "label": "Confirm whether anything was affected",
            "detail": ("An external condition was detected near an insured interest. "
                       "Only you can confirm whether it affected the business; the feed "
                       "cannot."),
        })
    if decision in {WATCH, MONITOR}:
        actions.append({
            "type": "NONE", "label": "No action needed",
            "detail": "Recorded and monitored. Nothing here needs you today.",
        })
    return actions


def attention_matrix(decisions: list[dict]) -> dict:
    """The carrier's Protection Attention Matrix: internal change against external risk.

    NOT a breach table. The axes are "has this business changed" and "is its environment
    adverse" — both observations, neither an accusation. The HIGH/HIGH cell is the product
    thesis in one square: a business that moved inside a environment that got worse
    outside, with documents that did neither.
    """
    def bucket(has_signal: bool, worst: str) -> str:
        if not has_signal:
            return "LOW"
        return {"HIGH": "HIGH", "ELEVATED": "HIGH", "MODERATE": "MED"}.get(worst, "LOW")

    grid: dict[str, dict[str, int]] = {
        row: {"LOW": 0, "MED": 0, "HIGH": 0} for row in ("LOW", "MED", "HIGH")}
    placed: list[dict] = []

    for item in decisions:
        internal_axis = "HIGH" if item.get("overlap") or item.get("internal_refs") \
            else "LOW"
        if internal_axis == "HIGH" and not item.get("policy_refs"):
            internal_axis = "MED"
        severities = [r.get("severity", "LOW") for r in item.get("external_refs") or []]
        external_axis = bucket(bool(severities),
                              _worst(severities, sentinel.SEVERITY_ORDER, "LOW"))
        grid[internal_axis][external_axis] += 1
        placed.append({"interest": item.get("interest"),
                       "internal": internal_axis, "external": external_axis,
                       "decision": item.get("decision")})

    return {
        "grid": grid,
        "placed": placed,
        "priority_cell": {"internal": "HIGH", "external": "HIGH",
                          "count": grid["HIGH"]["HIGH"]},
        "axes": {"x": "External risk", "y": "Internal business change"},
        "title": "Protection Attention Matrix",
        "notice": (
            "Attention, not adjudication. Both axes are observations — the business "
            "changed, the environment worsened — and neither is a finding against "
            "anyone. There is no breach or violation column in this matrix or anywhere "
            "in the schema."),
    }
