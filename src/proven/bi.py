"""VERIS BI — business interruption adequacy, the gap the industry itself names.

WHY THIS MODULE EXISTS. Property cover replaces the things you lost. Business
interruption cover replaces the trading you could not do while you replaced them. SMEs buy
the first and skip or under-buy the second, and it is the second that decides whether the
business is still there in a year.

This is not our opinion. It is the industry's own published position:

  * The Chartered Institute of Loss Adjusters, as reported in an insurer's published SME
    guidance, found that up to 43% of business interruption policies were underinsured,
    by an average of 53%.
  * The ABI's "Small Business, Big Risk" research (January 2026, conducted by Public
    First across 1,002 SME owners) found 48% of respondents had a physical premises — and
    of those, only 23% carried business interruption insurance.
  * Sedgwick, reporting UK government research, notes that one in five businesses suffer a
    major disruption each year, and around 80% of those affected close within 18 months.

So the single most valuable question this product can ask an SME is not "is your stock
sum insured high enough". It is "if you could not trade for nine months, what happens".

TWO FINDINGS, AND ONLY ONE OF THEM NEEDS ARITHMETIC.

  1. ABSENCE. Whether a business interruption section exists at all is a structural fact
     readable straight off the schedule's operative sections. It needs no accounting data,
     no forecast and no assumption. If the business trades from a premises and carries no
     BI section, that is provable from one document, and it is the highest-severity thing
     this module can say.

  2. ADEQUACY. Whether an existing BI sum insured is big enough requires gross profit and
     an indemnity period. If either is not on record the engine ABSTAINS and names the
     document that would resolve it. It does not estimate gross profit from a balance
     sheet, because a balance sheet does not contain one.

WHY THE INDEMNITY PERIOD IS ITS OWN FINDING. The sum insured and the indemnity period fail
independently, and the period is the one nobody checks. Two documented reasons it is
usually too short:

  * The sum insured must be built on FUTURE gross profit over the whole indemnity period,
    not last year's. Where a business grows faster than the forecast, average applies to
    the claim however carefully the forecast was prepared (broker-negligence case law).
  * Policyholders routinely select a shorter indemnity period to reduce premium, when 24
    months is the more realistic period to return a damaged business to its prior trading
    level. Swiss Re puts the consequence plainly: once the indemnity period has elapsed,
    further loss of profit is simply not insured.

DECLARED ASSUMPTIONS, LABELLED AS SUCH. The sector recovery benchmarks below are a
DECLARED planning assumption, not a measured Singapore statistic — no such published
series exists. They are used only to raise a question about the indemnity period, never to
compute a sum insured, and every output that leans on them says so. Inventing a precision
we do not have would be the same failure this whole system exists to refuse.

WHAT THIS IS NOT. A deterministic illustration and a prompt to get advice. Whether cover
responds, and what sum insured to actually buy, is a licensed adviser's determination.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

DISCLAIMER = ("Deterministic illustration — not a quotation and not advice on what to "
              "buy. Sizing a business interruption sum insured is a licensed adviser's "
              "determination.")

# ── how a BI section identifies itself across wordings ───────────────────────
# Carriers name this section differently. Matching on a set of names rather than one
# literal is what stops "no BI cover" being reported for a policy that calls it
# consequential loss.
BI_SECTION_NAMES = (
    "BUSINESS_INTERRUPTION", "BUSINESS INTERRUPTION",
    "CONSEQUENTIAL_LOSS", "CONSEQUENTIAL LOSS",
    "LOSS_OF_PROFITS", "LOSS OF PROFITS",
    "PROFITS", "REVENUE", "GROSS_PROFIT", "GROSS PROFIT",
    "BUSINESS_INCOME", "BUSINESS INCOME",
    "TIME_ELEMENT", "TIME ELEMENT",
)

# Sections that establish the business trades from a place that can be damaged. Without
# one of these, absence of BI cover is not necessarily a gap.
_PHYSICAL_SECTIONS = ("PROPERTY", "MATERIAL_DAMAGE", "MATERIAL DAMAGE",
                      "FIRE", "ALL_RISKS", "ALL RISKS")

# ── declared planning assumption, NOT a measured statistic ───────────────────
# Indicative months to return a damaged business to prior trading level, by broad
# activity. Basis: the widely-advised 24-month planning horizon for premises-dependent
# manufacturing and food production, scaled down for activities that can relocate or
# trade remotely. USED ONLY to question an indemnity period. NEVER to size a sum insured.
SECTOR_RECOVERY_MONTHS = (
    (("manufactur", "food", "production", "factory", "processing", "bakery",
      "sauces", "prepared meals"), 18,
     "Premises-dependent production: plant reinstatement, regulatory re-approval and "
     "customer re-qualification all sit on the critical path."),
    (("restaurant", "cafe", "f&b", "food and beverage", "catering", "hawker"), 12,
     "Fit-out and licensing dominate the rebuild; trade returns only once reopened."),
    (("warehouse", "logistics", "storage", "freight", "haulage"), 12,
     "Stock and racking replacement plus alternative-site transition."),
    (("retail", "shop", "store", "wholesale", "trade"), 12,
     "Fit-out and stock replacement, with customer habits to rebuild."),
    (("office", "consult", "service", "agency", "software", "profession"), 6,
     "Largely relocatable; recovery is driven by systems and staff, not structure."),
)
DEFAULT_RECOVERY_MONTHS = 12
_RECOVERY_BASIS = ("DECLARED planning assumption, not a measured Singapore statistic. "
                   "Used to question an indemnity period, never to size a sum insured.")

# The shortest indemnity period a premises-dependent business is commonly advised to
# consider. Below this we always raise the question.
_MIN_ADVISED_MONTHS = 12

_TWO = Decimal("0.01")


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(_TWO, rounding=ROUND_HALF_UP)


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


@dataclass
class BIFinding:
    """One business-interruption finding, with what proves it."""

    key: str                 # NO_BI_COVER | BI_SUM_INSURED_SHORT | INDEMNITY_TOO_SHORT
    severity: str            # HIGH | MODERATE | LOW
    headline: str
    detail: str
    basis: str               # PROVEN_FROM_DOCUMENT | COMPUTED | DECLARED_ASSUMPTION
    operands: list = field(default_factory=list)
    numbers: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"key": self.key, "severity": self.severity, "headline": self.headline,
                "detail": self.detail, "basis": self.basis,
                "operands": self.operands, "numbers": self.numbers}


def recovery_benchmark(business_description: Optional[str]) -> tuple[int, str]:
    """Indicative months to recover, plus why. A DECLARED assumption — see module docs."""
    desc = (business_description or "").lower()
    for needles, months, rationale in SECTOR_RECOVERY_MONTHS:
        if any(n in desc for n in needles):
            return months, rationale
    return DEFAULT_RECOVERY_MONTHS, ("No activity match; the general default is used.")


def required_sum_insured(annual_gross_profit: Decimal,
                         indemnity_months: int,
                         growth_pct: Optional[Decimal] = None) -> dict:
    """Gross profit that must be insured to carry an interruption of `indemnity_months`.

    The sum insured has to cover the WHOLE indemnity period, not one year, and it must be
    built on forward-looking gross profit. Where a growth rate is supplied it is applied,
    because a policy sized on last year's figure is already short on the day it starts.

        required = annual_gross_profit × (indemnity_months / 12) × (1 + growth)
    """
    if annual_gross_profit <= 0:
        raise ValueError("annual gross profit must be positive")
    if indemnity_months <= 0:
        raise ValueError("indemnity period must be a positive number of months")
    periods = Decimal(indemnity_months) / Decimal(12)
    uplift = Decimal(1) + (growth_pct / Decimal(100) if growth_pct else Decimal(0))
    required = _q(annual_gross_profit * periods * uplift)
    return {
        "annual_gross_profit": str(_q(annual_gross_profit)),
        "indemnity_months": indemnity_months,
        "period_multiple": str(periods.quantize(Decimal("0.001"))),
        "growth_pct": None if growth_pct is None else str(growth_pct),
        "growth_uplift": str(uplift.quantize(Decimal("0.001"))),
        "required_sum_insured": str(required),
        "operation": (f"{_q(annual_gross_profit)} × {periods.quantize(Decimal('0.001'))} "
                      f"({indemnity_months} months ÷ 12)"
                      + (f" × {uplift.quantize(Decimal('0.001'))} (growth "
                         f"{growth_pct}%)" if growth_pct else "")),
    }


def assess(case: dict) -> dict:
    """Assess business interruption cover for one case.

    Returns absence findings (provable from the schedule alone) and, where the accounting
    evidence supports it, adequacy findings. Abstains explicitly on anything it cannot
    establish, naming the document that would resolve it.
    """
    facts = case.get("facts") if isinstance(case, dict) else case

    sections_prov = _operand(facts, "operative_sections")
    desc_prov = _operand(facts, "business_description")
    premises_prov = _operand(facts, "scheduled_premises")
    bi_si_prov = _operand(facts, "bi_sum_insured")
    indemnity_prov = _operand(facts, "indemnity_period_months")
    gp_prov = _operand(facts, "gross_profit")
    turnover_prov = _operand(facts, "turnover")

    findings: list[BIFinding] = []
    abstentions: list[dict] = []

    # ── can we even tell whether BI cover exists? ───────────────────────────
    if sections_prov is None:
        return {
            "available": True,
            "abstained": True,
            "cover_present": None,
            "message": "Cannot determine business interruption cover from the evidence "
                       "provided.",
            "reason": ("The policy schedule's operative sections are not on record, so "
                       "the engine cannot say whether a business interruption section "
                       "exists. Supply the policy schedule and this becomes a "
                       "one-document answer."),
            "missing": ["operative_sections"],
            "findings": [], "abstentions": [], "disclaimer": DISCLAIMER,
        }

    sections = sections_prov["value"]
    sections_upper = str(sections).upper()
    cover_present = any(name in sections_upper for name in BI_SECTION_NAMES)
    physical = any(name in sections_upper for name in _PHYSICAL_SECTIONS)

    months, rationale = recovery_benchmark(
        desc_prov["value"] if desc_prov else None)

    # ── FINDING 1 — absence. Provable from one document. ────────────────────
    if not cover_present:
        if physical:
            findings.append(BIFinding(
                key="NO_BI_COVER",
                severity="HIGH",
                headline=("No business interruption cover on this policy, for a business "
                          "that trades from insured premises."),
                detail=(
                    "The schedule's operative sections are "
                    f"{sections}. None of them is a business interruption, "
                    "consequential loss or loss-of-profits section, while the policy "
                    "does insure physical property. Damage to the premises would "
                    "therefore be met on the property section, and the trading lost "
                    "while the premises were unusable would not be met at all. On the "
                    "declared activity, returning to prior trading would take an "
                    f"estimated {months} months, none of it insured."),
                basis="PROVEN_FROM_DOCUMENT",
                operands=[p for p in (sections_prov, premises_prov, desc_prov) if p],
                numbers={"uninsured_recovery_months": months,
                         "recovery_basis": _RECOVERY_BASIS,
                         "recovery_rationale": rationale},
            ))
        else:
            findings.append(BIFinding(
                key="NO_BI_COVER",
                severity="LOW",
                headline="No business interruption section, and no property section "
                        "either.",
                detail=("Neither a business interruption section nor a physical property "
                        "section is operative, so the absence of BI cover may be "
                        "deliberate rather than a gap. Confirm with the adviser whether "
                        "the business is premises-dependent."),
                basis="PROVEN_FROM_DOCUMENT",
                operands=[sections_prov],
                numbers={},
            ))

        # Absence closes the adequacy question — there is no sum insured to test.
        abstentions.append({
            "question": "Is the business interruption sum insured adequate?",
            "abstained": True,
            "reason": ("There is no business interruption sum insured to test. The "
                       "finding is the absence of the section, not the size of a figure."),
            "resolved_by": [],
        })
        return _result(cover_present, findings, abstentions, months, rationale,
                       sections_prov, desc_prov,
                       {"bi_sum_insured": bi_si_prov,
                        "indemnity_period_months": indemnity_prov,
                        "gross_profit": gp_prov, "turnover": turnover_prov})

    # ── cover exists. Now the two independent adequacy questions. ───────────

    # FINDING 2 — indemnity period against the recovery benchmark.
    if indemnity_prov is not None:
        try:
            chosen = int(Decimal(indemnity_prov["value"]))
        except Exception:
            chosen = None
        if chosen is None:
            abstentions.append({
                "question": "Is the indemnity period long enough?",
                "abstained": True,
                "reason": ("The indemnity period on the schedule could not be read as a "
                           "number of months. Abstaining rather than assuming 12."),
                "resolved_by": ["POLICY_SCHEDULE"],
            })
        elif chosen < months or chosen < _MIN_ADVISED_MONTHS:
            findings.append(BIFinding(
                key="INDEMNITY_TOO_SHORT",
                severity="HIGH" if chosen < months else "MODERATE",
                headline=(f"Indemnity period is {chosen} months against an estimated "
                          f"{months} months to recover."),
                detail=(
                    f"The policy indemnifies lost gross profit for {chosen} months. On "
                    f"the declared activity the estimated time to return to prior "
                    f"trading is {months} months. Once the indemnity period elapses, "
                    "further loss of profit is not insured — the shortfall is not "
                    "proportional, it is a hard stop. Lengthening the indemnity period "
                    "is usually a smaller premium change than the exposure it removes."),
                basis="DECLARED_ASSUMPTION",
                operands=[p for p in (indemnity_prov, desc_prov) if p],
                numbers={"chosen_months": chosen,
                         "benchmark_months": months,
                         "uninsured_months": max(0, months - chosen),
                         "recovery_basis": _RECOVERY_BASIS,
                         "recovery_rationale": rationale},
            ))
    else:
        abstentions.append({
            "question": "Is the indemnity period long enough?",
            "abstained": True,
            "reason": ("No indemnity period is on record. It is the most commonly "
                       "under-set term in a BI section and cannot be inferred, so the "
                       "engine abstains."),
            "resolved_by": ["POLICY_SCHEDULE"],
        })

    # FINDING 3 — sum insured against forward gross profit.
    if bi_si_prov is None:
        abstentions.append({
            "question": "Is the business interruption sum insured adequate?",
            "abstained": True,
            "reason": ("A business interruption section is operative but no sum insured "
                       "for it is on record."),
            "resolved_by": ["POLICY_SCHEDULE"],
        })
    elif gp_prov is None:
        abstentions.append({
            "question": "Is the business interruption sum insured adequate?",
            "abstained": True,
            "reason": (
                "Sizing a BI sum insured needs annual gross profit, which comes from a "
                "profit and loss statement. A balance sheet does not contain one and the "
                "engine will not derive it from stock, receivables or cash. Supply a P&L "
                "or management accounts showing gross profit and this becomes a measured "
                "figure."),
            "resolved_by": ["PROFIT_AND_LOSS", "MANAGEMENT_ACCOUNTS"],
            "note_on_turnover": (
                "Turnover is on record but gross profit is not. They are not "
                "interchangeable: BI insures gross profit, and using turnover would "
                "overstate the requirement."
                if turnover_prov else None),
        })
    else:
        chosen_months = None
        if indemnity_prov is not None:
            try:
                chosen_months = int(Decimal(indemnity_prov["value"]))
            except Exception:
                chosen_months = None
        # Without a stated period we cannot size the requirement. Abstain rather than
        # silently assuming twelve months, which is exactly the assumption that makes
        # real policies short.
        if chosen_months is None:
            abstentions.append({
                "question": "Is the business interruption sum insured adequate?",
                "abstained": True,
                "reason": ("Gross profit is established but the indemnity period is not. "
                           "The requirement scales with the period, so assuming one "
                           "would manufacture the answer."),
                "resolved_by": ["POLICY_SCHEDULE"],
            })
        else:
            gp = Decimal(gp_prov["value"])
            si = Decimal(bi_si_prov["value"])
            calc = required_sum_insured(gp, chosen_months)
            required = Decimal(calc["required_sum_insured"])
            ratio = (si / required) if required > 0 else Decimal(0)
            adequacy = (ratio * 100).quantize(Decimal("0.1"))
            if ratio < 1:
                shortfall = _q(required - si)
                findings.append(BIFinding(
                    key="BI_SUM_INSURED_SHORT",
                    severity="HIGH" if ratio < Decimal("0.85") else "MODERATE",
                    headline=(f"Business interruption sum insured is {adequacy}% of the "
                              f"gross profit it needs to cover."),
                    detail=(
                        f"The section is insured for S${_q(si)} over {chosen_months} "
                        f"months. Annual gross profit on record is S${_q(gp)}, so the "
                        f"gross profit exposed across the indemnity period is "
                        f"S${required} — a shortfall of S${shortfall}. Where a BI sum "
                        "insured is short, average reduces the claim proportionately "
                        "however carefully the original forecast was prepared."),
                    basis="COMPUTED",
                    operands=[bi_si_prov, gp_prov, indemnity_prov],
                    numbers={**calc, "sum_insured": str(_q(si)),
                             "adequacy_pct": str(adequacy),
                             "shortfall": str(shortfall)},
                ))
            else:
                findings.append(BIFinding(
                    key="BI_SUM_INSURED_ADEQUATE",
                    severity="LOW",
                    headline=(f"Business interruption sum insured covers {adequacy}% of "
                              f"the gross profit exposed."),
                    detail=("On the figures on record the sum insured meets the gross "
                            "profit exposed over the stated indemnity period. Re-check "
                            "at renewal — the requirement moves with trading."),
                    basis="COMPUTED",
                    operands=[bi_si_prov, gp_prov, indemnity_prov],
                    numbers={**calc, "sum_insured": str(_q(si)),
                             "adequacy_pct": str(adequacy)},
                ))

    return _result(cover_present, findings, abstentions, months, rationale,
                   sections_prov, desc_prov,
                   {"bi_sum_insured": bi_si_prov,
                    "indemnity_period_months": indemnity_prov,
                    "gross_profit": gp_prov, "turnover": turnover_prov})


def _result(cover_present, findings, abstentions, months, rationale,
            sections_prov, desc_prov, provenance) -> dict:
    worst = "NONE"
    for level in ("HIGH", "MODERATE", "LOW"):
        if any(f.severity == level for f in findings):
            worst = level
            break
    return {
        "available": True,
        "abstained": False,
        "cover_present": cover_present,
        "highest_severity": worst,
        "findings": [f.as_dict() for f in findings],
        "abstentions": abstentions,
        # The rule engine's BUSINESS-INTERRUPTION-NOT-CONFIRMED matches the single literal
        # "INTERRUPTION". A policy that names the section "Consequential Loss", "Loss of
        # Profits", "Business Income" or "Time Element" would be reported by that rule as
        # having no BI cover when it plainly has some. This module matches the whole family
        # and reports which name it found, so the two can be reconciled rather than
        # silently disagreeing on a screen.
        "section_matching": {
            "names_searched": list(BI_SECTION_NAMES),
            "matched": [n for n in BI_SECTION_NAMES
                        if n in str(sections_prov["value"]).upper()]
            if sections_prov else [],
            "note": ("Matched against the whole family of section names carriers use for "
                     "this cover, not one literal, so a policy calling it consequential "
                     "loss is not mis-reported as having no cover."),
        },
        "recovery_benchmark": {
            "months": months,
            "rationale": rationale,
            "basis": _RECOVERY_BASIS,
        },
        "provenance": {"operative_sections": sections_prov,
                       "business_description": desc_prov, **provenance},
        "evidence_base": {
            "note": ("Business interruption is the gap the industry itself names, which "
                     "is why this screen exists."),
            "citations": [
                {"source": ("Chartered Institute of Loss Adjusters, as reported in an "
                            "insurer's published SME guidance"),
                 "finding": "Up to 43% of business interruption policies underinsured, "
                            "by an average of 53%."},
                {"source": "ABI, Small Business Big Risk (Jan 2026, Public First, "
                           "1,002 SME owners)",
                 "finding": "48% of SMEs had a physical premises; of those only 23% "
                            "carried business interruption insurance."},
                {"source": "Sedgwick, reporting UK government research",
                 "finding": "One in five businesses suffer a major disruption each "
                            "year; around 80% of those affected close within 18 months."},
                {"source": "Swiss Re",
                 "finding": "Once the indemnity period has elapsed, further loss of "
                            "profit is not insured."},
            ],
        },
        "disclaimer": DISCLAIMER,
        "notice": ("Absence of cover is proven from the schedule. Adequacy is computed "
                   "only where gross profit and an indemnity period are both on record; "
                   "otherwise the engine abstains and names the document that would "
                   "resolve it. Recovery-time benchmarks are a declared assumption and "
                   "are never used to size a sum insured."),
    }


# Findings the deterministic rule engine already reports on its own. Emitting them again
# as orchestrator signals would double-count one document: the engine's
# BUSINESS-INTERRUPTION-NOT-CONFIRMED rule already raises the absence of the section, and
# the orchestrator's overlap logic exists to detect two INDEPENDENT signals agreeing, not
# one signal counted twice.
_ALREADY_IN_RULE_ENGINE = frozenset({"NO_BI_COVER"})

# Nothing here is a finding worth escalating.
_NOT_ESCALATED = frozenset({"BI_SUM_INSURED_ADEQUATE"})


def as_external_signals(assessment: dict) -> list[dict]:
    """Convert the NEW BI findings into the orchestrator's signal shape.

    Only the findings the rule engine does not already produce are emitted — the
    indemnity-period shortfall and the sum-insured shortfall. Absence of the section is
    deliberately withheld because `BUSINESS-INTERRUPTION-NOT-CONFIRMED` already reports
    it, and counting one document twice would inflate priority on false corroboration.

    Claim scope is RISK_SIGNAL, not CONFIRMED_EVIDENCE. These findings lean on a declared
    recovery benchmark and on a forward gross-profit projection, and neither is a
    documented current fact about the business. The ceiling is deliberate: it lets these
    raise attention and create a review without ever asserting that cover has failed.
    """
    if not assessment.get("available") or assessment.get("abstained"):
        return []
    signals = []
    for f in assessment.get("findings", []):
        key = f.get("key")
        if key in _NOT_ESCALATED or key in _ALREADY_IN_RULE_ENGINE:
            continue
        signals.append({
            "id": f"bi-{key.lower().replace('_', '-')}",
            "agent": "bi",
            "signal_type": key,
            "label": f["headline"],
            "detail": f["detail"],
            "severity": f["severity"],
            "claim_scope": "RISK_SIGNAL",
            # Only interests the orchestrator recognises; anything else is silently
            # dropped by design, so sending an unknown one would be an invisible no-op.
            "insured_interests": ["business_interruption"],
            "simulated": False,
            "source": {"name": "Policy schedule and accounts on record",
                       "is_live": False, "reliability": "AUTHORITATIVE"},
            "asserts": ("A documentary reading of this business's own policy against its "
                        "own accounts. It does NOT assert that a claim would be reduced, "
                        "which depends on the loss and on a licensed determination."),
        })
    return signals
