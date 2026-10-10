"""Protection Pulse — the score.

WHAT THIS IS FOR. The engine produces fifteen findings with four states and four
evidence classes. That is the right output for a reviewer and the wrong first thing to
show a business owner, who wants to know "am I alright?" before they want to know
"what does clause 4.2 say?". The score is the entry point; the findings are the
explanation behind it.

THREE RULES THAT KEEP IT HONEST.

  1. **No model touches it.** The score is arithmetic over findings the deterministic
     engine already produced. `ai.py` is not imported here, and that is the guarantee —
     if an LLM generated the headline number, every figure downstream of it would
     inherit the model's uncertainty and the product would have laundered a guess into
     a metric.

  2. **Every deduction names its finding.** `dimension.deductions[].rule_id` traces
     each point lost back to a rule and its evidence. A score nobody can decompose is
     a credit rating, and a credit rating is the thing this product explicitly refuses
     to build — there is no schema column for it and there is no hidden weighting here
     either.

  3. **Unknown is not the same as fine.** An UNESTABLISHED finding deducts, because
     "we could not establish this" is a real gap in the owner's knowledge. But it
     deducts LESS than a documented problem, and the reason string says so. Scoring an
     unknown as zero would make an incomplete upload look catastrophic; scoring it as
     full marks would make it look safe. Neither is true.

The score is a *coverage-evidence* score, not a risk rating. It measures how much of
this business's protection is actually established by documents — which is exactly the
gap in the problem statement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

# Dimensions. Chosen to match how an owner thinks about their business, not how the
# taxonomy is organised, and each maps to the rules that bear on it.
DIMENSIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("property", "Property & assets",
     "Buildings, stock, plant and equipment.",
     ("AVERAGE-ADEQUACY-BELOW-THRESHOLD", "BUILDINGS-VALUE-UNESTABLISHED",
      "PLANT-ADEQUACY-UNESTABLESHED", "PLANT-ADEQUACY-UNESTABLISHED",
      "THEFT-FORCIBLE-ENTRY-NOT-WRITTEN-BACK", "MINIMUM-SECURITY",
      "UNOCCUPANCY-WATER-EXCLUSION", "SUBLIMIT-CONFLICTING-ENDORSEMENTS")),
    ("continuity", "Business continuity",
     "Cover for lost income if you cannot trade.",
     ("BUSINESS-INTERRUPTION-NOT-CONFIRMED", "CLAIM-TIME-LIMIT")),
    ("obligations", "Policy obligations",
     "Conditions you must meet for the policy to respond.",
     ("PREMIUM-PAYMENT-WARRANTY-TIMING", "ALTERATION-PREMISES-NOT-ON-SCHEDULE",
      "ACTIVITY-SSIC-NOT-DECLARED", "ENDORSEMENT-ILLEGIBLE-ABSTAIN")),
    ("workforce", "Workforce",
     "Work injury and migrant worker obligations.",
     ("WICA-HEADCOUNT-UNDERDECLARED", "WORKPASS-MEDICAL-BELOW-MINIMUM")),
    # This tuple was empty for the whole of v1 and v2, which meant `digital` always
    # reported assessed:false — and because `overall` is the mean of ASSESSED
    # dimensions, an entire risk class was not merely unscored but INVISIBLE, and the
    # headline number was higher for its absence. The arithmetic was defensible and
    # the effect was not: the product was reproducing the exact blind spot it exists
    # to expose. CYBER-DATA-COVER-ABSENT now fills it from documents.
    ("digital", "Digital & data",
     "Cyber exposure and customer data.",
     ("CYBER-DATA-COVER-ABSENT",)),
    ("dependencies", "Who you depend on",
     "Suppliers, utilities and landlords your income relies on.",
     ("DEPENDENCY-COVER-NOT-POSSIBLE",)),
)

# Deduction weights by (state, class). A documented problem costs more than an
# unestablished one, because a known gap is a fact and an unknown is a question.
#
# These are product judgements, not measurements, and they are declared here in one
# table rather than scattered through the code so they can be argued with.
_STATE_WEIGHT: dict[str, int] = {
    "DOCUMENTED": 18,                  # evidenced problem
    "REQUIRES_AUTHORISED_REVIEW": 14,  # evidenced, consequence needs a human
    "NOT_CONFIRMED": 10,               # searched, not found
    "INFORMATION_MISSING": 8,          # cannot be established from what was given
}
_CLASS_MULTIPLIER: dict[str, float] = {
    "PROVEN": 1.0,                     # third-party or insurer authored
    "DECLARED": 0.85,                  # rests partly on self-authored input
    "MARKET_CONVENTION": 0.9,          # arithmetic not stated in the wording
    "UNESTABLISHED": 0.75,             # a required input has no document
}

# Findings that are informational rather than a gap. Locating a clause and quoting it
# is a service, not a problem, so it must not cost the owner points.
_INFORMATIONAL: frozenset[str] = frozenset({
    "MINIMUM-SECURITY", "CLAIM-TIME-LIMIT", "UNOCCUPANCY-WATER-EXCLUSION",
})

BANDS: tuple[tuple[int, str, str], ...] = (
    (85, "Well evidenced", "Most of your protection is established by documents."),
    (70, "Needs attention", "Some material gaps are documented or unestablished."),
    (50, "Significant gaps", "Several important items are unevidenced or documented "
                             "as shortfalls."),
    (0, "Largely unestablished", "Too little is evidenced to say your protection "
                                 "keeps up with your business."),
)


@dataclass
class Deduction:
    rule_id: str
    points: int
    state: str
    evidence_class: str
    headline: str

    def as_dict(self) -> dict:
        return {"rule_id": self.rule_id, "points": self.points, "state": self.state,
                "evidence_class": self.evidence_class, "headline": self.headline}


@dataclass
class Dimension:
    key: str
    label: str
    description: str
    score: int
    reason: str
    assessed: bool
    deductions: list[Deduction] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label,
                "description": self.description, "score": self.score,
                "reason": self.reason, "assessed": self.assessed,
                "deductions": [d.as_dict() for d in self.deductions]}


def _band(score: int) -> tuple[str, str]:
    for floor, status, note in BANDS:
        if score >= floor:
            return status, note
    return BANDS[-1][1], BANDS[-1][2]


def _deduction_for(finding: dict) -> Optional[Deduction]:
    rule_id = finding.get("rule_id", "")
    if rule_id in _INFORMATIONAL:
        return None
    state = finding.get("state", "")
    evidence_class = finding.get("class", "")
    base = _STATE_WEIGHT.get(state)
    if base is None:
        return None
    points = int(round(base * _CLASS_MULTIPLIER.get(evidence_class, 1.0)))
    return Deduction(rule_id=rule_id, points=points, state=state,
                     evidence_class=evidence_class,
                     headline=finding.get("headline", "")[:200])


def compute_score(brief: dict) -> dict:
    """Compute the Protection Pulse score from a Review Brief.

    Deterministic and idempotent: the same Brief always yields the same score, which
    is what lets the drift comparison in `compare_scores` mean anything.
    """
    findings = brief.get("findings", []) or []
    by_rule = {f.get("rule_id"): f for f in findings}

    dimensions: list[Dimension] = []
    for key, label, description, rule_ids in DIMENSIONS:
        relevant = [by_rule[r] for r in rule_ids if r in by_rule]
        if not relevant:
            # Nothing was assessed here. Report it as unassessed rather than as 100 —
            # "we did not look" must never render as "you are fine".
            dimensions.append(Dimension(
                key=key, label=label, description=description, score=0,
                reason=("Not assessed — no document in this bundle carries evidence "
                        "about it."),
                assessed=False))
            continue

        deductions = [d for d in (_deduction_for(f) for f in relevant) if d]
        total = sum(d.points for d in deductions)
        value = max(0, min(100, 100 - total))
        if not deductions:
            reason = "No gaps found in the documents reviewed."
        else:
            worst = max(deductions, key=lambda d: d.points)
            reason = (f"{len(deductions)} item(s) reduced this. Largest: "
                      f"{worst.headline[:120]}")
        dimensions.append(Dimension(
            key=key, label=label, description=description, score=value,
            reason=reason, assessed=True, deductions=deductions))

    assessed = [d for d in dimensions if d.assessed]
    overall = int(round(sum(d.score for d in assessed) / len(assessed))) \
        if assessed else 0
    status, status_note = _band(overall)

    # Exposure: the largest currency figure the engine actually computed. Never
    # invented, and absent when nothing was computable.
    exposure = _headline_exposure(findings)

    return {
        "overall": overall,
        "status": status,
        "status_note": status_note,
        "assessed_dimensions": len(assessed),
        "total_dimensions": len(dimensions),
        "dimensions": [d.as_dict() for d in dimensions],
        "issues": _priority_issues(findings),
        "headline_exposure": exposure,
        "method": "DETERMINISTIC",
        "notice": ("Computed only from findings the engine could evidence. No model "
                   "contributes to this number, and every point deducted names the "
                   "finding it came from. This measures how much of your protection "
                   "is established by documents — it is not a risk rating, and there "
                   "is nowhere in the schema to store one."),
    }


def _money_decimal(value: object) -> Decimal:
    """Parse a monetary operand without ever taking a float detour."""
    if isinstance(value, float):
        raise ValueError("money values must not be floats")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("not a decimal money value") from exc
    if not decimal.is_finite():
        raise ValueError("money values must be finite")
    return decimal


def _money_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _headline_exposure(findings: list[dict]) -> Optional[dict]:
    """The biggest computed money figure, with its basis. Never estimated."""
    best: Optional[dict] = None
    best_shortfall: Optional[Decimal] = None
    for f in findings:
        detail = f.get("detail") or {}
        illustration = detail.get("illustration")
        loss = detail.get("loss")
        if illustration is None or loss is None:
            continue
        try:
            loss_amount = _money_decimal(loss)
            illustrated_amount = _money_decimal(illustration)
        except ValueError:
            continue
        shortfall = loss_amount - illustrated_amount
        if shortfall <= 0:
            continue
        candidate = {
            "amount_sgd": _money_text(shortfall),
            "assessed_loss_sgd": _money_text(loss_amount),
            "illustrated_settlement_sgd": _money_text(illustrated_amount),
            "rule_id": f.get("rule_id"),
            "evidence_class": f.get("class"),
            "basis": detail.get("formula_basis"),
            "adequacy_ratio": detail.get("adequacy_ratio"),
            "caveat": ("Illustrative. This policy states the trigger but not the "
                       "formula, so this is market convention, not the amount "
                       "payable."
                       if detail.get("formula_basis") == "MARKET_CONVENTION" else
                       "Computed using the arithmetic stated in the wording."),
        }
        if best_shortfall is None or shortfall > best_shortfall:
            best, best_shortfall = candidate, shortfall
    return best


def _priority_issues(findings: list[dict]) -> list[dict]:
    """Findings ordered by how much they matter to the owner.

    Sort key is deliberate: an evidenced shortfall with money attached outranks an
    unestablished item, because the owner can act on the first today.
    """
    severity_rank = {
        "DOCUMENTED": 0,
        "REQUIRES_AUTHORISED_REVIEW": 1,
        "NOT_CONFIRMED": 2,
        "INFORMATION_MISSING": 3,
    }
    issues: list[dict] = []
    for f in findings:
        rule_id = f.get("rule_id", "")
        if rule_id in _INFORMATIONAL:
            continue
        detail = f.get("detail") or {}
        money: Optional[Decimal] = None
        if detail.get("illustration") is not None and detail.get("loss") is not None:
            try:
                money = max(
                    Decimal(0), _money_decimal(detail["loss"])
                    - _money_decimal(detail["illustration"]))
            except ValueError:
                money = None
        issues.append({
            "rule_id": rule_id,
            "severity": ("HIGH" if f.get("state") == "DOCUMENTED"
                         else "MEDIUM" if f.get("state") == "REQUIRES_AUTHORISED_REVIEW"
                         else "REVIEW"),
            "state": f.get("state"),
            "state_label": f.get("state_label"),
            "evidence_class": f.get("class"),
            "headline": f.get("headline"),
            "exposure_sgd": _money_text(money) if money is not None else None,
            "missing_documents": detail.get("missing_documents") or [],
            "_sort_exposure": money if money is not None else Decimal(0),
        })
    issues.sort(key=lambda i: (severity_rank.get(i["state"], 9), -i["_sort_exposure"]))
    for issue in issues:
        issue.pop("_sort_exposure", None)
    return issues


def compare_scores(before: dict, after: dict) -> dict:
    """Drift between two scores — the 'your business changed' moment.

    This is what makes the product's central claim visible rather than asserted: the
    same deterministic score, computed before and after a business change, moving
    because the documents did not move with the business.
    """
    delta = after.get("overall", 0) - before.get("overall", 0)
    before_dims = {d["key"]: d for d in before.get("dimensions", [])}
    changed = []
    for dim in after.get("dimensions", []):
        prior = before_dims.get(dim["key"])
        if prior and prior["score"] != dim["score"]:
            changed.append({
                "key": dim["key"], "label": dim["label"],
                "from": prior["score"], "to": dim["score"],
                "delta": dim["score"] - prior["score"],
            })
    return {
        "before": before.get("overall"),
        "after": after.get("overall"),
        "delta": delta,
        "direction": "down" if delta < 0 else "up" if delta > 0 else "unchanged",
        "dimensions_changed": changed,
        "summary": (
            f"Your protection score moved from {before.get('overall')} to "
            f"{after.get('overall')}. Your business changed; the documents evidencing "
            f"your cover did not."
            if delta < 0 else
            f"Your protection score moved from {before.get('overall')} to "
            f"{after.get('overall')}."),
    }
