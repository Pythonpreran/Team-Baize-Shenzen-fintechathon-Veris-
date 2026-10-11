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
from .lang.lexicons import en as english_lexicon
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
    """Envelope for every AI call, so the caller always knows what it got."""

    available: bool
    mode: str
    text: str = ""
    payload: dict = field(default_factory=dict)
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {"available": self.available, "mode": self.mode, "text": self.text,
                "error": self.error, **self.payload}


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
        return AIResult(True, "LLM_ASSISTED", error=error,
                        payload={"proposals": [], "verified_count": 0,
                                 "rejected_count": 0})

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
        return AIResult(False, "DETERMINISTIC_ONLY",
                        text=_deterministic_explanation(finding),
                        error=("AI processing needs the tenant's explicit consent."
                               if not model_allowed else llm.settings().disabled_reason),
                        payload={"citations": citations, "source": "TEMPLATE"})

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
        return AIResult(True, "LLM_ASSISTED", text=_deterministic_explanation(finding),
                        error=error,
                        payload={"citations": citations, "source": "TEMPLATE"})

    safe, rejection = llm.guard_user_visible(
        content, {"finding": finding, "facts": facts, "quoted": quoted, "passages": passages},
        citations=citations, require_citations=True)
    if not safe:
        return AIResult(True, "LLM_ASSISTED", text=_deterministic_explanation(finding),
                        error=rejection,
                        payload={"citations": citations,
                                 "source": "TEMPLATE_AFTER_REJECTION"})

    return AIResult(True, "LLM_ASSISTED", text=content,
                    payload={"citations": citations, "source": "LLM",
                             "retrieval_method": retriever.method,
                             "passages_used": len(extra)})


_OUTCOME_WORDS = english_lexicon.OUTCOME_PATTERN
_ADVICE_WORDS = english_lexicon.ADVICE_PATTERN


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


def _deterministic_explanation(finding: dict) -> str:
    """Template fallback. Always available, always safe, never adjudicating."""
    detail = finding.get("detail") or {}
    state = finding.get("state")
    parts = [finding.get("headline", "")]
    if detail.get("adequacy_ratio"):
        parts.append(
            f"The documents put your cover at {detail['adequacy_ratio']} of the value "
            f"at risk. Where a policy's average condition applies, a shortfall "
            f"reduces what is paid proportionately.")
    if state == "INFORMATION_MISSING":
        missing = detail.get("missing_documents") or []
        if missing:
            parts.append(
                "This could not be established because these documents were not "
                "provided: " + ", ".join(
                    m.replace("_", " ").lower() for m in missing) + ".")
        parts.append("The engine reports this rather than estimating it.")
    if state == "REQUIRES_AUTHORISED_REVIEW":
        parts.append(
            "The engine locates and quotes the term but does not decide its effect. "
            "That is a judgement for a licensed adviser.")
    return " ".join(p for p in parts if p)


# ── 3. ASK PULSE — the copilot ──────────────────────────────────────────────

def answer_question(conn: sqlite3.Connection, retriever: Retriever, question: str,
                    brief: dict, score: Optional[dict] = None, *,
                    model_allowed: bool = False, tenant_id: str = "unscoped") -> AIResult:
    """Answer a question about THIS case, from this case's evidence only.

    Grounded and bounded: the model gets the findings, the score breakdown and
    retrieved passages, and is told to refuse rather than speculate. Every retrieved
    citation is re-verified against the source before it is returned, so the citations
    under an answer are checkable even if the prose is imperfect.
    """
    question = (question or "").strip()
    if not question:
        return AIResult(False, "DETERMINISTIC_ONLY",
                        text="Ask a question about your protection review.",
                        payload={"citations": []})

    hits = retriever.search(question, top_k=config.MAX_CONTEXT_CHUNKS)
    verified_hits = [h for h in hits if retriever.verify_hit(conn, h)]
    citations = [h.as_dict() for h in verified_hits]

    if not model_allowed or not config.ai_enabled():
        return AIResult(
            False, "DETERMINISTIC_ONLY",
            text=_deterministic_answer(question, brief, score, verified_hits),
            error=("AI processing needs the tenant's explicit consent."
                   if not model_allowed else llm.settings().disabled_reason),
            payload={"citations": citations, "source": "DETERMINISTIC",
                     "retrieval_method": retriever.method})

    findings_summary = "\n".join(
        f"- [{f['state']}/{f['class']}] {f['headline']}"
        for f in brief.get("findings", [])[:20]) or "No findings."
    score_summary = json.dumps({
        "overall": (score or {}).get("overall"),
        "dimensions": {d["key"]: d["score"]
                       for d in (score or {}).get("dimensions", [])},
    }, default=str) if score else "Not computed."
    passages = "\n\n".join(
        llm.untrusted_document(f"{h.chunk.filename} page {h.chunk.page_no}", h.chunk.text)
        for h in verified_hits) or "No retrieved evidence."

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
        return AIResult(
            True, "LLM_ASSISTED",
            text=_deterministic_answer(question, brief, score, verified_hits),
            error=error,
            payload={"citations": citations, "source": "DETERMINISTIC"})

    safe, rejection = llm.guard_user_visible(
        content, {"brief": brief, "score": score, "passages": passages},
        citations=citations, require_citations=True)
    if not safe:
        return AIResult(
            True, "LLM_ASSISTED",
            text=("I can't answer that in those terms. This engine locates and "
                  "quotes what your documents say; whether cover applies is a "
                  "judgement for a licensed adviser. "
                  + _deterministic_answer(question, brief, score, verified_hits)),
            error=rejection,
            payload={"citations": citations, "source": "DETERMINISTIC_AFTER_REJECTION"})

    return AIResult(True, "LLM_ASSISTED", text=content,
                    payload={"citations": citations, "source": "LLM",
                             "retrieval_method": retriever.method,
                             "passages_verified": len(verified_hits),
                             "passages_retrieved": len(hits)})


def _deterministic_answer(question: str, brief: dict, score: Optional[dict],
                          hits: list[Hit]) -> str:
    """A useful answer with no model at all.

    Not a stub: it answers the questions people actually ask by reading the structured
    data the engine already produced. With no key configured, the copilot still works.
    """
    lowered = question.lower()
    findings = brief.get("findings", [])

    if score and any(w in lowered for w in ("score", "72", "why is my", "rating")):
        lines = [f"Your protection score is {score['overall']} out of 100 "
                 f"({score['status']}). It is computed only from findings the engine "
                 f"could evidence. The largest deductions are:"]
        for d in sorted(score.get("dimensions", []), key=lambda x: x["score"])[:3]:
            lines.append(f"- {d['label']}: {d['score']}/100 — {d['reason']}")
        return " ".join(lines)

    if any(w in lowered for w in ("missing", "what should i", "next", "do first",
                                  "priority", "upload")):
        missing: list[str] = []
        for f in findings:
            for doc in (f.get("detail") or {}).get("missing_documents", []) or []:
                if doc not in missing:
                    missing.append(doc)
        if missing:
            return ("The highest-value thing you can do is provide the documents the "
                    "engine could not find: "
                    + ", ".join(m.replace("_", " ").lower() for m in missing)
                    + ". Each one turns an unestablished item into a measured one.")
        return ("Every document the engine needed was present. The remaining items "
                "are ones requiring a licensed adviser's judgement.")

    if any(w in lowered for w in ("exposure", "at risk", "how much", "money",
                                 "cost", "shortfall")):
        for f in findings:
            detail = f.get("detail") or {}
            if detail.get("adequacy_ratio"):
                return (f"On the documents provided, cover sits at "
                        f"{detail['adequacy_ratio']} of the value at risk. The engine "
                        f"illustrates a reduced settlement of "
                        f"{_sgd(detail.get('illustration', '?'))} on an assessed loss of "
                        f"{_sgd(detail.get('loss', '?'))}. That is an illustration, not "
                        f"the amount payable — this policy states the trigger but not "
                        f"the formula.")
        return ("No adequacy shortfall was computed from the documents provided. "
                "Sums insured and a value at risk are both needed to compute one.")

    if hits:
        top = hits[0]
        return (f"The most relevant passage in your documents is on page "
                f"{top.chunk.page_no} of {top.chunk.filename}: "
                f"\u201c{top.chunk.text[:280]}\u2026\u201d. "
                f"There is no AI key configured, so this is a retrieval result rather "
                f"than a written answer.")

    return ("I don't have evidence in your documents to answer that. Upload the "
            "relevant document, or ask a licensed adviser to review it.")
