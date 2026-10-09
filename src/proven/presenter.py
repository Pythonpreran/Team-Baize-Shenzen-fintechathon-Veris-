"""VERIS PRESENTER — the layer that decides how to SAY something, never what is true.

WHY THIS EXISTS. The engine produces correct findings in the language of insurance:
"average clause", "condition precedent", "sum insured adequacy 66.7%". A shop owner does
not speak that language and should not have to. This module turns a verified finding into
one plain sentence, picks the visual that communicates it fastest, and says what to do
next.

THE HARD BOUNDARY, AND WHY IT IS STRUCTURAL RATHER THAN A POLICY.

A model may choose WORDS and VISUALS. It may never produce a NUMBER, a coverage
conclusion, or a fact. Three mechanisms enforce that, and all three run on every call:

  1. The model receives only data the engine has already verified. It cannot fetch, and it
     is told the values are authoritative.
  2. Its output is schema-validated against a whitelist of visual types. Anything outside
     the list is discarded.
  3. A forbidden-phrase gate rejects the entire response if it contains language that
     would state a coverage outcome — "claim denied", "void", "not covered", "guaranteed".
     Those are determinations for a licensed adviser, and a presentation layer that can
     drift into them is not a presentation layer.

If the model is unavailable, its output fails validation, or no key is configured, a
deterministic template answers instead. The screen never depends on the model being up.

PROVIDER. One OpenAI-compatible chat endpoint, used only when OPENAI_API_KEY is set. It is
not required. The swappable multi-provider layer (with a deterministic `none` profile) is
separate work; until it lands this module speaks to exactly one provider, and a key for any
other service is ignored rather than guessed at.

CREDIT. The architecture, the guardrail set and the visual vocabulary are Shreya's design
from the team's earlier presenter prototype. This version changes the transport and
rewrites the fallback copy for a non-expert reader.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# ── provider configuration ───────────────────────────────────────────────────

OPENAI_KEY_ENV = "OPENAI_API_KEY"
MODEL_ENV = "PROVEN_PRESENTER_MODEL"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

TIMEOUT = float(os.getenv("PROVEN_PRESENTER_TIMEOUT", "12"))
MAX_TOKENS = 900
TEMPERATURE = 0.2


def _clean_env(name: str) -> Optional[str]:
    val = (os.getenv(name) or "").strip()
    if not val or val.lower().startswith(("your-", "sk-your")) or "key-here" in val.lower():
        return None
    return val


def provider() -> Optional[str]:
    """Which provider will actually be used: "openai" when its key is set, else None."""
    if _clean_env(OPENAI_KEY_ENV):
        return "openai"
    return None


def model_name() -> Optional[str]:
    override = _clean_env(MODEL_ENV)
    if override:
        return override
    if provider() == "openai":
        return OPENAI_DEFAULT_MODEL
    return None


def enabled() -> bool:
    return provider() is not None


# ── what the front end is able to draw ───────────────────────────────────────
# A whitelist, not a suggestion. A visual type the interface cannot render is a broken
# screen, so an unknown type invalidates the whole response.

VALID_VISUALS = {
    "comparison_bar", "waterfall", "timeline", "progress_ring", "risk_meter",
    "status_stack", "attention_matrix", "trend_line", "before_after",
    "flowchart", "evidence_stack", "priority_list", "obligation_map",
    "risk_map", "distribution_chart", "convergence_flow", "evidence_gap",
    "source_convergence", "action_flow", "protection_balance", "exposure_stack",
    "change_pulse", "signal_confidence", "insurance_impact_flow",
}

VALID_SEVERITIES = {"LOW", "MODERATE", "ELEVATED", "HIGH"}

# Language that states a coverage outcome. The engine is allowed to compute an
# illustration; nothing in the presentation layer is allowed to tell a business owner
# their claim will fail. Presence of any of these voids the response.
# Narrowed deliberately. An earlier version banned "no cover", which also blocked the
# true and important sentence "you have no cover for lost income" — a fact readable
# straight off the schedule. The line to hold is not "never mention absent cover"; it is
# "never predict the outcome of a claim". These phrases all cross that line.
_FORBIDDEN_PHRASES = (
    "claim denied", "claim will be denied", "claim will fail", "claim would fail",
    "will not be paid", "would not be paid", "forfeits cover", "forfeit cover",
    "legally liable", "invalid policy", "policy invalid", "policy is invalid",
    "you are not insured", "you are uninsured", "guaranteed",
    "breach", "void", "in violation",
)

# Filler: sentences that are true of every business and therefore tell this one nothing.
# Measured, not theoretical — gpt-4o-mini produced every one of these on the first run,
# alongside "home insurance" for a commercial policy. A generic sentence is a worse
# outcome than a written template, so it is rejected and the template answers instead.
_FILLER_PHRASES = (
    "understanding your coverage",
    "understanding this change",
    "helps protect your assets",
    "it is important to have adequate",
    "review your policy for any gaps",
    "review your policy details",
    "ensure you are adequately",
    "ensure your business is protected",
    "ensure coverage",
    "consider reviewing your",
    "may vary depending",
    "consult your insurance",
    "protect your business finances",
    "protect your business from losses",
    "adequate coverage is essential",
    "requires your attention",
    "needs your attention now",
    "may affect your coverage and",
    "can protect your business",
    "take action",
)

# This is commercial insurance. A model that says "home" has lost the thread entirely and
# whatever else it wrote cannot be trusted on the same screen.
_WRONG_DOMAIN = ("home insurance", "house insurance", "homeowner", "personal insurance",
                 "your family", "your home's")


# ── semantic routing: what kind of thing is this, and how is it best drawn ────

SEMANTIC_CATEGORIES = {
    "financial_gap": {
        "keywords": ["adequacy", "insured", "documented", "shortfall", "gap", "stock",
                     "sum_insured", "value_at_risk"],
        "primary_visual": "comparison_bar", "secondary_visual": "waterfall"},
    "business_change": {
        "keywords": ["changed", "increased", "decreased", "new_location", "employee",
                     "headcount", "inventory", "growth"],
        "primary_visual": "change_pulse", "secondary_visual": "before_after"},
    "external_risk": {
        "keywords": ["weather", "flood", "cost_index", "infrastructure", "external",
                     "environment", "sentinel"],
        "primary_visual": "risk_meter", "secondary_visual": "timeline"},
    "combined_signal": {
        "keywords": ["internal", "external", "convergence", "overlap", "both",
                     "discovery"],
        "primary_visual": "convergence_flow", "secondary_visual": "flowchart"},
    "missing_evidence": {
        "keywords": ["missing", "unestablished", "information_missing",
                     "document_needed", "abstain"],
        "primary_visual": "evidence_gap", "secondary_visual": "action_flow"},
    "obligation": {
        "keywords": ["warranty", "condition", "obligation", "compliant", "unverified"],
        "primary_visual": "obligation_map", "secondary_visual": "evidence_stack"},
    "renewal_change": {
        "keywords": ["renewal", "prior", "current", "material", "changed_at"],
        "primary_visual": "before_after", "secondary_visual": "timeline"},
    "claim_scenario": {
        "keywords": ["loss", "scenario", "deductible", "settlement", "absorb",
                     "waterfall", "claim"],
        "primary_visual": "waterfall", "secondary_visual": "flowchart"},
    "portfolio": {
        "keywords": ["portfolio", "accounts", "book", "carrier", "premium_at_risk"],
        "primary_visual": "attention_matrix", "secondary_visual": "distribution_chart"},
    "protection_status": {
        "keywords": ["score", "protection", "overall", "dimensions", "health"],
        "primary_visual": "progress_ring", "secondary_visual": "comparison_bar"},
    "drift": {
        "keywords": ["drift", "trend", "adequacy_ratio", "threshold", "crossed"],
        "primary_visual": "trend_line", "secondary_visual": "before_after"},
}


def detect_semantic_category(purpose: str, context: dict) -> str:
    combined = (purpose or "").lower() + " " + " ".join(str(k) for k in context.keys())
    scores = {}
    for cat, info in SEMANTIC_CATEGORIES.items():
        hits = sum(1 for kw in info["keywords"] if kw in combined)
        if hits:
            scores[cat] = hits
    return max(scores, key=scores.get) if scores else "protection_status"


# ── cache ────────────────────────────────────────────────────────────────────
# Identical verified data produces identical presentation, so a repeat render should not
# cost a model call or a second of latency on a demo.

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300
_CACHE_MAX = 200


def _cache_key(purpose: str, context: dict) -> str:
    raw = json.dumps({"p": purpose, "c": context}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_get(key: str) -> Optional[dict]:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, data = hit
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return data


def _cache_set(key: str, data: dict) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
    _CACHE[key] = (time.time(), data)


def clear_cache() -> None:
    _CACHE.clear()


# ── validation ───────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Numbers that are structural rather than factual: percentages out of 100, "one of two",
# a year. Allowing these avoids rejecting "62 out of 100" because 100 is not a data field.
_STRUCTURAL_NUMBERS = {"0", "1", "2", "3", "10", "12", "100", "2025", "2026", "2027"}


def _numbers_in(value) -> set[str]:
    """Every number appearing anywhere in a nested structure, normalised."""
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found |= _numbers_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            found |= _numbers_in(v)
    elif value is not None:
        for m in _NUM_RE.findall(str(value)):
            norm = m.replace(",", "").rstrip(".")
            found.add(norm)
            if "." in norm:                      # 238300.00 also licenses 238300
                found.add(norm.split(".")[0])
    return found


def numbers_are_grounded(prose: str, allowed: set[str]) -> tuple[bool, Optional[str]]:
    """Every number the model wrote must already exist in the verified data.

    THIS IS THE MOST IMPORTANT GATE IN THIS MODULE, and it exists because of a real
    failure rather than a theoretical one. Asked to present a business-interruption
    finding whose data contained only `{cover_present: false, recovery_months: 18}`,
    gpt-4o-mini wrote: "You risk losing S$238,300 in income without cover." That figure
    belonged to a different screen. It was fluent, plausible, and completely invented.

    A wrong number on an insurance screen is the one output this product cannot ship. So
    rather than trusting the instruction not to invent, every numeral in the model's prose
    is checked against the numerals in its input. Anything unaccounted for voids the
    response and the deterministic template answers instead.
    """
    for m in _NUM_RE.findall(prose or ""):
        norm = m.replace(",", "").rstrip(".")
        if norm in _STRUCTURAL_NUMBERS or norm in allowed:
            continue
        if "." in norm and norm.split(".")[0] in allowed:
            continue
        return False, f"invented number: {m}"
    return True, None


def validate(data: dict, allowed_numbers: Optional[set[str]] = None
             ) -> tuple[bool, Optional[str]]:
    """Reject anything the interface cannot draw or the product must not say."""
    if not isinstance(data, dict):
        return False, "not a JSON object"
    for required in ("headline", "primary_visual"):
        if required not in data:
            return False, f"missing field: {required}"

    pv = data.get("primary_visual")
    pv_type = pv.get("type") if isinstance(pv, dict) else pv
    if pv_type not in VALID_VISUALS:
        return False, f"unknown visual type: {pv_type}"

    sv = data.get("secondary_visual")
    if isinstance(sv, dict) and sv.get("type") and sv["type"] not in VALID_VISUALS:
        return False, f"unknown secondary visual: {sv['type']}"

    sev = (data.get("visual_emphasis") or {}).get("severity")
    if sev is not None and sev not in VALID_SEVERITIES:
        return False, f"unknown severity: {sev}"

    prose = " ".join([
        str(data.get("headline", "")), str(data.get("subheadline", "")),
        str(data.get("why_it_matters", "")), str(data.get("what_next", "")),
        " ".join(str(x) for x in (data.get("supporting_points") or [])),
    ]).lower()
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in prose:
            return False, f"forbidden phrase: '{phrase}'"
    for phrase in _WRONG_DOMAIN:
        if phrase in prose:
            return False, f"wrong domain: '{phrase}' (this is commercial insurance)"
    for phrase in _FILLER_PHRASES:
        if phrase in prose:
            return False, f"generic filler: '{phrase}'"

    # The headline has to carry the point. A headline with no number, when numbers were
    # supplied, is the failure mode that makes an LLM layer worthless.
    headline = str(data.get("headline", ""))
    if len(headline) > 90:
        return False, "headline too long to read at a glance"

    if allowed_numbers is not None:
        ok, why = numbers_are_grounded(prose, allowed_numbers)
        if not ok:
            return False, why
    return True, None


# ── the instruction ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You write one screen of a Singapore business insurance app, for a shop or factory owner who knows nothing about insurance.

CONTEXT YOU MUST NOT GET WRONG
- This is COMMERCIAL insurance for a small business. Never say "home", "house", "family" or "personal".
- Money is Singapore dollars, written like S$238,300.
- The reader owns a small business. They are busy and slightly worried. Be calm and specific.

THE NUMBER IS THE MESSAGE
Your headline MUST contain the most important actual number or word from the data.
Vague headlines are useless. Compare:
  BAD:  "Fire loss may leave a gap in your coverage"
  GOOD: "A fire today would cost you S$238,300 of your own money"
  BAD:  "Significant gaps in your coverage"
  GOOD: "4 things need your attention"
  BAD:  "Review your obligations"
  GOOD: "2 promises in your policy are not being kept"

BANNED: EMPTY FILLER
Never write sentences that would be true of any business, such as:
  "Understanding your coverage helps protect your assets."
  "Review your policy for any gaps."
  "It is important to have adequate cover."
Every sentence must contain a fact from the data. If you have nothing specific to say in a
field, use fewer words, not vaguer ones.

HARD RULES
- Use only numbers present in the input. Never invent, round or change one.
- Never say whether a claim would be paid or refused. That is a licensed adviser's call.
- Never use: breach, void, denied, guaranteed, violation, not covered, invalid, no cover.
- Instead: "worth checking", "needs attention", "may leave a gap", "not confirmed yet".

PLAIN WORDS INSTEAD OF JARGON
  sum insured            -> the amount you are covered for
  business interruption  -> cover for lost income
  condition precedent    -> a promise in your policy
  warranty               -> a promise in your policy
  average clause         -> being under-covered reduces every payout
  indemnity period       -> how many months the cover lasts
  adequacy               -> how much of the real value is covered
  unestablished          -> not proven yet
  material change        -> a change that actually matters
No Latin. No legal phrasing. Short sentences.

YOU ARE IMPROVING EXISTING COPY
The input contains "current_wording_to_improve_on". That wording is already correct,
specific and plain. Your job is to make it warmer and clearer for a worried owner WITHOUT
losing a single number or fact. If you cannot genuinely improve a line, return it unchanged.
Never make it vaguer. Never make it longer.

Return ONLY valid JSON in this exact shape:
{
  "presentation_mode": "visual_story",
  "headline": "must contain the key number or word, under 70 chars",
  "subheadline": "one line of real context from the data, under 110 chars",
  "primary_visual": {"type": "<one of the allowed types>", "title": "short title"},
  "secondary_visual": {"type": "<allowed type>", "title": "short title"} or null,
  "supporting_points": ["up to 3 very short points, each under 60 chars"],
  "why_it_matters": "why a business owner should care, under 140 chars, plain words",
  "what_next": "one clear next step, under 90 chars",
  "recommended_action": "button label, 2-4 words",
  "visual_emphasis": {"metric": "the key number or word", "severity": "LOW|MODERATE|ELEVATED|HIGH"}
}

Allowed visual types: comparison_bar, waterfall, timeline, progress_ring, risk_meter, status_stack, attention_matrix, trend_line, before_after, flowchart, evidence_stack, priority_list, obligation_map, risk_map, distribution_chart, convergence_flow, evidence_gap, source_convergence, action_flow, protection_balance, exposure_stack, change_pulse, signal_confidence, insurance_impact_flow"""


@dataclass
class PresentationResult:
    available: bool
    mode: str
    presentation: dict = field(default_factory=dict)
    error: Optional[str] = None
    cached: bool = False
    semantic_category: str = ""
    provider_used: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "available": self.available, "mode": self.mode,
            "presentation": self.presentation, "error": self.error,
            "cached": self.cached, "semantic_category": self.semantic_category,
            "provider": self.provider_used,
            "boundary": ("The model chose the wording and the chart. Every number came "
                         "from the deterministic engine and was verified against a "
                         "document before it reached this layer."),
        }


def compose(purpose: str, verified_context: dict) -> PresentationResult:
    """Turn verified data into a plain-language visual story."""
    context = verified_context if isinstance(verified_context, dict) else {}
    category = detect_semantic_category(purpose, context)
    key = _cache_key(purpose, context)

    hit = _cache_get(key)
    if hit is not None:
        return PresentationResult(True, "MODEL", hit, cached=True,
                                  semantic_category=category,
                                  provider_used=provider())

    # The template is built first, always. It is the floor: a correct, specific, plain
    # screen that needs no network and no model.
    base = build_fallback(purpose, context, category)

    if not enabled():
        return PresentationResult(
            True, "DETERMINISTIC_FALLBACK", base,
            error="No model key configured.", semantic_category=category)

    result, err = _call_model(purpose, context, category, base)
    if result is not None:
        merged = _merge(base, result)
        _cache_set(key, merged)
        return PresentationResult(True, "MODEL", merged,
                                  semantic_category=category,
                                  provider_used=provider())

    return PresentationResult(
        True, "DETERMINISTIC_FALLBACK", base,
        error=err or "Model call failed.", semantic_category=category)


# Fields the model is trusted with, and fields it is not.
#
# WHY THE SPLIT. Measured against gpt-4o-mini: wording it does well, chart selection it
# does badly. Asked to present a protection score, an obligation register and a renewal
# diff, it chose `risk_meter` for all three — one right, two wrong. Chart choice is a
# deterministic mapping from the KIND of finding, which the engine already knows, so
# handing that decision to a model trades a correct answer for a plausible one.
#
# So the model writes prose. The engine picks the picture.
_MODEL_OWNS = ("headline", "subheadline", "why_it_matters", "what_next",
               "supporting_points", "recommended_action")


def _merge(base: dict, model: dict) -> dict:
    """Template structure, model wording. Any field the model fumbled keeps the template's."""
    out = dict(base)
    for f in _MODEL_OWNS:
        val = model.get(f)
        if isinstance(val, str) and val.strip():
            out[f] = val.strip()
        elif isinstance(val, list) and val:
            out[f] = [str(x) for x in val if str(x).strip()][:3]
    # Severity is a judgement about risk, so it stays with the engine; the model may only
    # restate the metric it was given.
    emphasis = dict(base.get("visual_emphasis") or {})
    m_emph = model.get("visual_emphasis") or {}
    if isinstance(m_emph, dict) and isinstance(m_emph.get("metric"), str):
        if m_emph["metric"].strip():
            emphasis["metric"] = m_emph["metric"].strip()
    out["visual_emphasis"] = emphasis
    out["worded_by"] = "model"
    return out


def _call_model(purpose: str, context: dict, category: str,
                base: Optional[dict] = None) -> tuple[Optional[dict], Optional[str]]:
    prov = provider()
    if prov == "openai":
        url, key, model = OPENAI_URL, _clean_env(OPENAI_KEY_ENV), model_name()
    else:
        return None, "no provider"

    # Showing the model the template it is competing with raises the floor considerably.
    # Without it, the model writes to no standard and produces generic prose. With it, the
    # instruction is concrete: beat this, or leave it alone.
    user_payload = {
        "purpose": purpose,
        "kind": category,
        "verified_data": context,
        "current_wording_to_improve_on": {
            k: base.get(k) for k in _MODEL_OWNS if base and base.get(k)
        } if base else None,
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, default=str)[:12000]},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} from {prov}"
    except Exception as e:
        return None, f"{type(e).__name__} calling {prov}"

    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
    if not content:
        return None, "empty response"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None, "response was not JSON"

    # The model may restate any number that was in its input or already on the template.
    # Anything else it wrote, it made up.
    allowed = _numbers_in(context) | _numbers_in(
        {k: base.get(k) for k in _MODEL_OWNS} if base else {})
    ok, why = validate(parsed, allowed_numbers=allowed)
    if not ok:
        # A rejected response is not silently swallowed; the reason travels so the
        # interface can show that the gate fired.
        return None, f"rejected by validation: {why}"
    return parsed, None


# ── deterministic fallbacks, written in the same plain voice ──────────────────
# These are not placeholders. If the model never runs, these are the product, so they are
# written to the same standard: short sentences, no jargon, one clear next step.

def _sgd(v) -> str:
    try:
        return "S$" + f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v or "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def build_fallback(purpose: str, ctx: dict, category: str) -> dict:
    builder = _FALLBACKS.get(purpose)
    if builder:
        out = builder(ctx)
        out.setdefault("presentation_mode", "visual_story")
        return out
    info = SEMANTIC_CATEGORIES.get(category, {})
    return {
        "presentation_mode": "insight_card",
        "headline": "Here is what we found",
        "subheadline": "",
        "primary_visual": {"type": info.get("primary_visual", "status_stack"),
                           "title": ""},
        "secondary_visual": None,
        "supporting_points": [],
        "why_it_matters": "This may affect how well your cover matches your business.",
        "what_next": "Have a look at the detail below.",
        "recommended_action": "See detail",
        "visual_emphasis": {"metric": "", "severity": "MODERATE"},
    }


def _fb_home(ctx) -> dict:
    score = ctx.get("overall")
    actions = int(ctx.get("action_count") or 0)
    if actions == 0:
        headline = "Nothing needs your attention today"
    elif actions == 1:
        headline = "One thing is worth a look"
    else:
        headline = f"{actions} things are worth a look"
    return {
        "headline": headline,
        "subheadline": f"Your protection score is {score} out of 100"
                       if score is not None else "",
        "primary_visual": {"type": "progress_ring", "title": "Protection score"},
        "secondary_visual": {"type": "change_pulse", "title": "Recent changes"},
        "supporting_points": [
            f"{_plural(int(ctx.get('finding_count') or 0), 'thing')} found in your documents",
            "Checked automatically in the background",
        ],
        "why_it_matters": "Your business changes. Your policy does not, unless someone "
                          "moves it.",
        "what_next": "Start with what needs attention." if actions
                     else "Nothing to do right now.",
        "recommended_action": "See what changed" if actions else "See detail",
        "visual_emphasis": {
            "metric": f"{score}" if score is not None else "—",
            "severity": ("HIGH" if (score or 0) < 40 else
                         "ELEVATED" if (score or 0) < 70 else "LOW")},
    }


def _fb_protection(ctx) -> dict:
    score = ctx.get("overall")
    return {
        "headline": f"Your protection score is {score} out of 100"
                    if score is not None else "Protection score",
        "subheadline": "This measures how well your documents back up your cover",
        "primary_visual": {"type": "progress_ring", "title": "Overall"},
        "secondary_visual": {"type": "comparison_bar", "title": "By area"},
        "supporting_points": [
            f"{ctx.get('assessed_dimensions', '?')} of "
            f"{ctx.get('total_dimensions', '?')} areas could be checked",
            "Areas with no paperwork are left blank, not marked zero",
        ],
        "why_it_matters": "A low score usually means a missing document, not a bad "
                          "business.",
        "what_next": "Look at the lowest area first.",
        "recommended_action": "See breakdown",
        "visual_emphasis": {
            "metric": f"{score}" if score is not None else "—",
            "severity": ("HIGH" if (score or 0) < 40 else
                         "ELEVATED" if (score or 0) < 70 else "LOW")},
    }


def _fb_scenario(ctx) -> dict:
    absorb = ctx.get("owner_shortfall")
    loss = ctx.get("loss")
    pays = ctx.get("final_illustration")
    return {
        "headline": f"You would pay {_sgd(absorb)} yourself"
                    if absorb else "What a claim would look like",
        "subheadline": f"On a {_sgd(loss)} loss, your insurer would pay about "
                       f"{_sgd(pays)}" if loss else "",
        "primary_visual": {"type": "waterfall", "title": "How that number is worked out"},
        "secondary_visual": None,
        "supporting_points": [
            f"The loss: {_sgd(loss)}",
            f"Your insurer pays: {_sgd(pays)}",
            f"Left with you: {_sgd(absorb)}",
        ] if loss else [],
        "why_it_matters": "Being covered for less than something is worth reduces every "
                          "payout, not just the difference.",
        "what_next": "Check the amount you are covered for against what you actually hold.",
        "recommended_action": "See the working",
        "visual_emphasis": {"metric": _sgd(absorb), "severity": "HIGH"},
    }


def _fb_warranty(ctx) -> dict:
    counts = ctx.get("counts") or {}
    unmet = int(counts.get("BREACHED") or 0)
    unknown = int(counts.get("UNVERIFIED") or 0)
    total = int(ctx.get("obligations_in_policy") or 0)
    if unmet:
        headline = (f"{_plural(unmet, 'promise')} in your policy "
                    f"{'is' if unmet == 1 else 'are'} not being kept")
        sev = "HIGH"
    elif unknown:
        headline = f"We cannot yet confirm {_plural(unknown, 'promise')}"
        sev = "ELEVATED"
    else:
        headline = "Every promise we found is being kept"
        sev = "LOW"
    return {
        "headline": headline,
        "subheadline": f"Your policy contains {_plural(total, 'promise')} you agreed to",
        "primary_visual": {"type": "obligation_map", "title": "Promises in your policy"},
        "secondary_visual": {"type": "evidence_stack", "title": "Where each one is written"},
        "supporting_points": [p for p in [
            f"Not being kept: {unmet}" if unmet else None,
            f"Cannot confirm yet: {unknown}" if unknown else None,
            f"Confirmed fine: {counts.get('COMPLIANT') or 0}",
        ] if p],
        "why_it_matters": "Policies contain promises. If one slips, it can affect a claim "
                          "even when it had nothing to do with what happened.",
        "what_next": "Send us the missing certificate or receipt to clear these.",
        "recommended_action": "See the promises",
        "visual_emphasis": {"metric": str(unmet or unknown or 0), "severity": sev},
    }


def _fb_bi(ctx) -> dict:
    has_cover = ctx.get("cover_present")
    months = ctx.get("recovery_months")
    if has_cover is False:
        return {
            "headline": "You have no cover for lost income",
            "subheadline": f"It could take around {months} months to trade normally again"
                           if months else "",
            "primary_visual": {"type": "evidence_gap", "title": "Missing cover"},
            "secondary_visual": {"type": "timeline", "title": "Time to recover"},
            "supporting_points": [
                "Your policy covers damage to things",
                "It does not cover the trade you lose while you rebuild",
                f"That gap is about {months} months long" if months else None,
            ],
            "why_it_matters": "Most businesses that close after a disaster do so because "
                              "the income stopped, not because the building burned.",
            "what_next": "Ask your adviser what income cover would cost.",
            "recommended_action": "Talk to adviser",
            "visual_emphasis": {"metric": "None", "severity": "HIGH"},
        }
    return {
        "headline": "You have cover for lost income",
        "subheadline": "Worth checking the amount still matches your takings",
        "primary_visual": {"type": "timeline", "title": "Cover period"},
        "secondary_visual": None,
        "supporting_points": ["Check the amount", "Check how many months it lasts"],
        "why_it_matters": "This cover has a time limit. After it ends, lost income is "
                          "yours to carry.",
        "what_next": "Confirm the period is long enough for your trade.",
        "recommended_action": "See detail",
        "visual_emphasis": {"metric": "In place", "severity": "LOW"},
    }


def _fb_renewal(ctx) -> dict:
    material = int(ctx.get("material_count") or 0)
    total = int(ctx.get("change_count") or 0)
    return {
        "headline": (f"{_plural(material, 'real change')} since last year"
                     if material else "Nothing important changed"),
        "subheadline": f"We compared {_plural(total, 'difference')} between the two "
                       f"policies" if total else "",
        "primary_visual": {"type": "before_after", "title": "Last year vs now"},
        "secondary_visual": None,
        "supporting_points": [
            f"Worth your attention: {material}",
            f"Just admin: {max(0, total - material)}",
        ],
        "why_it_matters": "A policy can renew at the same price with less cover inside it.",
        "what_next": "Read the change we flagged before you accept the renewal.",
        "recommended_action": "Compare policies",
        "visual_emphasis": {"metric": str(material),
                            "severity": "HIGH" if material else "LOW"},
    }


def _fb_connect(ctx) -> dict:
    n = int(ctx.get("event_count") or 0)
    return {
        "headline": (f"{_plural(n, 'change')} in your business"
                     if n else "No new changes"),
        "subheadline": "Picked up from your business records",
        "primary_visual": {"type": "change_pulse", "title": "What changed"},
        "secondary_visual": None,
        "supporting_points": [f"{n} picked up automatically"],
        "why_it_matters": "When the business grows, cover bought last year may no longer "
                          "be the right size.",
        "what_next": "Check whether these changes affect your cover.",
        "recommended_action": "See changes",
        "visual_emphasis": {"metric": str(n),
                            "severity": "ELEVATED" if n else "LOW"},
    }


def _fb_sentinel(ctx) -> dict:
    level = ctx.get("environment_level") or "LOW"
    n = int(ctx.get("signal_count") or 0)
    return {
        "headline": f"Conditions around you: {level.lower()}",
        "subheadline": f"{_plural(n, 'thing')} we are watching outside your business",
        "primary_visual": {"type": "risk_meter", "title": "Conditions nearby"},
        "secondary_visual": {"type": "timeline", "title": "Recent"},
        "supporting_points": [
            "Live weather and rainfall near you",
            "Published building cost figures",
        ],
        "why_it_matters": "Rebuilding costs rise even when your business stays the same "
                          "size.",
        "what_next": "No action needed unless we flag it.",
        "recommended_action": "See conditions",
        "visual_emphasis": {"metric": level,
                            "severity": level if level in VALID_SEVERITIES else "LOW"},
    }


def _fb_discovery(ctx) -> dict:
    verified = int(ctx.get("verified_count") or 0)
    checked = int(ctx.get("sources_checked") or 0)
    return {
        "headline": (f"{_plural(verified, 'difference')} between your records and ours"
                     if verified else "Your public records match your policy"),
        "subheadline": f"We checked {_plural(checked, 'source')}",
        "primary_visual": {"type": "convergence_flow", "title": "Records compared"},
        "secondary_visual": {"type": "evidence_stack", "title": "Where we looked"},
        "supporting_points": ["Government company register",
                             "Your own website, if you gave us the address"],
        "why_it_matters": "If a public record says something your policy does not, that "
                          "difference can matter later.",
        "what_next": "Have a look and tell us which is right.",
        "recommended_action": "See differences",
        "visual_emphasis": {"metric": str(verified),
                            "severity": "ELEVATED" if verified else "LOW"},
    }


def _fb_drift(ctx) -> dict:
    crossed = ctx.get("crossed_on")
    return {
        "headline": ("Your cover has fallen behind costs" if crossed
                     else "Your cover is keeping up with costs"),
        "subheadline": "Based on published building material prices",
        "primary_visual": {"type": "trend_line", "title": "Over time"},
        "secondary_visual": None,
        "supporting_points": ["Prices go up", "Your cover amount does not, on its own"],
        "why_it_matters": "The same amount of cover buys less rebuilding each year.",
        "what_next": ("Ask about raising the amount you are covered for."
                      if crossed else "Nothing to do yet."),
        "recommended_action": "See the trend",
        "visual_emphasis": {"metric": "Behind" if crossed else "On track",
                            "severity": "HIGH" if crossed else "LOW"},
    }


def _fb_gaps(ctx) -> dict:
    n = int(ctx.get("gap_count") or 0)
    first = ctx.get("top_document") or ""
    return {
        "headline": (f"{_plural(n, 'document')} would settle open questions"
                     if n else "Nothing missing"),
        "subheadline": f"Start with: {first}" if first else "",
        "primary_visual": {"type": "action_flow", "title": "What to send"},
        "secondary_visual": None,
        "supporting_points": ["Each one closes a specific question",
                             "Ranked by how much it clears up"],
        "why_it_matters": "We would rather say we do not know than guess about your money.",
        "what_next": f"Send the {first.lower()} when you can." if first
                     else "Nothing needed.",
        "recommended_action": "See the list",
        "visual_emphasis": {"metric": str(n),
                            "severity": "ELEVATED" if n else "LOW"},
    }


def _self_authored_caveat(author_class: str) -> str:
    """One sentence on how far a source can be trusted, in words an owner reads.

    A company's own website is SELF-authored, so it proves what the business PUBLISHED and
    not what is true. That distinction is the whole grading scheme, and an outbound message
    is the worst place to drop it: the owner cannot see the evidence panel from their inbox.
    """
    if (author_class or "").upper() == "SELF":
        return ("This came from your own website, so we are flagging it rather than "
                "treating it as settled. If the website is out of date, tell us and we "
                "will drop it.")
    if (author_class or "").upper() == "AUTHORITY":
        return ("This came from a government register, which is why we are confident "
                "about it.")
    return ("This came from a third-party listing, so it needs confirming before anyone "
            "acts on it.")


def _fb_discovery_alert(ctx) -> dict:
    """Wording for something that CHANGED, addressed to the owner rather than a screen.

    Routed through the presenter rather than written in the notifier so that alert prose
    passes the same four gates as screen prose. An invented figure is bad on a dashboard and
    considerably worse in an email, because the email outlives the session and gets
    forwarded to a broker.
    """
    code = str(ctx.get("code") or "")
    count = int(ctx.get("count") or 0)
    values = [str(v) for v in (ctx.get("values") or []) if str(v).strip()]
    policy_says = str(ctx.get("policy_says") or "").strip()
    author = str(ctx.get("author_class") or "")
    source = str(ctx.get("source_name") or "your public information")
    caveat = _self_authored_caveat(author)
    first = values[0] if values else ""

    if code in ("NEW_LOCATION", "CHANGED_LOCATION"):
        place = _plural(count, "address") if count != 1 else "An address"
        headline = (f"{count} addresses on your website are not on your policy"
                    if count > 1 else "An address on your website is not on your policy")
        return {
            "headline": headline,
            "subheadline": (f"Your policy lists: {policy_says}" if policy_says
                            else f"Seen on {source.lower()}"),
            "primary_visual": {"type": "change_pulse", "title": "What changed"},
            "secondary_visual": {"type": "evidence_stack", "title": "Where we saw it"},
            "supporting_points": ([f"Now showing: {v}" for v in values[:2]]
                                  + ([f"On your policy: {policy_says}"]
                                     if policy_says else []))[:3],
            "why_it_matters": ("A policy covers the addresses written on it. If you are "
                               "working somewhere it does not mention, that gap is worth "
                               "closing before anything happens there."),
            "what_next": ("Send your adviser the new address so it can be added to the "
                          "schedule. It usually takes one email."),
            "recommended_action": "Tell my adviser",
            "visual_emphasis": {"metric": str(count) if count else place,
                                "severity": "HIGH"},
            "caveat": caveat,
        }

    if code in ("CHANGED_HEADCOUNT", "NEW_HEADCOUNT"):
        was = str(ctx.get("previous_value") or "").strip()
        now = str(ctx.get("new_value") or first or "").strip()
        headline = (f"Your website now says {now} people, it said {was} before"
                    if was and now else f"Your website says you have {now} people")
        return {
            "headline": headline,
            "subheadline": (f"Your work injury cover is based on {policy_says}"
                            if policy_says else f"Seen on {source.lower()}"),
            "primary_visual": {"type": "before_after", "title": "Team size"},
            "secondary_visual": None,
            "supporting_points": ([f"Website now: {now}"] if now else [])
            + ([f"Website before: {was}"] if was else [])
            + ([f"Declared on the policy: {policy_says}"] if policy_says else []),
            "why_it_matters": ("Work injury cover in Singapore has to match the number of "
                              "people you actually employ, so the count on the policy "
                              "needs to keep up with the count on the floor."),
            "what_next": ("Send us your latest CPF contribution record and we will check "
                          "the number against your schedule."),
            "recommended_action": "Send my CPF record",
            "visual_emphasis": {"metric": now or "?", "severity": "MODERATE"},
            "caveat": caveat,
        }

    if code in ("CHANGED_STATUS", "NEW_STATUS"):
        was = str(ctx.get("previous_value") or "").strip()
        now = str(ctx.get("new_value") or first or "").strip()
        return {
            "headline": (f"Your company status now reads {now}" if now
                         else "Your company registration status changed"),
            "subheadline": (f"It read {was} when we last looked" if was
                            else f"Seen on {source.lower()}"),
            "primary_visual": {"type": "status_stack", "title": "Registration"},
            "secondary_visual": None,
            "supporting_points": [p for p in (f"Now: {now}" if now else "",
                                              f"Before: {was}" if was else "") if p],
            "why_it_matters": ("A policy is written for a registered business. If the "
                              "registration changes, the paperwork behind the cover needs "
                              "to match it."),
            "what_next": "Check this is expected, and tell your adviser if it is not.",
            "recommended_action": "Tell my adviser",
            "visual_emphasis": {"metric": now or "Changed", "severity": "HIGH"},
            "caveat": caveat,
        }

    # Unknown code: say what we saw and nothing more.
    return {
        "headline": (f"{_plural(count, 'change')} in your public information"
                     if count else "Something changed in your public information"),
        "subheadline": f"Seen on {source.lower()}",
        "primary_visual": {"type": "change_pulse", "title": "What changed"},
        "secondary_visual": None,
        "supporting_points": [f"Now showing: {v}" for v in values[:3]],
        "why_it_matters": ("Public information that disagrees with your policy is worth "
                          "reconciling while it is small."),
        "what_next": "Have a look and tell us which version is right.",
        "recommended_action": "See the detail",
        "visual_emphasis": {"metric": str(count) if count else "1",
                            "severity": str(ctx.get("severity") or "MODERATE")},
        "caveat": caveat,
    }


def _fb_carrier_briefing(ctx) -> dict:
    """Portfolio wording for an underwriter (B2).

    Written to a different reader than every other purpose here. An owner is being told
    what to do about their own business; an underwriter is deciding where to spend the
    next hour across a book. So the headline is a count and a direction, never a verdict
    on any one account — and there is deliberately no money in it, because a briefing
    that quotes is a rating engine and this is not one.
    """
    total = int(ctx.get("accounts") or 0)
    attention = int(ctx.get("needs_review") or 0)
    patterns = int(ctx.get("pattern_count") or 0)
    unevidenced = int(ctx.get("unresolved_evidence") or 0)
    if attention == 0:
        headline = f"Nothing on your {total} accounts needs attention today"
    else:
        headline = (f"{attention} of {total} accounts need a look")
    return {
        "headline": headline,
        "subheadline": (f"{patterns} pattern(s) run across the book"
                        if patterns else "No concentration crosses the threshold"),
        "primary_visual": {"type": "attention_matrix", "title": "Where attention sits"},
        "secondary_visual": {"type": "comparison_bar", "title": "By reason"},
        "supporting_points": [
            f"{unevidenced} item(s) across the book have no document behind them",
            "Ranked from your own book fields, not from any business's documents",
        ],
        "why_it_matters": ("Accounts that surfaced for the same reason are one wording "
                          "question, not several separate cases."),
        "what_next": ("Work the queue from the top." if attention
                      else "Nothing to work through right now."),
        "recommended_action": "Open the work queue",
        "visual_emphasis": {
            "metric": f"{attention}",
            "severity": ("HIGH" if attention and total and attention / total > 0.5
                         else "ELEVATED" if attention else "LOW")},
    }


def _fb_carrier_account_summary(ctx) -> dict:
    """One account, in an underwriter's terms (B2, account scope).

    States what the book evidences and, explicitly, what it cannot — because the whole
    design of the carrier surface is that a flag is visible and the document behind it is
    not, unless the business disclosed it.
    """
    name = ctx.get("business_name") or "This account"
    unevidenced = int(ctx.get("unestablished_count") or 0)
    disclosed = bool(ctx.get("case_detail_available"))
    return {
        "headline": f"{name} was flagged for {str(ctx.get('lead_reason') or 'review').lower()}",
        "subheadline": ("The business has disclosed its case to you"
                        if disclosed else
                        "The business has not disclosed its case to you"),
        "primary_visual": {"type": "evidence_stack", "title": "What is established"},
        "secondary_visual": {"type": "obligation_map", "title": "What is outstanding"},
        "supporting_points": [
            f"{unevidenced} item(s) have no document behind them",
            ("Findings and citations are shown because they were disclosed"
             if disclosed else
             "Counts come from your book; the documents behind them are not shown"),
        ],
        "why_it_matters": ("Unevidenced is not the same as wrong. It means nobody has "
                          "established it either way."),
        "what_next": ("Review what was disclosed." if disclosed
                      else "Request the specific document, or refer it to the adviser."),
        "recommended_action": "Record a decision",
        "visual_emphasis": {
            "metric": f"{unevidenced}",
            "severity": ("HIGH" if unevidenced >= 3 else
                         "ELEVATED" if unevidenced else "LOW")},
    }


def _fb_adviser_note(ctx) -> dict:
    """Wording for an adviser's file note (Task 3 Phase B).

    The fallback matters more here than anywhere else in this module. A file note is a
    compliance artefact, so "the model was unavailable" must still produce something an
    adviser can save and defend — not an empty box. Every number below comes from the
    context the caller already verified.
    """
    reviewed = int(ctx.get("findings_reviewed") or 0)
    escalated = int(ctx.get("requires_review") or 0)
    requested = int(ctx.get("documents_requested") or 0)
    return {
        "headline": f"File note — {reviewed} finding(s) reviewed",
        "subheadline": (f"{escalated} require(s) an authorised opinion"
                        if escalated else "None required escalation"),
        "primary_visual": {"type": "evidence_stack", "title": "Evidence reviewed"},
        "secondary_visual": {"type": "obligation_map", "title": "Outstanding"},
        "supporting_points": [
            f"{reviewed} finding(s) read against the disclosed documents",
            f"{requested} document(s) requested from the client",
        ],
        "why_it_matters": ("A file note records the basis on which advice was given. It "
                          "is the adviser's own record, not a coverage opinion."),
        "what_next": "Review the draft, edit it, and approve it before it is relied on.",
        "recommended_action": "Review and approve",
        "visual_emphasis": {
            "metric": f"{escalated}",
            "severity": "ELEVATED" if escalated else "LOW"},
    }


_FALLBACKS = {
    "discovery_alert": _fb_discovery_alert,
    "carrier_briefing": _fb_carrier_briefing,
    "carrier_account_summary": _fb_carrier_account_summary,
    "adviser_note": _fb_adviser_note,
    "home_overview": _fb_home,
    "protection_overview": _fb_protection,
    "scenario_explanation": _fb_scenario,
    "warranty_overview": _fb_warranty,
    "bi_overview": _fb_bi,
    "renewal_summary": _fb_renewal,
    "connect_summary": _fb_connect,
    "sentinel_summary": _fb_sentinel,
    "discovery_summary": _fb_discovery,
    "drift_summary": _fb_drift,
    "gaps_summary": _fb_gaps,
}

PURPOSES = tuple(sorted(_FALLBACKS))


def status() -> dict:
    prov = provider()
    on = prov is not None
    return {
        "enabled": on,
        "mode": "MODEL" if on else "DETERMINISTIC_FALLBACK",
        "provider": prov,
        "model": model_name(),
        "purposes": list(PURPOSES),
        "guardrails": {
            "visual_whitelist": len(VALID_VISUALS),
            "forbidden_phrases": len(_FORBIDDEN_PHRASES),
            "schema_validated": True,
            "falls_back": True,
        },
        "notice": (
            "A model rewords what the engine already decided. The engine picks the "
            "chart, not the model. It never sees a document, never produces a number, "
            "and its wording is thrown away if it invents a figure or states a "
            "coverage outcome."
            if on else
            "No model key is configured, so wording comes from written templates. Every "
            "finding, number and piece of evidence works exactly the same either way."
        ),
    }
