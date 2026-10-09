"""VERIS RENEWAL — what changed between last year's policy and this year's.

THE GAP THIS CLOSES. Every other engine in this system reads ONE policy and proves its
terms. None of them notices the thing that quietly hurts an SME most: a renewal where the
premium held steady, the schedule looked the same, and one endorsement form number
changed — and an entire category of loss is now uncovered. The real example the checklist
cites is the Jan-2026 ISO liability forms (CG 40 47 / CG 40 48 / CG 35 08) that let a
carrier carve AI-related claims out of standard cover.

WHAT THIS MODULE IS, AND IS NOT.

  * It is PURE LOGIC. No API, no model, no external dependency. It diffs two structured
    policy versions and returns a list of `TermChange`, each carrying the page and the
    verbatim quote from BOTH documents. The diff is deterministic and re-runnable.

  * It is NOT a coverage decision. A `TermChange` says "this exclusion was added and it is
    material"; it never says "you are not covered". Materiality is an attention signal fed
    to the orchestrator under the existing precedence rules — it is not a verdict.

THE ABSTENTION PRINCIPLE, again. `classify_materiality` may return UNKNOWN, and UNKNOWN is
the most important value it returns: a change the system cannot safely classify is more
dangerous than one it can, so UNKNOWN ESCALATES rather than being dropped. A bare form
number that changed, or an endorsement whose text could not be read, is UNKNOWN — never
silently assumed administrative.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

# ── materiality vocabulary ───────────────────────────────────────────────────

MATERIAL = "MATERIAL"
ADMINISTRATIVE = "ADMINISTRATIVE"
UNKNOWN = "UNKNOWN"

# ── change types ─────────────────────────────────────────────────────────────

LIMIT_CHANGED = "LIMIT_CHANGED"
DEDUCTIBLE_CHANGED = "DEDUCTIBLE_CHANGED"
SUBLIMIT_CHANGED = "SUBLIMIT_CHANGED"
ENDORSEMENT_ADDED = "ENDORSEMENT_ADDED"
ENDORSEMENT_REMOVED = "ENDORSEMENT_REMOVED"
FORM_NUMBER_CHANGED = "FORM_NUMBER_CHANGED"
EXCLUSION_ADDED = "EXCLUSION_ADDED"
EXCLUSION_REMOVED = "EXCLUSION_REMOVED"
WARRANTY_ADDED = "WARRANTY_ADDED"
WARRANTY_CHANGED = "WARRANTY_CHANGED"
EXTENSION_REMOVED = "EXTENSION_REMOVED"

# Direction of a change, for the reader.
INCREASED = "INCREASED"
DECREASED = "DECREASED"
ADDED = "ADDED"
REMOVED = "REMOVED"
CHANGED = "CHANGED"

# Term kinds a policy version is decomposed into.
LIMIT = "LIMIT"
DEDUCTIBLE = "DEDUCTIBLE"
SUBLIMIT = "SUBLIMIT"
ENDORSEMENT = "ENDORSEMENT"
EXCLUSION = "EXCLUSION"
WARRANTY = "WARRANTY"

# Endorsement mechanism sub-classes, read from the endorsement's own text.
MECH_EXCLUSION = "EXCLUSION"
MECH_WARRANTY = "WARRANTY"
MECH_EXTENSION = "EXTENSION"
MECH_OTHER = "OTHER"
MECH_UNREADABLE = "UNREADABLE"

_UNREADABLE_MARKERS = ("[unreadable]", "[not extractable]", "[illegible]", "")


@dataclass(frozen=True)
class PolicyTerm:
    """One term read from a policy version, with the page and verbatim quote it came
    from. `key` is the normalised identity used to match a term across versions."""

    kind: str
    key: str
    document_id: str
    page: int
    quote: str
    value: Optional[str] = None      # a money amount, where the term carries one
    mechanism: str = MECH_OTHER      # for endorsements: EXCLUSION|WARRANTY|EXTENSION...
    title: str = ""                  # human title, where distinct from the key

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "key": self.key, "document_id": self.document_id,
            "page": self.page, "quote": self.quote, "value": self.value,
            "mechanism": self.mechanism, "title": self.title,
        }


@dataclass
class PolicyVersion:
    """A structured, provenance-carrying decomposition of one policy document."""

    document_id: str
    label: str
    terms: list[PolicyTerm] = field(default_factory=list)

    @classmethod
    def from_pages(cls, pages: list[str], document_id: str, label: str
                   ) -> "PolicyVersion":
        return cls(document_id=document_id, label=label,
                   terms=extract_terms(pages, document_id))

    def by_kind(self, kind: str) -> list[PolicyTerm]:
        return [t for t in self.terms if t.kind == kind]


@dataclass
class TermChange:
    """One difference between two policy versions, fully sourced from BOTH sides."""

    change_type: str
    mechanism: str                   # the field/mechanism that changed
    direction: str
    prior_document_id: Optional[str]
    current_document_id: Optional[str]
    prior_page: Optional[int]
    current_page: Optional[int]
    prior_quote: str
    current_quote: str
    prior_value: Optional[str] = None
    current_value: Optional[str] = None
    label: str = ""
    materiality: str = UNKNOWN
    materiality_reason: str = ""

    @property
    def escalates(self) -> bool:
        """UNKNOWN and MATERIAL both demand a human look. ADMINISTRATIVE does not."""
        return self.materiality in (MATERIAL, UNKNOWN)

    def as_dict(self) -> dict:
        return {
            "change_type": self.change_type,
            "mechanism": self.mechanism,
            "direction": self.direction,
            "label": self.label,
            "prior_document_id": self.prior_document_id,
            "current_document_id": self.current_document_id,
            "prior_page": self.prior_page,
            "current_page": self.current_page,
            "prior_quote": self.prior_quote,
            "current_quote": self.current_quote,
            "prior_value": self.prior_value,
            "current_value": self.current_value,
            "materiality": self.materiality,
            "materiality_reason": self.materiality_reason,
            "escalates": self.escalates,
        }


# ── term extraction (deterministic, regex over the stored text) ──────────────

_MONEY = r"(?:S?\$?\s?[\d][\d,]*(?:\.\d+)?)"
_EXCLUSION_WORDS = ("exclusion", "excluded", "does not cover", "shall not cover",
                    "no liability", "not insured")
_WARRANTY_WORDS = ("warranty", "warranted", "it is a condition precedent")
_EXTENSION_WORDS = ("extension", "write-back", "written back", "buy-back",
                    "additional cover", "endorsed to cover")


def _norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _classify_endorsement_mechanism(text: str) -> str:
    low = (text or "").lower()
    if not low.strip():
        return MECH_UNREADABLE
    if any(w in low for w in _EXCLUSION_WORDS):
        return MECH_EXCLUSION
    if any(w in low for w in _WARRANTY_WORDS):
        return MECH_WARRANTY
    if any(w in low for w in _EXTENSION_WORDS):
        return MECH_EXTENSION
    return MECH_OTHER


def extract_terms(pages: list[str], document_id: str) -> list[PolicyTerm]:
    """Decompose a policy document's text into structured, sourced terms.

    Conservative by design: a line that does not match a recognised pattern yields no
    term, and no term is invented. Every term carries the page and the verbatim line it
    was read from, so a downstream change can quote both sides.
    """
    terms: list[PolicyTerm] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, key: str, page: int, quote: str, value=None,
            mechanism=MECH_OTHER, title=""):
        ident = (kind, key)
        if ident in seen:
            return
        seen.add(ident)
        terms.append(PolicyTerm(kind=kind, key=key, document_id=document_id,
                                page=page, quote=quote.strip(), value=value,
                                mechanism=mechanism, title=title))

    for page_no, text in enumerate(pages, start=1):
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            low = line.lower()

            # Endorsements — "Endorsement No. 3 — Communicable Disease Exclusion" or
            # a form code "Form CG 40 47".
            m = re.search(
                r"endorsement\s*(?:no\.?|number)?\s*([A-Za-z0-9][\w\- ]{0,20}?)\s*"
                r"[—:\-–]\s*(.+)", line, re.IGNORECASE)
            if m:
                num, title = m.group(1).strip(), m.group(2).strip()
                mech = _classify_endorsement_mechanism(title + " " + line)
                add(ENDORSEMENT, _norm_key(title), page_no, line,
                    mechanism=mech, title=title)
                continue
            m = re.search(r"\bform\s+([A-Z]{1,3}\s?\d[\d ]{1,12}\d)\b", line)
            if m:
                add(ENDORSEMENT, _norm_key("form " + m.group(1)), page_no, line,
                    mechanism=_classify_endorsement_mechanism(line),
                    title=f"Form {m.group(1).strip()}")
                continue

            # Deductible / excess.
            m = re.search(r"(deductible|excess)\b[^\n]*?(" + _MONEY + r")", low)
            if m:
                amt = _parse_money(m.group(2))
                add(DEDUCTIBLE, "deductible", page_no, line, value=amt)
                continue

            # Sub-limit.
            m = re.search(r"sub-?limit\b[^\n]*?(" + _MONEY + r")", low)
            if m:
                # key off the words before the amount so two sub-limits stay distinct
                subject = _norm_key(line.split(":")[0]) or "sublimit"
                add(SUBLIMIT, subject, page_no, line,
                    value=_parse_money(m.group(1)))
                continue

            # Limit of liability / indemnity / sum insured.
            m = re.search(r"(limit of (?:liability|indemnity)|sum insured)\b"
                          r"[^\n]*?(" + _MONEY + r")", low)
            if m:
                subject = _norm_key(line.split(":")[0].split("  ")[0]) or "limit"
                add(LIMIT, subject, page_no, line, value=_parse_money(m.group(2)))
                continue

            # Standalone exclusion clause (not an endorsement heading).
            if any(w in low for w in _EXCLUSION_WORDS) and "endorsement" not in low:
                add(EXCLUSION, _norm_key(line)[:80], page_no, line,
                    mechanism=MECH_EXCLUSION)
                continue

            # Standalone warranty clause.
            if any(w in low for w in _WARRANTY_WORDS) and "endorsement" not in low:
                add(WARRANTY, _norm_key(line)[:80], page_no, line,
                    mechanism=MECH_WARRANTY)
                continue

    return terms


def _parse_money(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"[^\d.]", "", raw)
    if not digits:
        return None
    try:
        return str(Decimal(digits))
    except InvalidOperation:
        return None


# ── the comparator ───────────────────────────────────────────────────────────

def compare_terms(prior: PolicyVersion, current: PolicyVersion) -> list[TermChange]:
    """Diff two policy versions and return every detected change, fully sourced.

    Deterministic set/dict logic — the model gets no vote. Amounts are compared as
    Decimals; presence is compared by normalised key; a matched title carried on a
    different form number surfaces as FORM_NUMBER_CHANGED.
    """
    changes: list[TermChange] = []

    # 1. Money terms matched by key: limit, sub-limit, deductible.
    for kind, ctype in ((LIMIT, LIMIT_CHANGED), (SUBLIMIT, SUBLIMIT_CHANGED),
                        (DEDUCTIBLE, DEDUCTIBLE_CHANGED)):
        prior_by = {t.key: t for t in prior.by_kind(kind)}
        curr_by = {t.key: t for t in current.by_kind(kind)}
        for key, ct in curr_by.items():
            pt = prior_by.get(key)
            if pt is None:
                continue
            if _money_differs(pt.value, ct.value):
                direction = _money_direction(pt.value, ct.value)
                changes.append(TermChange(
                    change_type=ctype, mechanism=key or kind.lower(),
                    direction=direction,
                    prior_document_id=prior.document_id,
                    current_document_id=current.document_id,
                    prior_page=pt.page, current_page=ct.page,
                    prior_quote=pt.quote, current_quote=ct.quote,
                    prior_value=pt.value, current_value=ct.value,
                    label=f"{kind.title()} '{key}' {direction.lower()}"))

    # 2. Endorsements / exclusions / warranties matched by presence.
    for kind in (ENDORSEMENT, EXCLUSION, WARRANTY):
        prior_by = {t.key: t for t in prior.by_kind(kind)}
        curr_by = {t.key: t for t in current.by_kind(kind)}

        for key, ct in curr_by.items():
            if key in prior_by:
                # present in both — a warranty whose wording changed still matters
                pt = prior_by[key]
                if kind == WARRANTY and _norm_key(pt.quote) != _norm_key(ct.quote):
                    changes.append(TermChange(
                        change_type=WARRANTY_CHANGED, mechanism=key,
                        direction=CHANGED,
                        prior_document_id=prior.document_id,
                        current_document_id=current.document_id,
                        prior_page=pt.page, current_page=ct.page,
                        prior_quote=pt.quote, current_quote=ct.quote,
                        label="Warranty wording changed"))
                continue
            # ADDED in the renewal
            ctype, mech = _added_change_type(kind, ct)
            changes.append(TermChange(
                change_type=ctype, mechanism=key or kind.lower(), direction=ADDED,
                prior_document_id=prior.document_id,
                current_document_id=current.document_id,
                prior_page=None, current_page=ct.page,
                prior_quote="", current_quote=ct.quote,
                label=f"{ct.title or key or kind.title()} added at renewal"))

        for key, pt in prior_by.items():
            if key in curr_by:
                continue
            # REMOVED at renewal
            ctype = _removed_change_type(kind, pt)
            changes.append(TermChange(
                change_type=ctype, mechanism=key or kind.lower(), direction=REMOVED,
                prior_document_id=prior.document_id,
                current_document_id=current.document_id,
                prior_page=pt.page, current_page=None,
                prior_quote=pt.quote, current_quote="",
                label=f"{pt.title or key or kind.title()} removed at renewal"))

    # 3. Form-number change: same endorsement title, different form key.
    changes.extend(_detect_form_number_changes(prior, current))

    for change in changes:
        change.materiality = classify_materiality(change)
        change.materiality_reason = _materiality_reason(change)
    # Material first, then Unknown (both escalate), then Administrative.
    order = {MATERIAL: 0, UNKNOWN: 1, ADMINISTRATIVE: 2}
    changes.sort(key=lambda c: order.get(c.materiality, 3))
    return changes


def _added_change_type(kind: str, term: PolicyTerm) -> tuple[str, str]:
    if kind == EXCLUSION:
        return EXCLUSION_ADDED, MECH_EXCLUSION
    if kind == WARRANTY:
        return WARRANTY_ADDED, MECH_WARRANTY
    # endorsement — sub-classify by its own text
    if term.mechanism == MECH_EXCLUSION:
        return EXCLUSION_ADDED, MECH_EXCLUSION
    if term.mechanism == MECH_WARRANTY:
        return WARRANTY_ADDED, MECH_WARRANTY
    return ENDORSEMENT_ADDED, term.mechanism


def _removed_change_type(kind: str, term: PolicyTerm) -> str:
    if kind == EXCLUSION:
        return EXCLUSION_REMOVED
    if term.mechanism == MECH_EXTENSION:
        return EXTENSION_REMOVED
    return ENDORSEMENT_REMOVED


def _detect_form_number_changes(prior: PolicyVersion,
                                current: PolicyVersion) -> list[TermChange]:
    out: list[TermChange] = []
    prior_forms = [t for t in prior.by_kind(ENDORSEMENT) if t.title]
    curr_forms = [t for t in current.by_kind(ENDORSEMENT) if t.title]
    # match on the descriptive title minus the form code, if the keys differ
    def desc(t: PolicyTerm) -> str:
        return _norm_key(re.sub(r"form\s+[a-z]{1,3}\s?\d[\d ]*", "",
                                t.title.lower())).strip()
    prior_by_desc = {desc(t): t for t in prior_forms if desc(t)}
    for ct in curr_forms:
        d = desc(ct)
        pt = prior_by_desc.get(d)
        if pt is not None and pt.key != ct.key:
            out.append(TermChange(
                change_type=FORM_NUMBER_CHANGED, mechanism=d or "endorsement",
                direction=CHANGED,
                prior_document_id=prior.document_id,
                current_document_id=current.document_id,
                prior_page=pt.page, current_page=ct.page,
                prior_quote=pt.quote, current_quote=ct.quote,
                label="Endorsement form number changed"))
    return out


def _money_differs(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return a != b
    try:
        return Decimal(a) != Decimal(b)
    except InvalidOperation:
        return a != b


def _money_direction(a: Optional[str], b: Optional[str]) -> str:
    try:
        da, db = Decimal(a), Decimal(b)
    except (InvalidOperation, TypeError):
        return CHANGED
    return INCREASED if db > da else DECREASED if db < da else CHANGED


# ── materiality classification ───────────────────────────────────────────────

def classify_materiality(change: TermChange) -> str:
    """MATERIAL | ADMINISTRATIVE | UNKNOWN.

    MATERIAL: a new exclusion, a new warranty, a reduced limit or sub-limit, an
    increased deductible, a removed extension — anything that reduces protection.
    ADMINISTRATIVE: an increased limit, a reduced deductible — a favourable or
    non-adverse change.
    UNKNOWN: a change whose significance cannot be safely determined — a bare form
    number change, or a change whose wording could not be read. UNKNOWN escalates.
    """
    ct = change.change_type

    # Unreadable wording on either side — cannot classify, so do not pretend to.
    if _is_unreadable(change.current_quote) and _is_unreadable(change.prior_quote):
        return UNKNOWN

    if ct in (EXCLUSION_ADDED, WARRANTY_ADDED, WARRANTY_CHANGED, EXTENSION_REMOVED):
        return MATERIAL

    if ct in (LIMIT_CHANGED, SUBLIMIT_CHANGED):
        return MATERIAL if change.direction == DECREASED else ADMINISTRATIVE

    if ct == DEDUCTIBLE_CHANGED:
        return MATERIAL if change.direction == INCREASED else ADMINISTRATIVE

    if ct == FORM_NUMBER_CHANGED:
        # A form number moving is exactly the case the checklist warns about: the
        # cover can shrink with the price unchanged. We cannot read the significance
        # of a form code, so we refuse to guess — and refusing escalates.
        return UNKNOWN

    if ct == ENDORSEMENT_ADDED:
        if change.mechanism == MECH_EXTENSION:
            return ADMINISTRATIVE      # an added extension widens cover
        if change.mechanism in (MECH_UNREADABLE, MECH_OTHER):
            return UNKNOWN             # cannot tell what it does — escalate
        return MATERIAL

    if ct == ENDORSEMENT_REMOVED:
        # Removing an endorsement could remove a cushion; if we cannot tell, escalate.
        return UNKNOWN

    if ct == EXCLUSION_REMOVED:
        return ADMINISTRATIVE          # removing an exclusion widens cover

    return UNKNOWN


def _materiality_reason(change: TermChange) -> str:
    m = change.materiality
    ct = change.change_type
    if m == MATERIAL:
        if ct == EXCLUSION_ADDED:
            return ("A new exclusion narrows what the policy responds to. A category "
                    "of loss that was covered last year may not be this year.")
        if ct in (WARRANTY_ADDED, WARRANTY_CHANGED):
            return ("A new or changed warranty is a condition the insured must meet; "
                    "failing it can affect a claim.")
        if ct == LIMIT_CHANGED:
            return "The limit was reduced, so the most the policy pays has fallen."
        if ct == SUBLIMIT_CHANGED:
            return "A sub-limit was reduced, capping this peril lower than before."
        if ct == DEDUCTIBLE_CHANGED:
            return "The deductible rose, so the insured absorbs more of every claim."
        if ct == EXTENSION_REMOVED:
            return "An extension present last year is gone, removing cover it added."
        return "This change reduces protection."
    if m == ADMINISTRATIVE:
        return ("A favourable or non-substantive change — it does not reduce the "
                "protection the policy provides.")
    return ("The system cannot safely determine what this change does to cover, so it "
            "is escalated rather than assumed harmless. A form number that moved or an "
            "endorsement whose text could not be read hides exactly the kind of "
            "narrowing that leaves an SME uncovered with the price unchanged.")


def _is_unreadable(quote: str) -> bool:
    q = (quote or "").strip().lower()
    return q in _UNREADABLE_MARKERS or "[unreadable" in q or "[not extractable" in q


# ── summary + orchestrator bridge ────────────────────────────────────────────

def compare(prior: PolicyVersion, current: PolicyVersion) -> dict:
    """The full renewal comparison as a serialisable dict, with a summary."""
    changes = compare_terms(prior, current)
    by_materiality = {MATERIAL: 0, ADMINISTRATIVE: 0, UNKNOWN: 0}
    for c in changes:
        by_materiality[c.materiality] = by_materiality.get(c.materiality, 0) + 1
    return {
        "prior": {"document_id": prior.document_id, "label": prior.label,
                  "terms": len(prior.terms)},
        "current": {"document_id": current.document_id, "label": current.label,
                    "terms": len(current.terms)},
        "changes": [c.as_dict() for c in changes],
        "change_count": len(changes),
        "by_materiality": by_materiality,
        "escalations": sum(1 for c in changes if c.escalates),
        "method": ("Deterministic diff of two structured policy versions. Every change "
                   "carries the page and verbatim quote from both documents. "
                   "Materiality is an attention signal, never a coverage decision; "
                   "UNKNOWN escalates rather than being dropped."),
        "notice": ("This compares what the two documents SAY. Whether a change affects "
                   "cover in a given claim is a licensed adviser's determination."),
    }


def material_signals(changes: list[TermChange]) -> list[dict]:
    """Convert MATERIAL and UNKNOWN changes into orchestrator external-signal dicts.

    A material coverage-affecting change becomes a signal the orchestrator reconciles
    under its EXISTING precedence rules — it does not create a parallel verdict. Only
    escalating changes are emitted; administrative ones are not attention items.
    """
    signals: list[dict] = []
    interest_map = {
        EXCLUSION_ADDED: ("stock", "buildings"),
        WARRANTY_ADDED: ("stock",),
        WARRANTY_CHANGED: ("stock",),
        LIMIT_CHANGED: ("stock",),
        SUBLIMIT_CHANGED: ("stock",),
        DEDUCTIBLE_CHANGED: ("stock",),
        EXTENSION_REMOVED: ("stock", "buildings"),
        FORM_NUMBER_CHANGED: ("stock",),
        ENDORSEMENT_ADDED: ("stock",),
        ENDORSEMENT_REMOVED: ("stock",),
    }
    for i, c in enumerate(changes):
        if not c.escalates:
            continue
        severity = "HIGH" if c.materiality == MATERIAL else "ELEVATED"
        signals.append({
            "id": f"RENEWAL-{i+1}",
            "agent": "renewal",
            "signal_type": f"RENEWAL_{c.change_type}",
            "label": c.label or c.change_type,
            "detail": c.materiality_reason,
            "severity": severity,
            "severity_rank": 3 if severity == "HIGH" else 2,
            "horizon": "CURRENT",
            "observed_on": None,
            "window": "This renewal versus the prior term",
            "regions": (),
            "sectors": (),
            "insured_interests": interest_map.get(c.change_type, ("stock",)),
            "relevant_rules": (),
            "claim_scope": "RISK_SIGNAL",
            "source": {"name": "VERIS Renewal comparison", "mode": "DETERMINISTIC",
                       "is_live": False},
            "simulated": False,
            "materiality": c.materiality,
        })
    return signals
