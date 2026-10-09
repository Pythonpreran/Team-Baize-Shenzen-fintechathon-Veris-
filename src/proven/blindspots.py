"""Blind spots — naming what we did not check, and why (spec §16).

WHY THIS MODULE EXISTS, AND WHY IT IS THE MOST IMPORTANT SCREEN IN THE PRODUCT.

Every other screen answers a question. This one reports the questions that were never
asked, because that is where an SME's real exposure lives. "Most SMEs don't know what
they don't know" is not a marketing sentence — it is a statement about the SHAPE of the
information, and a product that only reports findings actively conceals it.

Here is the concrete way this product used to get it wrong. `score.py` computes the
overall score as the mean of ASSESSED dimensions. It declared five dimensions and left
`digital` with an empty rule tuple, so cyber was never assessed, and an unassessed
dimension is excluded from the mean rather than counted as zero. The arithmetic is
defensible — scoring a dimension you did not examine would be worse — but the effect
was that an entire risk class was invisible AND the score went UP because of its
absence. The owner sees a number, feels informed, and the thing most likely to hurt them
was never on the page. The product was performing the problem statement.

The fix is not to invent a cyber score. It is to make the boundary of the assessment a
first-class output: how many risk areas exist, how many we can speak to, which we
cannot, what specifically is missing, and what single document would close each one.

THE ASYMMETRY THIS MODULE MAKES VISIBLE.

There are two ways a screen can be quiet:

  * We looked and found nothing wrong. That is good news.
  * We could not look. That is NOT good news, and it renders identically in every
    dashboard ever built — as an absence of red.

`silent_gaps` separates them. Every entry is a place where the absence of a finding is
caused by the absence of a document, and where an owner would reasonably but wrongly
read the quiet as safety. Nothing else in the system reports this, because every other
module is correctly scoped to what it can evidence.

WHAT THIS MODULE MUST NOT DO. It must not manufacture severity for an unassessed area.
An unknown is not a HIGH finding; it is an unknown, and it is ranked by what it would
take to resolve rather than by how bad it might turn out to be. Ranking unknowns by
imagined severity would be fear-based selling with extra steps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ── states of knowledge ─────────────────────────────────────────────────────
ASSESSED = "ASSESSED"          # we looked, and we have a verdict
PARTIAL = "PARTIAL"            # we looked, and could only answer part of it
NOT_ASSESSED = "NOT_ASSESSED"  # we never looked, because we could not

_STATE_LABELS: dict[str, str] = {
    ASSESSED: "Checked",
    PARTIAL: "Partly checked",
    NOT_ASSESSED: "Not checked",
}
_STATE_PLAIN: dict[str, str] = {
    ASSESSED: "We read the documents this needs and we have an answer for you.",
    PARTIAL: "We answered part of this. The rest needs a document we do not have.",
    NOT_ASSESSED: "We could not check this at all. Quiet here does not mean safe.",
}


@dataclass(frozen=True)
class RiskArea:
    """One area of a business's protection that can be examined or not."""

    key: str
    label: str
    plain: str
    # The single document that most moves this area from NOT_ASSESSED to ASSESSED.
    unlocking_document: str
    unlocks_what: str

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "plain": self.plain,
                "unlocking_document": self.unlocking_document,
                "unlocks_what": self.unlocks_what}


# The register. This is the denominator: the areas a business of any size has, whether
# or not this product can currently speak to them. Adding an area here without wiring
# an assessment makes the product report its own gap, which is the intended behaviour.
AREAS: tuple[RiskArea, ...] = (
    RiskArea("property", "Property and assets",
             "Buildings, stock, plant and fittings, and whether the sums insured "
             "keep up with what you actually hold.",
             "Management accounts and an asset register",
             "What your stock and equipment are really worth today."),
    RiskArea("continuity", "Trading through a loss",
             "Whether income is insured while you cannot trade.",
             "Your policy schedule showing operative sections",
             "Whether you hold a business interruption section at all."),
    RiskArea("obligations", "Conditions you must meet",
             "Warranties, premium payment terms and notification duties that can "
             "stop a policy responding.",
             "The full policy wording and every endorsement",
             "Which conditions attach, and by when."),
    RiskArea("workforce", "Your people",
             "Compulsory work injury cover and migrant worker obligations.",
             "CPF contribution record and work injury schedule",
             "Whether your declared headcount matches who you employ."),
    RiskArea("digital", "Data and systems",
             "Personal data you hold, and whether anything responds when it is "
             "lost or stolen.",
             "Your ACRA profile and policy wording",
             "Whether you are inside the PDPA, and whether your policy excludes "
             "data loss."),
    RiskArea("dependencies", "Who you depend on",
             "Suppliers, utilities, landlords and channels your income relies on.",
             "Your supplier list and tenancy agreement",
             "Which outside failures would stop you trading."),
)
BY_KEY: dict[str, RiskArea] = {a.key: a for a in AREAS}


@dataclass(frozen=True)
class Assessment:
    """The knowledge state of one risk area."""

    area: RiskArea
    state: str
    checked: tuple[str, ...] = ()
    could_not_check: tuple[str, ...] = ()
    finding_count: int = 0
    score: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            **self.area.as_dict(),
            "state": self.state,
            "state_label": _STATE_LABELS[self.state],
            "state_plain": _STATE_PLAIN[self.state],
            "checked": list(self.checked),
            "could_not_check": list(self.could_not_check),
            "finding_count": self.finding_count,
            "score": self.score,
        }


@dataclass
class BlindSpotReport:
    assessments: list[Assessment] = field(default_factory=list)
    silent_gaps: list[dict] = field(default_factory=list)

    def assert_invariant(self) -> None:
        """An unknown may never be dressed as a severity.

        The temptation on this screen is to sort unknowns by how frightening they
        could be, because that produces a more compelling demo. It is also the exact
        move that turns an honest instrument into fear-based selling, so it is
        blocked here rather than discouraged in a comment.
        """
        for gap in self.silent_gaps:
            if "severity" in gap:
                raise AssertionError(
                    "blindspots.py: a silent gap carries a severity; an unknown has "
                    "no severity, only a cost to resolve")
            if not gap.get("resolved_by"):
                raise AssertionError(
                    f"blindspots.py: silent gap {gap.get('area')!r} names no "
                    f"document that would resolve it, so it is an alarm rather than "
                    f"an action")
        keys = [a.area.key for a in self.assessments]
        if len(keys) != len(set(keys)):
            raise AssertionError("blindspots.py: duplicate risk area in register")


def _dimension_index(score: Optional[dict]) -> dict[str, dict]:
    return {str(d.get("key")): d
            for d in ((score or {}).get("dimensions") or [])
            if d.get("key")}


def assess(case: dict, *, score: Optional[dict] = None,
           cyber: Optional[dict] = None,
           dependency: Optional[dict] = None) -> dict:
    """Report the boundary of what this assessment could see.

    Takes the already-computed results rather than recomputing them: this module is a
    reporter on the others, and a second computation could disagree with the first,
    which would be a worse failure than not having the screen.
    """
    report = BlindSpotReport()
    dims = _dimension_index(score)
    findings = (case or {}).get("findings") or []

    for area in AREAS:
        if area.key == "digital":
            report.assessments.append(_digital(area, cyber, dims.get("digital")))
        elif area.key == "dependencies":
            report.assessments.append(_dependencies(area, dependency))
        else:
            report.assessments.append(_from_dimension(area, dims.get(area.key)))

    report.silent_gaps = _silent_gaps(report.assessments, cyber, dependency)
    report.assert_invariant()

    assessed = [a for a in report.assessments if a.state == ASSESSED]
    partial = [a for a in report.assessments if a.state == PARTIAL]
    unassessed = [a for a in report.assessments if a.state == NOT_ASSESSED]
    total = len(report.assessments)
    speakable = len(assessed) + len(partial)

    return {
        "areas": [a.as_dict() for a in report.assessments],
        "counts": {
            "total_areas": total,
            "assessed": len(assessed),
            "partial": len(partial),
            "not_assessed": len(unassessed),
        },
        "coverage": {
            "areas_we_can_speak_to": speakable,
            "areas_total": total,
            "percent": int(round(100 * speakable / total)) if total else 0,
            "statement": (
                f"We can speak to {speakable} of {total} areas of your protection. "
                f"Your score is calculated only from those {speakable}."),
        },
        "score_caveat": _score_caveat(score, unassessed),
        "silent_gaps": report.silent_gaps,
        "silent_gap_count": len(report.silent_gaps),
        "not_assessed": [a.area.key for a in unassessed],
        "method": "DETERMINISTIC",
        "why_this_screen_exists": (
            "Every other screen tells you what we found. This one tells you where we "
            "could not look, because an empty screen and a clean screen look "
            "identical and only one of them is good news."),
        "notice": (
            "Nothing on this screen is a prediction or a severity. An area we did not "
            "check is not automatically dangerous — it is simply unknown, and it is "
            "listed by what it would take to find out rather than by how bad it "
            "might be."),
    }


def _from_dimension(area: RiskArea, dim: Optional[dict]) -> Assessment:
    """Knowledge state for an area the score already covers."""
    if not dim:
        return Assessment(area, NOT_ASSESSED,
                          could_not_check=(f"No rules were evaluated for "
                                           f"{area.label.lower()}.",))
    if not dim.get("assessed"):
        reason = dim.get("reason") or (
            f"Nothing in the documents we read bears on {area.label.lower()}.")
        return Assessment(area, NOT_ASSESSED, could_not_check=(reason,),
                          score=None)
    deductions = dim.get("deductions") or []
    return Assessment(
        area, ASSESSED,
        checked=tuple(str(d.get("headline") or d.get("rule_id"))
                      for d in deductions) or
                (f"Checked, and nothing reduced your {area.label.lower()} score.",),
        finding_count=len(deductions),
        score=dim.get("score"))


def _digital(area: RiskArea, cyber: Optional[dict],
             dim: Optional[dict]) -> Assessment:
    """Knowledge state for data and systems.

    Deliberately does NOT fall back to the score dimension when cyber.py abstained:
    the dimension is empty by construction, so treating it as an answer would restore
    exactly the silence this module exists to break.
    """
    if not cyber:
        return Assessment(area, NOT_ASSESSED, could_not_check=(
            "We did not examine your data and systems position.",))
    if not cyber.get("available"):
        reasons = tuple(cyber.get("unestablished") or ()) or (
            str(cyber.get("reason") or "We could not establish whether you hold "
                                      "personal data."),)
        return Assessment(area, NOT_ASSESSED, could_not_check=reasons)

    checked: list[str] = []
    for holding in cyber.get("holdings") or []:
        checked.append(f"Established that you hold {str(holding.get('subject','')).lower()} "
                       f"personal data.")
    checked.append(f"Read your policy's position: {cyber.get('cover_label')}.")
    could_not: list[str] = list(cyber.get("unestablished") or ())
    state = PARTIAL if could_not else ASSESSED
    return Assessment(area, state, checked=tuple(checked),
                      could_not_check=tuple(could_not),
                      finding_count=int(cyber.get("uncovered_count") or 0),
                      score=dim.get("score") if dim and dim.get("assessed") else None)


def _dependencies(area: RiskArea, dependency: Optional[dict]) -> Assessment:
    """Knowledge state for who the business depends on.

    Always PARTIAL at best. The dependency map is built from documents that happen to
    name a counterparty, and that can never be exhaustive, so claiming ASSESSED would
    be the false-known failure this whole module is about.
    """
    if not dependency:
        return Assessment(area, NOT_ASSESSED, could_not_check=(
            "We did not examine your outside dependencies.",))
    if not dependency.get("available"):
        reasons = tuple(dependency.get("unestablished") or ()) or (
            str(dependency.get("reason") or "No dependency could be established."),)
        return Assessment(area, NOT_ASSESSED, could_not_check=reasons)

    deps = dependency.get("dependencies") or []
    checked = tuple(f"Found: {d.get('name')}." for d in deps)
    could_not = tuple(
        f"{item.get('document')} — would establish {item.get('establishes')}"
        for item in ((dependency.get("visibility") or {})
                     .get("what_would_complete_it") or []))
    return Assessment(area, PARTIAL, checked=checked, could_not_check=could_not,
                      finding_count=int((dependency.get("counts") or {})
                                        .get("uncovered_paths") or 0))


def _silent_gaps(assessments: list[Assessment], cyber: Optional[dict],
                 dependency: Optional[dict]) -> list[dict]:
    """Places where quiet is caused by missing evidence, not by safety.

    Ordered by how cheaply the unknown can be resolved — one document first — because
    that is the only ordering that is not a claim about severity.
    """
    out: list[dict] = []
    for a in assessments:
        if a.state == ASSESSED:
            continue
        for reason in (a.could_not_check or ()):
            out.append({
                "area": a.area.key,
                "area_label": a.area.label,
                "state": a.state,
                "what_we_could_not_check": reason,
                "resolved_by": a.area.unlocking_document,
                "resolving_establishes": a.area.unlocks_what,
                "why_quiet_is_not_safe": (
                    f"With nothing to read, {a.area.label.lower()} produces no "
                    f"findings. An area with no findings looks exactly like an area "
                    f"with no problems."),
            })
    # One document can resolve several reasons; collapse so the list reads as actions.
    seen: set[tuple[str, str]] = set()
    collapsed: list[dict] = []
    for gap in out:
        key = (gap["area"], gap["resolved_by"])
        if key in seen:
            continue
        seen.add(key)
        collapsed.append(gap)
    return collapsed


def _score_caveat(score: Optional[dict], unassessed: list[Assessment]) -> dict:
    """What the headline number does and does not include.

    This is the sentence that should appear next to the score everywhere it is shown.
    """
    overall = (score or {}).get("overall")
    names = [a.area.label for a in unassessed]
    if not score:
        return {"applies": False,
                "statement": "No score has been computed for this case."}
    if not names:
        return {
            "applies": True, "overall": overall, "excluded_areas": [],
            "statement": ("Your score covers every area in the register. Nothing was "
                          "left out of it."),
        }
    joined = ", ".join(names[:-1]) + (" and " + names[-1] if len(names) > 1
                                     else names[-1])
    return {
        "applies": True,
        "overall": overall,
        "excluded_areas": names,
        "statement": (
            f"Your score of {overall} is the average of the areas we could assess. "
            f"It does NOT include {joined}, because we had nothing to read there. "
            f"The score is not lower for those gaps — they are simply absent from it."),
        "why_not_zero": (
            "We do not score an area we did not examine. Giving it zero would invent "
            "a finding; giving it full marks would hide one. So it sits outside the "
            "number, and this sentence exists so you know that."),
    }


def catalogue() -> dict:
    """The register itself, for the UI and the docs."""
    return {
        "areas": [a.as_dict() for a in AREAS],
        "states": [{"state": k, "label": _STATE_LABELS[k], "plain": _STATE_PLAIN[k]}
                   for k in (ASSESSED, PARTIAL, NOT_ASSESSED)],
        "method": "DETERMINISTIC",
    }
