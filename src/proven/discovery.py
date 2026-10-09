"""VERIS DISCOVERY — the agent that goes and looks.

WHERE THE OTHER TWO AGENTS STOP, and why this one exists.

  Connect (connectors.py)   reads systems the SME already uses — accounting, ERP,
                            payroll. It knows what the business TOLD the software.
  Sentinel (sentinel.py)    reads the environment — weather, cost indices, flood
                            registries. It knows what is changing AROUND the business.

Neither one DISCOVERS a business change from the outside. If a hotel opens a second
branch and updates its website but not its policy, Connect does not know (no branch
in the accounting system yet), and Sentinel does not know (the environment has not
changed). The policy goes stale while the available evidence of the change sits in a
public registry.

That is what this agent does. It fetches public information about a specific business
and compares it against what the policy says. When the two disagree, it raises a
signal — and it is the signal that could not have come from anywhere else in the system.

THE EVIDENCE HIERARCHY, enforced at construction:

  ACRA BizFile, MOM work pass registry     → AUTHORITY    (can raise VERIFIED change)
  Company's own public website             → SELF         (hint only: POTENTIAL)
  Third-party directory (Google Maps etc.) → THIRD_PARTY  (needs corroboration)

WHY NOT JUST SCRAPE EVERYTHING. Three reasons:
  1. Evidence class. A scraped Instagram post is SELF-authored at best. Only an
     authority-published filing can move a finding.
  2. Consent. The SME is our customer and consents to monitoring their own public
     footprint. Scraping somebody else's requires a different story.
  3. Rate/ToS risk. A demo that trips a Google captcha is worse than one that uses a
     fixture and says so.

So the agent knows three source types, labels each honestly, and uses fixtures where
a live API is not available — with `mode: DEMO_FIXTURE | LIVE` travelling on every
signal, rendered on screen.

THE GROUNDING GATE. An LLM may extract candidate facts from a fetched page. Every
candidate must then be RE-LOCATED verbatim in the fetched bytes, or it is rejected as
hallucinated. This is the same gate as ai.py's propose→verify→bind — the model reads,
deterministic code decides.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Optional

from . import config

# ── claim scope: what the Discovery Agent may assert ─────────────────────────
#
# Discovery sits between the other two: it IS about this specific business (unlike
# Sentinel), but it comes from a PUBLIC source, not an internal system (unlike Connect).
# So it has a third ceiling:

DISCOVERY_VERIFIED = "DISCOVERY_VERIFIED"      # authority source, re-located in text
DISCOVERY_HINT = "DISCOVERY_HINT"              # non-authority source, or ungrounded

# What it may NOT emit, for the same reason as Sentinel:
CONFIRMED_EVIDENCE = "CONFIRMED_EVIDENCE"      # only a document establishes this


@dataclass(frozen=True)
class DiscoverySource:
    """Provenance for a discovery source."""

    id: str
    name: str
    category: str                    # REGISTRY | WEBSITE | DIRECTORY | NEWS
    publisher: str
    author_class: str                # AUTHORITY | SELF | THIRD_PARTY
    mode: str = "DEMO_FIXTURE"       # DEMO_FIXTURE | LIVE
    url_template: str = ""           # parameterised by UEN/business name
    live_equivalent: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "publisher": self.publisher, "author_class": self.author_class,
            "mode": self.mode, "is_live": self.mode == "LIVE",
            "live_equivalent": self.live_equivalent,
        }


@dataclass(frozen=True)
class DiscoveredFact:
    """One fact discovered from a public source."""

    id: str
    source: DiscoverySource
    fact_type: str                   # LOCATION | ACTIVITY | HEADCOUNT | EQUIPMENT | NAME
    label: str
    detail: str
    value: Optional[str] = None      # the extracted value (e.g. "Unit 03-12, ...")
    previous_value: Optional[str] = None  # what the policy says
    grounded: bool = False           # re-located verbatim in the fetched text
    ground_quote: str = ""           # the verbatim bytes it was found in
    discovered_on: str = ""          # ISO date
    insured_interests: tuple[str, ...] = ()
    claim_scope: str = DISCOVERY_HINT
    severity: str = "MODERATE"
    relevant_rules: tuple[str, ...] = ()
    # HOW this fact was pulled out of the page. "PATTERN" = a hand-written regex matched a
    # layout we anticipated. "MODEL" = a language model read the prose and proposed it.
    # Both then passed the SAME verbatim grounding gate, so this changes nothing about how
    # far the fact may be trusted — but it is shown on screen, because a reader deserves to
    # know which parts of the system a model touched.
    extraction: str = "PATTERN"

    def __post_init__(self) -> None:
        if self.claim_scope == CONFIRMED_EVIDENCE:
            raise ValueError(
                "Discovery may not emit CONFIRMED_EVIDENCE. Only a document can "
                "establish a fact. Discovery finds hints and verified signals.")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": "discovery",
            "source": self.source.as_dict(),
            "fact_type": self.fact_type,
            "label": self.label,
            "detail": self.detail,
            "value": self.value,
            "previous_value": self.previous_value,
            "grounded": self.grounded,
            "ground_quote": self.ground_quote,
            "discovered_on": self.discovered_on,
            "insured_interests": list(self.insured_interests),
            "claim_scope": self.claim_scope,
            "severity": self.severity,
            "relevant_rules": list(self.relevant_rules),
            "extraction": self.extraction,
            "simulated": self.source.mode != "LIVE",
            "asserts": (
                "A fact discovered from a public source about this specific business. "
                + ("Verified against an authority-published filing."
                   if self.claim_scope == DISCOVERY_VERIFIED
                   else "A hint requiring corroboration — not verified evidence.")),
        }


# ── sources ──────────────────────────────────────────────────────────────────

ACRA_SOURCE = DiscoverySource(
    id="disc-acra", name="ACRA BizFile+ (Company Registry)",
    category="REGISTRY", publisher="Accounting and Corporate Regulatory Authority",
    author_class="AUTHORITY",
    url_template="https://www.acra.gov.sg/bizfile/company-profile?uen={uen}",
    live_equivalent="ACRA BizFile+ API / data.gov.sg entities dataset",
)

MOM_SOURCE = DiscoverySource(
    id="disc-mom", name="MOM Work Pass Online",
    category="REGISTRY", publisher="Ministry of Manpower",
    author_class="AUTHORITY",
    url_template="https://www.mom.gov.sg/passes-and-permits",
    live_equivalent="MOM Work Pass API (employer access)",
)

COMPANY_WEBSITE = DiscoverySource(
    id="disc-web", name="Company's own website",
    category="WEBSITE", publisher="The insured business itself",
    author_class="SELF",
    url_template="{website_url}",
    live_equivalent="HTTP GET on the company's declared URL",
)

def as_live(source: DiscoverySource) -> DiscoverySource:
    """The same source, marked LIVE. Call ONLY after a fetch succeeded and parsed.

    MEASURED DEFECT this exists to fix. Every fact built below used the module-level source
    constant, and those constants hardcode `mode="DEMO_FIXTURE"`. Nothing ever flipped them.
    So `DiscoveredFact.as_dict()` — which derives `simulated` from `source.mode != "LIVE"` —
    reported `simulated: true` on facts that had genuinely been fetched from data.gov.sg and
    from a live website. Verified against the running demo site: the value changed when the
    site changed, and the label still said fixture.

    That is the mirror image of the sin this codebase treats as unforgivable. Rendering a
    fixture as live overstates the product; rendering live data as a fixture understates it,
    and on a demo screen it makes the one genuinely live capability look simulated. Both are
    the label failing to follow the fetch.

    `sentinel.py` already had this right — `dataclasses.replace(SOURCE, mode="LIVE")` after a
    successful call — so this is the same pattern rather than a new idea.
    """
    return replace(source, mode="LIVE")


GOOGLE_BUSINESS = DiscoverySource(
    id="disc-gmb", name="Google Business Profile",
    category="DIRECTORY", publisher="Google (third-party aggregator)",
    author_class="THIRD_PARTY",
    url_template="https://maps.google.com/?q={business_name}",
    live_equivalent="Google Places API / Google My Business",
)

SOURCES: tuple[DiscoverySource, ...] = (
    ACRA_SOURCE, MOM_SOURCE, COMPANY_WEBSITE, GOOGLE_BUSINESS)


# ── the grounding gate ───────────────────────────────────────────────────────

def ground_fact(candidate_value: str, fetched_text: str) -> tuple[bool, str]:
    """Re-locate a candidate fact verbatim in the fetched page text.

    Returns (grounded, quote). If the candidate cannot be found character-for-character
    in the source, it is rejected. This is the same discipline as ai.py's
    _verify_proposal: the model reads, deterministic code decides.
    """
    if not candidate_value or not fetched_text:
        return False, ""
    # Normalise whitespace for matching (pages have varying whitespace)
    norm_candidate = re.sub(r"\s+", " ", candidate_value.strip().lower())
    norm_text = re.sub(r"\s+", " ", fetched_text.lower())
    if norm_candidate in norm_text:
        # Find the actual substring in the original text for the quote
        start = norm_text.index(norm_candidate)
        # Return a window around it from the original (un-lowered) text
        orig_norm = re.sub(r"\s+", " ", fetched_text)
        quote = orig_norm[max(0, start - 20):start + len(norm_candidate) + 20]
        return True, quote.strip()
    return False, ""


# ── the live fetch layer ─────────────────────────────────────────────────────

_USER_AGENT = "VERIS-Discovery/1.0 (Insurance Intelligence Agent)"
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_TIMEOUT = 15

# The ACRA entities dataset on data.gov.sg. Public, no key. Split across 27 resources
# by first letter of the name; this partition is one of them. Name search works; an
# exact-UEN hit only lands if that UEN sits in this partition.
ACRA_DATASTORE_URL = (
    "https://data.gov.sg/api/action/datastore_search"
    "?resource_id=d_3f960c10fed6145404ca7b821f263b87&q={q}&limit={limit}")


def fetch_acra_by_name(entity_name: str, limit: int = 5
                       ) -> tuple[list[dict], str, str]:
    """Real ACRA lookup by entity name. Returns (records, raw_json_text, error).

    `raw_json_text` is the exact bytes the grounding gate re-locates each fact in: a
    field value that cannot be found verbatim in the fetched response is discarded, not
    trusted. Never raises.
    """
    q = (entity_name or "").strip()
    if not q:
        return [], "", "No entity name supplied for ACRA lookup."
    import urllib.parse
    url = ACRA_DATASTORE_URL.format(q=urllib.parse.quote(q), limit=int(limit))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read(512_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return [], "", f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return [], "", f"URL error: {e.reason}"
    except Exception as e:
        return [], "", f"Fetch failed: {type(e).__name__}: {e}"
    import json as _json
    try:
        payload = _json.loads(raw)
    except ValueError:
        return [], raw, "ACRA response was not valid JSON."
    records = ((payload.get("result") or {}).get("records")) or []
    return records, raw, ""


# ── the entity identity gate ─────────────────────────────────────────────────
#
# MEASURED FAILURE, and the worst one this agent has produced. The live register search for
# "Hock Lee Foods Pte Ltd" — a fictional company, so absent from ACRA — came back with
# MILLENNIUM AUTOMATION & SYSTEMS PTE. LTD. of Bencoolen Street, because `q=` is a fuzzy
# full-text search and something always ranks first. `_acra_live_facts` took `records[0]`
# as "the subject entity", and the grounding gate waved it through: the name and UEN really
# were in the fetched bytes, so `ground_fact` correctly confirmed the text was on the page.
#
# The result was DISCOVERY_VERIFIED facts asserting that this business is called Millennium
# Automation and is registered on Bencoolen Street. Built into a notification, that becomes
# an email telling an owner they have opened a branch they have never heard of.
#
# THE LESSON, which is the same one the type gate taught in a different costume: grounding
# proves a string is on the page. It says nothing about WHOSE page. An authority source is
# authoritative about the entity it describes, not about the entity you asked for.
#
# So identity is checked before extraction, against the BUSINESS — never against the search
# term. A partial query like "FOODS" may legitimately fetch many records; none of them is
# therefore this company. Failing to match produces zero facts and a plain statement that
# the register holds no entry for this business, which is the truthful answer.

_LEGAL_FORM = re.compile(
    r"\b(pte|ptd|private|limited|ltd|llp|llc|inc|incorporated|corporation|corp|sdn|"
    r"bhd)\b\.?", re.I)


def normalise_entity_name(name: str) -> str:
    """The comparable core of a company name.

    Only legal-form suffixes are stripped. Words like "Singapore", "Holdings" or "Company"
    are deliberately KEPT, however boilerplate they look: remove them and "Singapore
    Airlines" becomes "Airlines", which would match the wrong company — the exact class of
    error this function exists to stop.
    """
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9&\s]", " ", s)      # dots, commas, brackets, hyphens
    s = _LEGAL_FORM.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def record_identifies_business(
    record: dict, *, expected_name: Optional[str] = None,
    expected_uen: Optional[str] = None,
) -> tuple[bool, str]:
    """Is this register record actually the business we are asking about?

    UEN decides when both sides have one — it is the authoritative identifier, so a
    mismatch means a different company and no amount of name similarity overrides it.
    Name matching is the fallback, and it requires equality of the normalised core rather
    than overlap: "close enough" is what returned Millennium Automation.
    """
    rec_uen = str(record.get("uen") or "").strip().upper()
    rec_name = str(record.get("entity_name") or "").strip()
    want_uen = (expected_uen or "").strip().upper()
    want_name = (expected_name or "").strip()

    if not want_uen and not want_name:
        # No expectation given, so nothing can be verified. Callers inside the product
        # always pass one; this branch exists for direct unit tests of extraction.
        return True, "NOT_CHECKED"

    if want_uen and rec_uen:
        if rec_uen == want_uen:
            return True, "UEN_MATCH"
        return False, (f"The register's UEN {rec_uen} is not this business's "
                       f"{want_uen}.")

    if want_name:
        if normalise_entity_name(rec_name) == normalise_entity_name(want_name) != "":
            return True, "NAME_MATCH"
        return False, (f"The register returned \u201c{rec_name}\u201d, which is not "
                       f"\u201c{want_name}\u201d.")
    return False, "The record carries no identifier we can check."


def identify_subject_records(
    records: list[dict], *, expected_name: Optional[str] = None,
    expected_uen: Optional[str] = None,
) -> tuple[list[dict], dict]:
    """Split register results into "this is the business" and "this is somebody else"."""
    identified: list[dict] = []
    others: list[dict] = []
    basis = ""
    for rec in records or []:
        ok, why = record_identifies_business(
            rec, expected_name=expected_name, expected_uen=expected_uen)
        if ok:
            identified.append(rec)
            basis = basis or why
        else:
            others.append({
                "entity_name": str(rec.get("entity_name") or ""),
                "uen": str(rec.get("uen") or ""),
                "reason": why,
            })
    return identified, {
        "searched_name": expected_name,
        "searched_uen": expected_uen,
        "records_returned": len(records or []),
        "records_identified": len(identified),
        "identified_by": basis or None,
        "not_this_business": others[:5],
        "note": (
            "The register search is a fuzzy text match, so it returns whatever ranks "
            "highest — often another company entirely. A record may only produce facts "
            "about this business if its UEN matches, or its name matches once legal-form "
            "suffixes are removed. Everything else is listed above and used for nothing."
            if others else
            "Every record returned was confirmed to be this business."),
    }


def _acra_live_facts(records: list[dict], raw_text: str, policy_state: dict,
                     today: str) -> list["DiscoveredFact"]:
    """Turn real ACRA records into grounded facts, comparing against the policy.

    Only the top match (highest search rank) is used as the subject entity. Every
    surfaced value is re-located verbatim in the fetched JSON before it may reach
    DISCOVERY_VERIFIED; an ungrounded value drops to DISCOVERY_HINT.
    """
    out: list[DiscoveredFact] = []
    if not records:
        return out
    # These records came back from the data.gov.sg datastore, so the source is LIVE. Using
    # the module constant here is what made real registry facts report simulated: true.
    source = as_live(ACRA_SOURCE)
    rec = records[0]
    uen = str(rec.get("uen") or "").strip()
    name = str(rec.get("entity_name") or "").strip()
    status = str(rec.get("uen_status_desc") or "").strip()
    etype = str(rec.get("entity_type_desc") or "").strip()
    street = str(rec.get("reg_street_name") or "").strip()

    # 1. Registry confirmation — the entity exists in the authority register.
    if uen and name:
        g_uen, q_uen = ground_fact(uen, raw_text)
        g_name, _ = ground_fact(name, raw_text)
        grounded = g_uen and g_name
        out.append(DiscoveredFact(
            id=f"DISC-ACRA-LIVE-ENTITY-{len(out)+1}",
            source=source, fact_type="NAME",
            label="Entity located in the ACRA public register",
            detail=(f"ACRA lists {name} (UEN {uen}) as "
                    f"{status or 'a registered entity'}"
                    + (f", {etype}" if etype else "") + "."),
            value=f"{name} · {uen}" + (f" · {status}" if status else ""),
            previous_value=None,
            grounded=grounded, ground_quote=q_uen,
            discovered_on=today,
            insured_interests=("premises",),
            claim_scope=DISCOVERY_VERIFIED if grounded else DISCOVERY_HINT,
            severity="LOW",
            relevant_rules=(),
        ))

    # 2. Registered street not on the policy schedule.
    if street:
        on_policy = any(street.lower() in str(loc).lower()
                        for loc in policy_state.get("locations", []))
        if not on_policy:
            g_street, q_street = ground_fact(street, raw_text)
            out.append(DiscoveredFact(
                id=f"DISC-ACRA-LIVE-ADDR-{len(out)+1}",
                source=source, fact_type="LOCATION",
                label="ACRA-registered street not matched on the policy schedule",
                detail=(f"ACRA records the registered street as '{street}', which does "
                        f"not match any premises on the policy schedule."),
                value=street,
                previous_value=", ".join(str(x) for x in
                                         policy_state.get("locations", [])),
                grounded=g_street, ground_quote=q_street,
                discovered_on=today,
                insured_interests=("premises", "buildings"),
                claim_scope=DISCOVERY_VERIFIED if g_street else DISCOVERY_HINT,
                severity="MODERATE",
                relevant_rules=("ALTERATION-PREMISES-NOT-ON-SCHEDULE",),
            ))
    return out


# ── LLM extraction, behind the same grounding gate ───────────────────────────
#
# WHY THIS EXISTS. The regex extraction below works, and it works on exactly one page
# layout. `r"Branch \d+:\s*(.+?)"` finds a branch on our demo fixture and nothing on a real
# ACRA response, a company's About page, or next year's redesign of either. That is the
# honest limit of pattern matching against pages we do not control.
#
# A model reads arbitrary prose. That is the one thing it is genuinely better at than code,
# so it is the one job it is given here: read the fetched page and PROPOSE candidate facts.
#
# WHAT THE MODEL IS NOT TRUSTED WITH, WHICH IS EVERYTHING ELSE.
#
# Its proposals are candidates, not facts. Every one is passed through `ground_fact()` and
# re-located character-for-character in the bytes we actually fetched. A proposal that
# cannot be found is discarded — not softened, not flagged, discarded. This is the same
# gate `ai.py` applies to policy wording, and the reason a model can be useful here without
# being dangerous: it can only point at text that exists.
#
# The author class still caps what a grounded fact may claim. A government register can
# support DISCOVERY_VERIFIED; a company's own website cannot, however confident the model
# sounds. Grounding proves the words are there, not that they are true.
#
# Rejected proposals are counted and returned rather than dropped silently, because "we
# read 6 things and threw 2 away" is the most informative sentence this agent can produce.

_EXTRACT_TIMEOUT = float(os.getenv("PROVEN_DISCOVERY_MODEL_TIMEOUT", "20"))
_EXTRACT_MAX_CHARS = 9000

# The orchestrator maps insured interests from these, so an unknown type is unusable.
_FACT_TYPES = {
    "LOCATION": ("premises", "buildings", "stock"),
    "ACTIVITY": ("business_interruption",),
    "HEADCOUNT": ("workforce",),
    "EQUIPMENT": ("plant",),
    "STATUS": ("premises",),
    "REVENUE_CHANNEL": ("business_interruption",),
}

_EXTRACT_PROMPT = """You read one web page about a Singapore business and list facts that appear in it.

You are NOT judging insurance. You are not deciding anything. You are pointing at text.

Return ONLY facts that are literally written on the page. For each one, `value` MUST be an
EXACT substring copied from the page, character for character. Do not tidy it, reword it,
expand an abbreviation, or fix its punctuation. If you cannot copy it exactly, leave it out.

We verify every `value` by searching for it in the page. Anything we cannot find is thrown
away, so a reworded value is a wasted one.

Look for, and only for:
- LOCATION: an address, unit number, branch or premises
- ACTIVITY: what the business does or sells
- HEADCOUNT: a number of staff or employees
- EQUIPMENT: named machinery or plant
- STATUS: registration or operating status
- REVENUE_CHANNEL: a sales channel such as online, export, wholesale

Return at most 8. Fewer is better. If the page says nothing useful, return an empty list.

JSON only:
{"facts": [{"fact_type": "LOCATION", "value": "exact text from the page",
            "label": "short plain-English summary under 60 chars"}]}"""


def _model_key() -> Optional[str]:
    """Same provider resolution as the presentation layer, kept local so discovery has no
    dependency on it and degrades independently. One OpenAI-compatible provider until the
    swappable provider layer lands; a key for any other service is ignored."""
    for env in ("OPENAI_API_KEY",):
        val = (os.getenv(env) or "").strip()
        if val and "key-here" not in val.lower() and not val.lower().startswith("sk-your"):
            return env
    return None


def extraction_enabled() -> bool:
    return _model_key() is not None


def propose_facts_from_text(text: str, source_name: str) -> tuple[list[dict], dict]:
    """Ask a model what facts appear on this page. Returns (proposals, meta).

    Never raises and never blocks discovery: any failure returns an empty list, and the
    deterministic patterns below still run. The model is an addition, not a dependency.
    """
    meta = {"attempted": False, "model": None, "error": None, "proposed": 0}
    env = _model_key()
    if not env or not text:
        meta["error"] = "no model configured" if not env else "no page text"
        return [], meta

    url = "https://api.openai.com/v1/chat/completions"
    model = os.getenv("PROVEN_DISCOVERY_MODEL") or "gpt-4o-mini"
    meta["attempted"] = True
    meta["model"] = model

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _EXTRACT_PROMPT},
            {"role": "user", "content": (f"Source: {source_name}\n\nPage text:\n"
                                         + text[:_EXTRACT_MAX_CHARS])},
        ],
        "temperature": 0,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {os.getenv(env)}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_EXTRACT_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
        parsed = json.loads(content or "{}")
    except urllib.error.HTTPError as e:
        meta["error"] = f"HTTP {e.code}"
        return [], meta
    except Exception as e:
        meta["error"] = type(e).__name__
        return [], meta

    raw = parsed.get("facts")
    if not isinstance(raw, list):
        meta["error"] = "response had no facts list"
        return [], meta

    proposals = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        ftype = str(item.get("fact_type") or "").upper().strip()
        value = str(item.get("value") or "").strip()
        if ftype not in _FACT_TYPES or not value:
            continue
        proposals.append({
            "fact_type": ftype,
            "value": value,
            "label": str(item.get("label") or "").strip()[:80] or ftype.title(),
        })
    meta["proposed"] = len(proposals)
    return proposals, meta


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def is_duplicate_fact(fact_type: str, value: str,
                      existing: list["DiscoveredFact"]) -> bool:
    """True if a fact already found by a pattern says the same thing as this proposal.

    Exact matching is not enough. Measured on the ACRA fixture, the branch pattern captured
    the whole line — "21 Tai Seng Avenue #03-12, Singapore 534054 (Registered: 1 June 2026)"
    — while the model returned just the address inside it. Two rows, one fact.

    So containment counts as duplication, but only when the shorter side is long enough for
    containment to mean something: the headcount pattern yields the bare string "4", which
    is a substring of half the addresses in Singapore.
    """
    nv = _norm(value)
    if not nv:
        return True
    for f in existing:
        if not f.value:
            continue
        nf = _norm(f.value)
        if nv == nf:
            return True
        if min(len(nv), len(nf)) >= 8 and (nv in nf or nf in nv):
            return True
        # Counts compare as numbers, not as text: "Total Foreign Workers: 4" is "4".
        if fact_type == "HEADCOUNT" and f.fact_type == "HEADCOUNT":
            a = re.findall(r"\d[\d,]*", nv)
            b = re.findall(r"\d[\d,]*", nf)
            if a and b and a[0].replace(",", "") == b[0].replace(",", ""):
                return True
    return False


# ── second gate: does the value actually look like the type claimed? ─────────
#
# Grounding proves a string is on the page. It does NOT prove the model labelled it
# correctly, and mislabelling is not cosmetic here: `fact_type` selects the insured
# interests in _FACT_TYPES, which is what the orchestrator reasons over. A share-capital
# figure filed as HEADCOUNT would arrive downstream as a workforce signal.
#
# These are real failures from the first live run against the ACRA fixture, not imagined
# ones. The model returned, all of them genuinely present on the page:
#     HEADCOUNT  "100,000 ordinary shares"   → share capital, not people
#     LOCATION   "HOCK LEE FOODS PTE. LTD."  → the company name, not an address
#     STATUS     "Active Work Permits: 4"    → a count, not a registration status
#
# Same division of labour as everywhere else: the model reads, deterministic code decides.

_ADDRESS_HINT = re.compile(
    r"(#\d{1,3}\s*-\s*\d{1,4}|\bS?\(?\d{6}\)?\b|\b(street|st|road|rd|avenue|ave|lane|"
    r"drive|crescent|close|way|walk|park|block|blk|unit|level|floor|tower|building|"
    r"industrial|singapore)\b)", re.I)
_ENTITY_SUFFIX = re.compile(r"\b(pte|ltd|llp|limited|inc|sdn|bhd|corp)\b\.?", re.I)
_EQUITY_WORDS = re.compile(r"(share|capital|paid[-\s]?up|equity|dividend|currency|\$|%)", re.I)
_STATUS_VOCAB = ("live", "active", "registered", "struck off", "struck-off", "dissolved",
                 "liquidation", "cessation", "ceased", "amalgamated", "receivership",
                 "wound up", "winding up", "in default")


def type_is_plausible(fact_type: str, value: str) -> tuple[bool, str]:
    """Cheap deterministic sanity check that `value` could be a `fact_type` at all.

    Deliberately conservative: it rejects clear category errors, not merely odd-looking
    values. A false reject costs one discarded hint; a false accept puts a wrong signal
    into the orchestrator.
    """
    v = (value or "").strip()
    if len(v) < 2:
        return False, "Too short to be a usable fact."

    if fact_type == "HEADCOUNT":
        if _EQUITY_WORDS.search(v):
            return False, ("Reads as share capital or money, not a number of people, so it "
                           "was not accepted as a headcount.")
        nums = re.findall(r"\d[\d,]*", v)
        if not nums:
            return False, "No number in it, so it cannot be a headcount."
        n = int(nums[0].replace(",", ""))
        if not (1 <= n <= 20000):
            return False, f"{n:,} is not a plausible headcount for an SME."
        return True, ""

    if fact_type == "LOCATION":
        if not _ADDRESS_HINT.search(v):
            return False, ("Does not contain a unit number, street, or postcode, so it was "
                           "not accepted as a location.")
        if _ENTITY_SUFFIX.search(v) and not re.search(r"#\d{1,3}\s*-\s*\d{1,4}", v):
            return False, "Reads as a company name rather than an address."
        return True, ""

    if fact_type == "STATUS":
        low = v.lower()
        if not any(w in low for w in _STATUS_VOCAB):
            return False, "Not a recognised registration or operating status."
        if re.search(r"\d", v):
            return False, ("Contains a figure, so it reads as a count rather than a "
                           "status.")
        if len(v) > 40:
            return False, "Too long to be a status label."
        return True, ""

    if fact_type == "ACTIVITY":
        if len(v) < 8 or len(re.findall(r"[A-Za-z]{3,}", v)) < 2:
            return False, "Not descriptive enough to be a business activity."
        return True, ""

    # EQUIPMENT, REVENUE_CHANNEL: no reliable deterministic shape, so only length applies.
    if len(v) < 3:
        return False, "Too short to be a usable fact."
    return True, ""


def facts_from_proposals(
    proposals: list[dict],
    fetched_text: str,
    source: "DiscoverySource",
    *,
    today: str,
    start_index: int = 0,
    max_scope: Optional[str] = None,
) -> tuple[list["DiscoveredFact"], list[dict]]:
    """Turn model proposals into facts, discarding anything not in the fetched bytes.

    `max_scope` is the ceiling the SOURCE allows, not the ceiling the model asked for. A
    grounded quote from a company's own website is still self-reported.
    """
    kept: list[DiscoveredFact] = []
    rejected: list[dict] = []

    authority = getattr(source, "author_class", "") == "AUTHORITY"
    ceiling = max_scope or (DISCOVERY_VERIFIED if authority else DISCOVERY_HINT)

    for p in proposals:
        value = p["value"]
        # The model repeats itself within a single response too, so the same rule applies.
        if is_duplicate_fact(p["fact_type"], value, kept):
            continue

        # Gate 1 — is it actually on the page?
        grounded, quote = ground_fact(value, fetched_text)
        if not grounded:
            rejected.append({
                "fact_type": p["fact_type"],
                "value": value[:120],
                "gate": "GROUNDING",
                "reason": ("Not found word-for-word in the page we fetched, so it was "
                           "discarded rather than shown."),
            })
            continue

        # Gate 2 — could it be the kind of thing the model called it?
        plausible, why = type_is_plausible(p["fact_type"], value)
        if not plausible:
            rejected.append({
                "fact_type": p["fact_type"],
                "value": value[:120],
                "gate": "TYPE",
                "reason": why,
            })
            continue

        kept.append(DiscoveredFact(
            id=f"DISC-{source.id.upper()}-AI-{start_index + len(kept) + 1}",
            source=source,
            fact_type=p["fact_type"],
            label=p["label"],
            detail=(f"Read from {source.name} and re-located word-for-word in the page "
                    f"we fetched."),
            value=value,
            previous_value=None,
            grounded=True,
            ground_quote=quote,
            discovered_on=today,
            insured_interests=_FACT_TYPES[p["fact_type"]],
            claim_scope=ceiling,
            severity="LOW",
            relevant_rules=(),
            extraction="MODEL",
        ))
    return kept, rejected


# ── HTML → text, and why this is a correctness concern rather than tidying ───
#
# The fixtures were plain text. A real website is HTML, and that breaks the grounding gate
# in a way that is easy to miss: the gate re-locates the model's proposal in "the fetched
# text", so the model and the gate MUST be looking at the same string. Give the model clean
# prose and check it against raw markup and honest facts start getting thrown away —
# grounding would be rejecting the parser, not the model.
#
# The specific trap is entities. The demo site emits `Cold Storage &amp; Distribution`
# because that is correct HTML. Unescaped, the model reads "Cold Storage & Distribution"
# and proposes it; searched for in the raw bytes, `&` is not there and a true fact is
# discarded as a hallucination. So: strip once, unescape, and hand the SAME text to both.
#
# Unescaping happens AFTER tags are removed, never before. A page containing the literal
# text `&lt;script&gt;` must not have it turned into a tag by our own parser.

_LOOKS_HTML = re.compile(
    r"<\s*(!doctype\s+html|html|head|body|div|p|span|table|ul|ol|li|h[1-6]|section|"
    r"article|header|footer|nav|main)\b", re.I)
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_LINE_BREAK = re.compile(r"<\s*br\s*/?\s*>", re.I)
_BLOCK_CLOSE = re.compile(
    r"</\s*(p|div|li|tr|td|th|h[1-6]|section|article|header|footer|nav|main|ul|ol|"
    r"table|blockquote)\s*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    """Readable text from an HTML page, or the input unchanged if it is not HTML.

    The passthrough is deliberate and load-bearing. This function is also on the path of
    the ACRA datastore response, which is JSON that `_acra_live_facts` grounds against
    verbatim. Running a tag-stripper over JSON would corrupt the very bytes the evidence
    is anchored to, so anything that does not look like markup is returned untouched.
    """
    if not raw:
        return ""
    if not _LOOKS_HTML.search(raw):
        return raw
    text = _SCRIPT_STYLE.sub(" ", raw)
    text = _LINE_BREAK.sub("\n", text)
    text = _BLOCK_CLOSE.sub("\n", text)
    text = _ANY_TAG.sub(" ", text)
    text = _html.unescape(text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]")


def _is_local(url: str) -> bool:
    """Whether this URL points at this machine.

    Used for one thing only: turning "connection refused" into a message that says what to
    do about it. A demo that fails with `[WinError 10061]` looks broken; the same failure
    saying "start the demo website" is a step.
    """
    low = (url or "").lower()
    return any(h in low for h in _LOCAL_HOSTS)


def fetch_url(url: str) -> tuple[bool, str, str]:
    """Fetch a URL and return (success, text_content, error_message).

    This is the real HTTP layer. It is the one place where the Discovery Agent
    actually goes and looks at the outside world.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text" not in content_type and "json" not in content_type:
                return False, "", f"Non-text content type: {content_type}"
            raw = resp.read(512_000)  # 512KB cap
            encoding = resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(encoding, errors="replace")
            return True, text, ""
    except urllib.error.HTTPError as e:
        return False, "", f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, "", f"URL error: {e.reason}"
    except Exception as e:
        return False, "", f"Fetch failed: {type(e).__name__}: {e}"


# ── the demo discovery feed ──────────────────────────────────────────────────
#
# These are what the agent would find if it fetched ACRA, MOM, and the company's
# website for Hock Lee Foods. In LIVE mode it would fetch the actual pages and
# extract these facts; in DEMO_FIXTURE mode it returns them directly, labelled.

_DEMO_BUSINESS = {
    "name": "Hock Lee Foods Pte Ltd",
    "uen": "T09HL1234A",
    # No website here on purpose — see demo_business(). This used to read
    # "https://www.hockleefoods.com.sg", a domain that does not exist, so every live run
    # failed on DNS and the website source silently contributed nothing.
    "website": None,
    "region": "SG-TAI-SENG",
    "sector": "FOOD_MANUFACTURING",
}


def demo_business() -> dict:
    """The demo business, with its website address resolved at CALL time.

    Call time rather than import time for two reasons that both bit: the URL comes from the
    environment, and a module-level literal would freeze whatever was set when the process
    started — so `monkeypatch.setenv` in a test would appear to work and change nothing.
    """
    return {**_DEMO_BUSINESS, "website": config.demo_site_url()}

_DEMO_POLICY_STATE = {
    "locations": ["Unit 03-11, 21 Tai Seng Avenue"],
    "activity": "Manufacture of prepared meals; wholesale",
    "headcount_declared": 11,
}


def _demo_acra_page() -> str:
    """Simulated ACRA BizFile extract text — what a real fetch would return."""
    return """
ACCOUNTING AND CORPORATE REGULATORY AUTHORITY
Company Profile — BizFile+ Extract

Entity Name: HOCK LEE FOODS PTE. LTD.
UEN: T09HL1234A
Registration Date: 15 March 2009
Entity Status: Live

REGISTERED ADDRESS
21 Tai Seng Avenue #03-11, Singapore 534054

PRINCIPAL ACTIVITIES
Primary SSIC 2025: 10740 — Manufacture of prepared meals and dishes
Secondary SSIC 2025: 47190 — Retail sale via internet / online platforms

BRANCH REGISTRATIONS
Branch 1: 21 Tai Seng Avenue #03-12, Singapore 534054 (Registered: 1 June 2026)

SHARE CAPITAL
Issued: SGD 100,000 (100,000 ordinary shares)
"""


def _demo_website_page() -> str:
    """Simulated company website — what scraping their public page would show."""
    return """
Hock Lee Foods — Singapore's trusted prepared meals manufacturer

About Us
Hock Lee Foods has been serving Singapore's food service industry since 2009.
Operating from our facilities at Tai Seng, we specialise in prepared meals
for the wholesale and retail market.

Our Locations
- Main Production: 21 Tai Seng Avenue #03-11, Singapore 534054
- Cold Storage & Distribution: 21 Tai Seng Avenue #03-12, Singapore 534054
- NEW: Online ordering now available at shop.hockleefoods.com.sg

Our Team
We are a team of 14 dedicated professionals across production, logistics,
and administration.

Contact: info@hockleefoods.com.sg | Tel: 6789 0123
"""


def _demo_mom_page() -> str:
    """Simulated MOM work pass enquiry — what the employer portal would show."""
    return """
MINISTRY OF MANPOWER
Work Pass Enquiry — Employer View

Employer: HOCK LEE FOODS PTE. LTD.
UEN: T09HL1234A

Active Work Permits: 4
Active S Passes: 0
Active Employment Passes: 0

Total Foreign Workers: 4
"""


def _today() -> str:
    return date.today().isoformat()


def discover_business(
    business: Optional[dict] = None,
    policy_state: Optional[dict] = None,
    *,
    live: bool = False,
    entity_query: Optional[str] = None,
) -> dict:
    """Run the Discovery Agent for one business.

    In DEMO mode: returns findings from the simulated pages.
    In LIVE mode: makes a REAL ACRA datastore lookup (by `entity_query` or the business
    name) and grounds every surfaced field against the fetched JSON bytes. MOM stays a
    seam (employer login required) and the company website is fetched only if declared.

    Either way, every fact is grounded against the fetched text before it can be
    promoted to DISCOVERY_VERIFIED.
    """
    biz = business or demo_business()
    pol = policy_state or _DEMO_POLICY_STATE
    today = _today()

    facts: list[DiscoveredFact] = []
    fetch_log: list[dict] = []
    rejected: list[dict] = []
    extraction_log: list[dict] = []

    def _augment(page_text: str, source: DiscoverySource) -> None:
        """Let a model read this page too, then discard whatever it cannot prove.

        Runs AFTER the patterns, never instead of them. The patterns catch the layouts we
        anticipated; the model catches the prose we did not. Anything it proposes that a
        pattern already found is dropped as a duplicate, so this can only add facts.
        """
        if not page_text or not extraction_enabled():
            return
        proposals, meta = propose_facts_from_text(page_text, source.name)
        meta["source"] = source.name

        before = list(facts)
        fresh = [p for p in proposals
                 if not is_duplicate_fact(p["fact_type"], p["value"], before)]
        meta["duplicates_of_pattern"] = len(proposals) - len(fresh)

        kept, rej = facts_from_proposals(
            fresh, page_text, source, today=today, start_index=len(facts))
        meta["kept"] = len(kept)
        meta["discarded_ungrounded"] = len(rej)
        facts.extend(kept)
        rejected.extend(rej)
        extraction_log.append(meta)

    # ── Source 1: ACRA (authority) ──────────────────────────────────────────
    if live:
        query = (entity_query or biz.get("name") or "").strip()
        import urllib.parse
        acra_url = ACRA_DATASTORE_URL.format(
            q=urllib.parse.quote(query), limit=5)
        records, raw, err = fetch_acra_by_name(query)
        # Identity BEFORE extraction. Judged against the business, never against `query`:
        # a partial search may legitimately return many companies, and none of them is
        # therefore this one. See the entity identity gate above.
        subject, identity = identify_subject_records(
            records, expected_name=biz.get("name"), expected_uen=biz.get("uen"))
        live_facts = _acra_live_facts(subject, raw, pol, today)
        facts.extend(live_facts)
        if not err and records and not subject:
            err = (f"The register returned {len(records)} record(s), none of which is "
                   f"this business.")
        fetch_log.append({
            "source": "ACRA", "url": acra_url, "ok": bool(subject), "error": err,
            "query": query, "records_returned": len(records),
            "records_identified": len(subject),
            "identity": identity,
            "facts_extracted": len(live_facts),
            "facts_grounded": sum(1 for f in live_facts if f.grounded),
        })
        # The model reads the REAL registry response, grounded against the real bytes.
        if raw:
            _augment(raw, ACRA_SOURCE)
        ok, text = False, ""     # skip the demo-page parser in live mode
    else:
        text = _demo_acra_page()
        ok = True
        fetch_log.append({"source": "ACRA", "url": "DEMO_FIXTURE", "ok": True})

    if ok and text:
        # Look for branch registrations not on the policy
        branches = re.findall(r"Branch \d+:\s*(.+?)(?:\n|$)", text)
        for branch in branches:
            if is_duplicate_fact("LOCATION", branch.strip(), facts):
                continue        # same reason as the website loop below
            on_policy = any(
                branch.split(",")[0].strip().lower() in loc.lower()
                for loc in pol.get("locations", [])
            )
            if not on_policy:
                grounded, quote = ground_fact(branch.split(",")[0].strip(), text)
                facts.append(DiscoveredFact(
                    id=f"DISC-ACRA-BRANCH-{len(facts)+1}",
                    source=ACRA_SOURCE,
                    fact_type="LOCATION",
                    label="New branch registered with ACRA but not on policy",
                    detail=f"ACRA shows a registered branch at {branch} that does not "
                           f"appear on the policy schedule.",
                    value=branch.strip(),
                    previous_value=", ".join(pol.get("locations", [])),
                    grounded=grounded,
                    ground_quote=quote,
                    discovered_on=today,
                    insured_interests=("premises", "buildings", "stock"),
                    claim_scope=DISCOVERY_VERIFIED if grounded else DISCOVERY_HINT,
                    severity="HIGH",
                    relevant_rules=("ALTERATION-PREMISES-NOT-ON-SCHEDULE",),
                ))

        # Look for secondary activity not on the policy
        secondary = re.search(r"Secondary SSIC.*?:\s*\d+\s*—\s*(.+)", text)
        if secondary:
            activity = secondary.group(1).strip()
            if "online" in activity.lower() and "online" not in pol.get("activity", "").lower():
                grounded, quote = ground_fact(activity, text)
                facts.append(DiscoveredFact(
                    id=f"DISC-ACRA-ACTIVITY-{len(facts)+1}",
                    source=ACRA_SOURCE,
                    fact_type="ACTIVITY",
                    label="New business activity registered with ACRA",
                    detail=f"ACRA registers secondary activity '{activity}' which is "
                           f"not reflected in the policy description.",
                    value=activity,
                    previous_value=pol.get("activity"),
                    grounded=grounded,
                    ground_quote=quote,
                    discovered_on=today,
                    insured_interests=("business_interruption",),
                    claim_scope=DISCOVERY_VERIFIED if grounded else DISCOVERY_HINT,
                    severity="MODERATE",
                    relevant_rules=("ACTIVITY-SSIC-NOT-DECLARED",),
                ))

        _augment(text, ACRA_SOURCE)

    # ── Source 2: Company website (self-authored) ───────────────────────────
    if live:
        url = (biz.get("website") or "").strip()
        if url:
            ok, raw_html, err = fetch_url(url)
            # ONE conversion, and everything downstream — the regexes, the model, and the
            # grounding gate — reads this exact string. See html_to_text().
            text = html_to_text(raw_html) if ok else ""
            if not ok and _is_local(url):
                err = (f"{err}. The demo website is not running — start it with "
                       f"`python demo-site/site.py`.")
            fetch_log.append({
                "source": "Website", "url": url, "ok": ok, "error": err,
                "bytes_fetched": len(raw_html or ""),
                "text_extracted": len(text),
                "format": "HTML" if (raw_html or "").lstrip()[:1] == "<" else "TEXT",
            })
        else:
            ok, text = False, ""
            fetch_log.append({"source": "Website", "url": "none declared", "ok": False,
                              "error": "This business has not declared a website."})
    else:
        text = _demo_website_page()
        ok = True
        fetch_log.append({"source": "Website", "url": "DEMO_FIXTURE", "ok": True})

    # The label follows the FETCH, not the mode we were asked to run in. `live=True` with a
    # website that failed to resolve must still say fixture, and a successful fetch must say
    # live. Deriving it from `live` alone would produce both errors in different runs.
    website_source = (as_live(COMPANY_WEBSITE)
                      if (live and ok and text) else COMPANY_WEBSITE)

    if ok and text:
        # Look for headcount mentioned on the website
        headcount_match = re.search(r"team of (\d+)", text, re.IGNORECASE)
        if headcount_match:
            web_count = int(headcount_match.group(1))
            declared = pol.get("headcount_declared", 0)
            if web_count > declared:
                grounded, quote = ground_fact(headcount_match.group(0), text)
                facts.append(DiscoveredFact(
                    id=f"DISC-WEB-HEADCOUNT-{len(facts)+1}",
                    source=website_source,
                    fact_type="HEADCOUNT",
                    label="Website mentions more staff than policy declares",
                    detail=f"The company's own website says '{headcount_match.group(0)}' "
                           f"but the work injury schedule declares {declared}.",
                    value=str(web_count),
                    previous_value=str(declared),
                    grounded=grounded,
                    ground_quote=quote,
                    discovered_on=today,
                    insured_interests=("workforce",),
                    # SELF-authored: can only hint, not verify
                    claim_scope=DISCOVERY_HINT,
                    severity="MODERATE",
                    relevant_rules=("WICA-HEADCOUNT-UNDERDECLARED",),
                ))

        # Look for locations on the website
        locations = re.findall(r"#\d{2}-\d{2},?\s*\d+[^,\n]+", text)
        for loc in locations:
            on_policy = any(
                loc.strip().split(",")[0].lower() in p.lower()
                for p in pol.get("locations", [])
            )
            if not on_policy:
                # MEASURED: a real page mentions the same address more than once — the
                # locations list AND the news post announcing the opening — so `findall`
                # returns it twice and this loop used to append it twice. One branch became
                # "2 new branches" in the change feed. The old static fixture hid it by
                # mentioning every address exactly once.
                if is_duplicate_fact("LOCATION", loc.strip(), facts):
                    continue
                grounded, quote = ground_fact(loc.strip(), text)
                facts.append(DiscoveredFact(
                    id=f"DISC-WEB-LOCATION-{len(facts)+1}",
                    source=website_source,
                    fact_type="LOCATION",
                    label="Website lists a location not on the policy",
                    detail=f"The company advertises operations at '{loc.strip()}' which "
                           f"is not on the policy schedule.",
                    value=loc.strip(),
                    previous_value=", ".join(pol.get("locations", [])),
                    grounded=grounded,
                    ground_quote=quote,
                    discovered_on=today,
                    insured_interests=("premises",),
                    claim_scope=DISCOVERY_HINT,  # website = SELF
                    severity="MODERATE",
                    relevant_rules=("ALTERATION-PREMISES-NOT-ON-SCHEDULE",),
                ))

        _augment(text, website_source)

    # ── Source 3: MOM (authority) ───────────────────────────────────────────
    if live:
        # MOM requires employer login — seam only
        ok, text = False, ""
        fetch_log.append({"source": "MOM", "url": "requires employer portal login",
                          "ok": False, "error": "Not accessible without credentials"})
    else:
        text = _demo_mom_page()
        ok = True
        fetch_log.append({"source": "MOM", "url": "DEMO_FIXTURE", "ok": True})

    if ok and text:
        wp_match = re.search(r"Total Foreign Workers:\s*(\d+)", text)
        if wp_match:
            foreign_count = int(wp_match.group(1))
            # This fact corroborates Connect's headcount signal — an authority
            # confirmation that the workforce is larger than declared
            grounded, quote = ground_fact(wp_match.group(0), text)
            facts.append(DiscoveredFact(
                id=f"DISC-MOM-WORKERS-{len(facts)+1}",
                source=MOM_SOURCE,
                fact_type="HEADCOUNT",
                label="MOM records show foreign workers on permits",
                detail=f"MOM work pass registry shows {foreign_count} active permits "
                       f"for this employer.",
                value=str(foreign_count),
                grounded=grounded,
                ground_quote=quote,
                discovered_on=today,
                insured_interests=("workforce",),
                claim_scope=DISCOVERY_VERIFIED if grounded else DISCOVERY_HINT,
                severity="LOW",
                relevant_rules=("WORKPASS-MEDICAL-BELOW-MINIMUM",),
            ))

        _augment(text, MOM_SOURCE)

    # ── Assemble result ─────────────────────────────────────────────────────

    verified = [f for f in facts if f.claim_scope == DISCOVERY_VERIFIED]
    hints = [f for f in facts if f.claim_scope == DISCOVERY_HINT]

    return {
        "agent": "discovery",
        "business": biz.get("name"),
        "uen": biz.get("uen"),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "sources_checked": len(fetch_log),
        "sources": [s.as_dict() for s in SOURCES],
        "fetch_log": fetch_log,
        "facts_discovered": len(facts),
        "verified_count": len(verified),
        "hint_count": len(hints),
        "facts": [f.as_dict() for f in facts],
        # How the facts were pulled out, and what was thrown away doing it.
        "extraction": {
            "model_used": extraction_enabled(),
            "pattern_facts": sum(1 for f in facts if f.extraction == "PATTERN"),
            "model_facts": sum(1 for f in facts if f.extraction == "MODEL"),
            "discarded_ungrounded": len(rejected),
            "discarded_by_gate": {
                "not_on_page": sum(1 for r in rejected if r.get("gate") == "GROUNDING"),
                "wrong_kind_of_fact": sum(1 for r in rejected if r.get("gate") == "TYPE"),
            },
            "per_source": extraction_log,
            "note": (
                "A language model read each fetched page and proposed candidate facts. "
                "Every candidate was then searched for word-for-word in the bytes we "
                "actually fetched; anything not found there was discarded, not shown. "
                "Grounding proves the words are on the page — the source's author class, "
                "not the model, still decides how far the fact may be trusted."),
        },
        "discarded": rejected,
        "notice": (
            "DEMO FIXTURE. The pages below are simulated. What is real is the "
            "architecture: fetch → extract → ground → grade → compare. Swapping the "
            "fixtures for live HTTP calls changes the data and nothing below it."
            if not any(fl.get("url") not in ("DEMO_FIXTURE", "none declared",
                                             "requires employer portal login")
                       for fl in fetch_log)
            else "LIVE MODE. The facts below were fetched from real public sources."
        ),
        "method": (
            "Fetch public pages → extract candidate facts → ground each verbatim in "
            "the fetched text (reject if not found) → grade by source author class → "
            "compare against policy schedule. The model reads; deterministic code decides."
        ),
        "demo_mode": not live,
    }


def discovery_as_external_signals(result: dict) -> list[dict]:
    """Convert discovery facts into the signal format the orchestrator expects.

    This is the integration point: Discovery facts feed the orchestrator as external
    signals, so they participate in the overlap detection and precedence rules without
    any new code in the reconciliation engine.
    """
    signals = []
    for fact in result.get("facts", []):
        signals.append({
            "id": fact["id"],
            "agent": "discovery",
            "signal_type": f"DISCOVERY_{fact['fact_type']}",
            "label": fact["label"],
            "detail": fact["detail"],
            "severity": fact["severity"],
            "horizon": "CURRENT",
            "observed_on": fact["discovered_on"],
            "window": "Current public record",
            "regions": (),
            "sectors": (),
            "insured_interests": tuple(fact.get("insured_interests", ())),
            "relevant_rules": tuple(fact.get("relevant_rules", ())),
            "claim_scope": fact["claim_scope"],
            "source": fact["source"],
            "simulated": fact["simulated"],
            "grounded": fact["grounded"],
            "ground_quote": fact.get("ground_quote", ""),
        })
    return signals


def catalogue() -> dict:
    """The transparency panel: what sources exist and which are live."""
    return {
        "sources": [s.as_dict() for s in SOURCES],
        "live_count": sum(1 for s in SOURCES if s.mode == "LIVE"),
        "demo_count": sum(1 for s in SOURCES if s.mode != "LIVE"),
        "boundary": {
            "may_assert": [DISCOVERY_VERIFIED, DISCOVERY_HINT],
            "may_not_assert": [CONFIRMED_EVIDENCE],
            "enforced_by": ("DiscoveredFact.__post_init__ raises on claim_scope "
                            "== CONFIRMED_EVIDENCE. No code path lets discovery "
                            "emit a finding-grade assertion."),
        },
    }
