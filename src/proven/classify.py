"""Layer 2 step 2 — heading-keyword classification.

The rules live in `taxonomy/classification.yaml`; this module is the matcher. It is
deterministic string and regex comparison, with no model of any kind.

(This docstring used to add "and none anywhere else in the codebase". That is no
longer exactly true: `align.py` offers an optional TF-IDF proposer. It cannot decide
anything — a proposed span binds only if a deterministic slot verifier accepts it —
but the sentence was overstated and is corrected rather than left to rot.)

Three properties the rest of the system depends on:

  * `Classification.classifier` is always one of EXACT_MAP | HEADING_KEYWORD, and it
    travels with the provision into the database and the Review Brief. Alignment adds
    a third label, MECHANISM_ALIGNED, carried on its own results. A reader can always
    tell which of the three classified a given provision.
  * Ambiguity abstains. Two candidate codes for one provision leaves it UNACCOUNTED
    with both candidates recorded. The engine that refuses to break a precedence tie
    does not get to break a classification tie either.
  * A HEADING_KEYWORD classification may not feed a finding (enforced in rules.py).
    It shrinks the unaccounted bucket; it does not license a claim.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# parents[2] is the repository root. See the note in align.py.
CLASSIFICATION_PATH = (
    Path(__file__).resolve().parents[2] / "taxonomy" / "classification.yaml"
)

EXACT_MAP = "EXACT_MAP"
HEADING_KEYWORD = "HEADING_KEYWORD"

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "")).strip().lower()


@dataclass(frozen=True)
class KeywordRule:
    code: str
    label: str = ""
    heading_any_of: tuple[str, ...] = ()
    body_any_of: tuple[str, ...] = ()
    body_regex: Optional[str] = None
    exclude_any_of: tuple[str, ...] = ()

    def matches(self, heading: str, body: str) -> bool:
        h, b = _norm(heading), _norm(body)
        if not (self.heading_any_of or self.body_any_of or self.body_regex):
            return False                        # a rule with no condition matches nothing
        if any(x in b or x in h for x in self.exclude_any_of):
            return False
        if self.heading_any_of and not any(x in h for x in self.heading_any_of):
            return False
        if self.body_any_of and not any(x in b for x in self.body_any_of):
            return False
        if self.body_regex and not re.search(self.body_regex, body or "",
                                             re.IGNORECASE):
            return False
        return True


@dataclass
class Classification:
    """The outcome for one provision."""

    code: Optional[str] = None
    classifier: Optional[str] = None            # EXACT_MAP | HEADING_KEYWORD | None
    matched_on: Optional[str] = None
    ambiguous_candidates: list[str] = field(default_factory=list)

    @property
    def classified(self) -> bool:
        return self.code is not None


class HeadingClassifier:
    """Loads and applies the keyword rules. Immutable after construction."""

    def __init__(self, rules: list[KeywordRule], meta: Optional[dict] = None):
        self.rules = rules
        self.meta = meta or {}

    @classmethod
    def load(cls, path: Path | str = CLASSIFICATION_PATH) -> "HeadingClassifier":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            # A missing classifier is a degraded mode, not a crash: step 1 still
            # works and the accounting header reports zero keyword classifications.
            return cls([], {"step": 2, "loaded": False})
        rules = []
        for entry in raw.get("codes", []) or []:
            rules.append(KeywordRule(
                code=entry["code"],
                label=entry.get("label", ""),
                heading_any_of=tuple(_norm(x) for x in
                                     entry.get("heading_any_of", []) or []),
                body_any_of=tuple(_norm(x) for x in
                                  entry.get("body_any_of", []) or []),
                body_regex=entry.get("body_regex"),
                exclude_any_of=tuple(_norm(x) for x in
                                     entry.get("exclude_any_of", []) or []),
            ))
        meta = dict(raw.get("meta", {}) or {})
        meta["loaded"] = True
        meta["rule_count"] = len(rules)
        return cls(rules, meta)

    def classify(self, heading: str, body: str) -> Classification:
        """Return a Classification. Distinct competing codes abstain."""
        hits = [r for r in self.rules if r.matches(heading, body)]
        codes = sorted({r.code for r in hits})
        if not codes:
            return Classification()
        if len(codes) > 1:
            return Classification(ambiguous_candidates=codes)
        rule = hits[0]
        matched = next((x for x in rule.heading_any_of if x in _norm(heading)), None) \
            or next((x for x in rule.body_any_of if x in _norm(body)), None) \
            or (rule.body_regex and "pattern") or None
        return Classification(code=codes[0], classifier=HEADING_KEYWORD,
                              matched_on=matched)
