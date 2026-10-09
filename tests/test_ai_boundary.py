"""The AI and retrieval routes obey the same consent boundary as everything else.

WHY THIS FILE EXISTS — A MEASURED LEAK, NOT A HYPOTHETICAL ONE.

`/api/brief` and `/api/score` both carried `_deny_foreign_role`. The four AI/retrieval
routes carried no guard at all. Run against the live server as a CARRIER with no consent
granted, the result was:

    GET  /api/brief            -> 403   correct
    GET  /api/score            -> 403   correct
    GET  /api/evidence/search  -> 200   returned total_sum_insured_sgd: 340000
    POST /api/ai/ask           -> 200   returned the same figures
    POST /api/ai/explain       -> 200   returned the adequacy figures
    POST /api/ai/propose       -> 200   read the wording directly

So a carrier's underwriter could read an SME's stock figure and premises address through the
copilot while being correctly refused the Brief. The product's central claim — that a
carrier never sees one business's documents — was false through four doors.

This is the same defect SHAPE as the case-deletion bug already fixed in this codebase: a
guarantee that holds in the obvious place and fails in the derived ones. So these tests
are written per ROUTE and per ROLE rather than per feature, because the failure was never
in a feature — it was in the set of routes somebody forgot to enumerate.

THE ONE TEST THAT MATTERS MOST is `test_a_carrier_can_never_retrieve_the_schedule_figure`.
It uses the exact query that leaked, and asserts on the exact figure that came back.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven.api import SESSIONS, app

OWNER = {"email": "owner@hockleefoods.demo", "password": "Demo@123"}
ADVISER = {"email": "adviser@lionbrokers.demo", "password": "Demo@123"}
CARRIER = {"email": "underwriter@carrier.demo", "password": "Demo@123"}

# The figure that actually leaked, and the query that leaked it.
LEAKED_FIGURE = "340000"
LEAKED_QUERY = "stock sum insured"

# Every route that reads the case through the AI or retrieval layer. Declared as data so
# a new one cannot be added without either appearing here or failing the route-coverage
# guard in test_api_v2.py.
AI_ROUTES: tuple[tuple[str, str, dict | None], ...] = (
    ("POST", "/api/ai/explain", {"rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD"}),
    ("POST", "/api/ai/propose", None),
    ("POST", "/api/ai/ask", {"question": "what is my stock sum insured"}),
    ("GET", f"/api/evidence/search?q={LEAKED_QUERY.replace(' ', '%20')}", None),
)

ALL_KINDS = ["BASE_WORDING", "SCHEDULE", "ACRA_PROFILE", "ACCOUNTS", "TENANCY",
             "CPF_RECORD", "WIC_SCHEDULE", "WORK_PASS_LIST", "SUPPLIER_INVOICE",
             "ENDORSEMENT"]


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


def _call(client, method: str, path: str, body: dict | None):
    return client.get(path) if method == "GET" else client.post(path, json=body)


def _grant(client, fields=None):
    """Owner grants disclosure to the adviser. Returns the response."""
    client.post("/api/auth/login", json=OWNER)
    return client.post("/api/consent", json={
        "recipient": "Licensed adviser", "fields": fields or ALL_KINDS})


# ── the carrier boundary ────────────────────────────────────────────────────

def test_a_carrier_can_never_retrieve_the_schedule_figure(client):
    """The exact leak, pinned. Query and figure are the real ones from the incident."""
    client.get("/api/brief")
    client.post("/api/auth/login", json=CARRIER)

    response = client.get(f"/api/evidence/search?q={LEAKED_QUERY.replace(' ', '%20')}")
    assert response.status_code == 403, (
        "a carrier reached the retrieval index; this is the original leak")
    assert LEAKED_FIGURE not in response.text, (
        f"the schedule's total sum insured ({LEAKED_FIGURE}) reached a carrier")


@pytest.mark.parametrize("method,path,body", AI_ROUTES)
def test_a_carrier_is_refused_every_ai_route(client, method, path, body):
    """No AI read path around the book-level boundary. All four doors, not the one we
    happened to think of."""
    client.get("/api/brief")
    client.post("/api/auth/login", json=CARRIER)
    response = _call(client, method, path, body)
    assert response.status_code == 403
    assert LEAKED_FIGURE not in response.text


def test_a_carrier_is_refused_even_after_the_owner_disclosed_to_an_adviser(client):
    """Consent names a recipient. A grant to an adviser is not a grant to the world, and
    especially not to the counterparty."""
    client.get("/api/brief")
    assert _grant(client).status_code == 200
    client.post("/api/auth/login", json=CARRIER)
    response = client.get(f"/api/evidence/search?q={LEAKED_QUERY.replace(' ', '%20')}")
    assert response.status_code == 403
    assert LEAKED_FIGURE not in response.text


# ── the adviser boundary ────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", AI_ROUTES)
def test_an_adviser_without_consent_is_refused(client, method, path, body):
    """No grant, no answer. The consent gate is the access path, not a second opinion."""
    client.get("/api/brief")
    client.post("/api/auth/login", json=ADVISER)
    response = _call(client, method, path, body)
    assert response.status_code == 403
    assert LEAKED_FIGURE not in response.text


def test_an_adviser_with_consent_may_use_the_ai(client):
    """The fix must not delete a legitimate capability. With a grant, the adviser works."""
    client.get("/api/brief")
    assert _grant(client).status_code == 200
    client.post("/api/auth/login", json=ADVISER)

    search = client.get(f"/api/evidence/search?q={LEAKED_QUERY.replace(' ', '%20')}")
    assert search.status_code == 200
    body = search.json()
    assert body["scope"] == "CONSENT_SCOPED"
    assert body["scoped_to_document_kinds"], "a scoped search must name its scope"

    explain = client.post("/api/ai/explain",
                          json={"rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD"})
    assert explain.status_code == 200


def test_the_adviser_index_contains_only_disclosed_documents(client):
    """Authorization-FIRST, not filter-after.

    The owner discloses the schedule but NOT the accounts. A search must then be unable
    to reach the accounts at all — not merely decline to show them — because the index
    handed to the ranker never contained them.
    """
    client.get("/api/brief")
    assert _grant(client, fields=["SCHEDULE"]).status_code == 200
    client.post("/api/auth/login", json=ADVISER)

    body = client.get("/api/evidence/search?q=inventory%20stock%20value").json()
    assert body["scope"] == "CONSENT_SCOPED"
    assert body["scoped_to_document_kinds"] == ["SCHEDULE"]
    kinds_returned = {h["document_kind"] for h in body["hits"]}
    assert kinds_returned <= {"SCHEDULE"}, (
        f"undisclosed document kinds reached the adviser: "
        f"{kinds_returned - {'SCHEDULE'}}")
    # The index itself must be smaller, which is what proves the filter ran before the
    # search rather than after it.
    assert body["index"]["documents_indexed"] <= 1


def test_an_undisclosed_finding_is_not_explainable_by_an_adviser(client):
    """A finding withheld by the consent gate is not in the case the adviser may read, so
    the AI cannot narrate it either."""
    client.get("/api/brief")
    # Disclose only the wording, so findings resting on the accounts are withheld.
    assert _grant(client, fields=["BASE_WORDING"]).status_code == 200
    client.post("/api/auth/login", json=ADVISER)
    response = client.post("/api/ai/explain",
                           json={"rule_id": "AVERAGE-ADEQUACY-BELOW-THRESHOLD"})
    assert response.status_code == 404
    assert LEAKED_FIGURE not in response.text


# ── the owner keeps full access ─────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", AI_ROUTES)
def test_the_owner_still_has_full_access(client, method, path, body):
    """The boundary must not cost the owner anything. It is their case."""
    client.get("/api/brief")
    client.post("/api/auth/login", json=OWNER)
    response = _call(client, method, path, body)
    assert response.status_code == 200


def test_the_owner_search_is_not_scoped(client):
    client.get("/api/brief")
    client.post("/api/auth/login", json=OWNER)
    body = client.get(f"/api/evidence/search?q={LEAKED_QUERY.replace(' ', '%20')}").json()
    assert body["scope"] == "FULL_CASE"
    assert body["scoped_to_document_kinds"] is None


def test_anonymous_still_works_because_the_demo_depends_on_it(client):
    """Same asymmetry `_deny_foreign_role` documents: anonymous is the local demo
    surface and the whole offline suite rests on it. Gating it would trade a real
    capability for the appearance of one."""
    client.get("/api/brief")
    assert client.get("/api/evidence/search?q=average").status_code == 200
