"""VERIS WARRANTY — will this policy actually pay, or has a technicality already voided it?

WHY THIS MODULE EXISTS. Every other product in this space answers "what does my policy
say". This one answers a different and more consequential question: "will it pay". Those
diverge because commercial policies carry warranties and conditions precedent, and
breaching one can defeat a claim that the cover otherwise plainly responded to.

The legal position is the reason this matters, and it is harsher than most business owners
realise. Where a condition precedent to liability is not satisfied, insurers may be
entitled to decline the claim ENTIRELY — and depending on the policy terms, regardless of
whether the breach had any causal connection to the loss (Pinsent Masons, on conditions
precedent under the Insurance Act 2015; DLA Piper's claims-manager guidance to the same
effect).

Read that again in business terms. A lapsed alarm-maintenance certificate can defeat a
fire claim. The alarm has nothing to do with the fire. That is the point of a condition
precedent, and it is why this is the single most under-served question in SME insurance.

NOBODY PRODUCTISES THIS. Searching the market for a tool that tracks warranty and
condition-precedent compliance for an in-force SME policy returns law-firm explainers of
the principle and no product. The principle is well documented; the monitoring is not
built. That gap is what this module occupies.

THE DISCIPLINE: UNVERIFIED IS THE DEFAULT, AND IT IS NOT A FAILURE.

A compliance register that quietly assumes compliance is worse than no register, because
it manufactures confidence exactly where the exposure lives. So:

  * BREACHED requires evidence of the breach — a date arithmetic that fails, a document
    that contradicts the schedule. It is never inferred from absence.
  * COMPLIANT requires evidence of compliance. Silence is not compliance.
  * UNVERIFIED is what the engine says when it has located the obligation in the wording
    but holds nothing that speaks to whether it is met. Every UNVERIFIED entry names the
    document that would settle it, so the honest answer is also an actionable one.
  * NOT_IN_POLICY means the obligation was searched for in this wording and not found.

WHAT THIS IS NOT. Not a coverage determination, and not a legal opinion on whether an
insurer would in fact decline. Whether a breach defeats a particular claim turns on the
policy wording, the facts of the loss and applicable law — a licensed adviser's and
ultimately a lawyer's territory. What this module does is find the obligation, locate it
in the customer's own wording, and say plainly whether anything on record shows it is met.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from . import account, ingest, jurisdiction

DISCLAIMER = ("Compliance state read from the documents on record. Not a coverage "
              "determination and not legal advice — whether a breach defeats a "
              "particular claim depends on the wording, the loss and applicable law.")

# ── obligation kinds, ordered by how badly a breach bites ────────────────────
CONDITION_PRECEDENT = "CONDITION_PRECEDENT"
WARRANTY = "WARRANTY"
CONTINUING_CONDITION = "CONTINUING_CONDITION"

_KIND_CONSEQUENCE = {
    CONDITION_PRECEDENT: ("Insurers may be entitled to decline the claim entirely, and "
                          "depending on the wording regardless of whether the breach was "
                          "connected to the loss."),
    WARRANTY: ("Breach may suspend or discharge the insurer's liability for the affected "
               "cover from the date of breach."),
    CONTINUING_CONDITION: ("Breach may reduce or defeat a claim affected by the "
                           "non-compliance."),
}

# ── compliance states ────────────────────────────────────────────────────────
BREACHED = "BREACHED"
COMPLIANT = "COMPLIANT"
UNVERIFIED = "UNVERIFIED"
NOT_IN_POLICY = "NOT_IN_POLICY"

_STATE_SEVERITY = {BREACHED: "CRITICAL", UNVERIFIED: "MODERATE",
                   COMPLIANT: "LOW", NOT_IN_POLICY: "NONE"}
_STATE_ORDER = {BREACHED: 0, UNVERIFIED: 1, COMPLIANT: 2, NOT_IN_POLICY: 3}


@dataclass(frozen=True)
class ConditionSpec:
    """One obligation we know how to look for.

    `needles` are searched verbatim in the customer's own base wording, so an entry only
    enters the register if the obligation is genuinely in THEIR policy. `checker` is set
    only where the engine can decide compliance deterministically from facts on record;
    everything else is honestly UNVERIFIED.
    """

    id: str
    kind: str
    title: str
    why_it_matters: str
    needles: tuple[str, ...]
    verified_by: tuple[str, ...]
    checker: Optional[str] = None


# The catalogue. These are the obligations that actually defeat SME commercial claims.
# Deliberately not exhaustive: an obligation we cannot look for honestly is worse than one
# we omit and say we omitted.
CATALOGUE: tuple[ConditionSpec, ...] = (
    ConditionSpec(
        id="PREMIUM_PAYMENT",
        kind=CONDITION_PRECEDENT,
        title="Premium payment period",
        why_it_matters=("Cover can terminate automatically if premium is not received "
                        "within the stated period, leaving the business uninsured while "
                        "it believes it is covered."),
        needles=("automatically terminated", "premium payment", "premium warranty"),
        verified_by=("POLICY_SCHEDULE", "PREMIUM_RECEIPT"),
        checker="premium_payment",
    ),
    ConditionSpec(
        id="PREMISES_DECLARED",
        kind=CONTINUING_CONDITION,
        title="All occupied premises declared",
        why_it_matters=("Cover attaches to the premises on the schedule. Trading from an "
                        "undeclared unit leaves that unit, and the stock and plant in "
                        "it, outside the policy."),
        needles=("alteration", "material alteration", "change in the risk"),
        verified_by=("POLICY_SCHEDULE", "TENANCY"),
        checker="premises_declared",
    ),
    ConditionSpec(
        id="CLAIM_NOTIFICATION",
        kind=CONDITION_PRECEDENT,
        title="Claim notification period",
        why_it_matters=("Late notification is one of the most common grounds for "
                        "declinature, and it is usually a condition precedent — so it "
                        "can defeat an otherwise valid claim on timing alone."),
        needles=("as soon as reasonably practicable", "immediate written notice",
                 "give notice in writing", "written notice", "notify"),
        verified_by=("CLAIMS_CORRESPONDENCE",),
    ),
    ConditionSpec(
        id="REASONABLE_PRECAUTIONS",
        kind=CONDITION_PRECEDENT,
        title="Reasonable precautions to prevent loss",
        why_it_matters=("A general obligation to take reasonable care. Broad, frequently "
                        "a condition precedent, and assessed after the loss with "
                        "hindsight."),
        needles=("reasonable precautions", "reasonable care to prevent",
                 "take all reasonable"),
        verified_by=("RISK_SURVEY", "MAINTENANCE_RECORDS"),
    ),
    ConditionSpec(
        id="PROTECTIONS_MAINTAINED",
        kind=WARRANTY,
        title="Security protections maintained and set",
        why_it_matters=("Alarms and locks specified in the policy must be maintained "
                        "under contract and set when the premises are closed. A lapsed "
                        "maintenance contract is a breach even if the alarm works."),
        needles=("intruder alarm", "alarm system", "security protections",
                 "protections specified", "burglar alarm"),
        verified_by=("ALARM_MAINTENANCE_CONTRACT", "SERVICE_CERTIFICATE"),
    ),
    ConditionSpec(
        id="SPRINKLER_MAINTAINED",
        kind=WARRANTY,
        title="Sprinkler and fire system certification",
        why_it_matters=("Where the policy recognises a sprinkler or suppression system, "
                        "it normally requires periodic certification. An out-of-date "
                        "certificate is a breach of warranty."),
        needles=("sprinkler leakage", "automatic sprinkler", "sprinkler installation",
                 "fire extinguishing", "sprinkler"),
        verified_by=("SPRINKLER_CERTIFICATE", "FIRE_SAFETY_CERTIFICATE"),
    ),
    ConditionSpec(
        id="HOT_WORKS",
        kind=CONDITION_PRECEDENT,
        title="Hot works permit",
        why_it_matters=("Welding, cutting and soldering usually require a written permit "
                        "procedure. Hot works without a permit is a classic total "
                        "declinature in food and manufacturing premises."),
        needles=("hot work", "blow lamp", "welding or cutting"),
        verified_by=("HOT_WORKS_PERMITS",),
    ),
    ConditionSpec(
        id="WASTE_REMOVAL",
        kind=CONTINUING_CONDITION,
        title="Waste and combustible material removal",
        why_it_matters=("Accumulated packaging and waste is both a fire cause and a "
                        "breach of a housekeeping condition. Acute in food production."),
        needles=("combustible material", "removal of waste", "waste material",
                 "rubbish", "waste"),
        verified_by=("WASTE_CONTRACT", "RISK_SURVEY"),
    ),
    ConditionSpec(
        id="ELECTRICAL_INSPECTION",
        kind=WARRANTY,
        title="Electrical installation inspection",
        why_it_matters=("Periodic inspection of the fixed electrical installation is "
                        "commonly required. Electrical fault is a leading cause of "
                        "commercial fire, which is exactly why insurers warrant it."),
        needles=("electrical installation", "electrical inspection",
                 "electrical wiring", "electrical"),
        verified_by=("ELECTRICAL_INSPECTION_CERTIFICATE",),
    ),
    ConditionSpec(
        id="UNOCCUPANCY",
        kind=CONTINUING_CONDITION,
        title="Unoccupancy notification",
        why_it_matters=("Premises left unoccupied beyond a stated period usually trigger "
                        "notification and restricted cover — often removing escape of "
                        "water and malicious damage."),
        needles=("unoccupied", "unoccupancy", "left empty"),
        verified_by=("POLICY_SCHEDULE", "OWNER_DECLARATION"),
    ),
)


@dataclass
class ConditionState:
    """One obligation, its state, and what proves it."""

    id: str
    kind: str
    title: str
    why_it_matters: str
    state: str
    severity: str
    detail: str
    consequence: str
    verified_by: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    clause_located: bool = False
    clause_quote: Optional[str] = None
    clause_page: Optional[int] = None
    matched_phrase: Optional[str] = None
    needles_searched: list = field(default_factory=list)
    phrases_set_aside: list = field(default_factory=list)
    # Quote, link and status of a jurisdiction-pack figure the detail states.
    pack_sources: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "why_it_matters": self.why_it_matters, "state": self.state,
            "severity": self.severity, "detail": self.detail,
            "consequence": self.consequence,
            "verified_by": self.verified_by, "evidence": self.evidence,
            "clause_located": self.clause_located,
            "clause_quote": self.clause_quote, "clause_page": self.clause_page,
            # Which phrase matched, and everything that was tried. A located obligation
            # means the phrase appears on that page — not that a lawyer has confirmed the
            # clause creates the obligation. Publishing the phrase and the page lets a
            # reader check that judgement instead of taking it on trust.
            "matched_phrase": self.matched_phrase,
            "needles_searched": self.needles_searched,
            # Phrases that were located but judged not to create an obligation, each with
            # the excerpt and the reason. Published so a reader can overrule the judgement.
            "phrases_set_aside": self.phrases_set_aside,
            **({"pack_sources": self.pack_sources} if self.pack_sources else {}),
        }


_CONTEXT_CHARS = 200

# ── the obligation guard ─────────────────────────────────────────────────────
# A keyword appearing in a wording does not make it an obligation. Measured on the real
# Chubb Select+ wording, bare-word matching produced three confident false positives:
# "electrical" hit "mechanical or electrical breakdown" inside an EXCLUSION list,
# "sprinkler" hit the 2.4 Sprinkler Leakage EXTENSION (a grant of cover, not a duty), and
# "waste" hit the DEFINITION of Pollutants. Reporting any of those as "an obligation in
# your policy that we cannot show is met" would be exactly the invented finding this system
# exists to refuse.
#
# Real obligations in commercial wordings are phrased as duties. So a located phrase only
# enters the register as an obligation if duty language sits near it. This is a deliberate
# trade: it will miss obligations phrased unusually, and missing one is recoverable while
# inventing one is not.
_OBLIGATION_LANGUAGE = (
    "shall", "must", "the insured is required", "it is a condition",
    "condition precedent", "warranted", "it is warranted", "undertakes to",
    "obliged to", "duty to", "shall not", "is required to",
)

# Language that marks the surrounding text as something OTHER than a duty. If these
# dominate, the match is a definition, an exclusion or a grant of cover.
_NON_OBLIGATION_MARKERS = ("means ", "shall mean", "is defined as", "definitions")


def _reads_as_obligation(context: Optional[str]) -> tuple[bool, str]:
    """Does this excerpt read as a duty on the insured, or as definition/exclusion text?

    Returns (verdict, reason) so the register can publish WHY something was admitted or
    set aside, rather than silently filtering.
    """
    if not context:
        return False, "No surrounding text could be read, so the match cannot be judged."
    low = context.lower()
    duty = [p for p in _OBLIGATION_LANGUAGE if p in low]
    non_duty = [p for p in _NON_OBLIGATION_MARKERS if p in low]
    if not duty:
        return False, ("The phrase appears here but no duty language ('shall', 'must', "
                       "'condition precedent', 'warranted') sits near it, so this reads "
                       "as descriptive text rather than an obligation.")
    if non_duty and not any(p in low for p in ("shall ", "must ")):
        return False, (f"The phrase appears inside definition or exclusion text "
                       f"({', '.join(non_duty)}), not an obligation on the insured.")
    return True, (f"Duty language present near the match ({', '.join(duty[:3])}), so this "
                  f"reads as an obligation on the insured.")


def _clause_context(conn, document_id: str, span) -> Optional[str]:
    """The sentence around a located phrase, pulled from the immutable stored page.

    A register entry that says only "matched the word waste on page 5" is not checkable —
    the reader cannot tell a housekeeping condition from an incidental mention. Showing the
    surrounding text is what makes the match auditable instead of asking for trust. This is
    the same principle as every other quote in the system: publish the evidence, do not
    summarise it.
    """
    if conn is None or span is None:
        return None
    try:
        text = ingest.page_text(conn, document_id, span.page_no) or ""
    except Exception:
        return None
    if not text:
        return None
    start = max(0, int(span.char_start) - _CONTEXT_CHARS)
    end = min(len(text), int(span.char_end) + _CONTEXT_CHARS)
    excerpt = " ".join(text[start:end].split())
    if not excerpt:
        return None
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"


def _operand(facts, name: str) -> Optional[dict]:
    """Provenance for one established fact, or None. Same shape as scenario.py."""
    if facts is None:
        return None
    fact = facts.get(name)
    if not getattr(fact, "established", False):
        return None
    return {
        "operand": name,
        "value": str(fact.value),
        "document_kind": fact.doc.kind if fact.doc else None,
        "document_id": getattr(fact.doc, "id", None),
        "page": fact.page_no,
        "quote": fact.needle,
        "author_class": fact.author_class,
    }


# ── deterministic checkers ───────────────────────────────────────────────────
# Each returns (state, detail, evidence). A checker that cannot decide returns
# UNVERIFIED — it never guesses toward COMPLIANT, because a false clean bill of health is
# the one output that actively harms the customer.
#
# Each takes the jurisdiction pack the case is read under. A checker called without one
# reads under GENERIC: no figure from any country is assumed.

def _premium_period_entry(pack: Optional["jurisdiction.Pack"]
                          ) -> Optional["jurisdiction.Entry"]:
    entry = (pack or jurisdiction.generic()).find("MC-PREMIUM-PAYMENT-WARRANTY")
    if entry is None or not entry.owner_facing or entry.values.get("days") is None:
        return None
    return entry


def _premium_period_days(pack: Optional["jurisdiction.Pack"]) -> Optional[int]:
    """The premium payment period the pack verifies, or None.

    This was a module constant, 60 — the period the Singapore wording the engine was
    measured against states — applied to every wording from anywhere. It now lives in
    jurisdictions/SG.yaml with its quote, and a pack that does not verify one gets no
    comparison rather than a borrowed number.
    """
    entry = _premium_period_entry(pack)
    return None if entry is None else int(entry.values["days"])


def _check_premium_payment(facts, today: Optional[date],
                           pack: Optional["jurisdiction.Pack"] = None) -> tuple:
    inc = _operand(facts, "inception")
    rec = _operand(facts, "premium_received")
    if inc is None:
        return (UNVERIFIED,
                "Policy inception is not on record, so the premium payment period "
                "cannot be measured.", [])
    if rec is None:
        return (UNVERIFIED,
                "No premium receipt date is on record. Until one is supplied the engine "
                "cannot say whether the payment condition was met — and this is the "
                "condition that can terminate cover without anyone being told.",
                [inc])
    try:
        inception = date.fromisoformat(inc["value"])
        received = date.fromisoformat(rec["value"])
    except (ValueError, TypeError):
        return (UNVERIFIED,
                "Inception or receipt date could not be read as a date. Abstaining "
                "rather than assuming compliance.", [inc, rec])
    days = (received - inception).days
    period = _premium_period_days(pack)
    if period is None:
        return (UNVERIFIED,
                (f"Premium was received on day {days} of the policy period. The premium "
                 f"payment clause is in this wording, but these documents are not read "
                 f"under a jurisdiction pack that verifies a payment period, so no "
                 f"period is assumed. Read the period the clause itself states and "
                 f"compare it with day {days}."),
                [inc, rec])
    if days > period:
        return (BREACHED,
                (f"Premium was received on day {days} of the policy period against a "
                 f"{period}-day condition. On wordings where payment "
                 f"within the period is a condition precedent, cover can terminate "
                 f"automatically — meaning the business may have been trading uninsured "
                 f"for {days - period} days before anyone noticed. "
                 f"Confirm the position with the insurer in writing."),
                [inc, rec])
    return (COMPLIANT,
            (f"Premium received on day {days}, within the "
             f"{period}-day condition."),
            [inc, rec])


def _check_premises_declared(facts, today: Optional[date],
                             pack: Optional["jurisdiction.Pack"] = None) -> tuple:
    sch = _operand(facts, "scheduled_premises")
    ten = _operand(facts, "demised_premises")
    if sch is None or ten is None:
        missing = "the policy schedule" if sch is None else "the tenancy agreement"
        return (UNVERIFIED,
                (f"Cannot compare occupied premises against insured premises without "
                 f"{missing}. Supply it and this becomes a one-document answer."),
                [p for p in (sch, ten) if p])
    scheduled = {str(a).split(",")[0].strip().lower()
                 for a in _as_list(sch["value"])}
    demised = [str(d).strip() for d in _as_list(ten["value"])]
    missing_units = [d for d in demised
                     if not any(d.split(",")[0].strip().lower() in s
                                or s in d.lower() for s in scheduled)]
    if missing_units:
        return (BREACHED,
                (f"{len(missing_units)} occupied premises is not on the policy "
                 f"schedule: {', '.join(missing_units)}. Property at an undeclared "
                 f"location is outside the cover, and the alteration condition may be "
                 f"engaged for the policy as a whole."),
                [sch, ten])
    return (COMPLIANT,
            "Every premises in the tenancy agreement appears on the policy schedule.",
            [sch, ten])


def _as_list(value) -> list:
    """Facts store lists natively but provenance stringifies them. Accept either.

    Named to make clear it is a coercion helper and involves no evaluation of input.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1]
        parts = [p.strip().strip("'\"") for p in inner.split("',")]
        return [p.strip().strip("'\"") for p in parts if p.strip()]
    return [text] if text else []


_CHECKERS: dict[str, Callable] = {
    "premium_payment": _check_premium_payment,
    "premises_declared": _check_premises_declared,
}


def register(case: dict, *, today: Optional[date] = None) -> dict:
    """Build the warranty and condition-precedent compliance register for one case.

    Each catalogue obligation is searched for in the customer's OWN base wording. Only
    obligations actually located there enter the register as live obligations; the rest
    are reported as NOT_IN_POLICY so the reader can see what was looked for and not
    found. Where a deterministic checker exists it decides the state from facts on
    record; otherwise the state is UNVERIFIED with the resolving document named.
    """
    facts = case.get("facts") if isinstance(case, dict) else case
    conn = case.get("conn") if isinstance(case, dict) else None
    base_doc_id = case.get("base_doc_id") if isinstance(case, dict) else None
    pack = jurisdiction.pack_of(case)

    states: list[ConditionState] = []
    for spec in CATALOGUE:
        # Locate the obligation in the customer's own wording.
        # Try each phrase in turn and accept the first that both locates AND reads as a
        # duty. A phrase that appears only in definition or exclusion text is set aside
        # WITH ITS REASON rather than silently dropped, so the reader can see that we
        # looked, found the word, and declined to call it an obligation.
        span = None
        matched = None
        quote = None
        set_aside: list[dict] = []
        if conn is not None and base_doc_id:
            for needle in spec.needles:
                try:
                    candidate = account.find_clause_span(conn, base_doc_id, needle)
                except Exception:
                    candidate = None
                if candidate is None:
                    continue
                context = _clause_context(conn, base_doc_id, candidate)
                ok, reason = _reads_as_obligation(context)
                if ok:
                    span, matched, quote = candidate, needle, context
                    break
                set_aside.append({"phrase": needle, "page": candidate.page_no,
                                  "reason": reason,
                                  "excerpt": (context or "")[:260]})

        located = span is not None
        page = getattr(span, "page_no", None) if located else None

        if not located:
            if set_aside:
                detail = (
                    "The wording contains this phrase but not as an obligation on the "
                    "insured. "
                    + set_aside[0]["reason"]
                    + " Reported rather than dropped, because a reader deserves to know "
                      "the difference between 'not in your policy' and 'we found the "
                      "word and judged it was not a duty'.")
            else:
                detail = ("Searched this wording for the obligation and did not find it. "
                          "Reported so the reader can see what was checked, not only "
                          "what was found.")
            states.append(ConditionState(
                id=spec.id, kind=spec.kind, title=spec.title,
                why_it_matters=spec.why_it_matters,
                state=NOT_IN_POLICY, severity=_STATE_SEVERITY[NOT_IN_POLICY],
                detail=detail,
                consequence="Not applicable — the obligation is not in this wording.",
                verified_by=list(spec.verified_by), evidence=[],
                clause_located=False,
                needles_searched=list(spec.needles),
                phrases_set_aside=set_aside))
            continue

        checker = _CHECKERS.get(spec.checker) if spec.checker else None
        if checker is None:
            state, detail, evidence = (
                UNVERIFIED,
                ("This obligation is in the policy and nothing on record shows whether "
                 "it is met. That is not a clean bill of health — it is an open "
                 "exposure. Supply "
                 + " or ".join(k.replace("_", " ").lower()
                               for k in spec.verified_by)
                 + " to close it."),
                [])
        else:
            state, detail, evidence = checker(facts, today, pack)

        # The premium period, where one was compared against, is a pack figure: show its
        # quote and source with it.
        sources: list = []
        if spec.checker == "premium_payment" and state in (BREACHED, COMPLIANT):
            entry = _premium_period_entry(pack)
            if entry is not None:
                sources = [entry.provenance()]

        states.append(ConditionState(
            id=spec.id, kind=spec.kind, title=spec.title,
            why_it_matters=spec.why_it_matters,
            state=state, severity=_STATE_SEVERITY[state], detail=detail,
            consequence=_KIND_CONSEQUENCE[spec.kind],
            verified_by=list(spec.verified_by),
            evidence=[e for e in evidence if e],
            clause_located=True,
            clause_quote=(quote[:520] if isinstance(quote, str) else None),
            clause_page=page,
            matched_phrase=matched,
            needles_searched=list(spec.needles),
            pack_sources=sources))

    states.sort(key=lambda s: (_STATE_ORDER[s.state], s.id))

    counts = {st: sum(1 for s in states if s.state == st)
              for st in (BREACHED, UNVERIFIED, COMPLIANT, NOT_IN_POLICY)}
    in_policy = [s for s in states if s.state != NOT_IN_POLICY]
    breached_cp = [s for s in states
                   if s.state == BREACHED and s.kind == CONDITION_PRECEDENT]

    if counts[BREACHED]:
        headline = (f"{counts[BREACHED]} of {len(in_policy)} obligations in this policy "
                    f"is in breach." if counts[BREACHED] == 1 else
                    f"{counts[BREACHED]} of {len(in_policy)} obligations in this policy "
                    f"are in breach.")
        worst = "CRITICAL"
    elif counts[UNVERIFIED]:
        headline = (f"{counts[UNVERIFIED]} of {len(in_policy)} obligations in this policy "
                    f"cannot be shown to be met.")
        worst = "MODERATE"
    elif in_policy:
        headline = "Every obligation located in this policy is evidenced as met."
        worst = "LOW"
    else:
        headline = "No warranties or conditions precedent were located in this wording."
        worst = "NONE"

    return {
        "available": True,
        "headline": headline,
        "highest_severity": worst,
        "counts": counts,
        "obligations_in_policy": len(in_policy),
        "conditions_precedent_breached": len(breached_cp),
        "register": [s.as_dict() for s in states],
        "why_this_matters": {
            "note": ("Cover responding to a loss is not the same as a claim being paid. "
                     "Warranties and conditions precedent sit between the two."),
            "citations": [
                {"source": "Pinsent Masons, on conditions precedent under the Insurance "
                           "Act 2015",
                 "finding": ("Where a condition precedent to liability is not satisfied "
                             "and the policy does not spell out a remedy, common law "
                             "principles apply and the insurer may be entitled to "
                             "decline the claim entirely.")},
                {"source": "DLA Piper, Practical Guide for Claims Managers",
                 "finding": ("For some conditions precedent, insurers may not have to "
                             "pay the claim regardless of whether the breach had any "
                             "causal connection to the loss.")},
            ],
            "market_position": ("A search of the market found law-firm guidance on the "
                                "principle and no product that tracks compliance for an "
                                "in-force SME policy."),
        },
        "discipline": ("UNVERIFIED is the default and it is deliberate. COMPLIANT "
                       "requires evidence of compliance; silence is not compliance. "
                       "BREACHED requires evidence of the breach and is never inferred "
                       "from absence."),
        "disclaimer": DISCLAIMER,
    }


def as_external_signals(reg: dict) -> list[dict]:
    """Convert breaches and unverified conditions into orchestrator signals.

    Scope is RISK_SIGNAL throughout, including for breaches. A date arithmetic that fails
    is documented fact, but the CONSEQUENCE — that cover is void — is a legal
    determination this engine is not entitled to make. So these raise attention and
    create a review, and never assert that cover has failed.
    """
    if not reg.get("available"):
        return []
    signals = []
    for entry in reg.get("register", []):
        if entry["state"] not in (BREACHED, UNVERIFIED):
            continue
        # Only breaches and unverified CONDITIONS PRECEDENT are escalated. Unverified
        # continuing conditions are noise at this stage and would drown the register.
        if entry["state"] == UNVERIFIED and entry["kind"] != CONDITION_PRECEDENT:
            continue
        signals.append({
            "id": f"warranty-{entry['id'].lower().replace('_', '-')}",
            "agent": "warranty",
            "signal_type": f"{entry['kind']}_{entry['state']}",
            "label": f"{entry['title']}: {entry['state'].lower()}",
            "detail": entry["detail"],
            "severity": "HIGH" if entry["state"] == BREACHED else "MODERATE",
            "claim_scope": "RISK_SIGNAL",
            "insured_interests": ["premises", "stock"],
            "simulated": False,
            "source": {"name": "Policy wording and documents on record",
                       "is_live": False, "reliability": "AUTHORITATIVE"},
            "asserts": ("An obligation in the policy and what the record shows about it. "
                        "It does NOT assert that cover is void, which is a legal "
                        "determination."),
        })
    return signals
