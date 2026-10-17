"""T25 demo reset: tenant-scoped, idempotent, synthetic-only, and never an owner upload."""
from __future__ import annotations

import base64
import json
import os

import pdfbuild_household
import pytest
from fastapi.testclient import TestClient

from proven import api, auth, household, household_loader, persistence, sandbox, security

CONFIRM = {"confirm": "RESET_SYNTHETIC_EXAMPLE"}
OWNER = {"email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox.operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    monkeypatch.setenv("VERIS_MODE", "local")
    api.SESSIONS.clear()
    with TestClient(api.app) as instance:
        assert instance.post("/api/auth/login", json=OWNER).status_code == 200
        yield instance
    api.SESSIONS.clear()


def _scope(client: TestClient) -> persistence.TenantScope:
    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    with session.lock:
        return api._portfolio_scope(session)


def _load_example(client: TestClient) -> dict:
    """The browser's path: upload the generated PDFs through the pipeline, then persist."""
    kinds = [None if spec.kind == "SELF_DECLARATION" else spec.kind for spec in household.DOCUMENTS]
    upload = client.post("/api/bundle", files=[
        ("files", (name, pdfbuild_household.BUILDERS[name](), "application/pdf"))
        for name in household.document_names()
    ], data={"business_name": household.HOUSEHOLD_NAME, "kinds": json.dumps(kinds)})
    assert upload.status_code == 200, upload.text
    loaded = client.post("/api/portfolio/example-household")
    assert loaded.status_code == 200, loaded.text
    return loaded.json()["portfolio"]


def _photo(client: TestClient, claim_id: str, kind: str = "HOSPITAL_BILL") -> str:
    uploaded = client.post(f"/api/claims/{claim_id}/documents", files=[
        ("files", ("bill-photo.jpg", b"\xff\xd8\xff\xe0owner-photo-bytes", "image/jpeg")),
        ("kinds", (None, kind)),
    ])
    assert uploaded.status_code == 200, uploaded.text
    return uploaded.json()["documents"][0]["id"]


def _intake(client: TestClient, policy_id: str) -> dict:
    created = client.post("/api/claims/intake", json={
        "policy_id": policy_id, "incident_at": "2026-10-10T09:00:00+05:30", "mode": "REIMBURSEMENT"})
    assert created.status_code == 200, created.text
    return created.json()["claim"]


def test_reset_removes_exactly_the_example_and_a_second_reset_removes_nothing(client):
    _load_example(client)
    reset = client.post("/api/demo/reset", json=CONFIRM)
    assert reset.status_code == 200, reset.text
    removed = reset.json()["removed"]
    assert removed["households"] == 1
    assert removed["members"] == len(household.MEMBERS)
    assert removed["assets"] == len(household.ASSETS)
    assert removed["policies"] == len(household.POLICIES)
    assert removed["documents"] == len(household.DOCUMENTS)
    after = client.get("/api/portfolio").json()
    assert after["household"] is None
    assert after["members"] == [] and after["assets"] == [] and after["policies"] == []

    again = client.post("/api/demo/reset", json=CONFIRM)
    assert again.status_code == 200
    assert all(value == 0 for value in again.json()["removed"].values())

    ledger = client.get("/api/agent/ledger").json()
    kinds = [entry.get("kind") for entry in ledger["records"]]
    assert kinds.count("DEMO_RESET") == 1, "a reset that removes nothing writes no entry"
    assert ledger["verification"]["valid"] is True


def test_reset_keeps_every_owner_record_and_upload_mixed_into_the_example(client):
    portfolio = _load_example(client)
    scope = _scope(client)
    repository = api._portfolio_repository()

    # The owner's own material, overlapping the example wherever it can: a document with
    # an example FILENAME but different bytes, a policy and a member that land in the
    # example household by default, a business, and claim photos on an owner policy and
    # on an example policy.
    own_document = repository.add_document(
        scope, kind="SCHEDULE", blob=b"%PDF-1.4 owner schedule, not the generated example",
        language="en", country="SG", filename="rao-home-contents-schedule.pdf")
    own_policy = repository.create_policy(
        scope, country="SG", line_of_business="HEALTH", insurer="Owner Insurer",
        product="Owner health plan", masked_number="****-OWN1", status="ACTIVE",
        period_start="2026-01-01", period_end="2026-12-31",
        source_document_ids=[own_document["id"]])
    own_member = client.post("/api/household/members", json={"name": "Owner Cousin", "relationship": "cousin"})
    assert own_member.status_code == 200, own_member.text
    own_business = client.post("/api/businesses", json={"name": "Owner Bakery", "country": "SG"})
    assert own_business.status_code == 200, own_business.text
    own_claim = _intake(client, own_policy["id"])
    own_photo = _photo(client, own_claim["id"])
    example_health = next(policy for policy in portfolio["policies"]
                          if policy["masked_number"] == "****-HEALTH6")
    example_claim = _intake(client, example_health["id"])
    example_claim_photo = _photo(client, example_claim["id"])

    reset = client.post("/api/demo/reset", json=CONFIRM)
    assert reset.status_code == 200, reset.text
    body = reset.json()
    removed = body["removed"]
    assert removed["households"] == 0
    assert body["households_kept_for_owner_records"] == 1
    assert removed["members"] == len(household.MEMBERS)
    assert removed["policies"] == len(household.POLICIES)
    assert removed["documents"] == len(household.DOCUMENTS)
    assert removed["claims"] == 1
    assert body["kept_owner_uploads"] == 2

    after = client.get("/api/portfolio").json()
    assert after["household"]["provenance"] is None, "the kept household is no longer the example"
    assert [member["name"] for member in after["members"]] == ["Owner Cousin"]
    assert after["assets"] == []
    assert [policy["id"] for policy in after["policies"]] == [own_policy["id"]]
    assert [business["name"] for business in after["businesses"]] == ["Owner Bakery"]
    claims = client.get("/api/claims").json()["claims"]
    assert [claim["id"] for claim in claims] == [own_claim["id"]]
    # Every owner upload survives, including the photo attached to the example's claim
    # and the owner document that merely shares an example filename.
    for document_id in (own_document["id"], own_photo, example_claim_photo):
        assert repository.get_document(scope, document_id)["blob"]


def test_reset_can_be_reloaded_into_the_same_example(client):
    _load_example(client)
    assert client.post("/api/demo/reset", json=CONFIRM).status_code == 200
    reloaded = _load_example(client)
    assert len(reloaded["policies"]) == len(household.POLICIES)
    assert reloaded["household"]["provenance"] == {"kind": "SYNTHETIC_EXAMPLE", "seed_id": household.SEED_ID}


def test_reset_refuses_and_changes_nothing_when_an_owner_policy_insures_an_example_member(client):
    portfolio = _load_example(client)
    arjun = next(member for member in portfolio["members"] if member["name"] == "Arjun Rao")
    owned = client.post("/api/policies", json={
        "country": "SG", "line_of_business": "HEALTH", "insurer": "Owner Insurer",
        "product": "Owner plan", "masked_number": "****-OWN2", "status": "ACTIVE",
        "insured_member_ids": [arjun["id"]]})
    assert owned.status_code == 200, owned.text
    before = client.get("/api/portfolio").json()
    refused = client.post("/api/demo/reset", json=CONFIRM)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "SYNTHETIC_RESET_BLOCKED"
    after = client.get("/api/portfolio").json()
    assert {m["id"] for m in after["members"]} == {m["id"] for m in before["members"]}
    assert {p["id"] for p in after["policies"]} == {p["id"] for p in before["policies"]}
    assert after["household"]["provenance"] == before["household"]["provenance"]


def test_reset_needs_the_exact_confirmation(client):
    _load_example(client)
    assert client.post("/api/demo/reset", json={"confirm": "yes"}).status_code == 422
    assert client.post("/api/demo/reset", json={}).status_code == 422
    assert len(client.get("/api/portfolio").json()["policies"]) == len(household.POLICIES)


def test_reset_never_reaches_another_tenant(client):
    _load_example(client)
    repository = api._portfolio_repository()
    other = persistence.TenantScope(tenant_id="tenant-other", user_id="user-other",
                                    user_name="Other", role="SME")
    sources = {name: builder() for name, builder in pdfbuild_household.BUILDERS.items()}
    household_loader.load_example(repository, other, sources)
    assert client.post("/api/demo/reset", json=CONFIRM).status_code == 200
    untouched = repository.portfolio(other)
    assert len(untouched["members"]) == len(household.MEMBERS)
    assert len(untouched["policies"]) == len(household.POLICIES)
    assert untouched["household"]["provenance"]["kind"] == "SYNTHETIC_EXAMPLE"


def test_reset_withdraws_unconfirmed_actions_on_example_claims(client):
    portfolio = _load_example(client)
    assert client.post("/api/agent/consent", json={
        "scopes": ["agent:read", "agent:prepare", "agent:act"]}).status_code == 200
    example_health = next(policy for policy in portfolio["policies"]
                          if policy["masked_number"] == "****-HEALTH6")
    claim = _intake(client, example_health["id"])
    discharge = _photo(client, claim["id"], "HOSPITAL_DISCHARGE_SUMMARY")
    bill = _photo(client, claim["id"], "HOSPITAL_BILL")
    packet = client.post(f"/api/claims/{claim['id']}/packet", json={
        "document_ids": [*example_health["source_document_ids"], discharge, bill]})
    assert packet.status_code == 200, packet.text
    assert client.post(f"/api/claims/{claim['id']}/ready").status_code == 200
    pending = client.post(f"/api/claims/{claim['id']}/submit").json()["pending_action"]
    assert client.get("/api/agent/confirmations").json()["confirmations"]

    reset = client.post("/api/demo/reset", json=CONFIRM)
    assert reset.status_code == 200, reset.text
    assert reset.json()["removed"]["pending_confirmations_withdrawn"] == 1
    assert client.get("/api/agent/confirmations").json()["confirmations"] == []
    late = client.post("/api/agent/confirm", json={
        "action_id": pending["id"], "payload_hash": pending["payload_hash"],
        "password": auth.DEMO_PASSWORD})
    assert late.status_code in {404, 409}


@pytest.fixture()
def hosted_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIS_MODE", "hosted")
    monkeypatch.setenv(auth.HOSTED_AUTH_DB_ENV, str(tmp_path / "hosted-auth.sqlite"))
    monkeypatch.setenv(auth.HOSTED_INVITES_ENV, json.dumps({"judge-invite": "SME"}))
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, base64.urlsafe_b64encode(b"p" * 32).decode("ascii"))
    monkeypatch.setenv(security.RECORD_HMAC_KEY_ENV, "test-only-record-integrity-material-32-bytes")
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_LOG_ENV, str(tmp_path / "sandbox-operations.jsonl"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, base64.urlsafe_b64encode(b"s" * 32).decode("ascii"))
    api.SESSIONS.clear()
    auth.LOGIN_RATE_LIMITER.clear()
    with TestClient(api.app, base_url="https://testserver") as instance:
        yield instance
    api.SESSIONS.clear()
    auth.LOGIN_RATE_LIMITER.clear()


def _csrf(client: TestClient) -> dict[str, str]:
    assert client.get("/api/auth/csrf").status_code == 200
    return {security.CSRF_HEADER: client.cookies.get(security.CSRF_COOKIE)}


def test_hosted_reset_needs_a_signed_in_owner_and_the_csrf_token(hosted_client):
    assert hosted_client.post("/api/demo/reset", json=CONFIRM).status_code == 401
    headers = _csrf(hosted_client)
    assert hosted_client.post("/api/auth/signup", headers=headers, json={
        "email": "judge@example.test", "password": "long-enough-test-password",
        "invite_code": "judge-invite", "name": "Judge Account"}).status_code == 200
    assert hosted_client.post("/api/auth/login", headers=headers, json={
        "email": "judge@example.test", "password": "long-enough-test-password"}).status_code == 200
    assert hosted_client.post("/api/demo/reset", json=CONFIRM).status_code == 403
    # The refusal rotates the CSRF cookie, so the token is read after it.
    headers = _csrf(hosted_client)
    allowed = hosted_client.post("/api/demo/reset", json=CONFIRM, headers=headers)
    assert allowed.status_code == 200, allowed.text
    assert all(value == 0 for value in allowed.json()["removed"].values())
