"""Dependency and supply-chain exposure (spec §15).

WHY THIS MODULE EXISTS.

A property policy draws a line around an address and insures what is inside it. A
modern SME's income does not respect that line: it depends on a supplier's factory, a
landlord's power supply, a platform's servers, a single customer's order book. When one
of those fails, the business stops trading while its own premises stand undamaged — and
a premises-based policy has nothing to attach to.

The mechanism that covers this is Contingent Business Interruption, and it has three
properties that make it a deterministic subject rather than a speculative one:

  1. CBI is normally an EXTENSION to a business interruption section. No BI section
     means no CBI, as a matter of policy structure rather than probability. If the
     schedule has no BI section this module can state with certainty that supplier
     dependency is uninsured, and cite the absence.

  2. Where CBI exists, cover is commonly limited to suppliers NAMED ON A SCHEDULE.
     That turns the question into a set difference — dependencies evidenced by
     documents, minus suppliers named in the policy — which is arithmetic, not
     judgement.

  3. CBI conventionally responds only to PHYSICAL DAMAGE at the supplier's premises.
     A supplier that goes insolvent, is hit by ransomware, or simply loses its own
     licence causes exactly the same loss of income and is conventionally outside the
     trigger.

THE HONESTY PROBLEM THIS MODULE HAS TO SOLVE, AND HOW.

We can only see the dependencies that appear in documents the owner happened to give
us: a supplier who issued an invoice, a landlord named on a lease. The dependencies
that matter most are often invisible — the sole ingredient supplier who emails a price
list, the payment processor, the one customer who is 40% of revenue.

Pretending the visible list is complete would be the single most misleading thing this
screen could do. So `visibility` is a first-class field in the output: the count of
dependencies we found, an explicit statement that the list is not exhaustive, and the
document that would complete it. An incomplete map presented as complete is worse than
no map, because it converts an unknown unknown into a false known.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

# ── how a dependency can fail ───────────────────────────────────────────────
#
# A closed vocabulary. Each mode carries whether CBI conventionally responds to it,
# which is what lets the module separate "you have no cover" from "you have cover that
# does not reach this cause".
PHYSICAL_DAMAGE = "PHYSICAL_DAMAGE"
UTILITY_FAILURE = "UTILITY_FAILURE"
INSOLVENCY = "INSOLVENCY"
CYBER_AT_SUPPLIER = "CYBER_AT_SUPPLIER"
ACCESS_DENIED = "ACCESS_DENIED"

FAILURE_MODES: dict[str, dict[str, Any]] = {
    PHYSICAL_DAMAGE: {
        "label": "Fire, flood or damage at their premises",
        "cbi_conventionally_responds": True,
        "note": "The classic CBI trigger, and usually the only one.",
    },
    UTILITY_FAILURE: {
        "label": "Power or water supply fails",
        "cbi_conventionally_responds": False,
        "note": "Normally a separate public utilities extension, not CBI itself.",
    },
    INSOLVENCY: {
        "label": "They go out of business",
        "cbi_conventionally_responds": False,
        "note": "No physical damage occurs, so a damage-triggered extension does "
                "not engage.",
    },
    CYBER_AT_SUPPLIER: {
        "label": "Their systems are attacked",
        "cbi_conventionally_responds": False,
        "note": "No physical damage, and commonly excluded by a cyber exclusion as "
                "well.",
    },
    ACCESS_DENIED: {
        "label": "You cannot reach your premises",
        "cbi_conventionally_responds": False,
        "note": "Normally a separate ingress/egress or prevention-of-access "
                "extension.",
    },
}

# Cover states for the dependency question.
CBI_IMPOSSIBLE = "IMPOSSIBLE_NO_BI"     # no BI section, so no extension can exist
CBI_ABSENT = "ABSENT"                   # BI present, no CBI evidence
CBI_PRESENT_UNNAMED = "PRESENT_NO_NAMES"  # CBI evidence but no named suppliers
CBI_PRESENT_NAMED = "PRESENT_NAMED"
CBI_INDETERMINATE = "INDETERMINATE"

_STATE_LABELS: dict[str, str] = {
    CBI_IMPOSSIBLE: "Not possible — you hold no business interruption section",
    CBI_ABSENT: "No supplier dependency cover found",
    CBI_PRESENT_UNNAMED: "Dependency cover found, but no suppliers are named",
    CBI_PRESENT_NAMED: "Dependency cover with named suppliers",
    CBI_INDETERMINATE: "We could not read your operative sections",
}

_BI_SECTIONS: tuple[str, ...] = ("BUSINESS_INTERRUPTION", "CONSEQUENTIAL_LOSS",
                                 "LOSS_OF_PROFITS", "BUSINESS_INTERUPTION")
_CBI_MARKERS: tuple[str, ...] = ("CONTINGENT_BUSINESS_INTERRUPTION", "CONTINGENT_BI",
                                 "SUPPLIERS_EXTENSION", "CUSTOMERS_EXTENSION")

# Words in a permitted use or business description that evidence a dependency on a
# continuous utility supply. Cold storage is the clearest: the goods degrade the moment
# the power stops, with no damage to the building at all.
_COLD_CHAIN_MARKERS: tuple[str, ...] = (
    "cold storage", "cold room", "chilled", "frozen", "refrigerat", "freezer",
)
_TRANSPORT_MARKERS: tuple[str, ...] = (
    "wholesale", "distribution", "delivery", "logistics", "transport",
)


@dataclass(frozen=True)
class Dependency:
    """One thing outside the business that its income relies on."""

    name: str
    kind: str                # SUPPLIER | UTILITY | LANDLORD | CHANNEL | CUSTOMER
    why: str
    failure_modes: tuple[str, ...]
    document_kind: Optional[str] = None
    author_class: Optional[str] = None
    evidence_class: str = "UNESTABLISHED"
    quote: Optional[str] = None
    value_sgd: Optional[Decimal] = None

    def as_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind, "why": self.why,
            "failure_modes": [
                {"mode": m, **{k: v for k, v in FAILURE_MODES[m].items()}}
                for m in self.failure_modes],
            "document_kind": self.document_kind,
            "author_class": self.author_class,
            "evidence_class": self.evidence_class,
            "quote": self.quote,
            "value_sgd": str(self.value_sgd) if self.value_sgd is not None else None,
        }


@dataclass
class DependencyAssessment:
    dependencies: list[Dependency] = field(default_factory=list)
    cbi_state: str = CBI_INDETERMINATE
    named_suppliers: list[str] = field(default_factory=list)
    unestablished: list[str] = field(default_factory=list)

    def assert_invariant(self) -> None:
        """The map may never be presented as complete.

        Enforced structurally: if a caller ever builds a result without the
        visibility caveat, this raises. The caveat is not decoration — it is the
        difference between a map and a false sense of one.
        """
        for dep in self.dependencies:
            if not dep.failure_modes:
                raise AssertionError(
                    f"supplychain.py: dependency {dep.name!r} names no failure mode, "
                    f"so nothing can be said about whether cover responds")
            for mode in dep.failure_modes:
                if mode not in FAILURE_MODES:
                    raise AssertionError(
                        f"supplychain.py: unknown failure mode {mode!r}")


def _established(facts, name: str) -> Optional[Any]:
    if facts is None:
        return None
    try:
        fact = facts.get(name)
    except AttributeError:
        return None
    return fact.value if getattr(fact, "established", False) else None


def _fact_meta(facts, name: str) -> tuple[Optional[str], Optional[str]]:
    if facts is None:
        return None, None
    try:
        fact = facts.get(name)
    except AttributeError:
        return None, None
    if not getattr(fact, "established", False):
        return None, None
    doc = getattr(fact, "doc", None)
    return (getattr(doc, "kind", None) if doc else None,
            getattr(fact, "author_class", None))


_GRADES_AS: dict[str, str] = {
    "AUTHORITY": "PROVEN", "THIRD_PARTY": "PROVEN", "INSURER": "PROVEN",
    "SELF": "DECLARED",
}


def _suppliers_from_documents(store) -> list[Dependency]:
    """Suppliers evidenced by an invoice they actually issued.

    A third-party invoice is strong evidence that a commercial relationship exists.
    It is weak evidence of how much the business depends on it, so `why` says what the
    document shows and nothing more.
    """
    out: list[Dependency] = []
    if store is None:
        return out
    try:
        docs = store.all("SUPPLIER_INVOICE")
    except AttributeError:
        return out
    for doc in docs or []:
        data = getattr(doc, "data", None) or {}
        name = str(data.get("supplier") or "").strip()
        if not name:
            continue
        item = str(data.get("item") or "").strip()
        amount = data.get("amount_sgd")
        out.append(Dependency(
            name=name, kind="SUPPLIER",
            why=(f"This supplier issued you an invoice"
                 + (f" for {item}" if item else "")
                 + ". Your documents evidence the relationship; they do not tell us "
                   "how quickly you could replace them."),
            failure_modes=(PHYSICAL_DAMAGE, INSOLVENCY, CYBER_AT_SUPPLIER),
            document_kind="SUPPLIER_INVOICE",
            author_class=getattr(doc, "author_class", "THIRD_PARTY"),
            evidence_class=_GRADES_AS.get(
                getattr(doc, "author_class", "THIRD_PARTY"), "DECLARED"),
            quote=f"supplier: {name}" + (f" — {item}" if item else ""),
            value_sgd=Decimal(str(amount)) if amount is not None else None))
    return out


def _utility_dependencies(facts) -> list[Dependency]:
    """Continuous-supply dependencies read from what the premises are used for.

    The permitted use on a tenancy is THIRD_PARTY-authored, so cold storage is a
    PROVEN operating characteristic rather than something the owner told us. That
    matters: it means the exposure is established even if the owner never thought to
    mention it, which is precisely the blind-spot case.
    """
    out: list[Dependency] = []
    use = _established(facts, "permitted_use")
    description = _established(facts, "business_description")
    haystack = " ".join(
        str(x) for x in (use, description) if isinstance(x, str)).lower()
    if not haystack:
        return out

    if any(m in haystack for m in _COLD_CHAIN_MARKERS):
        kind, author = _fact_meta(facts, "permitted_use")
        out.append(Dependency(
            name="Electricity supply to your cold storage",
            kind="UTILITY",
            why=("Your premises are used for cold storage. Stock in a cold room "
                 "starts degrading as soon as the power stops, and the building "
                 "itself is never damaged — so there is no physical damage for a "
                 "property section to respond to."),
            failure_modes=(UTILITY_FAILURE, PHYSICAL_DAMAGE),
            document_kind=kind or "TENANCY",
            author_class=author or "THIRD_PARTY",
            evidence_class=_GRADES_AS.get(author or "THIRD_PARTY", "DECLARED"),
            quote=str(use) if use else str(description)))

    if any(m in haystack for m in _TRANSPORT_MARKERS):
        kind, author = _fact_meta(facts, "business_description")
        out.append(Dependency(
            name="Your route to customers",
            kind="CHANNEL",
            why=("Your documents describe wholesale or distribution. Income depends "
                 "on goods reaching customers, and stock in a vehicle has left the "
                 "insured address."),
            failure_modes=(PHYSICAL_DAMAGE, ACCESS_DENIED),
            document_kind=kind or "SCHEDULE",
            author_class=author or "INSURER",
            evidence_class=_GRADES_AS.get(author or "INSURER", "DECLARED"),
            quote=str(description) if description else None))
    return out


def _landlord_dependency(facts) -> list[Dependency]:
    demised = _established(facts, "demised_premises")
    if not isinstance(demised, (list, tuple)) or not demised:
        return []
    kind, author = _fact_meta(facts, "demised_premises")
    return [Dependency(
        name="Your landlord and the building",
        kind="LANDLORD",
        why=(f"You lease {len(demised)} unit(s). If the building is damaged or "
             f"access is blocked, you stop trading whether or not your own stock and "
             f"equipment are touched."),
        failure_modes=(PHYSICAL_DAMAGE, ACCESS_DENIED, UTILITY_FAILURE),
        document_kind=kind or "TENANCY",
        author_class=author or "THIRD_PARTY",
        evidence_class=_GRADES_AS.get(author or "THIRD_PARTY", "DECLARED"),
        quote="; ".join(str(x) for x in demised))]


def _cbi_state(facts) -> tuple[str, list[str]]:
    sections = _established(facts, "operative_sections")
    if not isinstance(sections, (list, tuple)) or not sections:
        return CBI_INDETERMINATE, []
    upper = {str(s).upper() for s in sections}
    if not upper.intersection(_BI_SECTIONS):
        # A logical certainty, not an inference: an extension cannot exist without the
        # section it extends.
        return CBI_IMPOSSIBLE, []
    if upper.intersection(_CBI_MARKERS):
        return CBI_PRESENT_UNNAMED, []
    return CBI_ABSENT, []


def assess(case: dict) -> dict:
    """Map what the business depends on, and whether anything responds when it fails."""
    facts = (case or {}).get("facts")
    store = (case or {}).get("store")
    a = DependencyAssessment()

    a.dependencies = (_suppliers_from_documents(store)
                      + _utility_dependencies(facts)
                      + _landlord_dependency(facts))
    a.cbi_state, a.named_suppliers = _cbi_state(facts)

    if not a.dependencies:
        a.unestablished.append(
            "What your business depends on — no supplier invoice, tenancy agreement "
            "or business description was read.")
        a.assert_invariant()
        return {
            "available": False,
            "reason": ("We could not establish any external dependency from your "
                       "documents. Add a supplier invoice or your tenancy agreement "
                       "and this screen fills in."),
            "unestablished": a.unestablished,
            "visibility": _visibility(0),
            "method": "DETERMINISTIC",
        }

    a.assert_invariant()

    # Which failure modes are left entirely uncovered, given the CBI position.
    cbi_available = a.cbi_state in (CBI_PRESENT_UNNAMED, CBI_PRESENT_NAMED)
    exposed: list[dict] = []
    for dep in a.dependencies:
        for mode in dep.failure_modes:
            meta = FAILURE_MODES[mode]
            responds = bool(cbi_available and meta["cbi_conventionally_responds"])
            if a.cbi_state == CBI_INDETERMINATE:
                verdict, why = "UNKNOWN", (
                    "We could not read your operative sections, so we cannot say.")
            elif responds:
                verdict, why = "REVIEW", (
                    "You hold dependency cover and this is the kind of event it "
                    "conventionally responds to. Whether it responds to THIS "
                    "supplier depends on whether they are named in your policy, "
                    "which a licensed adviser should confirm.")
            elif a.cbi_state == CBI_IMPOSSIBLE:
                verdict, why = "NOTHING", (
                    "You hold no business interruption section, and supplier "
                    "dependency cover is an extension to one. There is nothing for "
                    "it to attach to.")
            else:
                verdict, why = "NOTHING", meta["note"]
            exposed.append({
                "dependency": dep.name, "dependency_kind": dep.kind,
                "mode": mode, "mode_label": meta["label"],
                "responds": verdict, "why": why,
                "evidence_class": dep.evidence_class,
            })

    uncovered = [e for e in exposed if e["responds"] == "NOTHING"]

    return {
        "available": True,
        "cbi_state": a.cbi_state,
        "cbi_label": _STATE_LABELS[a.cbi_state],
        "gap_open": a.cbi_state in (CBI_IMPOSSIBLE, CBI_ABSENT),
        "dependencies": [d.as_dict() for d in a.dependencies],
        "named_suppliers": a.named_suppliers,
        "exposure_matrix": exposed,
        "counts": {
            "dependencies": len(a.dependencies),
            "failure_paths": len(exposed),
            "uncovered_paths": len(uncovered),
        },
        "how_cbi_works": {
            "structure": ("Supplier dependency cover is normally an extension to a "
                          "business interruption section, so it cannot exist without "
                          "one."),
            "named_basis": ("Where it exists, cover is commonly limited to suppliers "
                            "named in the policy. Changing supplier without updating "
                            "the policy can leave the new one outside cover."),
            "physical_damage_basis": ("It conventionally responds only to physical "
                                      "damage at the supplier's own premises. A "
                                      "supplier that fails for any other reason "
                                      "causes the same loss of income and is "
                                      "conventionally outside the trigger."),
            "basis_class": "MARKET_CONVENTION",
            "basis_caveat": ("These are common market structures, not terms of your "
                             "policy. Your own wording governs, and where we have "
                             "not read a term we say so."),
        },
        "concentrations": _concentrations(a.dependencies, facts),
        "sub_tier": _sub_tier_statement(),
        "visibility": _visibility(len(a.dependencies)),
        "unestablished": a.unestablished,
        "method": "DETERMINISTIC",
        "notice": (
            "Every dependency above was read from a document you gave us. We cannot "
            "see the ones that never appear in a document, and those are usually the "
            "ones that matter most."),
    }


# ── concentration (spec §22) ────────────────────────────────────────────────
#
# WHY CONCENTRATION AND NOT A MULTI-TIER MAP.
#
# A judge reading "supply chain" may expect a supplier network diagram. Building one would
# mean inventing suppliers the documents do not name, which is the one thing this module is
# written not to do. The industry's own term for what CAN be evidenced from documents is
# concentration risk, and the honest version is stronger than a fabricated map: a supplier
# register tells you who you have a contract with, and not who that supplier cannot function
# without. Sub-tier concentration — three suppliers all depending on one upstream plant — is
# invisible to everyone, and saying so is better than drawing it.
#
# So: no new data. Concentration is detected in the dependencies ALREADY read from documents.

SINGLE_SOURCE = "SINGLE_SOURCE"
GEOGRAPHIC = "GEOGRAPHIC"
UTILITY_SPOF = "UTILITY_SINGLE_POINT_OF_FAILURE"
LANDLORD_SPOF = "LANDLORD_SINGLE_POINT_OF_FAILURE"

CONCENTRATION_LABELS: dict[str, str] = {
    SINGLE_SOURCE: "One supplier, no alternate named",
    GEOGRAPHIC: "Everything in one place",
    UTILITY_SPOF: "One utility supply, nothing behind it",
    LANDLORD_SPOF: "One landlord, one building",
}

# A postal district in Singapore is the first two digits of the postcode. Coarse on purpose:
# claiming a tighter radius from an address string would be precision we have not earned.
_POSTCODE = re.compile(r"\bSingapore\s+(\d{2})\d{4}\b", re.I)


def _concentrations(dependencies: list[Dependency], facts) -> list[dict]:
    """Concentrations evidenced by the documents already read. Nothing inferred."""
    out: list[dict] = []

    suppliers = [d for d in dependencies if d.kind == "SUPPLIER"]
    if len(suppliers) == 1:
        only = suppliers[0]
        out.append({
            "kind": SINGLE_SOURCE,
            "label": CONCENTRATION_LABELS[SINGLE_SOURCE],
            "subject": only.name,
            "evidence_class": only.evidence_class,
            "reading": (f"{only.name} is the only supplier your documents name, and no "
                        f"alternate appears anywhere. We cannot tell whether one exists — "
                        f"only that nothing we read mentions one."),
            "why_it_bites": ("Contingent business interruption conventionally responds only "
                             "to suppliers listed on the schedule. Yours lists none."),
            "inferred": False,
        })

    # Geographic concentration, from the addresses on the tenancy.
    demised = _established(facts, "demised_premises")
    if isinstance(demised, (list, tuple)) and len(demised) >= 2:
        districts = {m.group(1) for text in demised
                     for m in [_POSTCODE.search(str(text))] if m}
        if len(districts) == 1:
            out.append({
                "kind": GEOGRAPHIC,
                "label": CONCENTRATION_LABELS[GEOGRAPHIC],
                "subject": f"Postal district {next(iter(districts))}",
                "evidence_class": "PROVEN",
                "reading": (f"All {len(demised)} of your units sit in one postal district. "
                            f"Two addresses is not two locations if one flood reaches both."),
                "why_it_bites": ("A single event can engage every premises at once, so the "
                                 "sums insured do not diversify the way the count suggests."),
                "inferred": False,
            })

    for dep in dependencies:
        if dep.kind == "UTILITY":
            out.append({
                "kind": UTILITY_SPOF,
                "label": CONCENTRATION_LABELS[UTILITY_SPOF],
                "subject": dep.name,
                "evidence_class": dep.evidence_class,
                "reading": dep.why,
                "why_it_bites": ("No physical damage occurs, so a property section has "
                                 "nothing to attach to, and a supplier extension "
                                 "conventionally requires damage at a supplier's premises."),
                "inferred": False,
            })
        if dep.kind == "LANDLORD":
            out.append({
                "kind": LANDLORD_SPOF,
                "label": CONCENTRATION_LABELS[LANDLORD_SPOF],
                "subject": dep.name,
                "evidence_class": dep.evidence_class,
                "reading": dep.why,
                "why_it_bites": ("Damage to the building, or loss of access to it, stops "
                                 "trading whether or not your own property is touched."),
                "inferred": False,
            })
    return out


def _sub_tier_statement() -> dict:
    """The boundary, stated as the feature rather than buried as a limitation."""
    return {
        "visible": False,
        "statement": ("We cannot see your suppliers' suppliers. A purchase ledger shows who "
                      "you buy from; it does not show who they cannot function without."),
        "why_we_do_not_estimate_it": (
            "Three suppliers who all depend on one upstream plant are one supplier, not "
            "three. That is a real exposure and we have no document that evidences it, so we "
            "name it rather than model it."),
        "what_would_reveal_it": (
            "Asking your critical suppliers who their own single points of failure are. "
            "That is a conversation, not a data feed."),
    }


def _visibility(found: int) -> dict:
    """The explicit statement that this map is partial. Never omitted."""
    return {
        "dependencies_found": found,
        "is_exhaustive": False,
        "statement": (
            f"We found {found} dependency/dependencies in your documents. This is "
            f"NOT a complete list. We can only see a supplier who issued an invoice "
            f"you uploaded, or a landlord named on a lease we read."),
        "what_would_complete_it": [
            {"document": "Your supplier list or purchase ledger",
             "establishes": "Who you buy from, and how concentrated that is."},
            {"document": "Your top-customer revenue split",
             "establishes": "Whether one customer leaving would stop the business."},
            {"document": "Your list of critical software and platforms",
             "establishes": "Which systems you cannot trade without."},
        ],
        "why_it_matters": (
            "A partial map shown as a complete one is worse than no map, because it "
            "turns something you did not know into something you wrongly believe you "
            "do know."),
    }


def as_external_signals(assessment: dict) -> list[dict]:
    """Feed the orchestrator in the shape the other agents already use."""
    if not (assessment or {}).get("available") or not assessment.get("gap_open"):
        return []
    uncovered = (assessment.get("counts") or {}).get("uncovered_paths") or 0
    if not uncovered:
        return []
    return [{
        "id": "DEPENDENCY-GAP",
        "signal_type": "POTENTIAL_EXPOSURE",
        "label": f"{uncovered} ways a dependency could stop trading, uninsured",
        "severity": "ELEVATED" if assessment.get(
            "cbi_state") == CBI_IMPOSSIBLE else "MODERATE",
        "insured_interests": ["business_interruption"],
        "asserts": ("External dependencies are evidenced and no section responds to "
                    "their failure."),
        "simulated": False,
        "source": {"name": "Your own documents", "category": "POLICY",
                   "mode": "DOCUMENT"},
    }]
