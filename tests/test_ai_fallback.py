"""Offline evidence-grounded fallback behavior for optional model transport."""
from __future__ import annotations

import json
import urllib.error

import pytest

from proven import ai, assistant, auth, llm
from proven.retrieval import Chunk, Retriever


_POLICY_TEXT = "The policy schedule records a waiting period of 30 days."


class _Response:
    def __init__(self, body: dict):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")


class _VerifiedRetriever(Retriever):
    def verify_hit(self, _conn, _hit) -> bool:
        return True


def _retriever() -> Retriever:
    return _VerifiedRetriever([Chunk(
        id=1, document_id="policy-a", document_kind="SCHEDULE",
        filename="policy-schedule.pdf", page_no=3, char_start=0,
        char_end=len(_POLICY_TEXT), text=_POLICY_TEXT,
        tokens=("policy", "schedule", "records", "waiting", "period", "30", "days"),
    )])


def _ask(*, model_allowed: bool = True):
    return ai.answer_question(
        None, _retriever(), "What does the waiting period say?", {"findings": []},
        model_allowed=model_allowed, tenant_id="tenant-fallback")


@pytest.fixture(autouse=True)
def _isolated_llm(monkeypatch):
    llm.clear_runtime_state()
    for name in (
            "VERIS_LLM_PROFILE", "VERIS_LLM_BASE_URL", "VERIS_LLM_MODEL",
            "VERIS_LLM_MODEL_VERSION", "VERIS_LLM_API_KEY", "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "VERIS_LLM_RETRIES",
            "VERIS_LLM_TIMEOUT", "PROVEN_AI_TIMEOUT", "VERIS_LLM_TENANT_TOKEN_BUDGET",
            "VERIS_LLM_GLOBAL_TOKEN_BUDGET"):
        monkeypatch.delenv(name, raising=False)
    yield
    llm.clear_runtime_state()


def _assert_cited_local_answer(result, *, mode: str):
    body = result.as_dict()
    assert body["available"] is False
    assert body["mode"] == mode
    assert body["source"] == "DETERMINISTIC"
    assert body["text"].strip()
    assert body["citations"]
    citation = body["citations"][0]
    assert citation["document"] == "policy-schedule.pdf"
    assert citation["page"] == citation["page_no"] == 3
    assert citation["quote"] == _POLICY_TEXT


def test_no_key_returns_a_cited_local_answer_without_transport(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *args, **kwargs: calls.append(args))

    _assert_cited_local_answer(_ask(), mode="DETERMINISTIC_ONLY")
    assert calls == []


def test_none_profile_uses_the_same_cited_local_answer(monkeypatch):
    calls: list[object] = []
    monkeypatch.setenv("VERIS_LLM_PROFILE", "none")
    monkeypatch.setenv("VERIS_LLM_API_KEY", "test-provider-token")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *args, **kwargs: calls.append(args))

    _assert_cited_local_answer(_ask(), mode="DETERMINISTIC_ONLY")
    assert calls == []


def test_quota_response_stops_retries_and_uses_local_answer(monkeypatch):
    calls: list[object] = []
    monkeypatch.setenv("VERIS_LLM_PROFILE", "openai")
    monkeypatch.setenv("VERIS_LLM_API_KEY", "test-provider-token")
    monkeypatch.setenv("VERIS_LLM_RETRIES", "3")

    def quota(request, **_kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 429, "quota exhausted", None, None)

    monkeypatch.setattr(llm.urllib.request, "urlopen", quota)
    first = _ask()
    _assert_cited_local_answer(first, mode="DETERMINISTIC_FALLBACK")
    assert "quota" in (first.error or "").lower()
    assert first.as_dict()["error"] is None
    assert "quota" not in first.text.lower()
    assert len(calls) == 1, "a confirmed quota failure must not be retried"

    second = _ask()
    assert second.mode == "DETERMINISTIC_FALLBACK"
    assert "circuit" in (second.error or "").lower()
    assert len(calls) == 1, "the quota circuit must prevent another provider call"


@pytest.mark.parametrize(("status", "expected_error"), [
    (401, "authentication"),
    (503, "temporarily unavailable"),
])
def test_http_provider_failures_fall_back_without_a_500(monkeypatch, status, expected_error):
    calls: list[object] = []
    monkeypatch.setenv("VERIS_LLM_PROFILE", "openai")
    monkeypatch.setenv("VERIS_LLM_API_KEY", "test-provider-token")
    monkeypatch.setenv("VERIS_LLM_RETRIES", "0")

    def failed(request, **_kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, status, "simulated", None, None)

    monkeypatch.setattr(llm.urllib.request, "urlopen", failed)
    result = _ask()

    _assert_cited_local_answer(result, mode="DETERMINISTIC_FALLBACK")
    assert expected_error in (result.error or "").lower()
    assert len(calls) == 1


def test_timeout_and_empty_model_response_use_local_answer(monkeypatch):
    monkeypatch.setenv("VERIS_LLM_PROFILE", "openai")
    monkeypatch.setenv("VERIS_LLM_API_KEY", "test-provider-token")
    monkeypatch.setenv("VERIS_LLM_RETRIES", "0")

    monkeypatch.setattr(
        llm.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    timed_out = _ask()
    _assert_cited_local_answer(timed_out, mode="DETERMINISTIC_FALLBACK")
    assert "timed out" in (timed_out.error or "").lower()

    llm.clear_runtime_state()
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda *_args, **_kwargs: _Response({"choices": [{"message": {"content": ""}}]}))
    malformed = _ask()
    _assert_cited_local_answer(malformed, mode="DETERMINISTIC_FALLBACK")
    assert "empty" in (malformed.error or "").lower()


def test_missing_evidence_abstains_and_names_the_required_document():
    result = ai.answer_question(
        None, _VerifiedRetriever([]), "What is the earthquake excess?", {"findings": []},
        model_allowed=True, tenant_id="tenant-fallback")

    body = result.as_dict()
    assert body["mode"] == "DETERMINISTIC_ONLY"
    assert body["source"] == "DETERMINISTIC"
    assert body["citations"] == []
    assert body["required_document"] == "the relevant policy wording or schedule"
    assert "cannot establish" in body["text"].lower()


def test_finding_explanation_keeps_existing_state_and_missing_input_without_a_model():
    finding = {
        "headline": "The policy schedule does not establish the deductible.",
        "state": "INFORMATION_MISSING",
        "state_label": "Information missing",
        "class": "UNESTABLISHED",
        "detail": {"missing_documents": ["POLICY_SCHEDULE"]},
        "resolution_path": "Obtain the current policy schedule.",
        "spans": [{
            "document_id": "policy-a", "page_no": 3,
            "quote": _POLICY_TEXT,
        }],
    }

    result = ai.explain_finding(None, _retriever(), finding, model_allowed=True,
                                tenant_id="tenant-fallback")
    body = result.as_dict()
    assert body["available"] is False
    assert body["mode"] == "DETERMINISTIC_ONLY"
    assert body["source"] == "DETERMINISTIC"
    assert body["output_state"] == "INFORMATION_MISSING"
    assert body["evidence_class"] == "UNESTABLISHED"
    assert body["required_document"] == "policy schedule"
    assert "obtain the current policy schedule" in body["text"].lower()


def test_navigation_keeps_the_deterministic_role_scoped_route_after_timeout(monkeypatch):
    calls: list[object] = []
    monkeypatch.setenv("VERIS_LLM_PROFILE", "openai")
    monkeypatch.setenv("VERIS_LLM_API_KEY", "test-provider-token")
    monkeypatch.setenv("VERIS_LLM_RETRIES", "0")
    monkeypatch.setenv("VERIS_LLM_TENANT_TOKEN_BUDGET", "50000")

    def timeout(request, **_kwargs):
        calls.append(request)
        raise TimeoutError()

    monkeypatch.setattr(llm.urllib.request, "urlopen", timeout)
    answer = assistant.answer("where is cyber", auth.SME, model_allowed=True,
                              tenant_id="tenant-fallback")

    assert len(calls) == 1
    assert answer["mode"] == "DETERMINISTIC"
    assert answer["route"]["id"] == "cyber"
    assert answer["route"]["view"].startswith("sme-")
