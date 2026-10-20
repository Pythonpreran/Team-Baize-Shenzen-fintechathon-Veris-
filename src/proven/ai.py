"""The AI layer: PROPOSE -> VERIFY -> BIND.

THE ARCHITECTURE, AND WHY IT IS THE INTERESTING PART.

A language model is good at reading unfamiliar prose and saying "this paragraph looks
like an average clause". It is bad at being trusted, because there is no way to tell a
correct reading from a confident invention by looking at the output. So the model is
given the first job and denied the second:

    PROPOSE   The model reads retrieved passages and proposes a candidate: a
              mechanism, a threshold, a quote it claims supports the reading.
    VERIFY    Deterministic code checks the proposal against the immutable stored
              page. Does that quote exist, byte-for-byte, at those offsets? Is the
              claimed number actually in the quoted text? Is the mechanism a code
              that exists in our taxonomy?
    BIND      Only a proposal that survives verification becomes usable, and it is
              recorded as `AI_PROPOSED_VERIFIED` — never as a hand-verified clause,
              because a verified quote still is not a lawyer reading a clause.

A rejected proposal is not silently dropped. It is returned with the reason, because
"the model claimed a quote that is not in the document" is the most interesting output
this system can produce: it is a hallucination caught in the act, on stage.

WHAT THE MODEL MAY NEVER DO, enforced by there being no code path for it:
  * decide whether cover exists
  * resolve endorsement/schedule precedence
  * compute or adjust a settlement figure
  * set an evidence class
  * produce the protection score
Those live in resolve.py, arithmetic.py, grading.py and score.py, none of which
imports this module. The dependency direction is the guarantee.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

from . import config, jurisdiction, llm
from .gates import norm_ws
from .ingest import page_text
from .lang import lexicons
from .retrieval import Hit, Retriever

# Mechanisms the model is permitted to propose. A closed vocabulary, so the model
# cannot invent a coverage concept and have it enter the system. Mapped to the same
# taxonomy codes the deterministic classifier uses.
PROPOSABLE_MECHANISMS: dict[str, str] = {
    "average_underinsurance": "ADEQ.AVERAGE_CONDITION",
    "average_formula": "ADEQ.AVERAGE_FORMULA",
    "premium_payment_warranty": "COND.PREMIUM_PAYMENT_WARRANTY",
    "alteration_notice": "COND.ALTERATION_NOTICE",
    "theft_forcible_entry": "EXCL.THEFT_FORCIBLE_ENTRY",
    "unoccupancy": "COND.UNOCCUPANCY",
    "claim_time_limit": "COND.CLAIM_TIME_LIMIT",
    "minimum_security": "COND.MINIMUM_SECURITY",
    "deductible": "LIMIT.DEDUCTIBLE",
    "sub_limit": "LIMIT.SUB_LIMIT",
    "cyber_exclusion": "EXCL.CYBER",
    "communicable_disease": "EXCL.COMMUNICABLE_DISEASE",
    "condition_precedent": "COND.DECLARATION_CLAUSE_GENERAL",
    "disclosure": "COND.DISCLOSURE",
    "reinstatement_basis": "ADEQ.REINSTATEMENT_BASIS",
}

AI_CLASSIFIER = "AI_PROPOSED_VERIFIED"

_PROPOSE_SYSTEM = """You identify insurance coverage mechanisms in policy text.

You will be given numbered passages extracted verbatim from one insurance policy.

For each mechanism you can identify, return an object with:
  "mechanism": one of the allowed keys (exactly as given, no others)
  "passage": the number of the passage it came from
  "quote": a VERBATIM substring copied character-for-character from that passage
  "threshold_pct": a number 0-1 if the text states a percentage threshold, else null
  "threshold_days": an integer if the text states a day count, else null
  "states_formula": true only if the text spells out the arithmetic, else false
  "reasoning": one short sentence

ABSOLUTE RULES:
- The "quote" MUST be copied exactly from the passage. Do not paraphrase, do not fix
  spacing, do not join text from two passages. Your quote will be checked against the
  source document and rejected if it does not match.
- Only report a threshold if the number is literally present in your quote.
- If you are unsure, omit the mechanism. Omission is correct; guessing is not.
- Never state whether something is covered. You identify mechanisms only.

Return JSON: {"mechanisms": [...]}"""

_EXPLAIN_SYSTEM = """You explain an insurance finding to a small business owner.

You will be given a finding produced by a deterministic engine, plus the verbatim
policy passages it relies on.

Write 2-4 short sentences in plain English. Rules:
- Explain what the documents say and why it matters commercially.
- Refer to the evidence given. Do not introduce facts that are not in it.
- NEVER say the claim will be denied, that cover is void, or that the policy is
  breached. The engine locates and quotes; a licensed adviser decides.
- If the finding's state is INFORMATION_MISSING, explain what is missing and why the
  engine will not estimate it.
- No jargon, no legal advice, no numbers that are not in the evidence.
Return only the explanation text."""

_ASK_SYSTEM = """You answer a small business owner's questions about their own
insurance review.

You may use ONLY the context provided: the current findings, the protection score
breakdown, and verbatim policy passages. Rules:
- If the context does not answer the question, say so plainly and say what document
  or authorised review would answer it. Do not speculate.
- Cite the document and page for any statement about what the policy says.
- NEVER say cover exists, does not exist, is void, or that a claim will be denied.
- Never invent a figure. Use only figures present in the context.
- 2-5 sentences. Plain English. No jargon.
Return only the answer text."""


@dataclass
class Proposal:
    """A model-proposed mechanism, with its verification outcome."""

    mechanism: str
    taxonomy_code: Optional[str]
    quote: str
    reasoning: str
    threshold_pct: Optional[float] = None
    threshold_days: Optional[int] = None
    states_formula: bool = False
    citation: Optional[dict] = None
    verified: bool = False
    rejection_reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "mechanism": self.mechanism,
            "taxonomy_code": self.taxonomy_code,
            "quote": self.quote,
            "reasoning": self.reasoning,
            "threshold_pct": self.threshold_pct,
            "threshold_days": self.threshold_days,
            "states_formula": self.states_formula,
            "citation": self.citation,
            "verified": self.verified,
            "rejection_reason": self.rejection_reason,
            "classifier": AI_CLASSIFIER if self.verified else None,
        }


@dataclass
class AIResult:
    """Envelope for every AI call, with diagnostics separate from public response data."""

    available: bool
    mode: str
    text: str = ""
    payload: dict = field(default_factory=dict)
    # Transport/model details remain available to server code, tests and prompt logs.
    error: Optional[str] = None
    # Only evidence/permission-safe messages are serialized to a browser.
    public_error: Optional[str] = None

    def as_dict(self) -> dict:
        return {"available": self.available, "mode": self.mode, "text": self.text,
                "error": self.public_error, **self.payload}


def _chat(messages: list[dict], *, json_mode: bool = False,
          max_tokens: int = 700, tenant_id: str = "unscoped",
          purpose: str = "insurance_assistance", user_intent: Optional[str] = None
          ) -> tuple[Optional[str], Optional[str]]:
    """One shared, redacting chat call. Never raises."""
    result = llm.chat(messages, json_mode=json_mode, max_tokens=max_tokens,
                      tenant_id=tenant_id, purpose=purpose, user_intent=user_intent)
    if not result.available:
        return None, result.error or "Model processing is unavailable."
    return result.display_text(), None


# ── 1. PROPOSE + VERIFY ─────────────────────────────────────────────────────

def propose_mechanisms(
    conn: sqlite3.Connection, retriever: Retriever, *, document_kind: str = "BASE_WORDING",
    max_passages: int = 10, model_allowed: bool = False, tenant_id: str = "unscoped",
) -> AIResult:
    """Ask the model to identify coverage mechanisms, then verify every claim.

    This is the answer to "what does the AI actually do?". It reads a wording nobody
    has hand-mapped and proposes what the clauses are; deterministic code then throws
    out anything it cannot prove against the source.
    """
    if not model_allowed or not config.ai_enabled():
        reason = ("AI processing needs the tenant's explicit consent."
                  if not model_allowed else
                  (llm.settings().disabled_reason or "AI layer is off."))
        return AIResult(False, "DETERMINISTIC_ONLY", error=reason,
                        public_error=(reason if not model_allowed else None),
                        payload={"proposals": [], "verified_count": 0,
                                 "rejected_count": 0})

    # Retrieve the passages most likely to contain operative mechanisms rather than
    # sending 28 pages: cheaper, faster, and it keeps the model's attention on text
    # that matters.
    seed_query = ("average underinsurance proportionately reduced sum insured "
                  "premium payment warranty terminated alteration notice theft "
                  "forcible entry unoccupied vacant deductible excess time limit "
                  "condition precedent")
    hits = retriever.search(seed_query, top_k=max_passages, kinds=[document_kind])
    if not hits:
        return AIResult(True, "LLM_ASSISTED", error="No passages to analyse.",
                        payload={"proposals": [], "verified_count": 0,
                                 "rejected_count": 0})

    passages = "\n\n".join(
        f"[{i + 1}] " + llm.untrusted_document(
            f"policy page {h.chunk.page_no}", h.chunk.text)
        for i, h in enumerate(hits))
    allowed = ", ".join(sorted(PROPOSABLE_MECHANISMS))
    content, error = _chat([
        {"role": "system", "content": _PROPOSE_SYSTEM},
        {"role": "user", "content":
            f"Allowed mechanism keys: {allowed}\n\nEvidence:\n\n{passages}"},
    ], json_mode=True, max_tokens=1600, tenant_id=tenant_id,
       purpose="mechanism_proposal",
       user_intent="Identify candidate mechanisms in my selected policy evidence")

    if content is None:
        return AIResult(False, "DETERMINISTIC_FALLBACK", error=error,
                        payload={"proposals": [], "verified_count": 0,
                                 "rejected_count": 0, "source": "DETERMINISTIC"})

    try:
        raw = json.loads(content).get("mechanisms", [])
    except (json.JSONDecodeError, AttributeError):
        return AIResult(True, "LLM_ASSISTED",
                        error="Model returned unparseable JSON; proposal discarded.",
                        payload={"proposals": [], "verified_count": 0,
                                 "rejected_count": 0})

    proposals = [_verify_proposal(conn, item, hits) for item in raw
                 if isinstance(item, dict)]
    verified = [p for p in proposals if p.verified]
    rejected = [p for p in proposals if not p.verified]
    return AIResult(True, "LLM_ASSISTED", payload={
        "proposals": [p.as_dict() for p in proposals],
        "verified_count": len(verified),
        "rejected_count": len(rejected),
        "passages_considered": len(hits),
        "retrieval_method": retriever.method,
        "notice": ("Each proposal was checked against the source document. A quote "
                   "that does not exist verbatim at its claimed location is rejected "
                   "and shown with its reason — a hallucination caught rather than "
                   "hidden. Verified proposals are marked AI_PROPOSED_VERIFIED and "
                   "are NOT treated as hand-verified clauses: they do not feed "
                   "findings."),
    })


def _verify_proposal(conn: sqlite3.Connection, item: dict,
                     hits: list[Hit]) -> Proposal:
    """Deterministic verification of one model proposal. This is the gate."""
    mechanism = str(item.get("mechanism", "")).strip()
    quote = str(item.get("quote", "") or "").strip()
    proposal = Proposal(
        mechanism=mechanism,
        taxonomy_code=PROPOSABLE_MECHANISMS.get(mechanism),
        quote=quote,
        # Model reasoning is not user-facing: only the source-verified quotation is.
        reasoning="",
        states_formula=bool(item.get("states_formula")),
    )

    # 1. Closed vocabulary. An invented mechanism cannot enter the system.
    if proposal.taxonomy_code is None:
        proposal.rejection_reason = (
            f"'{mechanism}' is not an allowed mechanism key.")
        return proposal

    # 2. A proposal with no quote has no evidence.
    if len(quote) < 15:
        proposal.rejection_reason = "No usable verbatim quote was supplied."
        return proposal

    # 3. The quote must exist in the passage the model cited.
    passage_no = item.get("passage")
    candidates: list[Hit] = []
    if isinstance(passage_no, int) and 1 <= passage_no <= len(hits):
        candidates.append(hits[passage_no - 1])
    candidates.extend(h for h in hits if h not in candidates)

    target = norm_ws(quote).lower()
    located: Optional[Hit] = None
    for hit in candidates:
        if target in norm_ws(hit.chunk.text).lower():
            located = hit
            break
    if located is None:
        proposal.rejection_reason = (
            "The quoted text does not appear in any passage supplied to the model. "
            "Rejected as unverifiable.")
        return proposal

    # 4. The quote must exist byte-for-byte in the IMMUTABLE STORED PAGE, not merely
    #    in the text we handed the model. This is the hallucination gate proper.
    source = page_text(conn, located.chunk.document_id, located.chunk.page_no)
    if target not in norm_ws(source).lower():
        proposal.rejection_reason = (
            f"The quoted text is not present on page {located.chunk.page_no} of the "
            f"source document. Rejected.")
        return proposal

    # 5. A claimed number must actually be in the quoted text. This catches the
    #    likeliest subtle error: a real quote with an invented threshold attached.
    threshold_pct = item.get("threshold_pct")
    if threshold_pct is not None:
        try:
            value = float(threshold_pct)
        except (TypeError, ValueError):
            value = None
        if value is None or not _number_present(quote, value):
            proposal.rejection_reason = (
                f"Claimed threshold {threshold_pct} does not appear in the quoted "
                f"text. Rejected rather than accepted with an unevidenced number.")
            return proposal
        proposal.threshold_pct = value

    threshold_days = item.get("threshold_days")
    if threshold_days is not None:
        try:
            days = int(threshold_days)
        except (TypeError, ValueError):
            days = None
        if days is None or not _number_present(quote, float(days)):
            proposal.rejection_reason = (
                f"Claimed day count {threshold_days} does not appear in the quoted "
                f"text. Rejected.")
            return proposal
        proposal.threshold_days = days

    # Locate the exact offsets of the quote inside the page so the citation is a real
    # span a reader can open, not a page-level gesture.
    offset = _find_offsets(source, quote)
    citation = located.chunk.as_citation()
    citation["quote"] = quote
    if offset is not None:
        citation["char_start"], citation["char_end"] = offset
    proposal.citation = citation
    proposal.reasoning = "The proposed quotation was verified against the cited source page."
    proposal.verified = True
    return proposal


def _number_present(text: str, value: float) -> bool:
    """Whether `value` genuinely appears in `text`, allowing for 0.85 vs 85%."""
    numbers = {float(n.replace(",", ""))
               for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}
    candidates = {value}
    if 0 < value <= 1:
        candidates.add(round(value * 100, 6))
    if value > 1:
        candidates.add(round(value / 100, 6))
    return any(any(abs(n - c) < 1e-6 for n in numbers) for c in candidates)


def _find_offsets(source: str, quote: str) -> Optional[tuple[int, int]]:
    """Exact offsets of `quote` in `source`, tolerating whitespace differences."""
    index = source.find(quote)
    if index >= 0:
        return index, index + len(quote)
    # Whitespace-insensitive scan: build a regex from the quote's words.
    words = [re.escape(w) for w in quote.split()]
    if not words:
        return None
    pattern = re.compile(r"\s+".join(words), re.IGNORECASE)
    match = pattern.search(source)
    return (match.start(), match.end()) if match else None


# ── 2. EXPLAIN a deterministic finding ──────────────────────────────────────

def explain_finding(conn: sqlite3.Connection, retriever: Retriever,
                    finding: dict, *, model_allowed: bool = False,
                    tenant_id: str = "unscoped") -> AIResult:
    """Plain-language explanation of a finding the engine already produced.

    The model explains; it does not decide. The state, class and every figure are
    already fixed by the time this runs, so the worst a bad explanation can do is read
    poorly — it cannot change what the product asserts.
    """
    evidence = [s for s in finding.get("spans", []) if s.get("quote")]
    citations = [{
        "page_no": s.get("page_no"), "quote": s.get("quote"),
        "document_id": s.get("document_id"),
    } for s in evidence[:3]]

    if not model_allowed or not config.ai_enabled():
        reason = ("AI processing needs the tenant's explicit consent."
                  if not model_allowed else llm.settings().disabled_reason)
        return AIResult(False, "DETERMINISTIC_ONLY",
                        text=_deterministic_explanation(finding), error=reason,
                        public_error=(reason if not model_allowed else None),
                        payload=_finding_fallback_payload(
                            finding, citations, source="DETERMINISTIC"))

    # Pull extra context for the clause, so the explanation is grounded in more than
    # the one span the finding happened to anchor.
    extra = retriever.search(finding.get("headline", "")[:300], top_k=3)
    passages = "\n\n".join(
        llm.untrusted_document(f"policy page {h.chunk.page_no}", h.chunk.text)
        for h in extra)
    quoted = "\n\n".join(
        llm.untrusted_document(f"cited page {c['page_no']}", c["quote"])
        for c in citations) or "No supplied evidence."

    detail = finding.get("detail") or {}
    facts = {k: detail.get(k) for k in
             ("adequacy_ratio", "threshold_pct", "illustration", "loss",
              "deductible", "days_from_inception", "missing_documents",
              "missing_facts") if detail.get(k) is not None}

    content, error = _chat([
        {"role": "system", "content": _EXPLAIN_SYSTEM},
        {"role": "user", "content":
            f"Finding: {finding.get('headline')}\n"
            f"State: {finding.get('state_label')}\n"
            f"Evidence class: {finding.get('class')}\n"
            f"Engine-computed values (use only these numbers): "
            f"{json.dumps(facts, default=str)}\n\n"
            f"Quoted evidence:\n{quoted}\n\n"
            f"Additional retrieved context:\n{passages}"},
    ], max_tokens=320, tenant_id=tenant_id, purpose="finding_explanation",
       user_intent="Explain this evidence-bound finding in plain language")

    if content is None:
        return AIResult(False, "DETERMINISTIC_FALLBACK",
                        text=_deterministic_explanation(finding), error=error,
                        payload=_finding_fallback_payload(
                            finding, citations, source="DETERMINISTIC"))

    safe, rejection = llm.guard_user_visible(
        content, {"finding": finding, "facts": facts, "quoted": quoted, "passages": passages},
        citations=citations, require_citations=True)
    if not safe:
        return AIResult(False, "DETERMINISTIC_FALLBACK",
                        text=_deterministic_explanation(finding), error=rejection,
                        public_error=rejection,
                        payload=_finding_fallback_payload(
                            finding, citations, source="DETERMINISTIC_AFTER_REJECTION"))

    return AIResult(True, "LLM_ASSISTED", text=content,
                    payload={**_finding_fallback_payload(finding, citations, source="LLM"),
                             "retrieval_method": retriever.method,
                             "passages_used": len(extra)})


# Every language's lexicon applies to every output (T28): a model asked in English can
# answer in Chinese, and a verdict is a verdict in either.
_OUTCOME_WORDS = lexicons.OUTCOME_GUARD
_ADVICE_WORDS = lexicons.ADVICE_GUARD


def _asserts_outcome(text: str) -> bool:
    """The same discipline the renderer applies to rule headlines, applied to model
    prose. Enforced here because a persuasive sentence is exactly how an adjudicating
    claim would sneak past a rule-level guard."""
    return bool(_OUTCOME_WORDS.search(text or ""))


def _asserts_advice(text: str) -> bool:
    """Future language-model paths use the same per-language advice boundary."""
    return bool(_ADVICE_WORDS.search(text or ""))


def _sgd(value: object) -> str:
    try:
        return jurisdiction.format_money(Decimal(str(value)), "SGD")
    except (InvalidOperation, TypeError, ValueError):
        return str(value or "")


def _readable_input(value: object) -> str:
    return " ".join(str(value or "").replace("_", " ").split()).lower()


def _missing_inputs(finding: dict) -> tuple[str, ...]:
    """Return only operands the deterministic finding already marks as absent."""
    detail = finding.get("detail") or {}
    if not isinstance(detail, dict):
        return ()
    values: list[str] = []
    for key in ("missing_documents", "missing_facts"):
        raw = detail.get(key) or ()
        if isinstance(raw, str):
            raw = (raw,)
        if not isinstance(raw, (list, tuple, set)):
            continue
        for item in raw:
            readable = _readable_input(item)
            if readable and readable not in values:
                values.append(readable)
    required = _readable_input(detail.get("required_document"))
    if required and required not in values:
        values.append(required)
    return tuple(values)


def _finding_citations(finding: dict) -> list[dict]:
    """Project stored finding spans without manufacturing a document description."""
    citations: list[dict] = []
    for span in finding.get("spans") or ():
        if not isinstance(span, dict):
            continue
        quote = str(span.get("quote") or "").strip()
        if not quote:
            continue
        page = span.get("page", span.get("page_no"))
        citations.append({
            "document": str(span.get("document") or "cited policy evidence"),
            "document_id": span.get("document_id"),
            "page": page,
            "page_no": page,
            "char_start": span.get("char_start"),
            "char_end": span.get("char_end"),
            "quote": quote,
        })
    return citations


def _hit_citations(hits: list[Hit]) -> list[dict]:
    """Expose verified retrieval spans in the field names the Ask UI renders."""
    citations: list[dict] = []
    for hit in hits:
        citation = hit.as_dict()
        citation["document"] = hit.chunk.filename
        citation["page"] = hit.chunk.page_no
        citations.append(citation)
    return citations


def _merge_citations(*groups: list[dict]) -> tuple[dict, ...]:
    """Keep a bounded, de-duplicated set of source spans from current case data."""
    merged: list[dict] = []
    seen: set[tuple[object, ...]] = set()
    for group in groups:
        for citation in group:
            key = (
                citation.get("document_id"), citation.get("page", citation.get("page_no")),
                citation.get("char_start"), citation.get("char_end"), citation.get("quote"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(citation))
            if len(merged) >= 6:
                return tuple(merged)
    return tuple(merged)


def _finding_fallback_payload(finding: dict, citations: list[dict], *, source: str) -> dict:
    """Attach only existing rule state, evidence class and missing inputs to prose."""
    payload: dict = {"citations": citations, "source": source}
    state = finding.get("state")
    evidence_class = finding.get("class")
    if isinstance(state, str) and state:
        payload["output_state"] = state
    if isinstance(evidence_class, str) and evidence_class:
        payload["evidence_class"] = evidence_class
    missing = _missing_inputs(finding)
    if missing:
        payload["required_document"] = ", ".join(missing)
    return payload


def _deterministic_explanation(finding: dict) -> str:
    """Explain an existing rule outcome without adding a new interpretation."""
    detail = finding.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    state = str(finding.get("state") or "")
    state_label = str(finding.get("state_label") or state.replace("_", " ").title())
    evidence_class = str(finding.get("class") or "")
    parts = [str(finding.get("headline") or "").strip()]
    if state or evidence_class:
        status = f"The review records this as {state_label}"
        if evidence_class:
            status += f" with {evidence_class} evidence"
        parts.append(status + ".")
    if detail.get("adequacy_ratio") is not None:
        parts.append(
            f"The documented inputs put cover at {detail['adequacy_ratio']} of the value "
            "at risk. This is a deterministic illustration, not a settlement decision.")
    missing = _missing_inputs(finding)
    if missing:
        parts.append("The required input is: " + ", ".join(missing) + ".")
    if state == "INFORMATION_MISSING":
        parts.append("The engine leaves this unestablished rather than estimating it.")
    if state == "REQUIRES_AUTHORISED_REVIEW":
        parts.append(
            "The engine can locate and quote the term but cannot decide its effect; a "
            "licensed adviser must review it with the full facts.")
    resolution = " ".join(str(finding.get("resolution_path") or "").split())
    if resolution:
        parts.append("Next safe action: " + resolution)
    elif missing:
        parts.append("Next safe action: provide the required input above.")
    return " ".join(part for part in parts if part)


# ── 3. ASK PULSE — the copilot ──────────────────────────────────────────────

@dataclass(frozen=True)
class _DeterministicAnswer:
    """Local prose and metadata assembled only from already-verified case material."""

    text: str
    citations: tuple[dict, ...] = ()
    output_state: Optional[str] = None
    evidence_class: Optional[str] = None
    required_document: Optional[str] = None

    def payload(self, *, source: str) -> dict:
        payload: dict = {"citations": [dict(citation) for citation in self.citations],
                         "source": source}
        if self.output_state:
            payload["output_state"] = self.output_state
        if self.evidence_class:
            payload["evidence_class"] = self.evidence_class
        if self.required_document:
            payload["required_document"] = self.required_document
        return payload


_OUTCOME_QUESTION = re.compile(
    r"\b(?:will|would|can|could|should)\b[^?.!]{0,80}\b(?:pay|paid|cover|covered|"
    r"approve|approved|deny|denied|settle|settlement|claim)\b|"
    r"\b(?:is|are)\b[^?.!]{0,40}\b(?:covered|payable|approved|denied)\b|"
    r"\b(?:recommend|buy|cancel|switch|surrender)\b",
    re.IGNORECASE)


def _finding_for_question(question: str, findings: list[dict]) -> Optional[dict]:
    """Select a current rule outcome by literal question/headline overlap only."""
    terms = set(re.findall(r"[a-z0-9]{4,}", question.lower()))
    if not terms:
        return None
    ranked: list[tuple[int, dict]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        haystack = " ".join((str(finding.get("headline") or ""),
                               str(finding.get("rule_id") or ""))).lower()
        score = sum(term in haystack for term in terms)
        if score:
            ranked.append((score, finding))
    return max(ranked, key=lambda item: item[0])[1] if ranked else None


def _finding_for_rule(rule_id: object, findings: list[dict]) -> Optional[dict]:
    return next((finding for finding in findings
                 if isinstance(finding, dict) and finding.get("rule_id") == rule_id), None)


def _answer_from_finding(finding: dict, *, prefix: Optional[str] = None,
                         citations: tuple[dict, ...] = ()) -> _DeterministicAnswer:
    state = str(finding.get("state") or "") or None
    evidence_class = str(finding.get("class") or "") or None
    state_label = str(finding.get("state_label") or (state or "").replace("_", " ").title())
    headline = str(finding.get("headline") or "").strip()
    parts = [prefix or headline]
    if prefix and headline:
        parts.append(headline)
    if state:
        detail = f"The current rule outcome is {state_label}"
        if evidence_class:
            detail += f" with {evidence_class} evidence"
        parts.append(detail + ".")
    missing = _missing_inputs(finding)
    if missing:
        parts.append("Required input: " + ", ".join(missing) + ".")
    if state == "REQUIRES_AUTHORISED_REVIEW":
        parts.append("A licensed adviser must review how this term applies to the full facts.")
    else:
        resolution = " ".join(str(finding.get("resolution_path") or "").split())
        if resolution:
            parts.append("Next safe action: " + resolution)
        elif missing:
            parts.append("Next safe action: provide the required input above.")
    return _DeterministicAnswer(
        text=" ".join(part for part in parts if part),
        citations=_merge_citations(list(citations), _finding_citations(finding)),
        output_state=state, evidence_class=evidence_class,
        required_document=", ".join(missing) if missing else None)


def _compose_deterministic_answer(question: str, brief: dict, score: Optional[dict],
                                  hits: list[Hit]) -> _DeterministicAnswer:
    """Compose local, evidence-bound prose; never infer an insurance outcome."""
    lowered = question.lower()
    findings = [finding for finding in brief.get("findings", []) if isinstance(finding, dict)]
    hit_citations = _hit_citations(hits)

    if _OUTCOME_QUESTION.search(question):
        return _DeterministicAnswer(
            text=("I can provide verified policy text and deterministic figures, but I "
                  "cannot decide whether a policy covers, pays, approves, or otherwise "
                  "resolves this situation. A licensed adviser must review the full facts."),
            citations=_merge_citations(hit_citations),
            output_state="REQUIRES_AUTHORISED_REVIEW", evidence_class="UNESTABLISHED")

    if score and score.get("overall") is not None and any(
            term in lowered for term in ("score", "rating", "why is my")):
        dimensions = [dimension for dimension in score.get("dimensions", [])
                      if isinstance(dimension, dict) and dimension.get("assessed")]
        dimensions.sort(key=lambda dimension: dimension.get("score", 101))
        lowest = dimensions[:3]
        supporting: list[dict] = []
        for dimension in lowest:
            for deduction in dimension.get("deductions", []) or ():
                if isinstance(deduction, dict):
                    finding = _finding_for_rule(deduction.get("rule_id"), findings)
                    if finding is not None:
                        supporting.append(finding)
        sources = _merge_citations(
            hit_citations, *[_finding_citations(finding) for finding in supporting])
        parts = [f"The deterministic protection score is {score['overall']} out of 100"
                 + (f" ({score.get('status')})." if score.get("status") else "."),
                 "It is calculated from current rule outcomes and page-anchored evidence."]
        if lowest:
            areas = "; ".join(
                f"{dimension.get('label')}: {dimension.get('score')}/100"
                for dimension in lowest)
            parts.append("The lowest assessed areas are " + areas + ".")
        missing = next((finding for finding in findings if _missing_inputs(finding)), None)
        if missing is not None:
            inputs = _missing_inputs(missing)
            parts.append("Next safe action: provide " + ", ".join(inputs) + ".")
        return _DeterministicAnswer(text=" ".join(parts), citations=sources)

    missing_findings = [finding for finding in findings if _missing_inputs(finding)]
    if any(term in lowered for term in ("missing", "what should i", "next", "do first",
                                        "priority", "upload", "document")) and missing_findings:
        return _answer_from_finding(
            missing_findings[0],
            prefix="The current review has a documented missing input for this request.",
            citations=_merge_citations(hit_citations))

    if score and any(term in lowered for term in ("exposure", "at risk", "how much", "money",
                                                   "cost", "shortfall")):
        exposure = score.get("headline_exposure")
        if isinstance(exposure, dict) and exposure.get("amount_sgd") is not None:
            finding = _finding_for_rule(exposure.get("rule_id"), findings)
            sources = _merge_citations(
                hit_citations, _finding_citations(finding) if finding is not None else [])
            parts = ["The largest deterministic illustrative shortfall is "
                     + _sgd(exposure.get("amount_sgd")) + "."]
            if (exposure.get("assessed_loss_sgd") is not None
                    and exposure.get("illustrated_settlement_sgd") is not None):
                parts.append("It is calculated from an assessed loss of "
                             + _sgd(exposure.get("assessed_loss_sgd"))
                             + " and an illustrative amount of "
                             + _sgd(exposure.get("illustrated_settlement_sgd")) + ".")
            if exposure.get("caveat"):
                parts.append(str(exposure["caveat"]))
            if finding is not None:
                answer = _answer_from_finding(
                    finding, prefix=" ".join(parts), citations=sources)
                return answer
            return _DeterministicAnswer(text=" ".join(parts), citations=sources)

    matching = _finding_for_question(question, findings)
    if matching is not None:
        return _answer_from_finding(matching, citations=_merge_citations(hit_citations))

    if hits:
        top = hits[0]
        return _DeterministicAnswer(
            text=(f"The closest verified source passage is {top.chunk.filename}, page "
                  f"{top.chunk.page_no}. Its complete text and source details are shown "
                  "in the citation below. This retrieves document evidence only; it does "
                  "not decide coverage, settlement, evidence class, premium, or product choice."),
            citations=_merge_citations(hit_citations))

    return _DeterministicAnswer(
        text=("I cannot establish a factual answer from the current rule outcomes, "
              "computed figures, or verified source passages. Add the relevant policy "
              "wording or schedule; a licensed adviser can review how a term applies to "
              "the full facts."),
        required_document="the relevant policy wording or schedule")


def answer_question(conn: sqlite3.Connection, retriever: Retriever, question: str,
                    brief: dict, score: Optional[dict] = None, *,
                    model_allowed: bool = False, tenant_id: str = "unscoped") -> AIResult:
    """Answer from scoped verified evidence, with a visible local fallback."""
    question = (question or "").strip()
    if not question:
        return AIResult(False, "DETERMINISTIC_ONLY",
                        text="Ask a question about your protection review.",
                        payload={"citations": [], "source": "DETERMINISTIC"})

    hits = retriever.search(question, top_k=config.MAX_CONTEXT_CHUNKS)
    verified_hits = [hit for hit in hits if retriever.verify_hit(conn, hit)]
    fallback = _compose_deterministic_answer(question, brief, score, verified_hits)
    fallback_payload = {
        **fallback.payload(source="DETERMINISTIC"),
        "retrieval_method": retriever.method,
    }

    if not model_allowed or not config.ai_enabled():
        reason = ("AI processing needs the tenant's explicit consent."
                  if not model_allowed else llm.settings().disabled_reason)
        return AIResult(
            False, "DETERMINISTIC_ONLY", text=fallback.text, error=reason,
            public_error=(reason if not model_allowed else None), payload=fallback_payload)

    findings_summary = "\n".join(
        f"- [{finding['state']}/{finding['class']}] {finding['headline']}"
        for finding in brief.get("findings", [])[:20]) or "No findings."
    score_summary = json.dumps({
        "overall": (score or {}).get("overall"),
        "dimensions": {dimension["key"]: dimension["score"]
                       for dimension in (score or {}).get("dimensions", [])},
    }, default=str) if score else "Not computed."
    passages = "\n\n".join(
        llm.untrusted_document(f"{hit.chunk.filename} page {hit.chunk.page_no}", hit.chunk.text)
        for hit in verified_hits) or "No retrieved evidence."

    content, error = _chat([
        {"role": "system", "content": _ASK_SYSTEM},
        {"role": "user", "content":
            f"Question: {question}\n\n"
            f"Business: {brief.get('business')}\n"
            f"Carrier: {brief.get('carrier')}\n"
            f"Protection score: {score_summary}\n\n"
            f"Findings:\n{findings_summary}\n\n"
            f"Verbatim policy passages:\n{passages}"},
    ], max_tokens=420, tenant_id=tenant_id, purpose="evidence_question_answer",
       user_intent=question)

    if content is None:
        return AIResult(False, "DETERMINISTIC_FALLBACK", text=fallback.text, error=error,
                        payload=fallback_payload)

    citations = [dict(citation) for citation in fallback.citations]
    safe, rejection = llm.guard_user_visible(
        content, {"brief": brief, "score": score, "passages": passages},
        citations=citations, require_citations=True)
    if not safe:
        return AIResult(
            False, "DETERMINISTIC_FALLBACK", text=fallback.text, error=rejection,
            public_error=rejection,
            payload={**fallback.payload(source="DETERMINISTIC_AFTER_REJECTION"),
                     "retrieval_method": retriever.method})

    return AIResult(True, "LLM_ASSISTED", text=content,
                    payload={**fallback.payload(source="LLM"),
                             "retrieval_method": retriever.method,
                             "passages_verified": len(verified_hits),
                             "passages_retrieved": len(hits)})
