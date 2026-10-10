"""Layer 3 — rule loader and executor.

Rules are DATA (rules/*.yaml): reviewer-readable headline, citations, output state
and `never` flags. The consequential evaluation for each rule is deterministic
Python, keyed by rule id below. No model sits in this path. This is the design rule
"the model never decides anything consequential" made concrete: every decision here
is a string, integer, date or Decimal comparison in code a domain reviewer can read
against the YAML.

TWO STRUCTURAL RULES, both of which used to be missing.

  1. Evaluators read `ctx.facts` (extract.py), never raw document JSON. A rule that
     indexed `sched.data["property_section"]["items"]` worked on one hand-built
     fixture and raised KeyError on every real document. Reading normalised facts is
     what lets an uploaded bundle produce findings, and lets an INCOMPLETE bundle
     produce an honest "not established" instead of a crash.

  2. A finding may only rest on a taxonomy code classified by the hand-verified
     `(carrier, clause_ref)` map. `ctx.verified_codes` carries that set. A
     HEADING_KEYWORD classification shrinks the unaccounted bucket but may not feed a
     claim, because nobody read the clause.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

import copy
import re

import yaml

from . import account, grading, jurisdiction
from .extract import Fact, PolicyFacts
from .gates import gate
from .ingest import DocStore, make_span, page_text
from .models import Finding, Operand, Span
from .taxonomy import Taxonomy

RULES_DIR = Path(__file__).resolve().parent / "rules"

# Language that must never appear in a rule identifier. The product's cardinal rule
# is "never assert breach", and a rule_id is rendered in the API payload, the eval
# report and the UI — so an id that adjudicates defeats the renderer's `never` guard
# by going around it. Asserted in test_invariants.py.
FORBIDDEN_RULE_ID_WORDS = ("BREACH", "BREACHED", "VIOLATION", "VOID", "DENIED",
                           "GUILTY", "NONCOMPLIANT", "NON_COMPLIANT")


def load_rules(rules_dir: Path | str = RULES_DIR) -> dict[str, dict]:
    """Load every rule from every YAML file. Returns {rule_id: rule_dict}."""
    rules: dict[str, dict] = {}
    for path in sorted(Path(rules_dir).glob("*.yaml")):
        for rule in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            rules[rule["id"]] = rule
    return rules


# ── binding the rules to the jurisdiction pack that applies ─────────────────
#
# A rule names the country whose law it is written against (`jurisdiction: SG`) and the
# pack entries its sentences quote figures from (`pack_values`). Neither the country nor
# the figures live in this file. `bind()` turns the loaded rules into the set that applies
# to ONE case:
#
#   * a rule for another country is NOT RUN, and is reported by name — so a Malaysian
#     wording is never read against Singapore law, and the reader is told what was not done;
#   * a figure the finding itself states (in its title or headline) must come from an entry
#     the pack verifies for owner-facing use, or the rule is not run either — a finding
#     cannot be stated without its own threshold;
#   * a figure only a SUPPORTING sentence states (a consequence note, a citation, a
#     deadline) drops THAT sentence when its entry is not verified, and the finding stands.
#     That is PLAN rule 18: an unverified source never gates an existing finding; the
#     sentence goes instead.

#: Fields that ARE the finding. A pack value they need must be verified or the rule is not run.
_FINDING_FIELDS: tuple[str, ...] = ("title", "headline")

#: `{name}`, `{name.money}` (the pack's currency, e.g. S$60,000), `{name.local}` (as the
#: country's own authorities write it, e.g. $2,600), `{name.millions}` (S$1 million) and
#: `{name.long_date}` (1 October 2022). Any other `{field}` belongs to the evaluator.
_PLACEHOLDER = re.compile(r"\{(\w+)(?:\.(money|local|millions|long_date))?\}")


def applies_under(rule: dict, pack: "jurisdiction.Pack") -> bool:
    """True when the rule is written for this pack's country, or for no country at all."""
    home = rule.get("jurisdiction")
    return home is None or home == pack.code


def _resolve_pack_values(rule: dict, pack: "jurisdiction.Pack"
                         ) -> tuple[dict[str, Any], dict[str, str]]:
    """(name -> value for every verified value, name -> why for every other one)."""
    values: dict[str, Any] = {}
    unverified: dict[str, str] = {}
    for name, ref in (rule.get("pack_values") or {}).items():
        local_id, _, key = str(ref).partition(":")
        entry = pack.find(local_id)
        if entry is None or not key or key not in entry.values:
            unverified[name] = (f"the {pack.name} pack holds no {pack.code}-{local_id} "
                                f"value {key!r}")
        elif not entry.owner_facing:
            unverified[name] = (f"{entry.id} is {entry.verification.status}, so no "
                                f"sentence may be built from it")
        else:
            values[name] = entry.values[key]
    return values, unverified


def _provenance(rule: dict, pack: "jurisdiction.Pack", names: set[str]) -> list[dict]:
    """The quote, source link and status of every pack entry a bound rule still states a
    figure from — PLAN rule 17's display requirement, carried with the finding. An entry
    whose only sentence was dropped is not listed: nothing shown rests on it any more."""
    out: dict[str, dict] = {}
    for name in sorted(names):
        local_id = str((rule.get("pack_values") or {}).get(name, "")).partition(":")[0]
        entry = pack.find(local_id)
        if entry is not None and entry.owner_facing and entry.id not in out:
            out[entry.id] = entry.provenance()
    return list(out.values())


def _render_value(value: Any, form: Optional[str],
                  pack: "jurisdiction.Pack") -> Optional[str]:
    if form is None:
        return str(value)
    if form == "long_date":
        return jurisdiction.long_date(value)
    if pack.currency is None:
        return None
    if form == "money":
        return pack.currency.format(value)
    if form == "local":
        return pack.currency.format(value, local=True)
    return pack.currency.millions(value)                 # form == "millions"


def _fill(text: str, rule: dict, values: dict, unverified: dict,
          pack: "jurisdiction.Pack", used: Optional[set[str]] = None
          ) -> tuple[str, list[str]]:
    """Substitute the pack placeholders in one sentence. Returns (text, what could not be
    filled). Placeholders that are not pack values are left for the evaluator. When the
    sentence fills completely, the pack values it used are added to `used`."""
    declared = set(rule.get("pack_values") or {})
    missing: list[str] = []
    names: set[str] = set()

    def sub(m: "re.Match[str]") -> str:
        name, form = m.group(1), m.group(2)
        if name not in declared:
            return m.group(0)
        if name in unverified:
            missing.append(unverified[name])
            return m.group(0)
        rendered = _render_value(values[name], form, pack)
        if rendered is None:
            missing.append(f"the {pack.name} pack states no currency to write "
                           f"{name} in")
            return m.group(0)
        names.add(name)
        return rendered

    filled = _PLACEHOLDER.sub(sub, text)
    if used is not None and not missing:
        used.update(names)
    return filled, missing


def _home_title(rule: dict) -> str:
    """The rule's title as its own country's pack writes it, for the not-applied list."""
    home = rule.get("jurisdiction")
    title = str(rule.get("title") or rule.get("id"))
    if not home or not jurisdiction.has_pack(home):
        return _PLACEHOLDER.sub("…", title)
    home_pack = jurisdiction.pack(home)
    values, unverified = _resolve_pack_values(rule, home_pack)
    text, missing = _fill(title, rule, values, unverified, home_pack)
    return _PLACEHOLDER.sub("…", text) if missing else text


def bind(all_rules: dict[str, dict], pack: "jurisdiction.Pack"
         ) -> tuple[dict[str, dict], list[dict], list[dict]]:
    """The rules that apply under `pack`, with their figures filled in from it.

    Returns `(rules, not_applied, sentences_dropped)`. The loaded rules are not modified:
    each applicable rule is a copy, so two cases read under two packs can never share a
    filled-in sentence.
    """
    bound: dict[str, dict] = {}
    not_applied: list[dict] = []
    dropped: list[dict] = []
    for rid, rule in all_rules.items():
        home = rule.get("jurisdiction")
        if not applies_under(rule, pack):
            name = jurisdiction.country_name(home)
            reason = (f"A {name} rule. These documents are not established as {name} "
                      f"documents, so {name} law is not applied to them."
                      if pack.is_generic else
                      f"A {name} rule. These documents are read under the {pack.name} "
                      f"pack, so {name} law is not applied to them.")
            not_applied.append({"rule_id": rid, "title": _home_title(rule),
                                "jurisdiction": home, "jurisdiction_name": name,
                                "reason": reason})
            continue

        values, unverified = _resolve_pack_values(rule, pack)
        out = copy.deepcopy({k: v for k, v in rule.items() if k != "pack_values"})
        used: set[str] = set()
        blocked: list[str] = []
        for fld in _FINDING_FIELDS:
            if isinstance(out.get(fld), str):
                out[fld], missing = _fill(out[fld], rule, values, unverified, pack, used)
                blocked.extend(missing)
        if blocked:
            not_applied.append({
                "rule_id": rid, "title": _home_title(rule), "jurisdiction": home,
                "jurisdiction_name": jurisdiction.country_name(home) if home else None,
                "reason": ("The finding states a figure the pack does not verify for "
                           "owner-facing use: " + "; ".join(sorted(set(blocked))) + ".")})
            continue

        if isinstance(out.get("consequence_note"), str):
            note, missing = _fill(out["consequence_note"], rule, values, unverified, pack,
                                  used)
            if missing:
                del out["consequence_note"]
                dropped.append({"rule_id": rid, "sentence": "consequence_note",
                                "reason": "; ".join(sorted(set(missing)))})
            else:
                out["consequence_note"] = note

        kept_cites = []
        for cite in out.get("cite") or []:
            if isinstance(cite, dict) and isinstance(cite.get("proposition"), str):
                text, missing = _fill(cite["proposition"], rule, values, unverified, pack,
                                      used)
                if missing:
                    dropped.append({"rule_id": rid, "sentence": "cite",
                                    "reason": "; ".join(sorted(set(missing)))})
                    continue
                cite = {**cite, "proposition": text}
            kept_cites.append(cite)
        if "cite" in out:
            out["cite"] = kept_cites

        deadline = out.get("deadline")
        if isinstance(deadline, dict) and "days_before" in deadline:
            name = deadline["days_before"]
            if name in values:
                out["deadline"] = {"basis": deadline.get("basis"),
                                   "offset_days": -int(values[name])}
                used.add(name)
            else:
                del out["deadline"]
                dropped.append({"rule_id": rid, "sentence": "deadline",
                                "reason": unverified.get(name, f"no value {name!r}")})

        # The figures the evaluator compares against, already verified. Never read the
        # pack again from inside an evaluator: one source per figure per case.
        out["_values"] = values
        out["_sources"] = _provenance(rule, pack, used)
        bound[rid] = out
    return bound, not_applied, dropped


def required_base_codes(rule: dict) -> set[str]:
    """Taxonomy codes this rule needs from the BASE WORDING, read from the rule's own
    `requires:` block. Nullable requirements are excluded — a null threshold is a
    measured fact (MSIG states none), not a missing input."""
    codes: set[str] = set()
    for req in rule.get("requires", []) or []:
        src = req.get("from") or {}
        if src.get("document_kind") != "BASE_WORDING":
            continue
        if req.get("nullable"):
            continue
        code = src.get("taxonomy_code")
        if code:
            codes.add(code)
    return codes


@dataclass
class Context:
    conn: sqlite3.Connection
    case_id: str
    store: DocStore
    base_doc_id: Optional[str]
    carrier: str
    taxonomy: Taxonomy
    rules: dict[str, dict]
    facts: PolicyFacts = field(default_factory=PolicyFacts)
    loss_scenario: dict = field(default_factory=dict)
    unresolved_codes: set = field(default_factory=set)
    verified_codes: set = field(default_factory=set)
    # The pack the case is read under. GENERIC unless the pipeline says otherwise: a
    # context built without one must not be read as any particular country.
    pack: "jurisdiction.Pack" = field(default_factory=lambda: jurisdiction.generic())

    def clause_basis_available(self, rule_id: str) -> bool:
        """True when a human has verified every base-wording clause this rule cites,
        for THIS carrier.

        `verified_codes` comes from `account.mapped_codes(carrier)` — the hand-built
        `(carrier, clause_ref)` table — never from the heading-keyword classifier. So
        a wording nobody has read produces no clause-cited findings, and a keyword
        guess can never be laundered into a claim.
        """
        rule = self.rules.get(rule_id)
        if rule is None:
            return False
        return required_base_codes(rule) <= set(self.verified_codes)


# ── operand helpers ─────────────────────────────────────────────────────────

def _fact_operand(ctx: Context, name: str, fact: Fact) -> Operand:
    """Build an operand from an extracted fact. The span is anchored in the source
    document's stored text so the fidelity gate can verify it; evidence_class is
    DERIVED from that document's author_class. An unestablished fact yields an
    operand with no span, which is what makes gate 3 fire."""
    if not fact.established:
        return Operand(name=name, value=None, unit=fact.unit, span_id=None,
                       evidence_class=grading.grade_operand(None, "SELF"))
    span = make_span(ctx.conn, fact.doc.id, fact.page_no, fact.needle)
    span_id = span.id if span else None
    return Operand(
        name=name, value=str(fact.value), unit=fact.unit, span_id=span_id,
        evidence_class=grading.grade_operand(span_id, fact.doc.author_class),
        author_class=fact.doc.author_class,
    )


def _base_operand(ctx: Context, name: str, needle: str) -> Operand:
    """Build an operand anchored to a clause in the base wording (INSURER)."""
    span = None
    if ctx.base_doc_id:
        span = account.find_clause_span(ctx.conn, ctx.base_doc_id, needle)
    span_id = span.id if span else None
    return Operand(
        name=name, value=needle, unit=None, span_id=span_id,
        evidence_class=grading.grade_operand(span_id, "INSURER"),
        author_class="INSURER",
    )


def _spans_of(ops: list[Operand], ctx: Context) -> list[Span]:
    spans: list[Span] = []
    for o in ops:
        if o.span_id is None:
            continue
        row = ctx.conn.execute(
            "SELECT * FROM span WHERE id=?", (o.span_id,)
        ).fetchone()
        if row:
            spans.append(Span(
                id=row["id"], document_id=row["document_id"], page_no=row["page_no"],
                char_start=row["char_start"], char_end=row["char_end"],
                quoted_text=row["quoted_text"], sha256=row["sha256"]))
    return spans


def _within_provision(ctx: Context) -> Callable[[Finding], bool]:
    """Gate 2 (boundary). Each base-wording span must sit inside the provision the
    finding is attributed to. The enclosing provision is the nearest clause heading
    at or before the span, found by scanning the span's OWN page text (with a walk
    back to earlier pages for clause bodies that continue onto the next page, e.g.
    cl 3.20). Scanning the live page rather than a deduped index matters because
    Chubb reuses a clause number across sections — 4.2 is both Average (p15) and an
    Electronic Equipment exclusion (p9). The enclosing heading must be consistent
    with a clause the finding cites (equal, or one a prefix of the other, so a body
    under 5.1(c) satisfies a citation of 5.1(c)(i)). A span under an unrelated clause
    is out of bounds and escalates rather than asserting.

    The enclosing-reference lookup and the citation-consistency test both live in
    account.py so the citation audit measures the same rule this gate enforces.
    """
    page_cache: dict[int, list[tuple[int, str]]] = {}

    def check(finding: Finding) -> bool:
        if not ctx.base_doc_id:
            return True
        cited = {c.get("clause") for c in finding.detail.get("cite", [])
                 if isinstance(c, dict) and c.get("clause")}
        for s in finding.spans:
            if s.document_id != ctx.base_doc_id:
                continue                     # only base-wording spans are bounded
            enc = account.enclosing_provision_ref(
                lambda p: page_text(ctx.conn, ctx.base_doc_id, p),
                s.page_no, s.char_start, page_cache)
            if not account.citation_consistent(enc, cited):
                return False
        return True

    return check


def _finalise(ctx: Context, finding: Finding) -> Finding:
    """Grade, attach spans, run all four abstention gates."""
    # Every figure this finding states from a jurisdiction pack travels with its quote,
    # source link and verification status (PLAN rule 17), so whatever renders the finding
    # can show where the statement about the law comes from.
    sources = (ctx.rules.get(finding.rule_id) or {}).get("_sources")
    if sources:
        finding.detail["pack_sources"] = sources
    finding.spans = _spans_of(finding.operands, ctx)
    finding.evidence_class = grading.grade_finding(
        finding.operands, finding.detail.get("computation_span"),
        has_computation=finding.detail.get("has_computation", False))
    return gate(finding, lambda d, p: page_text(ctx.conn, d, p),
                within_provision=_within_provision(ctx))


def _not_established(
    ctx: Context, rule_id: str, missing_facts: list[str],
    missing_docs: Optional[list[str]] = None, clause_gap: bool = False,
) -> Finding:
    """The honest output when a rule's inputs are not in the bundle.

    This is the shape that makes the engine usable on a document it has never seen:
    instead of a KeyError or a silent omission, the reader is told which rule could
    not run and exactly what is missing. It is graded UNESTABLISHED and stated as
    INFORMATION_MISSING, so it can never be mistaken for a clean result.
    """
    rule = ctx.rules.get(rule_id, {})
    title = rule.get("title") or rule_id
    op = Operand(name="required_inputs", value=None, unit=None, span_id=None,
                 evidence_class=grading.grade_operand(None, "SELF"))
    wants: list[str] = []
    if missing_docs:
        wants.append("documents not provided: " + ", ".join(
            sorted(set(d.replace("_", " ").lower() for d in missing_docs))))
    if missing_facts:
        wants.append("values not found: " + ", ".join(
            sorted(set(f.replace("_", " ") for f in missing_facts))))
    if clause_gap:
        wants.append(
            f"this wording's clause references are not in the verified map for "
            f"carrier {ctx.carrier or 'UNKNOWN'}, so no clause can be cited for it")
    detail = {"missing_facts": sorted(set(missing_facts)),
              "missing_documents": sorted(set(missing_docs or [])),
              "clause_basis_missing": clause_gap}
    if rule.get("cite"):
        detail["cite"] = rule["cite"]
    return _finalise(ctx, Finding(
        rule_id=rule_id, rule_version=int(rule.get("version", 1)),
        output_state="INFORMATION_MISSING", evidence_class="",
        headline=(f"Could not be established from the documents provided: {title}. "
                  f"Missing — {'; '.join(wants) if wants else 'required inputs'}. "
                  f"Add the document and re-run; the engine will not estimate it."),
        operands=[op], required_operands=[op], detail=detail))


# Which document kind each fact should have come from, for the "what's missing"
# message. Data, so the message stays true when the extractor changes.
FACT_SOURCE_KIND = {
    "stock_sum_insured": "SCHEDULE",
    "plant_sum_insured": "SCHEDULE",
    "buildings_sum_insured": "SCHEDULE",
    "deductible": "SCHEDULE",
    "theft_full_elected": "SCHEDULE",
    "inception": "SCHEDULE",
    "premium_received": "SCHEDULE",
    "scheduled_premises": "SCHEDULE",
    "operative_sections": "SCHEDULE",
    "business_description": "SCHEDULE",
    "stock_value": "ACCOUNTS",
    "contributor_count": "CPF_RECORD",
    "declared_headcount": "WIC_SCHEDULE",
    "workpass_lowest_medical": "WORK_PASS_LIST",
    "workpass_below_minimum_count": "WORK_PASS_LIST",
    "demised_premises": "TENANCY",
    "permitted_use": "TENANCY",
    "acra_secondary_activity": "ACRA_PROFILE",
    "invoice_equipment_value": "SUPPLIER_INVOICE",
}


def _guard(ctx: Context, rule_id: str, *required: str,
           needs_clause: bool = True) -> Optional[Finding]:
    """Shared precondition check. Returns a `not established` Finding when the rule
    cannot run, or None when it can."""
    missing = ctx.facts.missing(*required)
    clause_gap = needs_clause and not ctx.clause_basis_available(rule_id)
    if not missing and not clause_gap:
        return None
    missing_docs = [FACT_SOURCE_KIND[f] for f in missing if f in FACT_SOURCE_KIND
                    and FACT_SOURCE_KIND[f] not in ctx.facts.kinds_present]
    return _not_established(ctx, rule_id, missing, missing_docs, clause_gap)


# ── per-rule evaluators ─────────────────────────────────────────────────────

def _theft(ctx: Context) -> Optional[Finding]:
    rule_id = "THEFT-FORCIBLE-ENTRY-NOT-WRITTEN-BACK"
    blocked = _guard(ctx, rule_id, "theft_full_elected")
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]
    elected_fact = ctx.facts.get("theft_full_elected")
    if str(elected_fact.value).strip().lower() == "applicable":
        return None                     # extension elected -> nothing to report
    ops = [
        _base_operand(ctx, "theft_exclusion", "there is violent or forcible entry"),
        _fact_operand(ctx, "full_theft_elected", elected_fact),
    ]
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]), operands=ops, required_operands=ops,
        detail={"cite": rule["cite"]}))


def _average(ctx: Context) -> Optional[Finding]:
    from .arithmetic import settle

    rule_id = "AVERAGE-ADEQUACY-BELOW-THRESHOLD"
    blocked = _guard(ctx, rule_id, "stock_sum_insured", "stock_value")
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]

    si_fact = ctx.facts.get("stock_sum_insured")
    var_fact = ctx.facts.get("stock_value")
    sum_insured = Decimal(str(si_fact.value))
    stock_value = Decimal(str(var_fact.value))
    if stock_value <= 0:
        # Division by a zero or negative value at risk is not a finding, it is a
        # document we misread. Abstain rather than emit an absurd ratio.
        return _not_established(ctx, rule_id, ["stock_value"])
    deductible_fact = ctx.facts.get("deductible", unit="SGD")
    deductible = Decimal(str(deductible_fact.value)) \
        if deductible_fact.established else Decimal("0")

    # Which average provision governs can depend on the basis of settlement, and the
    # schedule does not always state it. Liberty carries two average provisions with
    # DIFFERENT thresholds — none on a value basis, 85% on a reinstatement basis — so
    # picking one silently would either remove a cushion the policy grants or apply
    # one it does not. Both are wrong answers. Read the basis if the documents
    # establish it; otherwise record that they do not.
    basis_fact = ctx.facts.get("basis_of_settlement")
    settlement_basis = str(basis_fact.value).upper() \
        if basis_fact.established and basis_fact.value else None
    basis_ambiguous = ctx.taxonomy.average_basis_ambiguous(
        ctx.carrier, settlement_basis)

    threshold = ctx.taxonomy.average_threshold(ctx.carrier, settlement_basis)
    formula_stated = ctx.taxonomy.average_formula_stated(
        ctx.carrier, settlement_basis)
    candidates = ctx.taxonomy.average_instances_for(ctx.carrier, settlement_basis)

    ops = [
        _fact_operand(ctx, "sum_insured", si_fact),
        _fact_operand(ctx, "value_at_risk", var_fact),
        _base_operand(ctx, "threshold_pct", "less than 85% of the actual value"),
    ]

    triggered = (sum_insured < (threshold * stock_value)) if threshold is not None \
        else (sum_insured < stock_value)
    if not triggered:
        return None

    loss = Decimal(str(ctx.loss_scenario.get("assessed_stock_loss_sgd", 60000)))

    # The computation span must be a REAL span in this wording, not a marker. Where
    # the taxonomy records that a carrier states the arithmetic, we locate and quote
    # it; if it cannot be located in the document in hand, the computation stays
    # MARKET_CONVENTION. Conservative in the direction that under-claims.
    formula_span = None
    formula_note = None
    if formula_stated and ctx.base_doc_id:
        span = account.find_clause_span(
            ctx.conn, ctx.base_doc_id, "such proportion of the loss")
        if span is not None:
            formula_span = span.id
        else:
            formula_note = (
                "The taxonomy records this carrier as stating the average "
                "arithmetic, but it could not be located in the document supplied. "
                "Treated as market convention.")

    s = settle(loss=loss, sum_insured=sum_insured, value_at_risk=stock_value,
               threshold_pct=threshold, sub_limit=None, deductible=deductible,
               formula_span=formula_span)

    detail = {
        "cite": rule["cite"],
        "computation_span": formula_span,
        "has_computation": True,
        "loss": str(loss),
        "adequacy_ratio": _pct(s.adequacy_ratio),
        "threshold_basis": s.threshold_basis,
        "threshold_pct": (None if threshold is None else _pct(threshold)),
        "illustration": str(s.illustration),
        "formula_basis": s.formula_basis,
        "formula_basis_class": s.formula_basis_class,
        "order_applied": s.order_applied,
        "deductible": str(deductible),
        "deductible_basis": ("read from the schedule"
                             if deductible_fact.established
                             else "no deductible found in the documents; treated as "
                                  "zero for the illustration"),
    }
    if formula_note:
        detail["formula_note"] = formula_note

    output_state = rule["output_state"]
    if basis_ambiguous:
        # Two average provisions, different thresholds, basis not established by the
        # documents. Report both and let a human decide which governs. The figure
        # shown uses the harsher threshold so the illustration under-claims.
        output_state = "REQUIRES_AUTHORISED_REVIEW"
        detail["basis_ambiguous"] = True
        detail["candidate_provisions"] = [
            {"verified_in": c.get("verified_in"),
             "settlement_basis": c.get("settlement_basis"),
             "threshold_pct": (None if c.get("threshold_pct") is None
                               else _pct(Decimal(str(c["threshold_pct"]))))}
            for c in candidates
        ]
        detail["basis_note"] = _clean("""
            This wording carries more than one average provision, on different bases
            of settlement, with different thresholds. The documents supplied do not
            establish which basis governs this item, so the engine does not choose.
            The figure shown applies the harsher threshold and therefore understates
            rather than overstates what the policy would pay.""")

    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=output_state, evidence_class="",
        headline=_clean(rule["headline"]), operands=ops, required_operands=ops,
        never=rule.get("never", []),
        detail=detail))


def _buildings(ctx: Context) -> Optional[Finding]:
    rule_id = "BUILDINGS-VALUE-UNESTABLISHED"
    rule = ctx.rules[rule_id]
    if ctx.facts.has("buildings_sum_insured"):
        return None
    # No reinstatement cost assessment on record -> operand with no span anywhere.
    op = Operand(name="building_value", value=None, unit="SGD", span_id=None,
                 evidence_class=grading.grade_operand(None, "THIRD_PARTY"))
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]), operands=[op], required_operands=[op],
        detail={"cite": rule["cite"]}))


def _wica(ctx: Context) -> Optional[Finding]:
    rule_id = "WICA-HEADCOUNT-UNDERDECLARED"
    blocked = _guard(ctx, rule_id, "declared_headcount", "contributor_count",
                     needs_clause=False)
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]
    d_fact = ctx.facts.get("declared_headcount")
    a_fact = ctx.facts.get("contributor_count")
    declared, actual = int(d_fact.value), int(a_fact.value)
    if not (actual > declared):
        return None
    ops = [_fact_operand(ctx, "declared", d_fact),
           _fact_operand(ctx, "actual", a_fact)]
    detail = {"cite": rule["cite"]}
    # Supporting sentences are present only when the pack verified every figure in
    # them (see `bind`); a dropped one is simply absent, never rendered half-filled.
    if rule.get("consequence_note"):
        detail["consequence_note"] = _clean(rule["consequence_note"])
    if "deadline" in rule:
        detail["deadline"] = rule.get("deadline")
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]).format(declared=declared, actual=actual),
        operands=ops, required_operands=ops,
        detail=detail))


def _premium(ctx: Context) -> Optional[Finding]:
    rule_id = "PREMIUM-PAYMENT-WARRANTY-TIMING"
    blocked = _guard(ctx, rule_id, "inception", "premium_received")
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]
    inc_fact = ctx.facts.get("inception")
    rec_fact = ctx.facts.get("premium_received")
    inception, received = inc_fact.value, rec_fact.value
    if not isinstance(inception, date) or not isinstance(received, date):
        return _not_established(ctx, rule_id, ["inception", "premium_received"])
    n = (received - inception).days
    # The period comes from the pack (SG: the 60 days the measured wording states). The
    # rule is only bound when the pack verifies it, so this is never a default.
    if n <= int(rule["_values"]["days"]):
        return None
    ops = [_fact_operand(ctx, "inception", inc_fact),
           _fact_operand(ctx, "received", rec_fact),
           _base_operand(ctx, "ppw", "automatically terminated")]
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]).format(n=n),
        operands=ops, required_operands=ops, never=rule.get("never", []),
        detail={"cite": rule["cite"], "days_from_inception": n}))


def _alteration(ctx: Context) -> Optional[Finding]:
    rule_id = "ALTERATION-PREMISES-NOT-ON-SCHEDULE"
    blocked = _guard(ctx, rule_id, "scheduled_premises", "demised_premises")
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]
    sch_fact = ctx.facts.get("scheduled_premises")
    ten_fact = ctx.facts.get("demised_premises")
    scheduled = {str(a).split(",")[0].strip() for a in sch_fact.value}
    demised = [str(d).strip() for d in ten_fact.value]
    missing = [d for d in demised
               if not any(d.split(",")[0].strip() in s or s in d for s in scheduled)]
    if not missing:
        return None
    # Anchor the tenancy operand on the unit that is ACTUALLY MISSING, not on the
    # first unit in the list. The default span was the first demised premises, which
    # is usually the one that IS on the schedule — so the finding displayed two
    # quotations of the same address and read as though nothing were wrong. The
    # quotation is the evidence; quoting the wrong line silently breaks the only
    # thing that makes a finding checkable.
    missing_fact = ten_fact
    for cand in missing:
        anchored = ctx.facts.reanchor(ten_fact, str(cand)) \
            if hasattr(ctx.facts, "reanchor") else None
        if anchored is not None:
            missing_fact = anchored
            break

    ops = [_fact_operand(ctx, "scheduled_premises", sch_fact),
           _fact_operand(ctx, "demised_premises", missing_fact),
           _base_operand(ctx, "alteration_clause",
                         "Chubb shall have no liability under this Policy")]
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]), operands=ops, required_operands=ops,
        never=rule.get("never", []),
        detail={"cite": rule["cite"], "missing_from_schedule": missing}))


def _endorsement_illegible(ctx: Context) -> Optional[Finding]:
    """Not a YAML rule — the abstention demonstration. An endorsement page whose
    extraction failed must produce INFORMATION_MISSING, never a guess at its effect.
    Covers both a fixture marked UNREADABLE and an uploaded endorsement whose text
    layer came back empty."""
    illegible = None
    for e in ctx.store.all("ENDORSEMENT"):
        marked = "UNREADABLE" in str(e.data.get("page_2_extraction", "")).upper()
        empty_text = bool(e.pages) and not any(p.strip() for p in e.pages)
        # An uploaded endorsement whose amended text is an unreadable scan is flagged
        # `illegible` by account.parse_endorsement — the PDF equivalent of the
        # fixture's page_2_extraction=UNREADABLE.
        parsed_illegible = bool(e.data.get("illegible"))
        if marked or empty_text or parsed_illegible:
            illegible = e
            break
    if illegible is None:
        return None
    op = Operand(name="endorsement_effect", value=None, unit=None, span_id=None,
                 evidence_class=grading.grade_operand(None, "INSURER"))
    ref = illegible.data.get("endorsement_ref") or illegible.filename or ""
    return _finalise(ctx, Finding(
        rule_id="ENDORSEMENT-ILLEGIBLE-ABSTAIN", rule_version=1,
        output_state="INFORMATION_MISSING", evidence_class="",
        headline=(f"Endorsement {ref} could not be read (illegible scan). Upload a "
                  f"clearer copy or request the wording from your insurer. The "
                  f"engine will not guess its effect."),
        operands=[op], required_operands=[op],
        detail={"document": illegible.data.get("title") or illegible.filename}))


def _locate_base(ctx: Context, rule_id: str, needle: str) -> Optional[Finding]:
    """Shared shape for informational rules that locate and quote a base-wording
    clause. DOCUMENTED, PROVEN — no computation, no adjudication. When the clause is
    not in this wording, the rule stays silent rather than reporting a Chubb clause
    against somebody else's policy."""
    if not ctx.clause_basis_available(rule_id) or not ctx.base_doc_id:
        return None
    rule = ctx.rules[rule_id]
    op = _base_operand(ctx, "clause", needle)
    if op.span_id is None:
        return None
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]), operands=[op], required_operands=[op],
        detail={"cite": rule["cite"]}))


def _unoccupancy(ctx: Context) -> Optional[Finding]:
    return _locate_base(ctx, "UNOCCUPANCY-WATER-EXCLUSION", "Vacant for a period")


def _claim_time_limit(ctx: Context) -> Optional[Finding]:
    return _locate_base(ctx, "CLAIM-TIME-LIMIT", "twelve months after")


def _minimum_security(ctx: Context) -> Optional[Finding]:
    return _locate_base(ctx, "MINIMUM-SECURITY", "when no Insured Person is on")


def _business_interruption_not_confirmed(ctx: Context) -> Optional[Finding]:
    rule_id = "BUSINESS-INTERRUPTION-NOT-CONFIRMED"
    blocked = _guard(ctx, rule_id, "operative_sections", needs_clause=False)
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]
    fact = ctx.facts.get("operative_sections")
    sections = [str(s).upper() for s in fact.value]
    if any("INTERRUPTION" in s for s in sections):
        return None                          # cover present -> no NOT_CONFIRMED
    op = _fact_operand(ctx, "operative_sections", fact)
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]), operands=[op], required_operands=[op],
        detail={"cite": rule["cite"], "searched_sections": sections}))


def _workpass_medical(ctx: Context) -> Optional[Finding]:
    rule_id = "WORKPASS-MEDICAL-BELOW-MINIMUM"
    if "WORK_PASS_LIST" not in ctx.facts.kinds_present:
        return None                          # no work pass list -> nothing searched
    blocked = _guard(ctx, rule_id, "workpass_below_minimum_count",
                     needs_clause=False)
    if blocked is not None:
        return blocked
    below = int(ctx.facts.get("workpass_below_minimum_count").value)
    if below == 0:
        return None
    rule = ctx.rules[rule_id]
    fact = ctx.facts.get("workpass_lowest_medical")
    if not fact.established:
        return _not_established(ctx, rule_id, ["workpass_lowest_medical"])
    op = _fact_operand(ctx, "holder_medical", fact)
    detail = {"cite": rule["cite"]}
    if rule.get("consequence_note"):
        detail["consequence_note"] = _clean(rule["consequence_note"])
    # The same verified pack figure the extractor compared each holder against.
    detail["holders_below_minimum"] = below
    detail["minimum_sgd"] = rule["_values"]["minimum"]
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]), operands=[op], required_operands=[op],
        detail=detail))


# Tokens that would show the ACRA secondary activity IS reflected in the policy's
# own business description. Data rather than a literal, so the check is auditable.
_ONLINE_ACTIVITY_TOKENS = ("online", "retail", "e-commerce", "ecommerce",
                           "web", "internet", "platform")


def _activity_ssic(ctx: Context) -> Optional[Finding]:
    rule_id = "ACTIVITY-SSIC-NOT-DECLARED"
    blocked = _guard(ctx, rule_id, "acra_secondary_activity",
                     "business_description")
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]
    acra_fact = ctx.facts.get("acra_secondary_activity")
    desc_fact = ctx.facts.get("business_description")
    description = str(desc_fact.value).lower()
    if any(w in description for w in _ONLINE_ACTIVITY_TOKENS):
        return None                          # reflected -> nothing to escalate
    ops = [_fact_operand(ctx, "acra_secondary", acra_fact),
           _fact_operand(ctx, "schedule_description", desc_fact),
           _base_operand(ctx, "alteration_clause",
                         "Chubb shall have no liability under this Policy")]
    # Substitute the extracted values into the headline. The sentence may only
    # restate what the documents established, so both specifics come from facts.
    headline = _clean(rule["headline"]).format(
        activity=str(acra_fact.value).strip(),
        description=str(desc_fact.value).strip())
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=headline, operands=ops, required_operands=ops,
        never=rule.get("never", []),
        detail={"cite": rule["cite"],
                "acra_secondary_activity": str(acra_fact.value)}))


def _plant_adequacy(ctx: Context) -> Optional[Finding]:
    rule_id = "PLANT-ADEQUACY-UNESTABLISHED"
    if not ctx.facts.has("plant_sum_insured"):
        return None                          # no plant item -> nothing to assess
    blocked = _guard(ctx, rule_id, "plant_sum_insured", "invoice_equipment_value",
                     needs_clause=False)
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]
    plant_fact = ctx.facts.get("plant_sum_insured")
    inv_fact = ctx.facts.get("invoice_equipment_value")
    # No complete asset register on record -> total plant value has no span anywhere.
    total_op = Operand(name="total_plant_value", value=None, unit="SGD",
                       span_id=None,
                       evidence_class=grading.grade_operand(None, "THIRD_PARTY"))
    ops = [_fact_operand(ctx, "plant_sum_insured", plant_fact),
           _fact_operand(ctx, "invoice_value", inv_fact), total_op]
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]), operands=ops, required_operands=ops,
        detail={"cite": rule["cite"],
                "plant_sum_insured": str(plant_fact.value),
                "invoice_value": str(inv_fact.value)}))


def _sublimit_precedence_conflict(ctx: Context) -> Optional[Finding]:
    """Two endorsements amend the same sub-limit at the same authority rank with no
    order of precedence between them. The resolver refused to break the tie, so this
    finding carries precedence_unresolved=True and gate 4 (authority) escalates it to
    REQUIRES_AUTHORISED_REVIEW. This is the fourth abstention gate firing on real
    data. We locate and quote both endorsements; we never pick one."""
    rule = ctx.rules["SUBLIMIT-CONFLICTING-ENDORSEMENTS"]
    if "LIMIT.SUB_LIMIT" not in ctx.unresolved_codes:
        return None
    conflicting = [e for e in ctx.store.all("ENDORSEMENT")
                   if e.data.get("amends_taxonomy_code") == "LIMIT.SUB_LIMIT"]
    if len(conflicting) < 2:
        return None
    ops = []
    for e in conflicting:
        ops.append(_fact_operand(ctx, f"endorsement_{e.data.get('endorsement_ref')}",
                                 Fact(name="endorsement",
                                      value=e.data.get("value_sgd"), doc=e,
                                      needle=e.data.get("text", ""), page_no=1,
                                      unit="SGD", source="STRUCTURED")))
    # The amounts are SGD wherever the case is read, so the symbol comes from whichever
    # pack writes SGD — not from the pack this case happens to be read under.
    symbol = jurisdiction.symbol_for("SGD")
    values = ", ".join(
        f"{e.data.get('endorsement_ref')} = {symbol}{e.data.get('value_sgd')}"
        for e in conflicting)
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]).format(values=values),
        operands=ops, required_operands=ops,
        precedence_unresolved=True, never=rule.get("never", []),
        resolution_path=("Two endorsements at ENDORSEMENT rank amend LIMIT.SUB_LIMIT "
                         "with no order of precedence — tie not broken."),
        detail={"cite": rule["cite"], "conflicting_values": values}))


# ── absence rules ───────────────────────────────────────────────────────────
#
# The two rules below differ from every other evaluator in this file: they establish
# that a risk class has NO cover, rather than that existing cover is inadequate. That
# inversion has one dangerous failure mode — concluding "absent" when the truth is
# "unreadable" — so both take the operative sections through the standard _guard and
# abstain when it is missing. An unreadable schedule yields INFORMATION_MISSING, never
# a confident absence.

# Sections that would respond to loss of electronic data, and to lost income. Declared
# as data next to the evaluators that read them so the vocabulary is auditable without
# reading control flow.
_CYBER_RESPONSIVE_SECTIONS = ("CYBER", "DATA")
_INTERRUPTION_RESPONSIVE_SECTIONS = ("INTERRUPTION", "CONSEQUENTIAL", "PROFITS")

# Tokens in an ACRA activity that evidence collecting customer data online. Same list
# the cyber module uses; kept here rather than imported so a change to a presentation
# module can never silently alter what the rules engine finds.
_DATA_ACTIVITY_TOKENS = ("online", "e-commerce", "ecommerce", "internet", "retail",
                         "web portal")


def _cyber_cover_absent(ctx: Context) -> Optional[Finding]:
    """Personal data is demonstrably held and no section responds to its loss.

    Deliberately requires BOTH halves. An online activity with cyber cover is not a
    finding, and a policy without cyber cover for a business that holds no personal
    data is not a finding either. Only the intersection is, and it is established from
    an AUTHORITY document (ACRA) and an INSURER document (the schedule) — so the
    finding grades PROVEN without anything self-authored in it.
    """
    rule_id = "CYBER-DATA-COVER-ABSENT"
    if rule_id not in ctx.rules:
        return None
    blocked = _guard(ctx, rule_id, "operative_sections", "acra_secondary_activity",
                     needs_clause=False)
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]

    activity_fact = ctx.facts.get("acra_secondary_activity")
    activity = str(activity_fact.value or "").lower()
    if not any(token in activity for token in _DATA_ACTIVITY_TOKENS):
        return None                          # no evidenced data-collecting channel

    sections_fact = ctx.facts.get("operative_sections")
    sections = [str(s).upper() for s in sections_fact.value]
    if any(any(m in s for m in _CYBER_RESPONSIVE_SECTIONS) for s in sections):
        return None                          # a responsive section is present

    ops = [_fact_operand(ctx, "activity", activity_fact),
           _fact_operand(ctx, "sections", sections_fact)]
    detail = {"cite": rule["cite"]}
    if rule.get("consequence_note"):
        detail["consequence_note"] = _clean(rule["consequence_note"])
    detail["searched_sections"] = sections
    detail["activity"] = activity_fact.value
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]).format(sections=", ".join(sections)),
        operands=ops, required_operands=ops, never=rule.get("never", []),
        detail=detail))


def _dependency_cover_not_possible(ctx: Context) -> Optional[Finding]:
    """An outside dependency is evidenced and no BI section exists to extend.

    The dependency count is read from documents that NAME a counterparty — a supplier
    invoice, a tenancy — never from the owner's description of their business. That
    keeps the finding PROVEN and, more importantly, keeps it honest: we are reporting
    dependencies we can see, and the payload says so rather than implying the list is
    complete.
    """
    rule_id = "DEPENDENCY-COVER-NOT-POSSIBLE"
    if rule_id not in ctx.rules:
        return None
    blocked = _guard(ctx, rule_id, "operative_sections", needs_clause=False)
    if blocked is not None:
        return blocked
    rule = ctx.rules[rule_id]

    sections_fact = ctx.facts.get("operative_sections")
    sections = [str(s).upper() for s in sections_fact.value]
    if any(any(m in s for m in _INTERRUPTION_RESPONSIVE_SECTIONS)
           for s in sections):
        return None                          # a BI section exists; not this rule

    named: list[str] = []
    for invoice in ctx.store.all("SUPPLIER_INVOICE"):
        supplier = str((invoice.data or {}).get("supplier") or "").strip()
        if supplier:
            named.append(supplier)
    if ctx.facts.get("demised_premises").established:
        named.append("Landlord and building")
    if not named:
        return None                          # nothing evidenced to depend on

    ops = [_fact_operand(ctx, "sections", sections_fact)]
    return _finalise(ctx, Finding(
        rule_id=rule["id"], rule_version=rule["version"],
        output_state=rule["output_state"], evidence_class="",
        headline=_clean(rule["headline"]).format(
            sections=", ".join(sections), dependency_count=len(named)),
        operands=ops, required_operands=ops, never=rule.get("never", []),
        detail={"cite": rule["cite"],
                "consequence_note": _clean(rule["consequence_note"]),
                "searched_sections": sections,
                "dependencies_found": named,
                "dependencies_are_not_exhaustive": True}))


EVALUATORS: list[Callable[[Context], Optional[Finding]]] = [
    _theft, _wica, _workpass_medical, _average, _plant_adequacy, _buildings,
    _alteration, _activity_ssic, _premium, _minimum_security, _claim_time_limit,
    _unoccupancy, _business_interruption_not_confirmed,
    _sublimit_precedence_conflict, _endorsement_illegible,
    _cyber_cover_absent, _dependency_cover_not_possible,
]

#: The YAML rule each evaluator implements. An evaluator whose rule is not in the case's
#: bound rule set — because it is another country's rule, or the pack does not verify the
#: figure it states — is not run at all, rather than run and allowed to fail into an
#: "internal error" finding. `_endorsement_illegible` is absent on purpose: it is not a
#: YAML rule, and refusing to guess an unreadable endorsement holds in every country.
EVALUATOR_RULE: dict[Callable[[Context], Optional[Finding]], str] = {
    _theft: "THEFT-FORCIBLE-ENTRY-NOT-WRITTEN-BACK",
    _wica: "WICA-HEADCOUNT-UNDERDECLARED",
    _workpass_medical: "WORKPASS-MEDICAL-BELOW-MINIMUM",
    _average: "AVERAGE-ADEQUACY-BELOW-THRESHOLD",
    _plant_adequacy: "PLANT-ADEQUACY-UNESTABLISHED",
    _buildings: "BUILDINGS-VALUE-UNESTABLISHED",
    _alteration: "ALTERATION-PREMISES-NOT-ON-SCHEDULE",
    _activity_ssic: "ACTIVITY-SSIC-NOT-DECLARED",
    _premium: "PREMIUM-PAYMENT-WARRANTY-TIMING",
    _minimum_security: "MINIMUM-SECURITY",
    _claim_time_limit: "CLAIM-TIME-LIMIT",
    _unoccupancy: "UNOCCUPANCY-WATER-EXCLUSION",
    _business_interruption_not_confirmed: "BUSINESS-INTERRUPTION-NOT-CONFIRMED",
    _sublimit_precedence_conflict: "SUBLIMIT-CONFLICTING-ENDORSEMENTS",
    _cyber_cover_absent: "CYBER-DATA-COVER-ABSENT",
    _dependency_cover_not_possible: "DEPENDENCY-COVER-NOT-POSSIBLE",
}


def evaluate_all(ctx: Context) -> list[Finding]:
    """Run every evaluator whose rule applies. One failing evaluator must not lose the
    other fourteen: an unexpected exception is converted into an INFORMATION_MISSING
    finding naming the rule, which is visible and safe, rather than a 500 that hides the
    whole Brief."""
    findings: list[Finding] = []
    for ev in EVALUATORS:
        rule_id = EVALUATOR_RULE.get(ev)
        if rule_id is not None and rule_id not in ctx.rules:
            continue                     # not bound for this pack; reported by bind()
        try:
            f = ev(ctx)
        except Exception as exc:                       # defensive, per rule
            rule_id = getattr(ev, "__name__", "rule").strip("_").upper()
            f = _not_established(ctx, rule_id, [f"internal: {type(exc).__name__}"])
        if f is not None and f.output_state != "DROPPED":
            findings.append(f)
    return findings


# ── small utilities ─────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return " ".join(s.split())


def _pct(d: Decimal) -> str:
    return f"{(d * 100):.1f}%"
