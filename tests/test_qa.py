"""T10 deterministic cited portfolio Q&A and conversation-scope guards."""
from __future__ import annotations

import base64
import copy
import os

import pytest
from fastapi.testclient import TestClient

import pdfbuild_household
from proven import api, health, llm, persistence, qa

TEMPORAL_QUOTE = (
    "Period: 1 April 2026 to 31 March 2027\n"
    "First policy commencement: 1 April 2026.\n"
    "Continuous coverage before this period: 0 months.\n"
    "Portability or migration credit: 0 months.\n"
    "Sum-insured enhancement: none."
)


def _health_items(document_id: str = "schedule-1") -> list[dict]:
    schedule = {
        "document": "rao-india-health-schedule.pdf",
        "source_document_id": document_id,
        "page": 1,
        "quote": TEMPORAL_QUOTE,
    }
    return [
        {
            "mechanism_code": "WAITING.PED", "state": "DOCUMENTED",
            "evidence_class": "PROVEN", "value": "2029-04-01",
            "citation": {
                "document": "Published policy wording", "page": 8,
                "quote": "A waiting period of thirty-six months applies to the declared condition.",
                "source_url": "https://publisher.example/wording",
            },
            "operand_citations": [schedule, {
                "document": "rao-india-health-schedule.pdf", "source_document_id": document_id,
                "page": 1, "quote": "Lakshmi has declared diabetes.",
            }],
            "member_scope": [{"id": "member-mum", "name": "Lakshmi", "age": "63"}],
            "missing_document_key": "health.document.policy_schedule",
        },
        {
            "mechanism_code": "SHARE.CO_PAYMENT", "state": "DOCUMENTED",
            "evidence_class": "PROVEN", "value": "5", "unit": "%",
            "citation": {
                "document": "Published policy wording", "page": 11,
                "quote": "The insured shall bear five per cent of each admissible claim.",
                "source_url": "https://publisher.example/wording",
            },
            "operand_citations": [schedule],
            "member_scope": [
                {"id": "member-dad", "name": "Raghavan", "age": "67"},
                {"id": "member-mum", "name": "Lakshmi", "age": "63"},
            ],
            "missing_document_key": "health.document.policy_schedule",
        },
        {
            "mechanism_code": "LIMIT.ROOM_RENT", "state": "DOCUMENTED",
            "evidence_class": "PROVEN", "value": "5000", "unit": "INR",
            "display": "MONEY_PER_DAY",
            "citation": {
                "document": "Published policy wording", "page": 6,
                "quote": "Room rent is limited to five thousand rupees per day.",
                "source_url": "https://publisher.example/wording",
            },
            "operand_citations": [schedule],
            "missing_document_key": "health.document.policy_wording",
        },
    ]


def _portfolio(document_id: str = "schedule-1") -> dict:
    return {
        "tenant_id": "tenant-a",
        "members": [
            {"id": "member-dad", "name": "Raghavan", "relationship": "parent",
             "health_facts": None},
            {"id": "member-mum", "name": "Lakshmi", "relationship": "parent",
             "health_facts": "Declared diabetes"},
        ],
        "policies": [{
            "id": "policy-health", "line_of_business": "HEALTH", "country": "IN",
            "source_document_ids": [document_id],
            "insured_member_ids": ["member-dad", "member-mum"],
            "health_items": _health_items(document_id),
        }],
    }


def test_waiting_period_answer_is_deterministic_cited_and_member_scoped():
    answer = qa.answer("When does Mum's diabetes waiting period end?", _portfolio())

    assert answer.mode == "DETERMINISTIC"
    assert answer.source == "DETERMINISTIC"
    assert answer.output_state == "DOCUMENTED"
    assert answer.evidence_class == "PROVEN"
    assert answer.policy_id == "policy-health"
    assert answer.member_id == "member-mum"
    assert "1 April 2029" in answer.text
    assert "will pay" not in answer.text.lower()
    assert answer.citations
    quotes = {citation["quote"] for citation in answer.citations}
    assert TEMPORAL_QUOTE in quotes
    assert "Lakshmi has declared diabetes." in quotes


def test_follow_up_uses_member_scope_without_inventing_a_second_waiting_date():
    first = qa.answer("When does Mum's diabetes waiting period end?", _portfolio())
    follow_up = qa.answer("And Dad's?", _portfolio(), memory=[{
        "policy_id": first.policy_id, "member_id": first.member_id,
        "intent": first.intent,
    }])

    assert follow_up.policy_id == "policy-health"
    assert follow_up.member_id == "member-dad"
    assert follow_up.output_state == "INFORMATION_MISSING"
    assert "2029" not in follow_up.text
    assert follow_up.required_document == "the policy schedule"


def test_common_limit_and_copayment_answers_keep_decimal_evidence_values():
    limit = qa.answer("What is the room rent limit?", _portfolio())
    copay = qa.answer("What co-payment applies?", _portfolio())

    assert limit.output_state == "DOCUMENTED"
    assert "₹5,000 per day" in limit.text
    assert copay.output_state == "DOCUMENTED"
    assert "5%" in copay.text
    assert "Raghavan" in copay.text and "Lakshmi" in copay.text


def test_unknown_and_outcome_requests_abstain_without_a_verdict():
    unknown = qa.answer("What is the surgical network?", _portfolio())
    outcome = qa.answer("Will they pay for knee surgery?", _portfolio())

    assert unknown.output_state == "INFORMATION_MISSING"
    assert unknown.required_document
    assert outcome.output_state == "REQUIRES_AUTHORISED_REVIEW"
    assert "will pay" not in outcome.text.lower()
    assert "not covered" not in outcome.text.lower()


def test_mocked_model_can_only_rephrase_the_deterministic_cited_answer(monkeypatch):
    deterministic = qa.answer("When does Mum's diabetes waiting period end?", _portfolio())
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return llm.ChatResult(
            True,
            content="The schedule records 1 April 2029 as the declared-condition waiting-period end date.",
            profile="mock", model="mock-model", model_version="mock-v1",
        )

    monkeypatch.setattr(qa.llm, "chat", fake_chat)
    modelled = qa.maybe_model_rephrase(
        deterministic, "When does Mum's diabetes waiting period end?",
        model_allowed=True, tenant_id="tenant-a")

    assert calls
    assert modelled.source == "LLM"
    assert modelled.mode == "LLM_ASSISTED"
    assert "1 April 2029" in modelled.text
    assert modelled.output_state == deterministic.output_state
    assert modelled.citations == deterministic.citations


def test_model_outcome_prose_is_discarded_in_favour_of_the_deterministic_answer(monkeypatch):
    deterministic = qa.answer("When does Mum's diabetes waiting period end?", _portfolio())
    monkeypatch.setattr(
        qa.llm, "chat",
        lambda *_args, **_kwargs: llm.ChatResult(True, content="The claim will be paid."))

    rejected = qa.maybe_model_rephrase(
        deterministic, "When does Mum's diabetes waiting period end?",
        model_allowed=True, tenant_id="tenant-a")

    assert rejected.source == "DETERMINISTIC_AFTER_REJECTION"
    assert rejected.text == deterministic.text
    assert "outcome" in (rejected.error or "").lower()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(
        persistence.MASTER_KEY_ENV, base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    monkeypatch.setenv("VERIS_LLM_PROFILE", "none")
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        assert instance.post("/api/auth/login", json={
            "email": "owner@hockleefoods.demo", "password": "Demo@123",
        }).status_code == 200
        yield instance
    api.SESSIONS.clear()


def _seed_portfolio(client: TestClient, monkeypatch) -> tuple[dict, str]:
    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    with session.lock:
        scope = api._portfolio_scope(session)
    repository = api._portfolio_repository()
    document = repository.add_document(
        scope, kind="SCHEDULE", blob=pdfbuild_household.india_health_schedule_pdf(),
        language="en", country="IN", filename="rao-india-health-schedule.pdf")
    dad = repository.add_member(scope, name="Raghavan", relationship="parent")
    mum = repository.add_member(
        scope, name="Lakshmi", relationship="parent", health_facts="Declared diabetes")
    policy = repository.create_policy(
        scope, country="IN", line_of_business="HEALTH", insurer="Sandbox insurer",
        product="Synthetic family health", masked_number="****-HEALTH6", status="ACTIVE",
        period_start="2026-04-01", period_end="2027-03-31",
        insured_member_ids=[dad["id"], mum["id"]], source_document_ids=[document["id"]],
    )

    def enrich(raw: dict) -> dict:
        value = copy.deepcopy(raw)
        selected = next(candidate for candidate in value["policies"]
                        if candidate["id"] == policy["id"])
        selected["health_items"] = _health_items(document["id"])
        selected["health_mapping"] = {"mapped": True}
        return value

    monkeypatch.setattr(health, "enrich_portfolio", enrich)
    return policy, document["id"]


def test_api_stores_redacted_tenant_memory_and_opens_the_cited_portfolio_page(client, monkeypatch):
    policy, document_id = _seed_portfolio(client, monkeypatch)
    answer = client.post("/api/ai/ask", json={
        "question": "When does Lakshmi's diabetes waiting period end?",
        "policy_id": policy["id"],
    })
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["retrieval"] == {
        "method": "POLICY_EVIDENCE", "tenant_scoped": True,
        "policy_id": policy["id"], "member_id": body["scope"]["member_id"],
    }
    local = next(citation for citation in body["citations"]
                 if citation.get("document_id") == document_id)
    assert local["viewer"] == "PORTFOLIO"
    assert local["citation_id"]
    assert local["verified"] is True
    assert local["char_end"] > local["char_start"]

    rendered = client.get(
        f"/api/portfolio/documents/{document_id}/page/1",
        params={"citation_id": local["citation_id"]},
    )
    assert rendered.status_code == 200, rendered.text
    page = rendered.json()
    assert page["quote"] == local["quote"]
    assert (page["char_start"], page["char_end"]) == (local["char_start"], local["char_end"])
    assert page["display"] in {"PAGE", "NONE"}
    assert client.get(
        f"/api/portfolio/documents/{document_id}/page/1",
        params={"citation_id": "not-a-session-citation"},
    ).status_code == 404

    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    memory = api._portfolio_qa_memory(session, "org-hocklee")
    assert memory and memory[-1]["intent"] == "waiting_period"
    assert "Lakshmi" not in memory[-1]["question"]
    assert api._portfolio_qa_memory(session, "another-tenant") == []
