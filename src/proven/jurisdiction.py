"""Jurisdiction packs: country behaviour as versioned data, and how a bundle selects one.

WHY THIS EXISTS. The SME engine was written for Singapore, and Singapore was everywhere in
it as code: a work injury threshold in a sentence in `stages.py`, a 60-day premium payment
period in `warranty.py`, a medical-insurance minimum in `extract.py`, who authors a CPF
record in `detect.py`. Nothing was wrong with any of those values. What was wrong is that
nothing could ever say "this document is not from Singapore" — so a Malaysian wording was
read against Singapore law, because Singapore was the only law the engine knew.

So a country is now a pack, `jurisdictions/<CODE>.yaml`, validated by the models below, and a
bundle's own documents choose which pack applies (`detect_jurisdiction`). When they do not
choose clearly — a country with no pack yet, two countries too evenly, or no country at all —
the GENERIC pack applies. GENERIC reads and quotes the policy, runs every check written
against the policy itself, runs no check written against a country's law, and says which pack
is missing. It never guesses a country.

THE VERIFICATION LADDER. Every entry that states something about the law carries
`verification: {status, source_url, retrieved_on, quote, verified_by}`:

    CANDIDATE       stored, shown in the evidence view, NEVER turned into a sentence an
                    owner reads
    SOURCE_QUOTED   a verbatim quote from the primary source, with its URL and the date it
                    was retrieved; `scripts/verify_pack_sources.py` checks the quote against
                    the fetched text
    HUMAN_VERIFIED  a person confirmed it, and says who

`Entry.owner_facing` is the one gate every caller uses before it renders a statement.

NO MODEL ANYWHERE IN THIS FILE. Choosing a country decides which law a finding is read
against, which is a consequential decision, so it is pattern matching over the documents'
own words with weights a reviewer can read below — and the reasons both ways travel with the
answer. Imports are deliberately narrow: this module must never import the AI layer, the
rules engine or `detect` (which imports it).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, \
    model_validator

log = logging.getLogger("proven.jurisdiction")

PACK_DIR = Path(__file__).resolve().parent / "jurisdictions"
SIGNALS_FILE = PACK_DIR / "signals.yaml"

GENERIC = "GENERIC"

CANDIDATE = "CANDIDATE"
SOURCE_QUOTED = "SOURCE_QUOTED"
HUMAN_VERIFIED = "HUMAN_VERIFIED"
#: Lowest to highest. Only the upper two may become a sentence an owner reads.
STATUS_LADDER: tuple[str, ...] = (CANDIDATE, SOURCE_QUOTED, HUMAN_VERIFIED)
OWNER_FACING_STATUSES: frozenset[str] = frozenset({SOURCE_QUOTED, HUMAN_VERIFIED})

AUTHOR_CLASSES: tuple[str, ...] = ("INSURER", "AUTHORITY", "THIRD_PARTY", "SELF")

_CODE = re.compile(r"^(?:[A-Z]{2}|GENERIC)$")


class PackError(ValueError):
    """A pack is missing, malformed, or contradicts another pack. Always loud."""


# ── the models ──────────────────────────────────────────────────────────────

def _one_line(text: Optional[str]) -> Optional[str]:
    """YAML folded scalars end in a newline and wrap mid-sentence; a statement does not."""
    return None if text is None else " ".join(str(text).split())


class Verification(BaseModel):
    """Who says so, and how we know. Every field is REQUIRED to be present, even when null,
    so an entry cannot silently omit the part of the block it could not fill in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["CANDIDATE", "SOURCE_QUOTED", "HUMAN_VERIFIED"]
    source_url: Optional[str]
    retrieved_on: Optional[date]
    quote: Optional[str]
    verified_by: Optional[str]
    note: Optional[str] = None

    @field_validator("quote", "verified_by", "note", mode="before")
    @classmethod
    def _flatten(cls, v):
        return _one_line(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _ladder(self) -> "Verification":
        if self.source_url is not None and not self.source_url.startswith("https://"):
            raise ValueError(f"source_url must be an https:// address, not "
                             f"{self.source_url!r}")
        if self.quote is not None and not self.quote.strip():
            raise ValueError("quote is empty; use null when there is no quote")
        if self.status == SOURCE_QUOTED:
            missing = [name for name in ("quote", "source_url", "retrieved_on")
                       if getattr(self, name) in (None, "")]
            if missing:
                raise ValueError(
                    "SOURCE_QUOTED needs a verbatim quote from the primary source, its URL "
                    "and the date it was retrieved; missing: " + ", ".join(missing))
        if self.status == HUMAN_VERIFIED and not (self.verified_by or "").strip():
            raise ValueError("HUMAN_VERIFIED needs verified_by: who confirmed it")
        return self

    @property
    def owner_facing(self) -> bool:
        return self.status in OWNER_FACING_STATUSES


class Entry(BaseModel):
    """One statement about a country's law, or one figure the engine uses, with its source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^(?:[A-Z]{2}|GENERIC)(?:-[A-Z0-9]+)+$")
    title: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    values: dict[str, Any] = Field(default_factory=dict)
    verification: Verification

    @field_validator("statement", "title", mode="before")
    @classmethod
    def _flatten(cls, v):
        return _one_line(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _no_invented_time_limit(self) -> "Entry":
        # A time limit is exactly the kind of figure that gets remembered rather than
        # sourced. One may only be recorded on an entry a reader is allowed to see.
        if self.values.get("time_limit_days") is not None \
                and not self.verification.owner_facing:
            raise ValueError(f"{self.id}: a time limit may only be recorded with a "
                             f"SOURCE_QUOTED or HUMAN_VERIFIED basis")
        return self

    @property
    def owner_facing(self) -> bool:
        return self.verification.owner_facing

    def provenance(self) -> dict:
        """What a screen must show next to any sentence built from this entry: the quote,
        the source link and the status badge."""
        v = self.verification
        return {
            "entry_id": self.id, "title": self.title, "statement": self.statement,
            "status": v.status, "owner_facing": v.owner_facing,
            "source_url": v.source_url,
            "retrieved_on": v.retrieved_on.isoformat() if v.retrieved_on else None,
            "quote": v.quote, "verified_by": v.verified_by,
        }


class DocumentKind(BaseModel):
    """Who authors a kind of document in this country. Decides the evidence class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(pattern=r"^[A-Z][A-Z_]*$")
    author_class: Literal["INSURER", "AUTHORITY", "THIRD_PARTY", "SELF"]
    author: Optional[str] = None
    absent_note: Optional[str] = None
    verification: Verification


class Currency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[A-Z]{3}$")
    symbol: str = Field(min_length=1)
    local_symbol: Optional[str] = None
    minor_units: int = Field(ge=0, le=4)
    group_separator: str
    decimal_separator: str
    # Three-digit grouping only, for now. Indian lakh/crore grouping arrives with the India
    # pack and its own tests; a pack asking for it before then is refused, not approximated.
    grouping: Literal["THREE"]

    def format(self, amount: Any, *, places: int = 0, local: bool = False) -> str:
        """`S$60,000`. Decimal throughout — an amount is never passed through a float."""
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        body = format(value, f",.{int(places)}f")
        if (self.group_separator, self.decimal_separator) != (",", "."):
            body = body.translate(str.maketrans(
                {",": self.group_separator, ".": self.decimal_separator}))
        symbol = (self.local_symbol or self.symbol) if local else self.symbol
        return f"{symbol}{body}"

    def millions(self, amount: Any) -> str:
        """`S$1 million`, `S$10 million`, `S$1.5 million` — the way a statute's ceiling is
        usually written in a sentence. Exact: the division is Decimal, never float."""
        value = (amount if isinstance(amount, Decimal) else Decimal(str(amount))) \
            / Decimal(1_000_000)
        text = format(value.normalize(), "f")
        return f"{self.symbol}{text} million"


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def long_date(value: Any) -> str:
    """`1 October 2022`. Month names are spelled out here rather than taken from
    `strftime("%B")`, which follows the server's locale and would put a different
    language into an English sentence on a machine configured for one."""
    d = value if isinstance(value, date) else date.fromisoformat(str(value))
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


class HolidayCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["holidays"]
    country: str = Field(pattern=r"^[A-Z]{2}$")
    wired: bool = False
    note: Optional[str] = None


class AdviceBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The product's own rule. Not a statement about any law, so it needs no source.
    product_boundary: str
    #: The law behind the boundary, where a country has one. Verified like any entry.
    regulatory_basis: Optional[Entry] = None

    @field_validator("product_boundary", mode="before")
    @classmethod
    def _flatten(cls, v):
        return _one_line(v) if isinstance(v, str) else v


#: The kinds of signal, strongest first, and what each is worth. The ORDER is the design
#: (PLAN: governing law, then regulator and statute names, currency, registration numbers,
#: postcodes, phone numbers); the numbers make it arithmetic. A governing-law clause alone
#: clears the bar. Everything below it has to agree with something else to.
SIGNAL_WEIGHTS: tuple[tuple[str, int, str], ...] = (
    ("governing_law", 10, "governing-law clause"),
    ("names", 5, "regulator or statute named"),
    ("currency", 3, "currency"),
    ("registration_numbers", 3, "registration number"),
    ("postcodes", 2, "postcode"),
    ("phone_numbers", 1, "phone number"),
)
_WEIGHT = {k: w for k, w, _ in SIGNAL_WEIGHTS}
_LABEL = {k: label for k, _, label in SIGNAL_WEIGHTS}

#: The least a country must score to be chosen, and how far ahead of the next it must be.
#: Below either, the answer is GENERIC — with the reasons both ways — and never a guess.
MIN_SCORE = 5
MARGIN = 5


class Signals(BaseModel):
    """How documents from one country are recognised. Patterns only; no law."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    governing_law: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    currency_codes: list[str] = Field(default_factory=list)
    currency_symbols: list[str] = Field(default_factory=list)
    registration_numbers: list[str] = Field(default_factory=list)
    postcodes: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)

    @field_validator("governing_law", "registration_numbers", "postcodes", "phone_numbers")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for p in patterns:
            try:
                re.compile(p)
            except re.error as exc:
                raise ValueError(f"pattern {p!r} does not compile: {exc}") from exc
        return patterns

    def compiled(self) -> dict[str, tuple[re.Pattern[str], ...]]:
        return _compile_signals(self)


class Pack(BaseModel):
    """One country's behaviour. See the module docstring and `jurisdictions/SG.yaml`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=_CODE.pattern)
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    summary: str
    locales: list[str] = Field(min_length=1)
    timezone: str = Field(pattern=r"^(?:UTC|[A-Za-z]+(?:/[A-Za-z_]+)+)$")
    holiday_calendar: Optional[HolidayCalendar]
    currency: Optional[Currency]
    regulators: list[Entry]
    statutes: list[Entry]
    compulsory_covers: list[Entry]
    regulatory_timelines: list[Entry]
    complaint_ladder: list[Entry]
    market_conventions: list[Entry]
    document_kinds: list[DocumentKind] = Field(min_length=1)
    data_protection: Optional[Entry]
    advice_boundary: AdviceBoundary
    detection: Optional[Signals]

    @field_validator("summary", mode="before")
    @classmethod
    def _flatten(cls, v):
        return _one_line(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _consistent(self) -> "Pack":
        entries = self.entries()
        ids = [e.id for e in entries]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate entry ids: {', '.join(dupes)}")
        wrong = [i for i in ids if not i.startswith(self.code + "-")]
        if wrong:
            raise ValueError(f"entry ids must start with '{self.code}-': {', '.join(wrong)}")
        kinds = [k.kind for k in self.document_kinds]
        kdupes = sorted({k for k in kinds if kinds.count(k) > 1})
        if kdupes:
            raise ValueError(f"document kinds listed twice: {', '.join(kdupes)}")
        unknown = next((k for k in self.document_kinds if k.kind == "UNKNOWN"), None)
        if unknown is None or unknown.author_class != "SELF":
            raise ValueError("the UNKNOWN document kind must be listed and must be SELF: "
                             "a document nobody could identify may only ever weaken a "
                             "finding")
        if self.code == GENERIC:
            stated = [e.id for e in entries]
            if stated or self.detection is not None or self.currency is not None:
                raise ValueError("GENERIC makes no country-specific statement: it may hold "
                                 "no entries, no detection signals and no currency")
        else:
            if self.detection is None or not self.detection.governing_law:
                raise ValueError(f"{self.code}: a country pack needs detection signals, "
                                 f"including at least one governing-law pattern")
        orders = [e.values.get("order") for e in self.complaint_ladder]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("complaint_ladder steps must carry order 1, 2, 3 ... in "
                             "the sequence they are listed")
        return self

    # ── reading a pack ──────────────────────────────────────────────────────

    @property
    def is_generic(self) -> bool:
        return self.code == GENERIC

    def entries(self) -> list[Entry]:
        """Every verified entry in the pack, in file order."""
        out: list[Entry] = []
        for section in (self.regulators, self.statutes, self.compulsory_covers,
                        self.regulatory_timelines, self.complaint_ladder,
                        self.market_conventions):
            out.extend(section)
        if self.data_protection is not None:
            out.append(self.data_protection)
        if self.advice_boundary.regulatory_basis is not None:
            out.append(self.advice_boundary.regulatory_basis)
        return out

    def entry(self, entry_id: str) -> Entry:
        for e in self.entries():
            if e.id == entry_id:
                return e
        raise PackError(f"pack {self.code} has no entry {entry_id!r}")

    def find(self, local_id: str) -> Optional[Entry]:
        """The entry `<CODE>-<local_id>`, or None. Lets country-agnostic code ask any pack
        for, say, its `COMP-WORK-INJURY` without naming a country."""
        target = f"{self.code}-{local_id}"
        return next((e for e in self.entries() if e.id == target), None)

    def statement(self, local_id: str) -> Optional[Entry]:
        """The entry, ONLY if a reader may be shown a sentence built from it."""
        e = self.find(local_id)
        return e if e is not None and e.owner_facing else None

    def value(self, ref: str) -> Any:
        """`"LOCAL-ID:key"` → the value. Raises when the entry or key is absent, because a
        figure that silently became None is how a default gets substituted for evidence."""
        local_id, _, key = ref.partition(":")
        e = self.find(local_id)
        if e is None or not key or key not in e.values:
            raise PackError(f"pack {self.code} has no value {ref!r}")
        return e.values[key]

    def kind(self, kind: str) -> Optional[DocumentKind]:
        return next((k for k in self.document_kinds if k.kind == kind), None)

    def lists(self, *kinds: str) -> bool:
        """True when this pack defines every one of these document kinds. Screens use it
        before naming a document to ask for: a CPF record is worth asking a Singapore
        business for and means nothing to anyone else."""
        return all(self.kind(k) is not None for k in kinds)

    def author_class_for(self, kind: str) -> str:
        """Unlisted kinds are SELF, the weakest class: a document this pack does not know
        can only ever weaken a finding."""
        k = self.kind(kind)
        return k.author_class if k is not None else "SELF"

    def absent_notes(self, present_kinds: Iterable[str]) -> list[str]:
        """The "not searched" lines for the documents this bundle lacks, in pack order."""
        present = set(present_kinds)
        return [k.absent_note for k in self.document_kinds
                if k.absent_note and k.kind not in present]

    def summary_dict(self) -> dict:
        counts = {s: 0 for s in STATUS_LADDER}
        for e in self.entries():
            counts[e.verification.status] += 1
        return {"code": self.code, "name": self.name, "version": self.version,
                "summary": self.summary, "currency": (self.currency.code
                                                      if self.currency else None),
                "timezone": self.timezone, "entries": len(self.entries()),
                "entries_by_status": counts,
                "owner_facing_entries": sum(1 for e in self.entries() if e.owner_facing)}

    def public_dict(self) -> dict:
        """The whole pack for the evidence view, every entry with its provenance. CANDIDATE
        entries are included — this is the internal view rule 17 allows — and each carries
        `owner_facing: false` so no screen can mistake it for a statement."""
        data = self.model_dump(mode="json")
        for section in ("regulators", "statutes", "compulsory_covers",
                        "regulatory_timelines", "complaint_ladder", "market_conventions"):
            for item in data[section]:
                item["owner_facing"] = item["verification"]["status"] in \
                    OWNER_FACING_STATUSES
        for item in (data.get("data_protection"),
                     (data.get("advice_boundary") or {}).get("regulatory_basis")):
            if item:
                item["owner_facing"] = item["verification"]["status"] in \
                    OWNER_FACING_STATUSES
        return data


class Country(BaseModel):
    """A country VERIS can recognise but holds no pack for (see signals.yaml)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[A-Z]{2}$")
    name: str
    signals: Signals


class _SignalsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    countries: list[Country]


# ── loading ─────────────────────────────────────────────────────────────────

def validate_pack(data: Any, *, source: str = "<pack>") -> Pack:
    """Validate one pack's parsed YAML. Raises `PackError` naming every problem."""
    if not isinstance(data, dict):
        raise PackError(f"{source}: a pack is a mapping, not {type(data).__name__}")
    try:
        return Pack.model_validate(data)
    except ValidationError as exc:
        raise PackError(f"{source}: {exc}") from exc


def _read(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PackError(f"{path.name}: could not be read: {exc}") from exc


def check_against_generic(p: Pack, generic: Pack) -> None:
    """A country pack may ADD document kinds; it may not re-grade a shared one. Raising the
    author class of, say, ACCOUNTS would let a self-written spreadsheet grade PROVEN in one
    country — exactly the manufacture of evidence author_class exists to stop."""
    for shared in generic.document_kinds:
        mine = p.kind(shared.kind)
        if mine is None:
            raise PackError(f"{p.code}: must list the shared document kind {shared.kind} "
                            f"({shared.author_class})")
        if mine.author_class != shared.author_class:
            raise PackError(f"{p.code}: re-grades the shared document kind {shared.kind} "
                            f"from {shared.author_class} to {mine.author_class}")


@lru_cache(maxsize=1)
def _packs() -> dict[str, Pack]:
    packs: dict[str, Pack] = {}
    for path in sorted(PACK_DIR.glob("*.yaml")):
        if path == SIGNALS_FILE:
            continue
        p = validate_pack(_read(path), source=path.name)
        if p.code != path.stem:
            raise PackError(f"{path.name}: declares code {p.code!r}; the file must be "
                            f"named after its code")
        packs[p.code] = p
    if GENERIC not in packs:
        raise PackError("the GENERIC pack is missing; there must always be somewhere "
                        "honest to fall back to")
    for p in packs.values():
        if not p.is_generic:
            check_against_generic(p, packs[GENERIC])
    return packs


@lru_cache(maxsize=1)
def _unpacked() -> dict[str, Country]:
    try:
        parsed = _SignalsFile.model_validate(_read(SIGNALS_FILE))
    except ValidationError as exc:
        raise PackError(f"{SIGNALS_FILE.name}: {exc}") from exc
    out: dict[str, Country] = {}
    for c in parsed.countries:
        if c.code in _packs():
            raise PackError(f"{SIGNALS_FILE.name}: {c.code} has a pack; its signals belong "
                            f"in the pack's own detection block, not in both places")
        if c.code in out:
            raise PackError(f"{SIGNALS_FILE.name}: {c.code} is listed twice")
        out[c.code] = c
    return out


def reload() -> None:
    """Drop the cached packs. Tests that write a pack to a temporary directory use it."""
    _packs.cache_clear()
    _unpacked.cache_clear()


def pack_codes() -> tuple[str, ...]:
    return tuple(sorted(_packs()))


def has_pack(code: Optional[str]) -> bool:
    return bool(code) and code in _packs()


def pack(code: str) -> Pack:
    """The pack for `code`. The code is checked against the loaded set, never joined onto a
    path, so a request cannot name a file."""
    if not isinstance(code, str) or not _CODE.match(code) or code not in _packs():
        raise PackError(f"no jurisdiction pack for {code!r}")
    return _packs()[code]


def generic() -> Pack:
    return _packs()[GENERIC]


def recognised_without_pack() -> list[dict]:
    return [{"code": c.code, "name": c.name} for c in _unpacked().values()]


def country_name(code: Optional[str]) -> str:
    if code and code in _packs():
        return _packs()[code].name
    if code and code in _unpacked():
        return _unpacked()[code].name
    return code or "an unidentified country"


def pack_of(case: Any) -> Pack:
    """The pack a case was read under, from the pipeline's `jurisdiction` record.

    A case with no record is read under GENERIC. Every case the pipeline builds carries one,
    so this only meets hand-built states — and for those, assuming a country would be the
    exact defect this module exists to remove.
    """
    view = case.get("jurisdiction") if isinstance(case, dict) else None
    code = view.get("pack") if isinstance(view, dict) else None
    return _packs()[code] if has_pack(code) else generic()


def author_class_for(kind: str, p: Optional[Pack] = None) -> str:
    """Who authors `kind`. With a pack, that pack's answer. Without one, use SELF unless
    every available pack lists the kind with the same class.

    An upload picker has no case and therefore no selected jurisdiction. A kind such as a
    CPF record is authority-authored under Singapore but unlisted — and consequently SELF —
    under GENERIC. Returning its strongest possible class before the bundle establishes a
    country would manufacture stronger evidence than the documents support.
    """
    if p is not None:
        return p.author_class_for(kind)
    answers: set[str] = set()
    for candidate in _packs().values():
        listed = candidate.kind(kind)
        if listed is None:
            return "SELF"
        answers.add(listed.author_class)
    return answers.pop() if len(answers) == 1 else "SELF"


def currency(code: str) -> Optional[Currency]:
    """The currency format of whichever pack writes `code` (e.g. SGD → the SG pack)."""
    for p in _packs().values():
        if p.currency is not None and p.currency.code == code:
            return p.currency
    return None


def symbol_for(code: str) -> str:
    """`S$` for SGD. A currency no pack formats is written as its ISO code and a space,
    which is unambiguous, rather than borrowing another currency's symbol."""
    c = currency(code)
    return c.symbol if c is not None else f"{code} "


def format_money(amount: Any, code: str, *, places: int = 0, local: bool = False) -> str:
    c = currency(code)
    if c is None:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        return f"{code} {format(value, f',.{int(places)}f')}"
    return c.format(amount, places=places, local=local)


# ── detection ───────────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def _rx(pattern: str, flags: int) -> re.Pattern[str]:
    return re.compile(pattern, flags)


def _name_pattern(name: str) -> re.Pattern[str]:
    # An acronym is matched as written, so a regulator's three capitals are not found
    # inside an ordinary lower-case word; a full name is matched in any case, because
    # document headers are often set in capitals.
    acronym = name.isupper() and len(name) <= 6
    return _rx(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])",
               0 if acronym else re.IGNORECASE)


# A profiled country owns the patterns that recognise its governing-law clause. This
# broader form exists only to notice an explicit clause we do *not* recognise. Such a
# clause is contrary evidence: the safe response is GENERIC, never a local pack selected
# by an incidental regulator name or currency. Generic boilerplate such as "subject to
# applicable law" names no jurisdiction, so it is not contrary evidence.
#
# Country references are deliberately case-insensitive: a policy may write `thai law` in
# running text. The generic-reference prefix is equally deliberate. It excludes phrases
# such as `laws of the jurisdiction in which it is issued` without making uppercase the
# proxy for a concrete country name.
_GENERIC_GOVERNING_LAW_REFERENCE = (
    r"(?:(?:the|any|all|this|such)\s+)?"
    r"(?:applicable|governing|relevant|local|prevailing|"
    r"jurisdiction|country|state|place|territory|land|law|laws)\b"
)
_CONCRETE_JURISDICTION_REFERENCE = (
    rf"(?!{_GENERIC_GOVERNING_LAW_REFERENCE})"
    r"(?:the\s+)?[A-Za-z][A-Za-z'\-]*"
    r"(?:\s+(?:[A-Za-z][A-Za-z'\-]*|of|the)){0,5}"
)
_ANY_GOVERNING_LAW = re.compile(
    rf"\b(?:governed by|construed in accordance with|interpreted in accordance with|"
    rf"determined in accordance with|subject to)\b[^.;]{{0,80}}?\b(?:"
    rf"(?:the\s+)?laws?\s+of\s+{_CONCRETE_JURISDICTION_REFERENCE}|"
    rf"{_CONCRETE_JURISDICTION_REFERENCE}\s+[Ll]aw)\b",
    re.IGNORECASE,
)


def _compile_signals(s: Signals) -> dict[str, tuple[re.Pattern[str], ...]]:
    return {
        "governing_law": tuple(_rx(p, re.IGNORECASE) for p in s.governing_law),
        "names": tuple(_name_pattern(n) for n in s.names),
        "currency": tuple(
            [_rx(r"(?<![A-Za-z])" + re.escape(c) + r"(?![A-Za-z])", 0)
             for c in s.currency_codes]
            + [_rx(r"(?<![A-Za-z])" + re.escape(sym) + r"\s?\d", 0)
               for sym in s.currency_symbols]),
        "registration_numbers": tuple(_rx(p, 0) for p in s.registration_numbers),
        "postcodes": tuple(_rx(p, 0) for p in s.postcodes),
        "phone_numbers": tuple(_rx(p, 0) for p in s.phone_numbers),
    }


def _profiles() -> list[tuple[str, str, Signals]]:
    out = [(p.code, p.name, p.detection) for p in _packs().values()
           if p.detection is not None]
    out += [(c.code, c.name, c.signals) for c in _unpacked().values()]
    return sorted(out)


@dataclass(frozen=True)
class SignalHit:
    country: str
    signal: str
    matched: str
    document: str
    page: int

    @property
    def weight(self) -> int:
        return _WEIGHT[self.signal]

    def as_dict(self, direction: str) -> dict:
        return {"direction": direction, "country": self.country,
                "country_name": country_name(self.country), "signal": self.signal,
                "signal_label": _LABEL[self.signal], "weight": self.weight,
                "matched": self.matched, "document": self.document, "page": self.page}

    def sentence(self) -> str:
        return (f'{_LABEL[self.signal]} "{self.matched}" ({self.document}, page '
                f'{self.page})')


@dataclass
class JurisdictionDetection:
    """`code` is the country the documents establish, or GENERIC. It is NOT the pack: a
    Malaysian wording is detected `MY` and then read under GENERIC because no MY pack
    exists — see `applicable()`.

    Unpacks as `(code, confidence, reasons)`, the shape the plan names."""

    code: str
    confidence: str                       # HIGH | MEDIUM | LOW | NONE
    reasons: list[dict] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)
    leading: Optional[str] = None         # the strongest country, even when not chosen
    why: str = ""

    def __iter__(self):
        return iter((self.code, self.confidence, self.reasons))

    @property
    def strongest_reason(self) -> Optional[dict]:
        own = [r for r in self.reasons
               if r.get("direction") == "for" and r.get("signal")]
        return max(own, key=lambda r: r["weight"]) if own else None


def _as_documents(documents: Iterable[Any]) -> list[tuple[str, list[str]]]:
    """Accept `(label, pages)` pairs, or anything with `.pages` (a LoadedDoc)."""
    out: list[tuple[str, list[str]]] = []
    for i, d in enumerate(documents, start=1):
        if isinstance(d, tuple) and len(d) == 2:
            label, pages = d
        else:
            pages = getattr(d, "pages", None) or []
            label = getattr(d, "filename", None) or getattr(d, "kind", None) \
                or f"document {i}"
        out.append((str(label), [str(p or "") for p in pages]))
    return out


def _excerpt(match: re.Match[str], kind: str) -> str:
    """The evidence for one signal, as the reasons will show it.

    A phone number is personal data and its country code is the whole of the evidence, so
    only the code is kept. Everything else is shown as matched, because the reader has to
    be able to find it in their own document.
    """
    text = " ".join(match.group(0).split())
    if kind == "phone_numbers":
        code = re.match(r"\+\d{1,3}", text)
        return (code.group(0) if code else "+") + " …"
    return text if len(text) <= 160 else text[:157] + "..."


def detect_jurisdiction(documents: Iterable[Any]) -> JurisdictionDetection:
    """Which country's documents these are, how sure, and why — both ways.

    Each KIND of signal counts once per country however often it repeats: a schedule that
    prints "SGD" forty times is one currency signal, not forty, so volume cannot outvote a
    governing-law clause. The first occurrence is kept as the evidence, with its document
    and page, the same way a finding cites its span.

    A country pack is selected only when its own governing-law clause appears, or when two
    independent signal kinds agree. An explicit clause for an unrecognised country, or
    conflicting recognised governing-law clauses, forces GENERIC: incidental names and
    currency cannot override a contract's stated law.
    """
    docs = _as_documents(documents)
    profiles = [(code, name, sig.compiled()) for code, name, sig in _profiles()]
    hits: dict[str, dict[str, SignalHit]] = {code: {} for code, _, _ in profiles}
    governing_patterns = tuple(
        rx for _code, _name, compiled in profiles for rx in compiled["governing_law"])
    unrecognised_governing_laws: list[dict] = []

    for label, pages in docs:
        for page_no, raw in enumerate(pages, start=1):
            text = " ".join(raw.split())
            if not text:
                continue
            for m in _ANY_GOVERNING_LAW.finditer(text):
                clause = m.group(0)
                if any(rx.search(clause) for rx in governing_patterns):
                    continue
                unrecognised_governing_laws.append({
                    "direction": "against",
                    "country": None,
                    "country_name": None,
                    "signal": "unrecognised_governing_law",
                    "signal_label": "unrecognised governing-law clause",
                    "weight": _WEIGHT["governing_law"],
                    "matched": _excerpt(m, "governing_law"),
                    "document": label,
                    "page": page_no,
                })
            for code, _name, compiled in profiles:
                found = hits[code]
                for kind, patterns in compiled.items():
                    if kind in found:
                        continue
                    for rx in patterns:
                        m = rx.search(text)
                        if m:
                            found[kind] = SignalHit(code, kind, _excerpt(m, kind), label,
                                                    page_no)
                            break

    scores = {code: sum(h.weight for h in found.values())
              for code, found in hits.items() if found}
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    leading = ranked[0][0] if ranked else None
    top = ranked[0][1] if ranked else 0
    second = ranked[1][1] if len(ranked) > 1 else 0
    governing_countries = [code for code, found in hits.items()
                           if "governing_law" in found]

    if unrecognised_governing_laws:
        code, confidence = GENERIC, "LOW"
        why = ("An explicit governing-law clause names a country VERIS does not "
               "recognise, so it will not apply another country's jurisdiction pack.")
    elif len(governing_countries) > 1:
        code, confidence = GENERIC, "LOW"
        names = ", ".join(country_name(country) for country in governing_countries)
        why = ("The documents contain conflicting governing-law clauses for "
               f"{names}, so VERIS will not choose between them.")
    elif top == 0:
        code, confidence = GENERIC, "NONE"
        why = ("No governing-law clause, regulator, statute, currency, registration "
               "number, postcode or phone number of a country VERIS recognises was found "
               "in any document.")
    elif top < MIN_SCORE:
        code, confidence = GENERIC, "LOW"
        why = (f"The only signals point to {country_name(leading)}, and they are too weak "
               f"to read the documents against that country's law (score {top}, "
               f"{MIN_SCORE} needed).")
    elif "governing_law" not in hits[leading] and len(hits[leading]) < 2:
        code, confidence = GENERIC, "LOW"
        why = (f"The signals point to {country_name(leading)}, but they do not include "
               "a governing-law clause or two independent signal kinds, so VERIS will "
               "not apply that country's jurisdiction pack.")
    elif top - second < MARGIN:
        code, confidence = GENERIC, "LOW"
        runner = ranked[1][0]
        why = (f"The documents point to {country_name(leading)} (score {top}) and to "
               f"{country_name(runner)} (score {second}) too evenly to choose between "
               "them.")
    else:
        code = leading
        confidence = "HIGH" if "governing_law" in hits[leading] else "MEDIUM"
        why = ""

    # Both ways, relative to the strongest country whether or not it was chosen: what points
    # to it ("for"), and what points elsewhere or is missing ("against"). A GENERIC answer
    # therefore still shows what nearly decided it, which is what a reader needs to supply
    # the document that would settle it.
    subject = code if code != GENERIC else leading
    reasons: list[dict] = []
    for kind, _w, _label in SIGNAL_WEIGHTS:
        if subject and kind in hits.get(subject, {}):
            reasons.append(hits[subject][kind].as_dict("for"))
    for other, found in hits.items():
        if other == subject:
            continue
        for kind, _w, _label in SIGNAL_WEIGHTS:
            if kind in found:
                reasons.append(found[kind].as_dict("against"))
    reasons.extend(unrecognised_governing_laws)
    if code != GENERIC and "governing_law" not in hits[code]:
        reasons.append({"direction": "against",
                        "statement": "No governing-law clause was found in any document; "
                                     "the country is read from the other signals."})
    if why:
        reasons.append({"direction": "against", "statement": why})

    result = JurisdictionDetection(code=code, confidence=confidence, reasons=reasons,
                                   scores=dict(ranked), leading=leading, why=why)
    # Logged both ways, as the plan requires — but only as country, signal kind and page.
    # Upload filenames and matched text stay in the case record, never the server log.
    log.info("jurisdiction detected: %s (%s) scores=%s for=%s against=%s", code,
             confidence, result.scores,
             [(r.get("country"), r.get("signal"), r.get("page"))
              for r in reasons if r.get("direction") == "for" and r.get("signal")],
             [(r.get("country"), r.get("signal"), r.get("page"))
              if r.get("signal") else "no governing-law clause or margin"
              for r in reasons if r.get("direction") == "against"])
    return result


# ── which pack applies, said out loud ───────────────────────────────────────

@dataclass
class AppliedJurisdiction:
    """The detection, the pack actually applied, and the sentence that explains the gap
    between them when there is one."""

    detection: JurisdictionDetection
    pack: Pack
    notice: Optional[str]

    @property
    def pack_available(self) -> bool:
        """Whether a country-specific pack was established for this case.

        GENERIC is always available as a safety fallback, but it is deliberately not an
        available jurisdiction pack: it makes no country-specific statement and its notice
        must remain visible to the owner.
        """
        return not self.pack.is_generic and self.pack.code == self.detection.code

    def summary(self) -> str:
        d = self.detection
        best = d.strongest_reason
        if self.pack_available:
            because = f": {SignalHit(**_hit_fields(best)).sentence()}" if best else ""
            return (f"Read as {self.pack.name} ({d.confidence.lower()} confidence)"
                    f"{because}.")
        return self.notice or ""

    def as_dict(self, rules_not_applied: Iterable[dict] = (),
                sentences_dropped: Iterable[dict] = ()) -> dict:
        d = self.detection
        return {
            "pack": self.pack.code,
            "pack_name": self.pack.name,
            "pack_version": self.pack.version,
            "pack_available": self.pack_available,
            "detected": d.code,
            "detected_name": country_name(d.code) if d.code != GENERIC else None,
            "leading": d.leading,
            "confidence": d.confidence,
            "summary": self.summary(),
            "notice": self.notice,
            "reasons": d.reasons,
            "scores": d.scores,
            "rules_not_applied": list(rules_not_applied),
            "sentences_dropped": list(sentences_dropped),
            "method": "DETERMINISTIC",
        }


def _hit_fields(reason: dict) -> dict:
    return {"country": reason["country"], "signal": reason["signal"],
            "matched": reason["matched"], "document": reason["document"],
            "page": reason["page"]}


def applicable(detection: JurisdictionDetection) -> AppliedJurisdiction:
    """The pack for a detection: the country's own when it exists, otherwise GENERIC with a
    notice that names what is missing. Never another country's."""
    if detection.code != GENERIC and has_pack(detection.code):
        return AppliedJurisdiction(detection, pack(detection.code), None)
    if detection.code != GENERIC:
        name = country_name(detection.code)
        best = detection.strongest_reason
        because = f" ({SignalHit(**_hit_fields(best)).sentence()})" if best else ""
        notice = (f"These documents read as {name}{because}. VERIS has no {name} "
                  f"jurisdiction pack yet, so it reads and quotes your documents but makes "
                  f"no statement about the law of {name}, and it does not read them "
                  f"against any other country's law.")
    else:
        notice = (f"VERIS could not tell which country's law governs these documents. "
                  f"{detection.why} It reads and quotes your documents but makes no "
                  f"statement about any country's law.")
    return AppliedJurisdiction(detection, generic(), notice)


def assess(documents: Iterable[Any]) -> AppliedJurisdiction:
    return applicable(detect_jurisdiction(documents))
