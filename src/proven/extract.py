"""Layer 2b — field extraction. The layer that was missing.

WHY THIS EXISTS. Every rule evaluator used to reach straight into fixture JSON:
`sched.data["property_section"]["items"]`, `cpf.data["contributor_count"]`. Those
keys existed because the fixtures were hand-written to match the code, so Layer 3
worked on exactly one bundle and raised `KeyError` on anything else. Nothing sat
between raw page text and the rule evaluators, and that gap is why the engine could
not accept a document a user supplied.

WHAT IT DOES. It normalises both provenances into one flat set of named `Fact`s:

    structured  a JSON fixture (or any pre-parsed document)  -> STRUCTURED
    text        an uploaded PDF's extracted page text        -> PDF_TEXT
    neither     the document was not provided, or the field  -> ABSENT
                was not found in it

`PolicyFacts.get()` ALWAYS returns a `Fact`. It never raises and never invents a
default, so a missing schedule produces `Fact(value=None, source=ABSENT)`, which
flows through grading as UNESTABLISHED and through gate 3 as INFORMATION_MISSING.
That is the whole point: the failure mode for an unfamiliar document is an honest
abstention, not a stack trace and not a guess.

Every Fact carries the document and the anchor text it was read from, so the operand
built from it gets a real span at real offsets and the span-fidelity gate can verify
it byte-for-byte.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from . import detect, jurisdiction
from .ingest import DocStore, LoadedDoc
from .lang import date_parser_for, money_parser_for

STRUCTURED = "STRUCTURED"
PDF_TEXT = "PDF_TEXT"
ABSENT = "ABSENT"


@dataclass(frozen=True)
class Fact:
    """One extracted value with its documentary basis.

    `value is None` means NOT ESTABLISHED. There is deliberately no separate
    "unknown" sentinel: a field the document states as blank and a field we could
    not find are both "we cannot evidence this", and conflating them is safer than
    inventing a distinction the evidence does not support.
    """

    name: str
    value: Any = None
    doc: Optional[LoadedDoc] = None
    needle: Optional[str] = None
    page_no: int = 1
    unit: Optional[str] = None
    source: str = ABSENT

    @property
    def established(self) -> bool:
        return self.value is not None and self.doc is not None and bool(self.needle)

    @property
    def author_class(self) -> str:
        return self.doc.author_class if self.doc is not None else "SELF"


@dataclass
class PolicyFacts:
    """Named facts plus the document kinds that were actually present."""

    facts: dict[str, Fact] = field(default_factory=dict)
    kinds_present: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def put(self, fact: Fact) -> None:
        # First establishment wins: a later ABSENT probe must not erase a value
        # already evidenced by another document.
        existing = self.facts.get(fact.name)
        if existing is not None and existing.established and not fact.established:
            return
        self.facts[fact.name] = fact

    def get(self, name: str, unit: Optional[str] = None) -> Fact:
        return self.facts.get(name) or Fact(name=name, unit=unit, source=ABSENT)

    def has(self, name: str) -> bool:
        return self.get(name).established

    def missing(self, *names: str) -> list[str]:
        return [n for n in names if not self.has(n)]

    def reanchor(self, fact: Fact, needle: str) -> Optional[Fact]:
        """Return the same fact quoted on a DIFFERENT line of its own document.

        Some facts are lists — every unit on a tenancy, every holder on a work pass
        list — and the interesting line is rarely the first one. A finding about the
        unit that is *missing* from the schedule must quote that unit, not whichever
        one happened to be extracted first, or the evidence shown contradicts the
        finding made.

        Returns None if the phrase is not verbatim in the document, so a failed
        re-anchor falls back to the original span rather than promising a quotation
        the fidelity gate would drop.
        """
        if not fact.established or fact.doc is None or not needle:
            return None
        for page_no, text in enumerate(fact.doc.pages or [], start=1):
            if needle in text:
                return Fact(name=fact.name, value=fact.value, doc=fact.doc,
                            needle=needle, page_no=page_no, unit=fact.unit,
                            source=fact.source)
        return None

    def as_dict(self) -> dict:
        return {
            name: {
                "value": (str(f.value) if isinstance(f.value, (Decimal, date))
                          else f.value),
                "unit": f.unit,
                "source": f.source,
                "document_kind": f.doc.kind if f.doc else None,
                "author_class": f.author_class if f.established else None,
            }
            for name, f in sorted(self.facts.items())
        }


# ── text helpers ────────────────────────────────────────────────────────────


def _verbatim(segment: str, end: int) -> str:
    return segment[:end].strip()


def find_money_with_currency(pages: list[str], labels: tuple[str, ...],
                             window: int = 90, *, currency: Optional[str] = "SGD") \
        -> Optional[tuple[Decimal, str, int, str]]:
    """Read a labelled amount only when a pack or an explicit document marker identifies
    its currency. A bare number under an unknown pack is not money evidence."""
    # The language layer selects candidates from the source document's script; this
    # layer only associates an unambiguous candidate with its field label.
    from .lang import money_candidates_for_document

    document_text = "\n".join(pages)
    parser = money_parser_for(currency)
    for page_no, text in enumerate(pages, start=1):
        low = text.lower()
        for label in labels:
            idx = low.find(label.lower())
            while idx >= 0:
                segment = text[idx:idx + window]
                # A marker proves currency only for the expression it prefixes. Do not
                # select a parser from the full scan: a later conversion or neighbouring
                # value is a separate field. Multiple values before a field delimiter are
                # ambiguous, so extraction abstains rather than assigning the wrong one.
                field = re.split(r"[;\n|]", segment[len(label):], maxsplit=1)[0]
                candidates = money_candidates_for_document(document_text, field)
                if len(candidates) == 1:
                    candidate = candidates[0]
                    unit = candidate.currency or (parser.currency if parser else None)
                    if unit is not None:
                        end = len(label) + candidate.end
                        return (candidate.amount, _verbatim(segment, end), page_no, unit)
                idx = low.find(label.lower(), idx + 1)
    return None


def find_money(pages: list[str], labels: tuple[str, ...],
               window: int = 90, *, currency: Optional[str] = "SGD") \
        -> Optional[tuple[Decimal, str, int]]:
    """Compatibility wrapper for callers that only need the amount and its anchor."""
    hit = find_money_with_currency(pages, labels, window, currency=currency)
    return None if hit is None else hit[:3]


def find_date(pages: list[str], labels: tuple[str, ...],
              window: int = 90, *, currency: Optional[str] = "SGD") \
        -> Optional[tuple[date, str, int]]:
    """First unambiguous English date within `window` characters after any label."""
    parser = date_parser_for(currency or "SGD")
    if parser is None:
        return None
    for page_no, text in enumerate(pages, start=1):
        low = text.lower()
        for label in labels:
            idx = low.find(label.lower())
            while idx >= 0:
                segment = text[idx:idx + window]
                hit = parser.locate_date(segment)
                if hit is not None:
                    return hit.value, _verbatim(segment, hit.end), page_no
                idx = low.find(label.lower(), idx + 1)
    return None


def find_int(pages: list[str], labels: tuple[str, ...],
             window: int = 60) -> Optional[tuple[int, str, int]]:
    for page_no, text in enumerate(pages, start=1):
        low = text.lower()
        for label in labels:
            idx = low.find(label.lower())
            while idx >= 0:
                segment = text[idx:idx + window]
                m = re.search(r"\b(\d{1,6})\b", segment)
                if m:
                    return int(m.group(1)), _verbatim(segment, m.end()), page_no
                idx = low.find(label.lower(), idx + 1)
    return None


def _anchor(doc: LoadedDoc, needle: str, page_no: int = 1) -> Optional[str]:
    """Confirm `needle` really occurs in the document's stored text before we promise
    a span over it. A needle that is not there yields an ABSENT fact rather than a
    span the fidelity gate will later drop."""
    if not needle:
        return None
    pages = doc.pages or []
    if 1 <= page_no <= len(pages) and needle in pages[page_no - 1]:
        return needle
    for text in pages:
        if needle in text:
            return needle
    return None


def _fact(name: str, value: Any, doc: Optional[LoadedDoc], needle: Optional[str],
          page_no: int = 1, unit: Optional[str] = None,
          source: str = STRUCTURED) -> Fact:
    if value is None or doc is None or not needle:
        return Fact(name=name, unit=unit, source=ABSENT)
    confirmed = _anchor(doc, needle, page_no)
    if confirmed is None:
        return Fact(name=name, unit=unit, source=ABSENT)
    return Fact(name=name, value=value, doc=doc, needle=confirmed, page_no=page_no,
                unit=unit, source=source)


# ── structured (fixture / pre-parsed JSON) extraction ───────────────────────

def _schedule_structured(facts: PolicyFacts, doc: LoadedDoc) -> None:
    d = doc.data
    prop = d.get("property_section") or {}

    items = prop.get("items") or []

    def item_sum(match: str) -> Optional[tuple[Any, str]]:
        for it in items:
            if match.lower() in str(it.get("item", "")).lower():
                si = it.get("sum_insured_sgd")
                if si is None:
                    return None
                return Decimal(str(si)), f"sum_insured_sgd: {si}"
        return None

    stock = item_sum("stock")
    if stock:
        facts.put(_fact("stock_sum_insured", stock[0], doc, stock[1], unit="SGD"))
    plant = item_sum("plant")
    if plant:
        facts.put(_fact("plant_sum_insured", plant[0], doc, plant[1], unit="SGD"))

    ded = prop.get("deductible_sgd")
    if ded is not None:
        facts.put(_fact("deductible", Decimal(str(ded)), doc,
                        f"deductible_sgd: {ded}", unit="SGD"))
    bldg = prop.get("buildings_sum_insured_sgd")
    if bldg is not None:
        facts.put(_fact("buildings_sum_insured", Decimal(str(bldg)), doc,
                        f"buildings_sum_insured_sgd: {bldg}", unit="SGD"))

    ext = (prop.get("additional_extensions") or {}).get(
        "2.17_theft_endorsement_full_theft")
    if ext is not None:
        facts.put(_fact(
            "theft_full_elected", ext, doc,
            f"2.17_theft_endorsement_full_theft: {ext}"))

    period = d.get("policy_period") or {}
    inception = period.get("inception")
    if inception:
        facts.put(_fact("inception", date.fromisoformat(inception), doc,
                        f"inception: {inception}"))
    received = (d.get("premium") or {}).get("received_date")
    if received:
        facts.put(_fact("premium_received", date.fromisoformat(received), doc,
                        f"received_date: {received}"))

    premises = [p.get("address", "") for p in (d.get("insured_premises") or [])
                if p.get("address")]
    if premises:
        facts.put(_fact("scheduled_premises", premises, doc, "insured_premises:"))

    sections = [str(s).upper() for s in (d.get("operative_sections") or [])]
    if sections:
        facts.put(_fact("operative_sections", sections, doc, "operative_sections:"))

    desc = d.get("business_description")
    if desc:
        facts.put(_fact("business_description", desc.lower(), doc,
                        "business_description:"))


def _schedule_text(facts: PolicyFacts, doc: LoadedDoc, *,
                   currency: Optional[str]) -> None:
    """Read a schedule that arrived as a PDF. Conservative by design: a label we do
    not recognise yields no fact, and no fact yields an honest abstention."""
    pages = doc.pages

    for name, labels in (
        ("stock_sum_insured", ("stock in trade", "stock and merchandise", "stock")),
        ("plant_sum_insured", ("plant, machinery", "plant and machinery",
                               "machinery and equipment")),
        ("buildings_sum_insured", ("buildings sum insured", "building sum insured",
                                   "buildings")),
        ("deductible", ("deductible", "excess")),
    ):
        hit = find_money_with_currency(pages, labels, currency=currency)
        if hit:
            facts.put(_fact(name, hit[0], doc, hit[1], hit[2], unit=hit[3],
                            source=PDF_TEXT))

    for name, labels in (
        ("inception", ("period of insurance", "inception", "from")),
        ("premium_received", ("premium received", "date received", "payment received")),
    ):
        hit = find_date(pages, labels, currency=currency)
        if hit:
            facts.put(_fact(name, hit[0], doc, hit[1], hit[2], source=PDF_TEXT))

    # Theft extension election: "2.17" alongside an applicability word. Note the
    # alternation puts "not applicable" FIRST — with "applicable" first the regex
    # would match the tail of "not applicable" and invert the finding.
    for page_no, text in enumerate(pages, start=1):
        m = re.search(r"2\.17[^\n]{0,80}?(not applicable|not elected|applicable|"
                      r"elected)", text, re.IGNORECASE)
        if m:
            elected = "applicable" if m.group(1).lower() in {"applicable", "elected"} \
                else "not applicable"
            facts.put(_fact("theft_full_elected", elected, doc, m.group(0).strip(),
                            page_no, source=PDF_TEXT))
            break

    for page_no, text in enumerate(pages, start=1):
        m = re.search(r"(?:business|trade|occupation)\s*(?:description)?\s*[:\-]\s*"
                      r"([^\n]{5,160})", text, re.IGNORECASE)
        if m:
            facts.put(_fact("business_description", m.group(1).strip().lower(), doc,
                            m.group(0).strip(), page_no, source=PDF_TEXT))
            break

    premises: list[str] = []
    premises_needle = None
    premises_page = 1
    for page_no, text in enumerate(pages, start=1):
        for m in re.finditer(r"((?:Unit|Blk|Block|No\.?)\s*[\w\-#/]+[^\n]{0,80}"
                             r"Singapore\s*\d{6})", text, re.IGNORECASE):
            premises.append(" ".join(m.group(1).split()))
            if premises_needle is None:
                premises_needle = m.group(1).strip()
                premises_page = page_no
    if premises:
        facts.put(_fact("scheduled_premises", premises, doc, premises_needle,
                        premises_page, source=PDF_TEXT))

    sections = []
    section_needle = None
    for page_no, text in enumerate(pages, start=1):
        for token in ("PROPERTY", "BUSINESS INTERRUPTION", "PUBLIC LIABILITY",
                      "PRODUCTS LIABILITY", "MONEY", "WORK INJURY",
                      "FIDELITY GUARANTEE"):
            if re.search(r"\b" + re.escape(token) + r"\b", text, re.IGNORECASE):
                sections.append(token.replace(" ", "_"))
                if section_needle is None:
                    section_needle = token
    if sections and section_needle:
        page_no = next((i for i, t in enumerate(pages, start=1)
                        if section_needle in t), 1)
        facts.put(_fact("operative_sections", sorted(set(sections)), doc,
                        section_needle, page_no, source=PDF_TEXT))

    _health_schedule_facts(facts, doc, currency=currency)


def _health_schedule_facts(facts: PolicyFacts, doc: LoadedDoc, *,
                           currency: Optional[str]) -> None:
    """Extract health schedule operands without importing policy wording assumptions.

    The schedule can establish who is named, whether a pool is shared, a declared
    condition and the policy start. It cannot establish a waiting-period duration,
    co-payment or room-rent term; those remain absent until the policy wording says so.
    """
    detected = detect.detect_health_schedule(doc.pages)
    if not detected.is_health:
        return

    for name, value, evidence_key in (
        ("health_family_floater", detected.family_floater, "family_floater"),
        ("health_insured_members", list(detected.members), "members"),
        ("health_member_ages", dict(detected.member_ages), "member_ages"),
        ("health_declared_ped", detected.declared_ped, "declared_ped"),
        ("health_continuous_coverage", detected.continuous_coverage,
         "continuous_coverage"),
    ):
        if value in (None, [], {}, False):
            continue
        needle_page = detected.evidence.get(evidence_key)
        if needle_page is not None:
            facts.put(_fact(name, value, doc, needle_page[0], needle_page[1],
                            source=PDF_TEXT))

    if detected.policy_start:
        try:
            start = datetime.strptime(detected.policy_start, "%d %B %Y").date()
        except ValueError:
            start = None
        needle_page = detected.evidence.get("policy_start")
        if start is not None and needle_page is not None:
            facts.put(_fact("health_policy_start", start, doc, needle_page[0],
                            needle_page[1], source=PDF_TEXT))

    hit = find_money_with_currency(
        doc.pages, ("family-floater sum insured", "family floater sum insured",
                    "health sum insured", "sum insured"), currency=currency)
    if hit is not None:
        facts.put(_fact("health_sum_insured", hit[0], doc, hit[1], hit[2], unit=hit[3],
                        source=PDF_TEXT))


def _workpass_not_compared(facts: PolicyFacts, doc: LoadedDoc) -> None:
    facts.notes.append(
        f"{doc.kind} ({doc.filename or 'document'}) was read, but the jurisdiction pack "
        f"that applies states no medical insurance minimum for work pass holders, so no "
        f"holder's cover was compared against one.")


def _supporting_structured(facts: PolicyFacts, doc: LoadedDoc,
                           workpass_minimum: Optional[int] = None) -> None:
    d, kind = doc.data, doc.kind
    if kind == "ACCOUNTS":
        inv = (d.get("current_assets") or {}).get("inventory_sgd")
        if inv is not None:
            facts.put(_fact("stock_value", Decimal(str(inv)), doc,
                            f"inventory_sgd: {inv}", unit="SGD"))
    elif kind == "CPF_RECORD":
        n = d.get("contributor_count")
        if n is not None:
            facts.put(_fact("contributor_count", int(n), doc,
                            f"contributor_count: {n}"))
    elif kind == "WIC_SCHEDULE":
        n = d.get("declared_employee_count")
        if n is not None:
            facts.put(_fact("declared_headcount", int(n), doc,
                            f"declared_employee_count: {n}"))
    elif kind == "WORK_PASS_LIST":
        holders = d.get("holders") or []
        if workpass_minimum is None:
            _workpass_not_compared(facts, doc)
            return
        below = [h for h in holders
                 if _to_int(h.get("medical_insurance_sum_sgd")) is not None
                 and _to_int(h.get("medical_insurance_sum_sgd")) < workpass_minimum]
        if below:
            worst = min(below, key=lambda h: _to_int(h["medical_insurance_sum_sgd"]))
            amount = _to_int(worst["medical_insurance_sum_sgd"])
            facts.put(_fact("workpass_lowest_medical", amount, doc,
                            f"medical_insurance_sum_sgd: {amount}", unit="SGD"))
            facts.put(_fact("workpass_below_minimum_count", len(below), doc,
                            f"medical_insurance_sum_sgd: {amount}"))
        elif holders:
            facts.put(_fact("workpass_below_minimum_count", 0, doc, "holders:"))
    elif kind == "TENANCY":
        demised = [str(x).strip() for x in (d.get("demised_premises") or [])]
        if demised:
            facts.put(_fact("demised_premises", demised, doc, "demised_premises:"))
        use = d.get("permitted_use")
        if use:
            facts.put(_fact("permitted_use", use, doc, "permitted_use:"))
    elif kind == "ACRA_PROFILE":
        sec = d.get("secondary_ssic_description")
        if sec:
            facts.put(_fact("acra_secondary_activity", sec, doc,
                            f"secondary_ssic_description: {sec}"))
        primary = d.get("primary_ssic_2025")
        if primary:
            facts.put(_fact("primary_ssic", str(primary), doc,
                            f"primary_ssic_2025: {primary}"))
    elif kind == "SUPPLIER_INVOICE":
        amount = d.get("amount_sgd")
        if amount is not None:
            facts.put(_fact("invoice_equipment_value", Decimal(str(amount)), doc,
                            f"amount_sgd: {amount}", unit="SGD"))


def _supporting_text(facts: PolicyFacts, doc: LoadedDoc,
                     workpass_minimum: Optional[int] = None,
                     *, currency: Optional[str] = None) -> None:
    pages, kind = doc.pages, doc.kind
    if kind == "ACCOUNTS":
        hit = find_money_with_currency(pages, ("inventory", "stock in trade", "stocks"),
                                       currency=currency)
        if hit:
            facts.put(_fact("stock_value", hit[0], doc, hit[1], hit[2], unit=hit[3],
                            source=PDF_TEXT))
    elif kind == "CPF_RECORD":
        hit = find_int(pages, ("total employees", "number of employees",
                               "contributions for", "total contributors"))
        if hit:
            facts.put(_fact("contributor_count", hit[0], doc, hit[1], hit[2],
                            source=PDF_TEXT))
    elif kind == "WIC_SCHEDULE":
        hit = find_int(pages, ("number of employees", "declared employees",
                               "no. of employees", "employees insured"))
        if hit:
            facts.put(_fact("declared_headcount", hit[0], doc, hit[1], hit[2],
                            source=PDF_TEXT))
    elif kind == "TENANCY":
        # `\s*[:\-]?\s*` rather than a `{0,10}` filler: a greedy filler ate the first
        # characters of the value, so "Light food manufacturing" was captured as
        # "od manufacturing".
        for page_no, text in enumerate(pages, start=1):
            m = re.search(r"permitted\s*use\s*[:\-]?\s*([^\n]{5,160})", text,
                          re.IGNORECASE)
            if m:
                facts.put(_fact("permitted_use", m.group(1).strip(), doc,
                                m.group(0).strip(), page_no, source=PDF_TEXT))
                break
        demised: list[str] = []
        needle, dpage = None, 1
        for page_no, text in enumerate(pages, start=1):
            for m in re.finditer(r"((?:Unit|Blk|Block|No\.?)\s*[\w\-#/]+[^\n]{0,80})",
                                 text):
                demised.append(" ".join(m.group(1).split()))
                if needle is None:
                    needle, dpage = m.group(1).strip(), page_no
        if demised:
            facts.put(_fact("demised_premises", demised, doc, needle, dpage,
                            source=PDF_TEXT))
    elif kind == "ACRA_PROFILE":
        # Take the DESCRIPTION line, not merely the first line beginning "secondary".
        #
        # An ACRA profile lists the code and the description on consecutive lines:
        #   secondary_ssic_2025: 47190
        #   secondary_ssic_description: Retail sale via online platforms
        # The old pattern made every label part optional, so it matched the code line
        # first and stored "_ssic_2025: 47190" — a value carrying its own label and a
        # stray underscore. The finding then displayed a numeric code as evidence for
        # a headline that speaks of online retail, so the quotation did not support
        # the claim. For a product whose entire promise is that the quote proves the
        # finding, that is the most damaging class of defect available.
        #
        # Ordered: a real description first, the bare code only as a fallback, and
        # each pattern requires its label rather than treating it as optional.
        patterns = (
            r"secondary[_\s]*(?:ssic[_\s]*)?description[_\s]*[:\-]?\s*([^\n]{4,120})",
            r"secondary[_\s]*activity[_\s]*[:\-]?\s*([^\n]{4,120})",
            r"secondary[_\s]*(?:ssic[_\s]*)?(?:2025[_\s]*)?[:\-]\s*(\d{4,6})",
        )
        done = False
        for pat in patterns:
            for page_no, text in enumerate(pages, start=1):
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    facts.put(_fact("acra_secondary_activity", m.group(1).strip(),
                                    doc, m.group(0).strip(), page_no,
                                    source=PDF_TEXT))
                    done = True
                    break
            if done:
                break
    elif kind == "WORK_PASS_LIST":
        # A work pass list is one row per holder, so read EVERY medical figure and
        # keep the lowest that breaches the minimum. Reading only the first match
        # would miss the holder who is actually short — the list is not sorted, and
        # the compliant holders usually come first.
        if workpass_minimum is None:
            _workpass_not_compared(facts, doc)
            return
        found: list[tuple[int, str, int]] = []
        for page_no, text in enumerate(pages, start=1):
            for m in re.finditer(
                    r"medical\s*(?:insurance)?\s*(?:sum)?(?:_sgd)?\s*[:\-]?\s*"
                    r"(?:S?\$)?\s*([\d,]{3,12})", text, re.IGNORECASE):
                amount = _to_int(m.group(1).replace(",", ""))
                if amount is not None:
                    found.append((amount, " ".join(m.group(0).split()), page_no))
        if found:
            below = [f for f in found if f[0] < workpass_minimum]
            if below:
                worst = min(below, key=lambda f: f[0])
                facts.put(_fact("workpass_lowest_medical", worst[0], doc, worst[1],
                                worst[2], unit="SGD", source=PDF_TEXT))
                facts.put(_fact("workpass_below_minimum_count", len(below), doc,
                                worst[1], worst[2], source=PDF_TEXT))
            else:
                # Established as compliant, which is a fact and not a gap.
                facts.put(_fact("workpass_below_minimum_count", 0, doc,
                                found[0][1], found[0][2], source=PDF_TEXT))
    elif kind == "SUPPLIER_INVOICE":
        hit = find_money_with_currency(
            pages, ("total", "amount due", "invoice total", "grand total"),
            currency=currency)
        if hit:
            facts.put(_fact("invoice_equipment_value", hit[0], doc, hit[1], hit[2],
                            unit=hit[3], source=PDF_TEXT))


def workpass_minimum(pack: "jurisdiction.Pack") -> Optional[int]:
    """The per-holder medical insurance minimum the pack verifies, or None.

    This used to be a module constant, 60000, which applied Singapore's Work Permit rule
    to a work pass list from anywhere. It now comes from the pack that applies — the same
    entry the WORKPASS rule's headline quotes — and a pack that does not verify one gets
    no comparison at all rather than a borrowed figure.
    """
    entry = pack.find("COMP-WORKPASS-MEDICAL")
    if entry is None or not entry.owner_facing:
        return None
    value = entry.values.get("minimum_annual_cover")
    return None if value is None else int(value)


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── entry point ─────────────────────────────────────────────────────────────

def extract_facts(store: DocStore,
                  pack: Optional["jurisdiction.Pack"] = None) -> PolicyFacts:
    """Normalise every document in the store into named facts.

    Dispatch is on provenance, not on document identity: a structured document uses
    its parsed keys, an uploaded PDF uses its page text, and both land in the same
    `PolicyFacts`. Rules read only from here.

    `pack` is the jurisdiction the case is read under; the pipeline always passes it.
    Without one, it is detected from the store's own documents — never assumed.
    """
    if pack is None:
        pack = jurisdiction.assess(store.documents()).pack
    minimum = workpass_minimum(pack)
    money_currency = pack.currency.code if pack.currency is not None else None
    facts = PolicyFacts()
    for doc in store.documents():
        facts.kinds_present.add(doc.kind)
        structured = bool(doc.data)
        try:
            if doc.kind == "SCHEDULE":
                if structured:
                    _schedule_structured(facts, doc)
                else:
                    _schedule_text(facts, doc, currency=money_currency)
            elif structured:
                _supporting_structured(facts, doc, minimum)
            else:
                _supporting_text(facts, doc, minimum, currency=money_currency)
        except Exception as exc:
            # Deliberately broad. The input is "whatever documents a small business
            # holds", so the set of ways one can be malformed is open-ended — a
            # string where a list was expected, a nested null, a date that is not a
            # date. Enumerating exception types here means the next unlisted one
            # takes down the review of the other seven documents. A malformed
            # document degrades to ABSENT facts, which grade as UNESTABLISHED and
            # abstain; it is never allowed to produce a wrong value.
            facts.notes.append(
                f"{doc.kind} ({doc.filename or 'document'}) could not be fully "
                f"read: {type(exc).__name__}. Fields from it are reported as not "
                f"established rather than guessed.")
    return facts
