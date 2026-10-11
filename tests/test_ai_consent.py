"""Tenant AI-processing consent and API model-gate coverage."""
from __future__ import annotations

import base64
import json
import os

import pytest
from fastapi.testclient import TestClient

from proven import api, llm, persistence
from proven.retrieval import Retriever

OWNER = {"email": "owner@hockleefoods.demo", "password": "Demo@123"}


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps({"choices": [{"message": {
            "content": "The cited policy wording describes the stated average clause."}}]}).encode("utf-8")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(
        persistence.MASTER_KEY_ENV, base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    monkeypatch.setenv("VERIS_LLM_PROFILE", "none")
    llm.clear_runtime_state()
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        yield instance
    api.SESSIONS.clear()
    llm.clear_runtime_state()


def test_ai_processing_consent_is_owner_scoped_revocable_and_route_covered(client):
    assert client.get("/api/ai/consent").status_code == 401
    assert client.post("/api/auth/login", json=OWNER).status_code == 200

    initial = client.get("/api/ai/consent").json()
    assert initial == {"granted": False, "health_documents": False, "created_at": None}

    granted = client.post("/api/ai/consent", json={"health_documents": False}).json()
    assert granted["granted"] is True
    assert granted["health_documents"] is False
    assert client.get("/api/ai/consent").json()["granted"] is True

    renewed = client.post("/api/ai/consent", json={"health_documents": True}).json()
    assert renewed["granted"] is True
    assert renewed["health_documents"] is True
    assert client.get("/api/ai/consent").json()["health_documents"] is True

    assert client.get("/api/ai/prompt-log").json()["entries"] == []
    revoked = client.post("/api/ai/consent/revoke").json()
    assert revoked["revoked"] == 1
    assert client.get("/api/ai/consent").json()["granted"] is False


def test_model_calls_need_consent_and_health_needs_its_own_flag(client, monkeypatch):
    monkeypatch.setenv("VERIS_LLM_PROFILE", "openai")
    monkeypatch.setenv("VERIS_LLM_API_KEY", "test-provider-token")
    calls: list[object] = []

    def fake_urlopen(request, **_kwargs):
        calls.append(request)
        return _Response()

    # The current product keeps retrieval lexical so document text cannot bypass the
    # redacting provider boundary through an embedding SDK.
    monkeypatch.setattr(Retriever, "ensure_embeddings", lambda _self: False)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    client.post("/api/auth/login", json=OWNER)
    client.get("/api/brief")
    blocked = client.post("/api/ai/ask", json={"question": "What does the average clause say?"}).json()
    assert blocked["source"] == "DETERMINISTIC"
    assert calls == []

    client.post("/api/ai/consent", json={"health_documents": False})
    health = client.post("/api/presentation/compose", json={
        "purpose": "home_overview", "verified_context": {"health": "hospital admission"},
    }).json()
    assert health["mode"] == "DETERMINISTIC_FALLBACK"
    assert "consent" in (health["error"] or "").lower()
    assert calls == []

    client.post("/api/ai/consent", json={"health_documents": True})
    answer = client.post("/api/ai/ask", json={"question": "What does the average clause say?"}).json()
    assert answer["source"] == "LLM"
    assert calls, "consented non-health evidence should reach the shared boundary"
    audit = client.get("/api/ai/prompt-log").json()["entries"]
    assert audit and audit[-1]["purpose"] == "evidence_question_answer"
    assert audit[-1]["token_summary"] is not None


def test_ai_consent_cannot_cross_tenants(tmp_path):
    repo = persistence.PortfolioRepository(tmp_path / "portfolio.sqlite", os.urandom(32))
    first = persistence.TenantScope("tenant-a", "user-a")
    second = persistence.TenantScope("tenant-b", "user-b")

    assert repo.grant_ai_processing_consent(first, health_documents=True)["granted"] is True
    assert repo.ai_processing_consent(first)["health_documents"] is True
    assert repo.ai_processing_consent(second)["granted"] is False
    assert repo.revoke_ai_processing_consent(second)["revoked"] == 0
    assert repo.ai_processing_consent(first)["granted"] is True
