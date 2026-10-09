"""Layer 2 step 3 — coverage-mechanism alignment. Propose, Verify, Bind.

WHAT THIS SOLVES
----------------
The exact (carrier, clause_ref) map in account.py is precise and does not scale: a
human must read a wording before the engine can say anything about it. Three carriers
took a week of reading. The reason a map is needed at all is that carriers name the
same mechanism differently — "Average", "UNDERINSURANCE", "Average Relief" are one
mechanism under three names, and Liberty uses two of them with different thresholds.

So this module identifies a mechanism by the STRUCTURE of its text rather than by its
clause number or its heading.

THE THREE STAGES, AND WHY THE SPLIT MATTERS
-------------------------------------------
  PROPOSE  Offer candidate spans for a mechanism. Deliberately over-inclusive, and
           explicitly ALLOWED TO BE WRONG. A proposer may be lexical, TF-IDF or a
           dense embedding model; it never sees a number and never emits a code that
           is not already in mechanisms.yaml.

  VERIFY   Deterministic. Every mandatory slot must match inside the candidate span
           and no negative slot may match, or the candidate is REJECTED. Ordinary
           regex over the stored page text — no probabilities, no model.

  BIND     Only a verified alignment becomes an Alignment a rule may rest on.
           Everything else stays unaccounted and is quoted.

The proposer gets no vote on whether it was right. That is what keeps the standing
rule — no machine-learning model makes a consequential decision — true even though a
model may now participate in reading.

REJECTIONS ARE OUTPUT, NOT ERROR
--------------------------------
Every rejection is returned with the slot that failed. They are displayed, counted and
scored. A verifier whose rejections are hidden is indistinguishable from one that
rubber-stamps, so the rejection list is the evidence that this stage is load-bearing.

MEASURED: the negative slots are not defensive boilerplate. A first version used only
the two mandatory slots for proportionate reduction and wrongly verified a CONTRIBUTION
clause in the QBE industrial wording — contribution apportions a loss between two
INSURERS and shares the phrase "rateable proportion" with average, which apportions
between insurer and INSURED by reference to a value at risk. The vocabulary overlaps;
the comparison does not. See mechanisms.yaml.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Protocol

import yaml

# parents[2] is the repository root. This used to be `parents[3] / "proven"`, which
# resolved to a SIBLING directory of the repository — so the engine loaded its mechanism
# definitions from outside the checkout and a clean clone had no taxonomy at all.
MECHANISMS_PATH = (
    Path(__file__).resolve().parents[2] / "taxonomy" / "mechanisms.yaml"
)

# Classifier label that travels with a bound alignment, alongside account.py's
# EXACT_MAP and classify.py's HEADING_KEYWORD. A reader can always tell which of the
# three produced a given classification.
MECHANISM_ALIGNED = "MECHANISM_ALIGNED"

# How much text around a proposal anchor forms the candidate span. A clause body in
# these wordings runs a few hundred characters; 900 covers the mechanism plus its
# threshold and formula without swallowing the next clause.
CANDIDATE_WINDOW = 900

_WS = re.compile(r"\s+")


def _flex(pattern: str) -> re.Pattern:
    """Compile a pattern tolerant of the hard newlines pypdf emits mid-sentence.

    A literal space in a mechanisms.yaml pattern must match any run of whitespace or
    almost nothing in these documents matches: "greater value than the sum insured"
    arrives as "greater value than the\nsum insured".
    """
    return re.compile(pattern.replace(" ", r"\s+"), re.IGNORECASE)


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


# ── mechanism definitions ───────────────────────────────────────────────────

# Where an optional slot's value may legitimately be read from.
#
#   clause    ONLY the tightened region around the verified mechanism. Use this where
#             picking the value up from a neighbouring clause would change its
#             meaning — settlement_basis is the measured example: read widely, Chubb
#             cl 4.2 ("the actual value") reported as REINSTATEMENT because the
#             window reached the next clause.
#   candidate The whole proposed window. Safe where the value cannot belong to a
#             different mechanism — a S$25 minimum premium inside a premium payment
#             warranty is unambiguous, and it sits further from the anchor than the
#             tightened region reaches.
SCOPE_CLAUSE = "clause"
SCOPE_CANDIDATE = "candidate"


@dataclass(frozen=True)
class Slot:
    """One structural requirement. `any_of` satisfies the slot; `capture` extracts a
    value; `map` assigns a label by first matching pattern."""

    name: str
    description: str = ""
    any_of: tuple[str, ...] = ()
    capture: Optional[str] = None
    value_map: tuple[tuple[str, str], ...] = ()
    scope: str = SCOPE_CANDIDATE

    def find(self, text: str) -> Optional[re.Match]:
        for pat in self.any_of:
            m = _flex(pat).search(text)
            if m:
                return m
        if self.capture:
            return _flex(self.capture).search(text)
        for _label, pat in self.value_map:
            m = _flex(pat).search(text)
            if m:
                return m
        return None

    def extract(self, text: str) -> Optional[str]:
        """The value this slot carries, if any. None means the slot did not match."""
        if self.capture:
            m = _flex(self.capture).search(text)
            if m:
                return m.group(1) if m.groups() else m.group(0)
            return None
        for label, pat in self.value_map:
            if _flex(pat).search(text):
                return label
        m = self.find(text)
        return _norm(m.group(0)) if m else None


@dataclass(frozen=True)
class Mechanism:
    key: str
    label: str
    taxonomy_code: str
    known_names: tuple[str, ...] = ()
    mandatory: tuple[Slot, ...] = ()
    negative: tuple[Slot, ...] = ()
    optional: tuple[Slot, ...] = ()
    note: str = ""
    engine_behaviour: Optional[str] = None
    never: Optional[str] = None

    @property
    def anchor_patterns(self) -> tuple[str, ...]:
        """Patterns used to PROPOSE candidates — every mandatory slot's vocabulary.

        MEASURED: anchoring on the consequence slot alone missed Liberty General
        Condition 4. GC4 and cl 5 "Basis of Settlement" sit close together, so a
        single anchor produced one window covering both and the second provision was
        never offered as its own candidate. Liberty's two average provisions carry
        DIFFERENT thresholds, and losing either is a wrong answer in one direction or
        the other — which is the exact error this whole module exists to prevent.

        Anchoring on all mandatory slots proposes more candidates and rejects more of
        them. That trade is correct: recall belongs to the proposer, precision belongs
        to the verifier.
        """
        pats: list[str] = []
        for slot in self.mandatory:
            pats.extend(slot.any_of)
        return tuple(dict.fromkeys(pats))


def _slot(name: str, raw: dict) -> Slot:
    if not isinstance(raw, dict):
        return Slot(name=name)
    return Slot(
        name=name,
        description=raw.get("description", ""),
        any_of=tuple(raw.get("any_of", []) or []),
        capture=raw.get("capture"),
        value_map=tuple((raw.get("map") or {}).items()),
        scope=raw.get("scope", SCOPE_CANDIDATE),
    )


def load_mechanisms(path: Path | str = MECHANISMS_PATH) -> dict[str, Mechanism]:
    """Load mechanisms.yaml. A missing file is a degraded mode, not a crash: the
    exact map and the keyword classifier still work without it."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    out: dict[str, Mechanism] = {}
    for key, entry in (raw.get("mechanisms") or {}).items():
        out[key] = Mechanism(
            key=key,
            label=entry.get("label", key),
            taxonomy_code=entry.get("taxonomy_code", key),
            known_names=tuple(entry.get("known_names", []) or []),
            mandatory=tuple(_slot(n, s) for n, s in
                            (entry.get("mandatory_slots") or {}).items()),
            negative=tuple(_slot(n, s) for n, s in
                           (entry.get("negative_slots") or {}).items()),
            optional=tuple(_slot(n, s) for n, s in
                           (entry.get("optional_slots") or {}).items()),
            note=entry.get("note", ""),
            engine_behaviour=entry.get("engine_behaviour"),
            never=entry.get("never"),
        )
    return out


_cache: Optional[dict[str, Mechanism]] = None


def mechanisms() -> dict[str, Mechanism]:
    global _cache
    if _cache is None:
        _cache = load_mechanisms()
    return _cache


# ── results ─────────────────────────────────────────────────────────────────

@dataclass
class Alignment:
    """A VERIFIED alignment. Only these may support a finding."""

    mechanism: str
    taxonomy_code: str
    label: str
    page_no: int
    char_start: int
    char_end: int
    quote: str                                  # verbatim, for the fidelity gate
    matched_slots: dict = field(default_factory=dict)
    values: dict = field(default_factory=dict)  # threshold_pct, days, basis...
    called: Optional[str] = None                # what THIS carrier names it
    classifier: str = MECHANISM_ALIGNED
    proposer: str = ""
    continues_next_page: bool = False           # clause ran past the page break

    def as_dict(self) -> dict:
        return {
            "mechanism": self.mechanism,
            "taxonomy_code": self.taxonomy_code,
            "label": self.label,
            "page_no": self.page_no,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote": self.quote,
            "matched_slots": self.matched_slots,
            "values": self.values,
            "called": self.called,
            "classifier": self.classifier,
            "proposer": self.proposer,
            "continues_next_page": self.continues_next_page,
        }


@dataclass
class Rejection:
    """A proposal the verifier refused. Displayed, never hidden."""

    mechanism: str
    page_no: int
    reason: str                                 # MISSING_SLOT | NEGATIVE_SLOT
    failed_slot: str
    quote: str

    def as_dict(self) -> dict:
        return {
            "mechanism": self.mechanism,
            "page_no": self.page_no,
            "reason": self.reason,
            "failed_slot": self.failed_slot,
            "quote": self.quote,
        }


@dataclass
class AlignmentResult:
    carrier: str
    alignments: list[Alignment] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    proposals: int = 0
    proposer: str = ""
    # Verified alignments collapsed by _dedupe as identical restatements of a
    # mechanism already bound with the same values.
    #
    # MEASURED: this counter did not exist, and the reported figures did not add up.
    # MSIG offered 23 proposals, rejected 13 and displayed 4 bound — six verified
    # duplicates vanished silently between the count and the list. Every other number
    # this project puts on screen reconciles, and a rejection rate a reader cannot
    # check by arithmetic is the kind of number that should not be trusted. Now it
    # closes: proposals == verified + rejected + collapsed.
    collapsed: int = 0

    @property
    def verified(self) -> int:
        return len(self.alignments)

    @property
    def rejected(self) -> int:
        return len(self.rejections)

    def by_mechanism(self, key: str) -> list[Alignment]:
        return [a for a in self.alignments if a.mechanism == key]

    def assert_invariant(self) -> None:
        """Every proposal ended in exactly one of three places. Mirrors the same
        discipline as account.py's Accounting.assert_invariant."""
        total = self.verified + self.rejected + self.collapsed
        if total != self.proposals:
            raise AssertionError(
                f"alignment accounting does not close for {self.carrier}: "
                f"{self.proposals} proposals but verified {self.verified} + "
                f"rejected {self.rejected} + collapsed {self.collapsed} = {total}")

    def as_dict(self) -> dict:
        self.assert_invariant()
        return {
            "carrier": self.carrier,
            "proposer": self.proposer,
            "proposals": self.proposals,
            "verified": self.verified,
            "rejected": self.rejected,
            "collapsed": self.collapsed,
            "rejection_rate": (round(self.rejected / self.proposals, 3)
                               if self.proposals else 0.0),
            "alignments": [a.as_dict() for a in self.alignments],
            "rejections": [r.as_dict() for r in self.rejections],
        }


# ── PROPOSE ─────────────────────────────────────────────────────────────────

class Proposer(Protocol):
    """Offers candidate spans. May be wrong; the verifier decides.

    Deliberately a Protocol so the proposal stage is swappable without touching the
    verifier. A dense-embedding proposer can be dropped in behind this interface and
    nothing downstream changes — which is the point of keeping the two stages apart.
    """

    name: str

    def propose(self, mech: Mechanism, pages: list[str]
                ) -> Iterable[tuple[int, int, str]]:
        """Yield (page_no, char_start, candidate_text)."""
        ...


# How much of the following page is appended to a candidate for VERIFICATION only.
#
# MEASURED: Chubb cl 3.20 was missed entirely. "within 60 days of the inception of
# Policy Period, then:" ends page 8; "automatically terminated" opens page 9, behind a
# page footer. Proposing per page meant no candidate ever held both mandatory slots, so
# the single highest-confidence mechanism in the corpus went unaligned. Clauses running
# across a page break are ordinary in these documents, not an edge case.
#
# The bleed is used to VERIFY. It is never used to bind offsets: char_start and page_no
# stay on the page where the anchor matched, so a bound span still lands at real offsets
# in the stored page text and survives the fidelity gate unchanged.
PAGE_BLEED = 800


class LexicalProposer:
    """Default proposer: anchor phrases, then a window either side.

    Chosen as the default because it needs no model download and runs identically on
    any machine, which matters for a demo. It is over-inclusive on purpose — recall is
    the proposer's job and precision is the verifier's.
    """

    name = "lexical"

    def __init__(self, window: int = CANDIDATE_WINDOW, bleed: int = PAGE_BLEED):
        self.window = window
        self.bleed = bleed

    def propose(self, mech: Mechanism, pages: list[str]
                ) -> Iterable[tuple[int, int, str]]:
        seen: set[tuple[int, int]] = set()
        for page_no, text in enumerate(pages, start=1):
            nxt = pages[page_no] if page_no < len(pages) else ""
            for pat in mech.anchor_patterns:
                for m in _flex(pat).finditer(text):
                    start = max(0, m.start() - self.window // 2)
                    end = min(len(text), m.end() + self.window // 2)
                    # collapse near-duplicate windows on the same page
                    if any(abs(start - s) < 200 for p, s in seen if p == page_no):
                        continue
                    seen.add((page_no, start))
                    candidate = text[start:end]
                    # Only bleed when the window actually reaches the page end, so a
                    # mid-page clause is not polluted by unrelated later text.
                    if nxt and end >= len(text) - 4:
                        candidate += "\n" + nxt[:self.bleed]
                    yield page_no, start, candidate


class TfidfProposer:
    """Optional proposer: rank candidate windows by character n-gram similarity to
    the mechanism's known names and slot vocabulary.

    Uses scikit-learn, already a transitive dependency, so still no new download.
    Character n-grams rather than words because legal boilerplate is stable at the
    substring level and survives the hyphenation and line-breaking pypdf introduces.

    Kept behind the same Protocol as LexicalProposer and NOT the default: it changes
    which candidates are offered, never which are accepted, so it cannot alter a
    finding. Enable it to widen recall on an unfamiliar wording.
    """

    name = "tfidf"

    def __init__(self, window: int = CANDIDATE_WINDOW, top_k: int = 12):
        self.window = window
        self.top_k = top_k

    def propose(self, mech: Mechanism, pages: list[str]
                ) -> Iterable[tuple[int, int, str]]:
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
        except Exception:
            yield from LexicalProposer(self.window).propose(mech, pages)
            return

        windows: list[tuple[int, int, str]] = []
        step = self.window // 2
        for page_no, text in enumerate(pages, start=1):
            for start in range(0, max(1, len(text)), step):
                chunk = text[start:start + self.window]
                if len(chunk) > 120:
                    windows.append((page_no, start, chunk))
        if not windows:
            return

        query = " ".join(list(mech.known_names)
                         + [p for s in mech.mandatory for p in s.any_of])
        query = re.sub(r"[\\^$.|?*+()\[\]{}]", " ", query)
        try:
            vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(4, 5),
                                  min_df=1, max_features=40000)
            matrix = vec.fit_transform([w[2] for w in windows] + [query])
        except ValueError:
            yield from LexicalProposer(self.window).propose(mech, pages)
            return
        import numpy as np

        qv = matrix[-1]
        sims = (matrix[:-1] @ qv.T).toarray().ravel()
        for idx in np.argsort(-sims)[:self.top_k]:
            if sims[idx] <= 0:
                break
            page_no, start, chunk = windows[int(idx)]
            yield page_no, start, chunk


# ── VERIFY ──────────────────────────────────────────────────────────────────

def verify(mech: Mechanism, candidate: str) -> tuple[bool, str, str, dict]:
    """Deterministic slot verification.

    Returns (ok, reason, failed_slot, matched). Order matters: negative slots are
    tested FIRST so a contribution clause is rejected before its shared vocabulary
    can satisfy the average slots.
    """
    for slot in mech.negative:
        m = slot.find(candidate)
        if m:
            return False, "NEGATIVE_SLOT", slot.name, {}

    matched: dict = {}
    for slot in mech.mandatory:
        m = slot.find(candidate)
        if not m:
            return False, "MISSING_SLOT", slot.name, {}
        matched[slot.name] = _norm(m.group(0))[:160]
    return True, "VERIFIED", "", matched


def _local_name(mech: Mechanism, candidate: str) -> Optional[str]:
    """What this carrier calls the mechanism, if one of the known names appears.

    Recorded as a hint only. The alignment does not depend on it — that is the whole
    point of defining a mechanism structurally.
    """
    low = candidate.lower()
    hits = [n for n in mech.known_names if n.lower() in low]
    if not hits:
        return None
    return max(hits, key=len)


def _values(mech: Mechanism, clause: str, candidate: str) -> dict:
    """Extract optional slot values, each from the scope its slot declares.

    Two texts rather than one: `clause` is the tightened region around the verified
    mechanism, `candidate` the whole proposed window. A slot scoped to the clause is
    never read from the wider window, because a value borrowed from a neighbouring
    provision is a wrong answer even when the alignment itself is correct.
    """
    out: dict = {}
    for slot in mech.optional:
        text = clause if slot.scope == SCOPE_CLAUSE else candidate
        v = slot.extract(text)
        if v is not None:
            out[slot.name] = v
    # A worded day count is as good as a numeric one; normalise so callers see `days`.
    if "days" not in out and "days_word" in out:
        out["days"] = out["days_word"]
    return out


# ── BIND ────────────────────────────────────────────────────────────────────

def align_pages(
    pages: list[str],
    carrier: str = "",
    proposer: Optional[Proposer] = None,
    only: Optional[Iterable[str]] = None,
) -> AlignmentResult:
    """Run Propose -> Verify -> Bind over one document's page text.

    `pages` is the stored page text, so char offsets are real offsets into it and a
    bound alignment survives the span-fidelity gate unchanged.
    """
    prop = proposer or LexicalProposer()
    result = AlignmentResult(carrier=carrier, proposer=prop.name)
    wanted = set(only) if only else None

    for key, mech in mechanisms().items():
        if wanted and key not in wanted:
            continue
        for page_no, start, candidate in prop.propose(mech, pages):
            result.proposals += 1
            ok, reason, failed, matched = verify(mech, candidate)
            if not ok:
                result.rejections.append(Rejection(
                    mechanism=key, page_no=page_no, reason=reason,
                    failed_slot=failed, quote=_norm(candidate)[:220]))
                continue
            # Tighten the bound span to the matched region so the quote a reader sees
            # is the mechanism, not 900 characters of surrounding wording.
            #
            # The tightened region is ALSO the region optional values are read from.
            # MEASURED: reading them from the full candidate window reported Chubb
            # cl 4.2 with a REINSTATEMENT basis, because the window reached into the
            # neighbouring clause. A value that belongs to a different clause is a
            # wrong answer even when the alignment itself is right, so value
            # extraction is scoped to the clause that was actually verified.
            # Anchor on whichever mandatory slot actually matched earliest, so the
            # quote centres on the mechanism rather than on a fixed slot that may sit
            # at the far end of the candidate.
            positions = [m.start() for m in
                         (s.find(candidate) for s in mech.mandatory) if m]
            centre = min(positions) if positions else 0
            a = max(0, centre - 260)
            b = min(len(candidate), centre + 400)
            quote = candidate[a:b]
            # A candidate may carry bled text from the following page. Offsets are
            # only valid inside the anchor's own page, so clamp the bound span there
            # and record that the clause continues rather than mis-citing an offset.
            page_len = len(pages[page_no - 1]) - start
            continues = b > page_len
            if continues:
                b = min(b, max(a + 1, page_len))
                quote = candidate[a:b]
            result.alignments.append(Alignment(
                mechanism=key,
                taxonomy_code=mech.taxonomy_code,
                label=mech.label,
                page_no=page_no,
                char_start=start + a,
                char_end=start + b,
                quote=_norm(quote),
                matched_slots=matched,
                values=_values(mech, quote, candidate),
                called=_local_name(mech, candidate),
                proposer=prop.name,
                continues_next_page=continues,
            ))

    before = len(result.alignments)
    result.alignments = _dedupe(result.alignments)
    result.collapsed = before - len(result.alignments)
    result.assert_invariant()
    return result


def _dedupe(alignments: list[Alignment]) -> list[Alignment]:
    """Collapse alignments of the same mechanism carrying the same values.

    A wording repeats a mechanism per section — Liberty states Average Relief at p.7
    and again at p.26. Distinct VALUES are kept, because Liberty's two thresholds are
    two real provisions and losing either is a wrong answer in one direction or the
    other. Identical restatements are collapsed to the first occurrence.
    """
    out: list[Alignment] = []
    seen: set[tuple] = set()
    for a in sorted(alignments, key=lambda x: (x.mechanism, x.page_no)):
        key = (a.mechanism,
               a.values.get("threshold_pct"),
               a.values.get("days"),
               a.values.get("settlement_basis"),
               a.values.get("instrument"),
               bool(a.values.get("stated_formula")))
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def align_document(conn, document_id: str, carrier: str = "",
                   proposer: Optional[Proposer] = None,
                   only: Optional[Iterable[str]] = None) -> AlignmentResult:
    """Align a document already ingested into the database."""
    rows = conn.execute(
        "SELECT page_no, text FROM page_text WHERE document_id=? ORDER BY page_no",
        (document_id,)).fetchall()
    pages = [r["text"] for r in rows]
    return align_pages(pages, carrier=carrier, proposer=proposer, only=only)
