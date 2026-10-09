"""Layer 2 — Account. Every operative provision gets exactly one taxonomy code, or
lands in UNACCOUNTED where it is quoted and counted.

The invariant, asserted in code and in tests:
    classified_count + unaccounted_count == detected_count

DETECTION is scheme-plural, because clause numbering is not a market standard.
`DETECTION_SCHEMES` below carries one detector per numbering convention observed in
the corpus (dotted `4.2`, alpha-prefixed `GC12`, plain `5.`, `SECTION 2`), plus an
all-caps heading detector used only as a fallback for narrative wordings that number
nothing. Every scheme that fired is reported by name with its yield, so "92 detected"
is never a number whose provenance a reader has to guess at. A wording this build
cannot segment reports `detected = 0` and says so, rather than silently reporting a
clean bill of health on a document it could not read.

CLASSIFICATION is two steps, and the difference between them is disclosed everywhere:

  1. EXACT_MAP — the hand-verified `(carrier, clause_ref)` table below. Somebody read
     the wording. This is the ONLY classification a finding may be built on, and the
     only one counted in `verified_classified`.
  2. HEADING_KEYWORD — `taxonomy/classification.yaml`, applied by classify.py. It
     shrinks the unaccounted bucket honestly and feeds no findings.

  Step 3 exists, and it is NOT the constrained model classification specified in
  technical-implementation.md §5. It is `align.py`: structural mechanism alignment,
  where a candidate span may be proposed by any means — lexical, TF-IDF, a dense
  embedding model — and is bound only if a deterministic slot verifier accepts it.
  That split is what keeps the standing claim true: no machine-learning model makes a
  consequential decision. The accounting header says so rather than implying more.

  Both classification steps above remain pure string and regex comparison. Alignment
  is reported separately, with its rejections, so a reader can always tell which of
  the three produced a given classification.

Report the REAL counts. No invented number on a slide.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, Pattern

from .classify import EXACT_MAP, HEADING_KEYWORD, Classification, HeadingClassifier
from .db import new_id
from .ingest import make_span, page_text

# ── detection schemes ───────────────────────────────────────────────────────
# `ref` is group 1, `heading` is group 2 in every pattern, so the caller does not
# branch per scheme.


@dataclass(frozen=True)
class DetectionScheme:
    name: str
    regex: Pattern[str]
    description: str
    is_clause_level: bool = True    # feeds the boundary gate and citations
    fallback: bool = False          # only used when no primary scheme fires


# Dotted decimal — Chubb Select+ and most Singapore package wordings.
NUMERIC_DOTTED = DetectionScheme(
    "NUMERIC_DOTTED",
    re.compile(r"^\s*(\d{1,2}\.\d{1,2}(?:\([a-z]\))?(?:\([ivx]+\))?)\s+(.{0,80})",
               re.M),
    "Dotted decimal references such as 4.2 or 5.1(c)(i).",
)

# Alpha-prefixed general/section conditions — MSIG GC12, Liberty GC4.
# "CL" is deliberately absent from the prefix list: it would match prose such as
# "cl 3.20 provides..." at a line start and invent a provision out of a citation.
ALPHA_PREFIXED = DetectionScheme(
    "ALPHA_PREFIXED",
    re.compile(r"^\s*((?:GC|SC|PC|EC|GE|SE|GX|SX)\s?\.?\s?\d{1,2}[A-Za-z]?)"
               r"[\.\)\:\s]\s*(.{0,80})", re.M | re.I),
    "Alpha-prefixed condition references such as GC12 or SC4.",
)

# Plain integer headings — "5. Basis of Settlement". Requires a capitalised heading
# so numbered prose inside a clause body is not mistaken for a provision.
NUMERIC_SIMPLE = DetectionScheme(
    "NUMERIC_SIMPLE",
    re.compile(r"^\s*(\d{1,2})[\.\)]\s+([A-Z][A-Za-z''\-/&() ]{3,79})\s*$", re.M),
    "Plain integer headings such as '5. Basis of Settlement'.",
)

# Section headings — structural rather than clause-level, so excluded from the
# boundary gate: a citation is a clause reference, never a section number.
SECTION = DetectionScheme(
    "SECTION",
    re.compile(r"^\s*(SECTION\s+[0-9IVXivx]{1,4}[A-Za-z]?)\b[ \-–:\.]*(.{0,80})",
               re.M | re.I),
    "Section headings such as 'SECTION 2 — Liability'.",
    is_clause_level=False,
)

# Narrative wordings that number nothing. Noisy, so used only when nothing else
# fires — reported explicitly when it does.
UPPER_HEADING = DetectionScheme(
    "UPPER_HEADING",
    re.compile(r"^\s*([A-Z][A-Z''&/\-\. ]{7,60})\s*$", re.M),
    "Unnumbered capitalised headings (narrative wordings).",
    is_clause_level=False,
    fallback=True,
)

DETECTION_SCHEMES: tuple[DetectionScheme, ...] = (
    NUMERIC_DOTTED, ALPHA_PREFIXED, NUMERIC_SIMPLE, SECTION, UPPER_HEADING,
)

PRIMARY_SCHEMES = tuple(s for s in DETECTION_SCHEMES if not s.fallback)
FALLBACK_SCHEMES = tuple(s for s in DETECTION_SCHEMES if s.fallback)

# The boundary gate (gate 2) compares a span's enclosing heading against the clause
# refs a finding CITES. Citations are clause references, so only clause-level schemes
# participate — a section heading between a clause and its own body must not be
# mistaken for the enclosing provision.
BOUNDARY_SCHEMES = tuple(s for s in DETECTION_SCHEMES if s.is_clause_level)

# Below this many primary hits a document is treated as unnumbered and the fallback
# heading detector is added.
_FALLBACK_THRESHOLD = 5

# ── Exact (carrier, clause_ref) -> taxonomy_code map ────────────────────────
# Verified against the real wordings. Chubb read in full (28pp); MSIG and Liberty
# on four targeted topics, extended below where the corpus text was read directly.
# Keyed by (carrier, clause_ref). NEVER by clause_ref alone: the same behaviour sits
# at different references in different wordings.
CARRIER_CLAUSE_MAP: dict[tuple[str, str], str] = {
    ("CHUBB", "4.2"): "ADEQ.AVERAGE_CONDITION",
    ("CHUBB", "5.1(c)(i)"): "EXCL.THEFT_FORCIBLE_ENTRY",
    ("CHUBB", "3.1"): "COND.ALTERATION_NOTICE",
    ("CHUBB", "3.20"): "COND.PREMIUM_PAYMENT_WARRANTY",
    ("CHUBB", "3.12"): "LIMIT.DEDUCTIBLE",
    ("CHUBB", "4.9"): "COND.MINIMUM_SECURITY",
    ("CHUBB", "4.13"): "COND.CLAIM_TIME_LIMIT",
    ("CHUBB", "3.13"): "COND.DISCLOSURE",
    ("CHUBB", "3.21"): "COND.STATUTORY_COMPLIANCE_GENERAL",
    ("CHUBB", "5.1(b)(v)"): "EXCL.VACANCY_WATER",
    ("CHUBB", "2.35"): "DEFN.VACANT",
    ("CHUBB", "3.7"): "COND.CLAIMS_PROCEDURE",
    ("CHUBB", "4.11"): "ADEQ.REINSTATEMENT_BASIS",
    ("CHUBB", "4.10"): "ADEQ.INDEMNITY_BASIS",
    ("CHUBB", "4.6"): "LIMIT.AGGREGATE",
    ("CHUBB", "5.1(a)(i)"): "EXCL.WEAR_AND_TEAR",
    ("CHUBB", "5.1(b)(iv)"): "EXCL.MECHANICAL_BREAKDOWN",
    ("CHUBB", "5.1(c)(ii)"): "EXCL.FRAUD_DISHONESTY",
    ("CHUBB", "5.1(c)(iii)"): "EXCL.INVENTORY_SHORTAGE",
    # MSIG (verified on four topics)
    ("MSIG", "GC12"): "ADEQ.UNDERINSURANCE_NO_THRESHOLD",
    ("MSIG", "GC17"): "COND.PRECEDENT_PREMIUM_HISTORY",
    ("MSIG", "PPW"): "COND.PREMIUM_PAYMENT_WARRANTY",
    # Liberty (verified on four topics)
    ("LIBERTY", "GC4"): "ADEQ.AVERAGE_CONDITION",
    ("LIBERTY", "5"): "ADEQ.AVERAGE_FORMULA",
    ("LIBERTY", "14"): "COND.PREMIUM_PAYMENT_WARRANTY",
}

# Authority rank by document kind (lower governs).
RANK = {"ENDORSEMENT": 1, "SCHEDULE": 2, "BASE_WORDING": 3, "PROPOSAL": 4}


# A dedicated construction / order-of-precedence clause is detected on these
# phrases. Chubb Select+ has NONE of these — its only conflict statement is the
# section-prevails sentence, which is not an order of precedence across the whole
# policy. Searched, not found, and that fact is surfaced on the record.
_CONSTRUCTION_PHRASES = (
    "order of precedence",
    "order of priority",
    "shall prevail in the following order",
    "the following order of",
)

# The clause quoted for an unaccounted provision is capped at this many chars so a
# human can read it without the whole page.
_QUOTE_CAP = 300

_classifier_cache: Optional[HeadingClassifier] = None


def heading_classifier() -> HeadingClassifier:
    """Process-wide classifier. Loaded once; the YAML does not change at runtime."""
    global _classifier_cache
    if _classifier_cache is None:
        _classifier_cache = HeadingClassifier.load()
    return _classifier_cache


@dataclass
class Accounting:
    detected: int
    classified: int
    unaccounted: int
    unaccounted_quotes: list[dict]     # {clause_ref, heading, quote, page_no}
    classified_refs: list[dict]        # {clause_ref, code, page_no, classifier}
    duplicates_collapsed: int          # clause refs seen more than once (TOC etc.)
    detection_note: str
    has_construction_clause: bool
    verified_classified: int = 0       # step 1 only — what a claim may rest on
    keyword_classified: int = 0        # step 2 — disclosed separately, never claimed
    ambiguous_abstained: int = 0       # two candidate codes -> left unaccounted
    schemes_used: list[dict] = field(default_factory=list)
    carrier: str = ""
    carrier_in_clause_map: bool = True

    def assert_invariant(self) -> None:
        assert self.classified + self.unaccounted == self.detected, (
            f"accounting invariant broken: {self.classified} + {self.unaccounted}"
            f" != {self.detected}"
        )
        assert self.verified_classified + self.keyword_classified == self.classified, (
            f"classifier split broken: {self.verified_classified} + "
            f"{self.keyword_classified} != {self.classified}"
        )

    def as_dict(self) -> dict:
        return {
            "detected": self.detected,
            "classified": self.classified,
            "unaccounted": self.unaccounted,
            "verified_classified": self.verified_classified,
            "keyword_classified": self.keyword_classified,
            "ambiguous_abstained": self.ambiguous_abstained,
            "unaccounted_quotes": self.unaccounted_quotes,
            "classified_refs": self.classified_refs,
            "duplicates_collapsed": self.duplicates_collapsed,
            "detection_note": self.detection_note,
            "has_construction_clause": self.has_construction_clause,
            "schemes_used": self.schemes_used,
            "carrier": self.carrier,
            "carrier_in_clause_map": self.carrier_in_clause_map,
        }


def empty_accounting(carrier: str = "", note: str = "") -> Accounting:
    """The honest zero. Used when there is no base wording to account for: an upload
    of supporting documents only must not imply a wording was read."""
    return Accounting(
        detected=0, classified=0, unaccounted=0, unaccounted_quotes=[],
        classified_refs=[], duplicates_collapsed=0,
        detection_note=note or "No base policy wording was provided, so no "
                               "provisions were detected. Nothing about the "
                               "policy's terms is claimed here.",
        has_construction_clause=False, carrier=carrier,
        carrier_in_clause_map=carrier_is_mapped(carrier))


def carrier_is_mapped(carrier: str) -> bool:
    """True if any verified clause reference exists for this carrier."""
    return any(c == carrier for c, _ in CARRIER_CLAUSE_MAP)


def mapped_codes(carrier: str) -> set[str]:
    """Taxonomy codes a human has verified against this carrier's wording.

    This — not the set of provisions the regex happened to segment — is the test of
    whether a rule may cite a clause. Chubb's exclusions render as `5.1` followed by
    `(b)(v)` on later lines, so `5.1(b)(v)` never appears as a literal string for the
    detector to find, yet the clause was read in full and page-cited by hand against the
    corpus wording. Gating on detection would silence findings whose evidence is
    strongest, which is precisely backwards.
    """
    return {code for (c, _ref), code in CARRIER_CLAUSE_MAP.items() if c == carrier}


# ── detection ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProvisionHit:
    """One detected provision heading. `raw` is the matched text exactly as it
    appears in the stored page, so a span anchored on it lands at real offsets and
    survives the fidelity gate."""

    start: int
    ref: str
    heading: str
    raw: str
    scheme: str


def scheme_matches(
    text: str, schemes: tuple[DetectionScheme, ...] = PRIMARY_SCHEMES,
) -> list[ProvisionHit]:
    """Every provision heading in `text`, in reading order, deduplicated across
    schemes.

    Overlap policy: when two schemes match at (or very near) the same offset, the
    earlier scheme in `DETECTION_SCHEMES` wins. Order is specificity order, so a
    dotted reference is never demoted to a plain integer.
    """
    hits: list[ProvisionHit] = []
    claimed: set[int] = set()
    for scheme in schemes:
        for m in scheme.regex.finditer(text):
            start = m.start()
            # A match whose start sits inside a range already claimed by a more
            # specific scheme is the same heading seen twice.
            if any(abs(start - c) < 2 for c in claimed):
                continue
            heading = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            hits.append(ProvisionHit(
                start=start, ref=_clean_quote(m.group(1)),
                heading=_clean_quote(heading), raw=m.group(0).strip(),
                scheme=scheme.name))
            claimed.add(start)
    hits.sort(key=lambda h: h.start)
    return hits


def detect_provisions(
    conn: sqlite3.Connection, document_id: str, page_count: int,
) -> tuple[dict[int, list[ProvisionHit]], dict[str, int], bool]:
    """Detect provisions across every page. Returns (per-page hits, per-scheme
    counts, fallback_used)."""

    def scan(schemes: tuple[DetectionScheme, ...]):
        per_page: dict[int, list[ProvisionHit]] = {}
        counts: dict[str, int] = {}
        total = 0
        for page_no in range(1, page_count + 1):
            hits = scheme_matches(page_text(conn, document_id, page_no), schemes)
            per_page[page_no] = hits
            total += len(hits)
            for h in hits:
                counts[h.scheme] = counts.get(h.scheme, 0) + 1
        return per_page, counts, total

    per_page, counts, total = scan(PRIMARY_SCHEMES)
    if total >= _FALLBACK_THRESHOLD:
        return per_page, counts, False

    # Unnumbered or near-unnumbered document: add the heading detector so a
    # narrative wording is segmented rather than reported as containing nothing.
    per_page, counts, _ = scan(PRIMARY_SCHEMES + FALLBACK_SCHEMES)
    return per_page, counts, True


def enclosing_provision_ref(
    page_text_fn, page_no: int, char_start: int,
    cache: Optional[dict[int, list[tuple[int, str]]]] = None,
) -> Optional[str]:
    """The clause reference whose heading encloses a position in a document.

    ONE definition, shared by the boundary gate (rules.py) and the citation audit
    (eval/citation_audit.py). They had separate copies and the copies disagreed: the
    audit reported cl 3.20's span as out of bounds because it lacked the walk-back to
    earlier pages, which is exactly the case cl 3.20 needs — its heading is on page 8
    and its (a)(b)(c) limbs are on page 9. Two implementations of the same rule means
    the auditor and the thing it audits can disagree about what passing means.

    Only CLAUSE-LEVEL schemes participate: a citation is a clause reference, so a
    `SECTION 2` heading between a clause and its own body must not be mistaken for
    the enclosing provision.
    """
    cache = cache if cache is not None else {}

    def matches_on(page: int) -> list[tuple[int, str]]:
        if page not in cache:
            cache[page] = [(h.start, h.ref) for h in
                           scheme_matches(page_text_fn(page), BOUNDARY_SCHEMES)]
        return cache[page]

    preceding = [ref for start, ref in matches_on(page_no) if start <= char_start]
    if preceding:
        return preceding[-1]
    for page in range(page_no - 1, 0, -1):     # clause body continued from earlier
        hits = matches_on(page)
        if hits:
            return hits[-1][1]
    return None


def citation_consistent(enclosing: Optional[str], cited: set[str]) -> bool:
    """Whether an enclosing reference is consistent with what a finding cites.

    Equal, or one a prefix of the other, so a body under 5.1(c) satisfies a citation
    of 5.1(c)(i). Shared for the same reason as `enclosing_provision_ref`.
    """
    if enclosing is None or not cited:
        return False
    return any(enclosing == c or c.startswith(enclosing) or enclosing.startswith(c)
               for c in cited)


def detect_construction_clause(conn: sqlite3.Connection, document_id: str) -> bool:
    """True only if the wording states a dedicated order-of-precedence clause.
    Read FIRST (spec §6.2). Chubb has none — this returns False, which drives the
    market-convention precedence note."""
    row = conn.execute(
        "SELECT page_count FROM document WHERE id=?", (document_id,)
    ).fetchone()
    page_count = row["page_count"] if row else 0
    for page_no in range(1, page_count + 1):
        text = page_text(conn, document_id, page_no).lower()
        if any(p in text for p in _CONSTRUCTION_PHRASES):
            return True
    return False


def account_base_wording(
    conn: sqlite3.Connection, document_id: str, carrier: str,
) -> Accounting:
    """Detect and classify every provision in a base wording. Creates provision rows
    and returns the real detected/classified/unaccounted counts.

    Honesty audit (spec §5.2): the detectors match every numbered provision including
    definitions (2.x) and important notices (1.x). A ref that recurs — a
    table-of-contents line ahead of the body, or a running header — is counted ONCE,
    at first occurrence, and the number of collapsed repeats is reported explicitly
    rather than hidden. Each unaccounted provision is quoted so a human can read
    exactly what we could not classify.
    """
    page_count_row = conn.execute(
        "SELECT page_count FROM document WHERE id=?", (document_id,)
    ).fetchone()
    page_count = page_count_row["page_count"] if page_count_row else 0

    per_page, scheme_counts, fallback_used = detect_provisions(
        conn, document_id, page_count)
    classifier = heading_classifier()

    seen_refs: set[str] = set()
    classified_refs: list[dict] = []
    unaccounted_quotes: list[dict] = []
    detected = 0
    duplicates_collapsed = 0
    verified = keyword = ambiguous = 0

    for page_no in range(1, page_count + 1):
        text = page_text(conn, document_id, page_no)
        hits = per_page.get(page_no, [])
        for i, hit in enumerate(hits):
            clause_ref, heading = hit.ref, hit.heading
            if clause_ref in seen_refs:
                duplicates_collapsed += 1
                continue
            seen_refs.add(clause_ref)
            detected += 1

            # Quote from this heading to the next detected provision on the page
            # (capped), so the unaccounted list is genuinely quoted text.
            body_end = hits[i + 1].start if i + 1 < len(hits) else len(text)
            raw_body = text[hit.start:body_end]
            quote = _clean_quote(raw_body[:_QUOTE_CAP])

            result = _classify_provision(
                classifier, carrier, clause_ref, heading, raw_body[:_QUOTE_CAP * 2])
            if result.ambiguous_candidates:
                ambiguous += 1

            # Anchor on the raw matched text, not a normalised copy of it: the
            # fidelity gate compares byte-for-byte against the stored page.
            span = make_span(conn, document_id, page_no, hit.raw[:80])
            span_id = span.id if span else _placeholder_span(
                conn, document_id, page_no)
            scheme_name = hit.scheme
            status = "CLASSIFIED" if result.classified else "UNACCOUNTED"
            conn.execute(
                "INSERT INTO provision (id, document_id, span_id, clause_ref, "
                "taxonomy_code, status, authority_rank, classifier, "
                "detection_scheme) VALUES (?,?,?,?,?,?,?,?,?)",
                (new_id(), document_id, span_id, clause_ref, result.code, status,
                 RANK["BASE_WORDING"], result.classifier, scheme_name),
            )
            if result.classified:
                if result.classifier == EXACT_MAP:
                    verified += 1
                else:
                    keyword += 1
                classified_refs.append({
                    "clause_ref": clause_ref, "code": result.code,
                    "page_no": page_no, "classifier": result.classifier,
                    "matched_on": result.matched_on, "heading": heading,
                })
            else:
                unaccounted_quotes.append({
                    "clause_ref": clause_ref, "heading": heading,
                    "quote": quote, "page_no": page_no,
                    "detection_scheme": scheme_name,
                    "ambiguous_candidates": result.ambiguous_candidates,
                })

    conn.commit()
    schemes_used = [
        {"name": s.name, "hits": scheme_counts.get(s.name, 0),
         "description": s.description}
        for s in DETECTION_SCHEMES if scheme_counts.get(s.name)
    ]
    acc = Accounting(
        detected=detected,
        classified=len(classified_refs),
        unaccounted=len(unaccounted_quotes),
        unaccounted_quotes=unaccounted_quotes,
        classified_refs=classified_refs,
        duplicates_collapsed=duplicates_collapsed,
        detection_note="",
        has_construction_clause=detect_construction_clause(conn, document_id),
        verified_classified=verified,
        keyword_classified=keyword,
        ambiguous_abstained=ambiguous,
        schemes_used=schemes_used,
        carrier=carrier,
        carrier_in_clause_map=carrier_is_mapped(carrier),
    )
    acc.detection_note = _detection_note(acc, page_count, fallback_used)
    acc.assert_invariant()
    return acc


def _classify_provision(
    classifier: HeadingClassifier, carrier: str, clause_ref: str, heading: str,
    body: str,
) -> Classification:
    """Step 1 then step 2. There is no step 3."""
    code = CARRIER_CLAUSE_MAP.get((carrier, clause_ref))
    if code:
        return Classification(code=code, classifier=EXACT_MAP,
                              matched_on=f"{carrier} {clause_ref}")
    return classifier.classify(heading, body)


def _detection_note(acc: Accounting, page_count: int, fallback_used: bool) -> str:
    schemes = ", ".join(f"{s['name']} ({s['hits']})" for s in acc.schemes_used) \
        or "none — no numbering or heading scheme matched"
    parts = [
        f"Detected across all {page_count} pages by deterministic pattern matching. "
        f"Schemes that fired: {schemes}.",
        f"Count is unique provision references (definitions and notices included); "
        f"{acc.duplicates_collapsed} recurring references (table-of-contents lines, "
        f"running headers) were collapsed to their first occurrence.",
        f"Classified: {acc.verified_classified} by the hand-verified "
        f"(carrier, clause_ref) map — the only classification a finding may rest on "
        f"— and {acc.keyword_classified} by heading keyword, which shrinks the "
        f"unaccounted bucket but feeds no finding.",
    ]
    if acc.ambiguous_abstained:
        parts.append(
            f"{acc.ambiguous_abstained} provisions matched two or more candidate "
            f"codes and were left UNACCOUNTED rather than resolved by guess.")
    if not acc.carrier_in_clause_map:
        parts.append(
            f"Carrier {acc.carrier or 'UNKNOWN'} has no verified clause map in this "
            f"build, so every classification here came from heading keywords and no "
            f"finding is built on this wording's clause references.")
    if fallback_used:
        parts.append(
            "This document numbers few or no provisions, so unnumbered capitalised "
            "headings were used as the segmentation fallback. Treat the counts as "
            "heading-level rather than clause-level.")
    # This sentence used to read "There is no model anywhere in this pipeline." That
    # was true when it was written and stopped being exactly true when align.py gained
    # an optional TF-IDF proposer. A claim in the product's own output drifting out of
    # date is the specific failure this product exists to prevent, so it now states
    # what the architecture enforces instead of what was once incidentally the case.
    parts.append(
        "Classification above is deterministic string and regex comparison. Layer 2 "
        "step 3 is implemented as structural mechanism alignment (see the clause "
        "alignment view), not as constrained model classification: a candidate span "
        "may be proposed by any means, including a model, but it is bound only if "
        "every mandatory structural element matches and no disqualifying one does. "
        "No machine-learning model makes a consequential decision anywhere in this "
        "pipeline.")
    return " ".join(parts)


def _clean_quote(s: str) -> str:
    return " ".join((s or "").split())


# Amendment phrasings mapped to the taxonomy code they touch. Small and explicit on
# purpose: an endorsement's effect must be READ, not guessed, so only phrasings we can
# defend map to a code, and anything else parses to no code and is treated as an
# endorsement whose effect we could not establish.
_ENDORSEMENT_CODE_MARKERS: tuple[tuple[str, str], ...] = (
    ("sub-limit", "LIMIT.SUB_LIMIT"),
    ("sub limit", "LIMIT.SUB_LIMIT"),
    ("sublimit", "LIMIT.SUB_LIMIT"),
)

# Language that tells us the amended text itself could not be read. An endorsement we
# can see is an endorsement but whose EFFECT is an unreadable scan is the honest
# abstention case — we say so rather than invent what it changed.
_ENDORSEMENT_ILLEGIBLE_MARKERS: tuple[str, ...] = (
    "could not be reproduced", "scanned image", "illegible", "unreadable",
    "not legible",
)

_END_REF = re.compile(
    r"endorsement\s*(?:no\.?|number|#)?\s*[:\-]?\s*"
    r"([A-Z]{2,5}-?\d{1,4}|\d{1,4})", re.IGNORECASE)
_END_VALUE = re.compile(r"(?:SGD|S\$)\s*([\d,]+)", re.IGNORECASE)
_END_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse_endorsement(pages: list[str]) -> dict:
    """Read an uploaded endorsement PDF into the same shape a fixture endorsement has.

    WHY THIS EXISTS. `account_endorsements` and the illegible-endorsement rule both
    read structured fields (`amends_taxonomy_code`, `value_sgd`, ...). Those exist for
    JSON fixtures but not for an uploaded PDF, so before this, an endorsement a user
    uploaded produced no finding at all — the conflicting-sub-limit escalation and the
    illegible-scan abstention only ever fired on the demo fixtures.

    This is deterministic pattern reading, not inference: a phrasing maps to a code
    only if it is in `_ENDORSEMENT_CODE_MARKERS`, a value is taken only if it is
    written as SGD/S$, and an endorsement whose amended text is described as an
    unreadable scan is marked `illegible` rather than assigned a guessed effect.

    Returns {} when the text does not read as an endorsement at all, so a
    mis-detected document contributes nothing.
    """
    text = "\n".join(p for p in pages if p)
    low = text.lower()
    if "endors" not in low:
        return {}

    ref_m = _END_REF.search(text)
    ref = ref_m.group(1) if ref_m else None
    if ref and "-" not in ref and ref.isdigit():
        ref = f"END-{int(ref):02d}"

    date_m = _END_DATE.search(text)
    effective_date = date_m.group(1) if date_m else None

    code = None
    for marker, mapped in _ENDORSEMENT_CODE_MARKERS:
        if marker in low:
            code = mapped
            break

    value = None
    val_m = _END_VALUE.search(text)
    if val_m:
        try:
            value = int(val_m.group(1).replace(",", ""))
        except ValueError:
            value = None

    illegible = (value is None and code is None
                 and any(m in low for m in _ENDORSEMENT_ILLEGIBLE_MARKERS))

    # The amendment sentence, for the verbatim quote. First line mentioning the code
    # phrasing or "amended", else the first non-empty line.
    amend_line = ""
    for line in text.splitlines():
        ll = line.lower()
        if "amend" in ll or "sub-limit" in ll or "sub limit" in ll:
            amend_line = line.strip()
            break
    if not amend_line:
        amend_line = next((l.strip() for l in text.splitlines() if l.strip()), "")

    return {
        "endorsement_ref": ref,
        "amends_taxonomy_code": code,
        "amends_clause_ref": amend_line[:120] or None,
        "value_sgd": value,
        "text": amend_line or text.strip()[:200],
        "effective_date": effective_date,
        "illegible": illegible,
        "_document_kind": "ENDORSEMENT",
        "_author_class": "INSURER",
    }


def account_endorsements(conn: sqlite3.Connection, store) -> list:
    """Create provision rows for endorsement documents that declare which taxonomy
    code they amend (`amends_taxonomy_code`). These sit at ENDORSEMENT rank (1) and
    feed the precedence resolver: two endorsements amending the same code with no
    order of precedence between them is a tie the resolver must refuse to break.

    Returns the list of Provision objects created (base + these are resolved
    together by the caller)."""
    from .models import Provision

    created: list = []
    for doc in store.all("ENDORSEMENT"):
        code = doc.data.get("amends_taxonomy_code")
        if not code:
            continue
        needle = doc.data.get("text") or doc.data.get("amends_clause_ref") or code
        span = make_span(conn, doc.id, 1, needle)
        span_id = span.id if span else _placeholder_span(conn, doc.id, 1)
        pid = new_id()
        clause_ref = doc.data.get("amends_clause_ref")
        conn.execute(
            "INSERT INTO provision (id, document_id, span_id, clause_ref, "
            "taxonomy_code, status, authority_rank, classifier, detection_scheme) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, doc.id, span_id, clause_ref, code, "CLASSIFIED",
             RANK["ENDORSEMENT"], EXACT_MAP, "DECLARED_BY_ENDORSEMENT"),
        )
        created.append(Provision(
            id=pid, document_id=doc.id, span_id=span_id, clause_ref=clause_ref,
            taxonomy_code=code, status="CLASSIFIED",
            authority_rank=RANK["ENDORSEMENT"],
            quoted_text=(span.quoted_text if span else "")))
    conn.commit()
    return created


def _placeholder_span(conn: sqlite3.Connection, document_id: str, page_no: int) -> str:
    """Insert a zero-width span when a heading could not be re-anchored, so the
    provision row still references a real (empty) span for referential integrity."""
    sid = new_id()
    conn.execute(
        "INSERT INTO span (id, document_id, page_no, char_start, char_end, "
        "quoted_text, sha256) VALUES (?,?,?,?,?,?,?)",
        (sid, document_id, page_no, 0, 0, "", ""),
    )
    return sid


def find_clause_span(
    conn: sqlite3.Connection, document_id: str, needle: str,
):
    """Search every page of a document for `needle`, return an anchored Span or
    None. Used by the rules engine to attach base-wording operands to real spans."""
    row = conn.execute(
        "SELECT page_count FROM document WHERE id=?", (document_id,)
    ).fetchone()
    page_count = row["page_count"] if row else 0
    for page_no in range(1, page_count + 1):
        span = make_span(conn, document_id, page_no, needle)
        if span:
            return span
    return None


def verified_codes(conn: sqlite3.Connection, document_id: str) -> set[str]:
    """Taxonomy codes classified by the EXACT_MAP in this document. rules.py checks
    against this set so a HEADING_KEYWORD classification can never feed a finding."""
    rows = conn.execute(
        "SELECT taxonomy_code FROM provision WHERE document_id=? "
        "AND status='CLASSIFIED' AND classifier=?",
        (document_id, EXACT_MAP),
    ).fetchall()
    return {r["taxonomy_code"] for r in rows if r["taxonomy_code"]}
