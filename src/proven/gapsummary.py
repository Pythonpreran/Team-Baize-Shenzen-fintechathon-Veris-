"""Gap summary — a presentation layer, and deliberately nothing more (spec §19).

WHAT THIS MODULE IS FORBIDDEN FROM BEING.

A second opinion. Every fact here is read from a result another module already computed —
`blindspots`, `score`, `cyber`, `supplychain`, `actions`, `voi`, `compounding`. This module
groups and words them. It does not evaluate a rule, does not read a document, and does not
know what a clause says.

That constraint is not tidiness. Two engines that disagree is worse than one engine that is
wrong, because a disagreement cannot be debugged by a reader — they have no way to know
which surface lied. `test_gap_summary.py` asserts every value here is derivable from the
existing endpoints, so a future change that starts computing something new fails the build.

THE THREE CATEGORIES, AND WHY THEY ARE NOT ALL CALLED GAPS.

The temptation is to count everything unresolved as a gap, because one big red number is a
better demo. It is also the exact dishonesty this product exists to refuse, and it would
turn an instrument into a scare:

    ACTION_REQUIRED     evidenced. Two documents disagree, or a clause plainly does not
                        respond. There is something to do and we can show why.
    NEEDS_VERIFICATION  possible, not established. We say "we cannot confirm this yet" and
                        never "you are not covered", because those are different claims and
                        only one of them is supported.
    NOT_YET_ASSESSED    we did not look, usually because a document is missing. Neutral.
                        Rendered grey, never red — an unexamined area is not a dangerous
                        one, it is an unknown one.

The ordering rule follows from that: ACTION_REQUIRED first because it is evidenced, then
NEEDS_VERIFICATION, then NOT_YET_ASSESSED. Never by imagined severity, because severity for
something unexamined would have to be invented.
"""
from __future__ import annotations

from typing import Any, Optional

ACTION_REQUIRED = "ACTION_REQUIRED"
NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
NOT_YET_ASSESSED = "NOT_YET_ASSESSED"

CATEGORIES: tuple[str, ...] = (ACTION_REQUIRED, NEEDS_VERIFICATION, NOT_YET_ASSESSED)

# Label, plain-English meaning, the tone to render it in, and an ICON — because a status
# carried by colour alone is unreadable to a colour-blind reader and invisible in a
# printout, and this is a screen people will screenshot into an email.
CATEGORY_META: dict[str, dict[str, str]] = {
    ACTION_REQUIRED: {
        "label": "Action required",
        "plain": "We can show you the documents that prove this needs attention.",
        "tone": "critical",
        "icon": "!",
        "colour_hint": "red",
    },
    NEEDS_VERIFICATION: {
        "label": "Needs verification",
        "plain": "This might be a problem. We cannot confirm it yet, and we are not "
                 "going to tell you it is one until we can.",
        "tone": "warning",
        "icon": "?",
        "colour_hint": "amber",
    },
    NOT_YET_ASSESSED: {
        "label": "Not yet assessed",
        "plain": "We have not looked at this. That is not the same as it being wrong.",
        "tone": "neutral",
        "icon": "–",
        "colour_hint": "grey",
    },
}

# The engine's own finding states, mapped to the three categories.
#
# DOCUMENTED and REQUIRES_AUTHORISED_REVIEW are evidenced — a document establishes the
# position, even where the CONSEQUENCE needs a licensed reader. NOT_CONFIRMED means we
# searched and did not find, which is a real observation but not a proven gap.
# INFORMATION_MISSING means a required input has no document at all.
_STATE_CATEGORY: dict[str, str] = {
    "DOCUMENTED": ACTION_REQUIRED,
    "REQUIRES_AUTHORISED_REVIEW": ACTION_REQUIRED,
    "NOT_CONFIRMED": NEEDS_VERIFICATION,
    "INFORMATION_MISSING": NOT_YET_ASSESSED,
}

# Phrases that would convert an unknown into a denial. Checked on the way out.
_FORBIDDEN: tuple[str, ...] = (
    "you are not covered", "not covered", "no cover", "will not pay",
    "claim will fail", "uninsured",
)

_ORDER = {ACTION_REQUIRED: 0, NEEDS_VERIFICATION: 1, NOT_YET_ASSESSED: 2}


def _category_for(finding: dict) -> str:
    return _STATE_CATEGORY.get(str(finding.get("state") or ""), NEEDS_VERIFICATION)


def _item(*, key: str, title: str, category: str, why: str,
          evidence_class: Optional[str] = None, source: str = "",
          age_days: Optional[int] = None,
          resolved_by: Optional[str] = None,
          verbatim: bool = False) -> dict:
    """One row. `verbatim` marks text copied unchanged from an engine output.

    That flag exists because of a real false positive worth recording. The language guard
    below rejects "not covered", and `BUSINESS-INTERRUPTION-NOT-CONFIRMED`'s own headline
    reads: '"Not confirmed" is not "not covered" — it states exactly what the reviewed
    documents do and do not show.' The engine was being scrupulously careful, and a blunt
    phrase match called that dishonest.

    So the guard polices what THIS module composes. Engine headlines already passed the
    engine's own `never: [assert_breach, assert_claim_outcome]` gates, and re-policing them
    here would mean rejecting another module's correct disclaimer — which is worse than not
    checking, because it would push authors toward vaguer wording to get past us.
    """
    meta = CATEGORY_META[category]
    return {
        "key": key,
        "title": title,
        "category": category,
        "category_label": meta["label"],
        "category_plain": meta["plain"],
        "tone": meta["tone"],
        "icon": meta["icon"],
        "colour_hint": meta["colour_hint"],
        "why": why,
        "evidence_class": evidence_class,
        "source": source,
        "age_days": age_days,
        "resolved_by": resolved_by,
        "verbatim_from_engine": verbatim,
    }


def build(*, brief: Optional[dict] = None, score: Optional[dict] = None,
          blind: Optional[dict] = None, cyber: Optional[dict] = None,
          dependency: Optional[dict] = None, gaps: Optional[dict] = None,
          compounding: Optional[dict] = None) -> dict:
    """Compose the summary from results already computed elsewhere.

    Every argument is another module's output. Nothing is recomputed, and a missing
    argument simply contributes nothing rather than being reconstructed from a second
    source — which is what keeps this a projection instead of a rival engine.
    """
    items: list[dict] = []

    # 1. Findings the engine produced. Category follows the engine's own state.
    for f in (brief or {}).get("findings") or []:
        items.append(_item(
            key=str(f.get("rule_id") or ""),
            title=str(f.get("headline") or f.get("rule_id") or ""),
            category=_category_for(f),
            why=str(f.get("state_label") or f.get("state") or ""),
            evidence_class=f.get("class"),
            source="Your documents",
            verbatim=True))

    # 2. Areas the blind-spot register could not assess. Explicitly NOT_YET_ASSESSED, and
    #    each carries the single document that would open it — an unknown with a named
    #    remedy is an action; without one it is just anxiety.
    for area in (blind or {}).get("areas") or []:
        if area.get("state") == "NOT_ASSESSED":
            items.append(_item(
                key=f"area:{area.get('key')}",
                title=f"{area.get('label')} has not been checked",
                category=NOT_YET_ASSESSED,
                why=(area.get("could_not_check") or ["No document was read for this."])[0],
                source="Coverage of assessment",
                resolved_by=area.get("unlocking_document")))

    # 3. Documents that would settle the most, from the value-of-information ranking.
    for gap in ((gaps or {}).get("gaps") or [])[:5]:
        items.append(_item(
            key=f"voi:{gap.get('operand')}",
            title=f"{gap.get('document_needed')} would settle this",
            category=NOT_YET_ASSESSED,
            why=str(gap.get("note") or ""),
            source="What would help",
            resolved_by=gap.get("document_needed")))

    # 4. Open loops, with their age. The age is a real day count from `compounding`, not a
    #    severity, and the wording stays free of fear language.
    for loop in (compounding or {}).get("open_loops") or []:
        items.append(_item(
            key=f"loop:{loop.get('label')}",
            title=str(loop.get("label") or ""),
            category=NEEDS_VERIFICATION,
            why=str(loop.get("detail") or ""),
            evidence_class=loop.get("evidence_class"),
            source="Timeline",
            age_days=loop.get("days_open"),
            resolved_by=loop.get("what_would_close_it"),
            verbatim=True))

    # 5. Cyber. Only ACTION_REQUIRED when the carrier's own exclusion was located and
    #    quoted; otherwise it is a verification question, not a denial.
    if (cyber or {}).get("available") and cyber.get("gap_open"):
        excluded = cyber.get("cover_state") == "EXCLUDED"
        items.append(_item(
            key="cyber",
            title=("Your policy excludes cover for data loss in writing" if excluded
                   else "No cyber section was found on your schedule"),
            category=ACTION_REQUIRED if excluded else NEEDS_VERIFICATION,
            why=str(cyber.get("cover_label") or ""),
            evidence_class=cyber.get("evidence_class"),
            source="Cyber and data"))

    # 6. Dependencies. IMPOSSIBLE_NO_BI is structural and evidenced; anything else is a
    #    verification question.
    if (dependency or {}).get("available") and dependency.get("gap_open"):
        structural = dependency.get("cbi_state") == "IMPOSSIBLE_NO_BI"
        items.append(_item(
            key="dependencies",
            title=("Supplier dependency cover cannot attach — you hold no business "
                   "interruption section" if structural else
                   "No supplier dependency cover was found"),
            category=ACTION_REQUIRED if structural else NEEDS_VERIFICATION,
            why=str(dependency.get("cbi_label") or ""),
            source="Who you depend on"))

    items.sort(key=lambda i: (_ORDER[i["category"]],
                              -(i.get("age_days") or 0),
                              i["title"]))

    buckets = {c: [i for i in items if i["category"] == c] for c in CATEGORIES}
    counts = {c: len(buckets[c]) for c in CATEGORIES}

    result = {
        "categories": [
            {"category": c, **CATEGORY_META[c], "count": counts[c],
             "items": buckets[c]}
            for c in CATEGORIES
        ],
        "counts": counts,
        "total": len(items),
        # The default view. Three evidenced items, what is uncertain, and one next step —
        # because a list of seventeen is a list nobody starts.
        "top_three": buckets[ACTION_REQUIRED][:3],
        "uncertain_count": counts[NEEDS_VERIFICATION],
        "unassessed_count": counts[NOT_YET_ASSESSED],
        "best_next_action": _best_next_action(buckets, gaps),
        "executive_summary": _summary(counts, score, blind),
        "oldest_open_days": max(
            [i["age_days"] for i in items if i.get("age_days")] or [0]),
        "method": "COMPOSITION_ONLY",
        "composed_from": ["/api/brief", "/api/score", "/api/blindspots", "/api/cyber",
                          "/api/supply-chain", "/api/information-value",
                          "/api/compounding"],
        "language_rules": {
            "uncertain_is_not_a_gap": (
                "Something we cannot confirm is reported as needing verification, never "
                "as a gap."),
            "unassessed_is_not_uncovered": (
                "An area we did not examine is neutral. It is not rendered as a risk and "
                "carries no severity."),
            "not_colour_alone": (
                "Every status carries an icon and a label as well as a colour."),
        },
        "notice": ("This screen groups and words results the engine already produced. It "
                   "evaluates nothing of its own, so it cannot disagree with the screen "
                   "you came from."),
    }
    assert_language(result)
    return result


def _best_next_action(buckets: dict[str, list[dict]],
                      gaps: Optional[dict]) -> dict:
    """One thing to do next. Prefers the cheapest resolution, never the scariest item.

    Ranking by fear would put the most frightening unknown first, which is both unhelpful
    and unearned. The value-of-information ranking already computes which single document
    settles the most, so that is used where it exists.
    """
    top_gap = ((gaps or {}).get("gaps") or [None])[0]
    if top_gap:
        return {
            "what": f"Send us your {str(top_gap.get('document_needed') or '').lower()}",
            "because": str(top_gap.get("note") or ""),
            "settles": top_gap.get("uncertainty_removed"),
            "basis": "Ranked by how much it would settle, not by how bad it looks.",
        }
    if buckets[ACTION_REQUIRED]:
        first = buckets[ACTION_REQUIRED][0]
        return {"what": f"Look at: {first['title']}",
                "because": first["why"],
                "basis": "The one evidenced item at the top of your list."}
    return {"what": "Nothing is waiting on you.",
            "because": "No evidenced action is outstanding.",
            "basis": "Every item is either verified or not yet assessed."}


def _summary(counts: dict[str, int], score: Optional[dict],
             blind: Optional[dict]) -> str:
    action = counts[ACTION_REQUIRED]
    unsure = counts[NEEDS_VERIFICATION]
    unknown = counts[NOT_YET_ASSESSED]
    coverage = (blind or {}).get("coverage") or {}
    lead = (f"{action} thing(s) need action" if action
            else "Nothing needs action right now")
    parts = [lead]
    if unsure:
        parts.append(f"{unsure} cannot be confirmed yet")
    if unknown:
        parts.append(f"{unknown} area(s) have not been assessed")
    tail = ""
    if coverage.get("areas_total"):
        tail = (f" Your score covers {coverage.get('areas_we_can_speak_to')} of "
                f"{coverage.get('areas_total')} areas.")
    return ", and ".join(parts) + "." + tail


def assert_language(result: dict) -> None:
    """Refuse to serialise a summary that turns an unknown into a denial.

    §46 of the brief calls this non-negotiable, so it is a code path rather than a review
    note. The check runs over the ITEM text — the language-rules block deliberately quotes
    the forbidden phrases in order to forbid them, so it is exempt.
    """
    for cat in result.get("categories") or []:
        for item in cat.get("items") or []:
            # Verbatim engine output is exempt — see the note on `_item`. The check is on
            # wording this module chose, which is where a new dishonesty would enter.
            if item.get("verbatim_from_engine"):
                continue
            blob = f"{item.get('title','')} {item.get('why','')}".lower()
            for phrase in _FORBIDDEN:
                if phrase in blob:
                    raise AssertionError(
                        f"gapsummary: item {item.get('key')!r} states {phrase!r}, which "
                        f"converts an observation into a coverage denial")
            if item["category"] == NOT_YET_ASSESSED and item.get("tone") != "neutral":
                raise AssertionError(
                    f"gapsummary: {item.get('key')!r} is not assessed but is rendered "
                    f"as {item.get('tone')!r}; an unexamined area is neutral")


def catalogue() -> dict:
    return {
        "categories": [{"category": c, **CATEGORY_META[c]} for c in CATEGORIES],
        "method": "COMPOSITION_ONLY",
    }
