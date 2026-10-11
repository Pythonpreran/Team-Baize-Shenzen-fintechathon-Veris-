"""Deterministic, evidence-bound portfolio questions.

This module is deliberately downstream of the portfolio and health mappers.  It selects
already-derived policy facts and their citations; it never decides an outcome, creates an
evidence class, or fills a missing operand.  An optional model may only rephrase the
selected deterministic answer after the shared grounding gate accepts it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from . import llm

_OUTPUT_STATES = frozenset({
    "DOCUMENTED", "NOT_CONFIRMED", "INFORMATION_MISSING",
    "REQUIRES_AUTHORISED_REVIEW",
})
_EVIDENCE_CLASSES = frozenset({
    "PROVEN", "DECLARED", "MARKET_CONVENTION", "UNESTABLISHED",
})

_OUTCOME_OR_ADVICE = re.compile(
    r"\b(?:will|would|can|should)\b[^?.!]{0,100}\b(?:pay|paid|cover|covered|"
    r"approve|approved|buy|cancel|switch|surrender|recommend)\b|"
    r"\b(?:recommend|buy|cancel|switch|surrender)\b",
    re.IGNORECASE,
)

_MODEL_SYSTEM = (
    "Restate the supplied deterministic, evidence-bound insurance answer in one short "
    "sentence. Do not add any number, date, condition, conclusion, advice, coverage "
    "decision, or claim-payment statement. The citations are rendered separately."
)


@dataclass(frozen=True)
class Answer:
    """One owner-facing answer whose facts and evidence were selected deterministically."""

    text: str
    output_state: str
    evidence_class: str
    citations: tuple[dict[str, Any], ...] = ()
    required_document: Optional[str] = None
    policy_id: Optional[str] = None
    member_id: Optional[str] = None
    intent: str = "unknown"
    mode: str = "DETERMINISTIC"
    source: str = "DETERMINISTIC"
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "mode": self.mode,
            "source": self.source,
            "text": self.text,
            "error": self.error,
            "output_state": self.output_state,
            "evidence_class": self.evidence_class,
            "citations": [dict(citation) for citation in self.citations],
            "required_document": self.required_document,
            "intent": self.intent,
            "scope": {"policy_id": self.policy_id, "member_id": self.member_id},
        }


def _normalise(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _valid_state(value: object) -> str:
    state = str(value or "")
    return state if state in _OUTPUT_STATES else "INFORMATION_MISSING"


def _valid_class(value: object) -> str:
    evidence_class = str(value or "")
    return evidence_class if evidence_class in _EVIDENCE_CLASSES else "UNESTABLISHED"


def _document_name(item: Mapping[str, Any]) -> str:
    key = str(item.get("missing_document_key") or "").lower()
    if "schedule" in key:
        return "the policy schedule"
    if "wording" in key:
        return "the policy wording"
    return "the policy schedule or policy wording"


def _citation(raw: Mapping[str, Any], evidence_class: str) -> Optional[dict[str, Any]]:
    quote = str(raw.get("quote") or "").strip()
    if not quote:
        return None
    page = raw.get("page", raw.get("page_no"))
    try:
        page_number = int(page) if page is not None else None
    except (TypeError, ValueError):
        page_number = None
    document_id = raw.get("source_document_id", raw.get("document_id"))
    return {
        "document": str(raw.get("document") or raw.get("filename") or "policy evidence"),
        "document_id": str(document_id) if document_id else None,
        "page": page_number,
        "quote": quote,
        "source_url": str(raw.get("source_url") or "") or None,
        "evidence_class": evidence_class,
    }


def _citations(item: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    evidence_class = _valid_class(item.get("evidence_class"))
    values: list[dict[str, Any]] = []
    primary = item.get("citation")
    if isinstance(primary, Mapping):
        citation = _citation(primary, evidence_class)
        if citation is not None:
            values.append(citation)
    operands = item.get("operand_citations")
    if isinstance(operands, Sequence) and not isinstance(operands, (str, bytes)):
        for operand in operands:
            if isinstance(operand, Mapping):
                citation = _citation(operand, evidence_class)
                if citation is not None:
                    values.append(citation)
    return tuple(values)


def _policy_members(policy: Mapping[str, Any], members: Mapping[str, Mapping[str, Any]]) -> list[dict]:
    ids = policy.get("insured_member_ids")
    if not isinstance(ids, list):
        return []
    return [dict(members[member_id]) for member_id in ids
            if isinstance(member_id, str) and isinstance(members.get(member_id), Mapping)]


def _member_from_question(question: str, members: Sequence[Mapping[str, Any]],
                          previous_member_id: Optional[str]) -> Optional[dict]:
    text = _normalise(question)
    for member in members:
        name = _normalise(member.get("name"))
        first = name.split(" ", 1)[0] if name else ""
        if name and (re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text)
                     or (len(first) >= 3 and re.search(
                         r"(?<!\w)" + re.escape(first) + r"(?!\w)", text))):
            return dict(member)

    mother_words = ("mum", "mom", "mother")
    father_words = ("dad", "father")
    if any(re.search(r"(?<!\w)" + word + r"(?!\w)", text) for word in mother_words):
        candidates = [member for member in members if member.get("health_facts")]
        if len(candidates) == 1:
            return dict(candidates[0])
    if any(re.search(r"(?<!\w)" + word + r"(?!\w)", text) for word in father_words):
        candidates = [member for member in members
                      if _normalise(member.get("relationship")) == "parent"
                      and member.get("id") != previous_member_id]
        without_health_fact = [member for member in candidates if not member.get("health_facts")]
        if len(without_health_fact) == 1:
            return dict(without_health_fact[0])
        if len(candidates) == 1:
            return dict(candidates[0])
    return None


def _intent(question: str) -> str:
    text = _normalise(question)
    if _OUTCOME_OR_ADVICE.search(text):
        return "authorised_review"
    if any(term in text for term in ("wait", "diabetes", "pre-existing", "ped")):
        return "waiting_period"
    if any(term in text for term in ("co-pay", "copay", "co payment")):
        return "co_payment"
    if any(term in text for term in ("deadline", "intimation", "notify", "notice period")):
        return "deadline"
    if any(term in text for term in ("document", "paperwork", "what do i need", "what do we need")):
        return "documents"
    if any(term in text for term in ("limit", "room rent", "icu", "sum insured", "sub-limit")):
        return "limit"
    return "unknown"


def _select_scope(question: str, portfolio: Mapping[str, Any], memory: Sequence[Mapping[str, Any]],
                  policy_id: Optional[str], member_id: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
    policies = [dict(policy) for policy in portfolio.get("policies", [])
                if isinstance(policy, Mapping)]
    members_by_id = {
        str(member.get("id")): dict(member)
        for member in portfolio.get("members", [])
        if isinstance(member, Mapping) and member.get("id")
    }
    previous = next((entry for entry in reversed(memory)
                     if isinstance(entry, Mapping)
                     and entry.get("policy_id") in {policy.get("id") for policy in policies}), {})
    previous_policy_id = previous.get("policy_id") if isinstance(previous, Mapping) else None
    previous_member_id = previous.get("member_id") if isinstance(previous, Mapping) else None

    selected_member = members_by_id.get(member_id) if member_id else None
    all_members = list(members_by_id.values())
    if selected_member is None and not member_id:
        selected_member = _member_from_question(question, all_members, previous_member_id)

    selected_policy = next((policy for policy in policies if policy.get("id") == policy_id), None)
    if selected_policy is None and not policy_id and previous_policy_id:
        selected_policy = next((policy for policy in policies if policy.get("id") == previous_policy_id), None)

    health_policies = [policy for policy in policies
                       if _normalise(policy.get("line_of_business")) == "health"]
    candidates = health_policies
    if selected_member is not None:
        candidates = [policy for policy in candidates
                      if selected_member.get("id") in (policy.get("insured_member_ids") or [])]
    if selected_policy is None:
        waiting_candidates = [policy for policy in candidates if any(
            item.get("mechanism_code") == "WAITING.PED"
            for item in policy.get("health_items", []) if isinstance(item, Mapping))]
        if len(waiting_candidates) == 1:
            selected_policy = waiting_candidates[0]
        elif len(candidates) == 1:
            selected_policy = candidates[0]

    if selected_policy is not None and selected_member is not None:
        insured_ids = selected_policy.get("insured_member_ids") or []
        if selected_member.get("id") not in insured_ids:
            selected_member = None
    if selected_policy is not None and selected_member is None:
        selected_member = _member_from_question(
            question, _policy_members(selected_policy, members_by_id), previous_member_id)
    return selected_policy, selected_member


def _money(value: object, unit: object) -> Optional[str]:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    text = format(amount, "f")
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    whole, dot, fraction = text.partition(".")
    if str(unit).upper() == "INR":
        if len(whole) > 3:
            tail, head = whole[-3:], whole[:-3]
            groups: list[str] = []
            while head:
                groups.append(head[-2:])
                head = head[:-2]
            whole = ",".join(reversed(groups)) + "," + tail
        return "₹" + sign + whole + (dot + fraction if dot else "")
    if str(unit).upper() == "SGD":
        whole = f"{int(whole):,}"
        return "S$" + sign + whole + (dot + fraction if dot else "")
    return sign + whole + (dot + fraction if dot else "") + (f" {unit}" if unit else "")


def _date(value: object) -> Optional[str]:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def _item_for_intent(policy: Mapping[str, Any], intent: str) -> Optional[dict]:
    items = [dict(item) for item in policy.get("health_items", []) if isinstance(item, Mapping)]
    wanted = {
        "waiting_period": ("WAITING.PED", "WAITING.INITIAL", "WAITING.SPECIFIC"),
        "co_payment": ("SHARE.CO_PAYMENT",),
        "deadline": ("DEADLINE.CLAIM_INTIMATION",),
        "limit": ("LIMIT.ROOM_RENT", "LIMIT.ICU", "LIMIT.SUM_INSURED"),
    }.get(intent, ())
    for code in wanted:
        item = next((candidate for candidate in items
                     if candidate.get("mechanism_code") == code), None)
        if item is not None:
            return item
    return None


def _missing(text: str, *, intent: str, policy_id: Optional[str], member_id: Optional[str],
             item: Optional[Mapping[str, Any]] = None) -> Answer:
    citations = _citations(item) if item is not None else ()
    return Answer(
        text=text,
        output_state="INFORMATION_MISSING",
        evidence_class="UNESTABLISHED",
        citations=citations,
        required_document=_document_name(item or {}),
        policy_id=policy_id,
        member_id=member_id,
        intent=intent,
    )


def _answer_item(item: Mapping[str, Any], *, intent: str, policy: Mapping[str, Any],
                 member: Optional[Mapping[str, Any]]) -> Answer:
    policy_id = str(policy.get("id") or "") or None
    member_id = str(member.get("id") or "") if member else None
    state = _valid_state(item.get("state"))
    evidence_class = _valid_class(item.get("evidence_class"))
    citations = _citations(item)
    value = item.get("value")
    if state != "DOCUMENTED" or value in (None, ""):
        return _missing(
            "The cited policy materials do not establish that value. Add the document "
            "named below rather than relying on a default.",
            intent=intent, policy_id=policy_id, member_id=member_id, item=item)

    if intent == "waiting_period":
        when = _date(value)
        if when is None:
            return _missing(
                "The cited policy materials do not establish a waiting-period end date.",
                intent=intent, policy_id=policy_id, member_id=member_id, item=item)
        member_name = str(member.get("name") or "this member") if member else "this member"
        return Answer(
            text=f"The declared-condition waiting period recorded for {member_name} ends on {when}.",
            output_state=state, evidence_class=evidence_class, citations=citations,
            policy_id=policy_id, member_id=member_id, intent=intent)

    if intent == "co_payment":
        names = [str(scoped.get("name") or "") for scoped in item.get("member_scope", [])
                 if isinstance(scoped, Mapping) and scoped.get("name")]
        people = ", ".join(names) if names else "the insured members named in the schedule"
        return Answer(
            text=f"The cited wording records a {value}% co-payment condition for {people}.",
            output_state=state, evidence_class=evidence_class, citations=citations,
            policy_id=policy_id, member_id=member_id, intent=intent)

    if intent == "deadline":
        unit = str(item.get("unit") or "")
        return Answer(
            text=f"The cited wording records a claim-intimation timing of {value} {unit}.".strip(),
            output_state=state, evidence_class=evidence_class, citations=citations,
            policy_id=policy_id, member_id=member_id, intent=intent)

    amount = _money(value, item.get("unit"))
    if amount is None:
        return _missing(
            "The cited policy materials do not establish a limit value.",
            intent=intent, policy_id=policy_id, member_id=member_id, item=item)
    qualifier = " per day" if item.get("display") == "MONEY_PER_DAY" else ""
    return Answer(
        text=f"The cited wording records a limit of {amount}{qualifier}.",
        output_state=state, evidence_class=evidence_class, citations=citations,
        policy_id=policy_id, member_id=member_id, intent=intent)


def answer(question: str, portfolio: Mapping[str, Any], *, memory: Sequence[Mapping[str, Any]] = (),
           policy_id: Optional[str] = None, member_id: Optional[str] = None) -> Answer:
    """Answer one policy question only from the selected tenant policy and member evidence."""
    cleaned = " ".join(str(question or "").split())[:500]
    intent = _intent(cleaned)
    if intent == "unknown" and re.match(r"^\s*(?:and|what about|how about|his|her|their)\b", cleaned, re.I):
        prior = next((entry for entry in reversed(memory)
                      if isinstance(entry, Mapping)
                      and entry.get("intent") in {
                          "waiting_period", "co_payment", "deadline", "limit", "documents"}), None)
        if prior is not None:
            intent = str(prior["intent"])
    policy, member = _select_scope(cleaned, portfolio, memory, policy_id, member_id)
    selected_policy_id = str(policy.get("id") or "") if policy else None
    selected_member_id = str(member.get("id") or "") if member else None

    if intent == "authorised_review":
        return Answer(
            text=("I can quote the relevant policy terms, but I cannot make a coverage or "
                  "claim outcome decision. A licensed adviser can review that question "
                  "with the full facts."),
            output_state="REQUIRES_AUTHORISED_REVIEW", evidence_class="UNESTABLISHED",
            policy_id=selected_policy_id, member_id=selected_member_id, intent=intent)

    if policy is None:
        return _missing(
            "I could not identify one policy from the documents available here. Add the "
            "policy schedule or select the policy that should answer this question.",
            intent=intent, policy_id=None, member_id=selected_member_id)

    if intent == "documents":
        missing_items = [item for item in policy.get("health_items", [])
                         if isinstance(item, Mapping)
                         and _valid_state(item.get("state")) == "INFORMATION_MISSING"]
        if missing_items:
            document = _document_name(missing_items[0])
            return _missing(
                f"The next document needed to establish this policy detail is {document}.",
                intent=intent, policy_id=selected_policy_id, member_id=selected_member_id,
                item=missing_items[0])
        return _missing(
            "The available documents do not establish a further checklist item. The policy "
            "schedule and policy wording are the documents that would answer a missing detail.",
            intent=intent, policy_id=selected_policy_id, member_id=selected_member_id)

    if intent == "unknown":
        return _missing(
            "I could not locate a documented answer for that question. The policy schedule "
            "or policy wording would establish the relevant detail.",
            intent=intent, policy_id=selected_policy_id, member_id=selected_member_id)

    item = _item_for_intent(policy, intent)
    if item is None:
        return _missing(
            "The policy materials available here do not establish that term. Add the policy "
            "wording that names it rather than relying on a general convention.",
            intent=intent, policy_id=selected_policy_id, member_id=selected_member_id)

    if intent == "waiting_period" and member is not None:
        scoped = {str(entry.get("id")) for entry in item.get("member_scope", [])
                  if isinstance(entry, Mapping) and entry.get("id")}
        if scoped and str(member.get("id")) not in scoped:
            return _missing(
                "The cited schedule does not establish a declared-condition waiting period "
                f"for {member.get('name')}. Add that member's policy schedule if one exists.",
                intent=intent, policy_id=selected_policy_id, member_id=selected_member_id,
                item=item)
    if intent == "waiting_period" and member is None:
        return _missing(
            "The policy materials do not identify which insured member this waiting-period "
            "question concerns. Add or select the policy schedule for that member.",
            intent=intent, policy_id=selected_policy_id, member_id=None, item=item)
    return _answer_item(item, intent=intent, policy=policy, member=member)


def maybe_model_rephrase(answer: Answer, question: str, *, model_allowed: bool,
                          tenant_id: str) -> Answer:
    """Use a model only to rephrase a documented deterministic answer under the shared gate."""
    if (not model_allowed or answer.output_state != "DOCUMENTED" or not answer.citations):
        return answer
    evidence = {
        "deterministic_answer": answer.text,
        "citations": [dict(citation) for citation in answer.citations],
    }
    result = llm.chat(
        [{"role": "system", "content": _MODEL_SYSTEM},
         {"role": "user", "content": (
             f"Question: {question}\n\nDeterministic answer: {answer.text}\n\n"
             f"Verified citations: {answer.citations}")}],
        max_tokens=180, purpose="portfolio_question_answer", tenant_id=tenant_id,
        user_intent=question)
    if not result.available:
        return replace(answer, error=result.error)
    text = result.display_text().strip()
    safe, reason = llm.guard_user_visible(
        text, evidence, citations=answer.citations, require_citations=True,
        trusted_evidence=answer.text)
    if not safe:
        return replace(answer, source="DETERMINISTIC_AFTER_REJECTION", error=reason)
    return replace(answer, text=text, mode="LLM_ASSISTED", source="LLM")


def redacted_memory_question(question: str, members: Sequence[Mapping[str, Any]]) -> str:
    """Keep a structural turn marker, never a recoverable copy of user text.

    The selected intent and opaque tenant policy/member ids are enough for a follow-up
    such as "And Dad's?".  Retaining no lexical question text is stronger than trying
    to recognise every possible name or identifier a person might type.
    """
    _ = question, members
    return "[REDACTED_CONVERSATION_TURN]"
