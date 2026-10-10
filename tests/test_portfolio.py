"""Durable tenant portfolio tests.

The portfolio has its own file-backed store. These tests exercise the properties that
cannot be inferred from a schema: encrypted bytes never contain a sensitive marker,
data survives a fresh repository instance, deleting a tenant key makes the ciphertext
unreadable, and every HTTP collection is constrained by the authenticated tenant.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proven import api, auth, persistence


def _key() -> bytes:
    return os.urandom(32)


def _scope(tenant: str, user: str) -> persistence.TenantScope:
    return persistence.TenantScope(
        tenant_id=tenant,
        user_id=user,
        user_name=f"{user} name",
        user_email=f"{user}@example.invalid",
        role=auth.SME,
    )


def _policy_payload(**overrides) -> dict:
    payload = {
        "country": "SG",
        "line_of_business": "HEALTH",
        "insurer": "Lotus General (Sandbox)",
        "product": "Family health plan",
        "masked_number": "****-1234",
        "status": "ACTIVE",
        "coverage_items": [],
    }
    payload.update(overrides)
    return payload


def _all_database_bytes(path: Path) -> bytes:
    return b"".join(part.read_bytes() for part in sorted(path.parent.glob(path.name + "*"))
                    if part.is_file())


def test_development_master_key_initialisation_is_atomic_across_parallel_first_use(
        tmp_path, monkeypatch):
    """Parallel first use must settle on one durable local master key."""
    db_path = tmp_path / "portfolio.sqlite"
    key_path = tmp_path / ".veris-dev-master-key"
    scope = _scope("tenant-race", "user-race")
    workers = 8
    barrier = threading.Barrier(workers)
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(db_path))
    monkeypatch.setenv(persistence.DEV_MASTER_KEY_ENV, str(key_path))
    monkeypatch.delenv(persistence.MASTER_KEY_ENV, raising=False)

    def first_use(index: int) -> dict:
        barrier.wait(timeout=10)
        repo = persistence.repository_from_environment()
        return repo.add_member(scope, name=f"member {index}", relationship="self")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        members = list(executor.map(first_use, range(workers)))

    assert len({member["id"] for member in members}) == workers
    assert key_path.exists()
    restarted = persistence.repository_from_environment()
    assert {member["name"] for member in restarted.list_members(scope)} == {
        f"member {index}" for index in range(workers)}


def test_portfolio_encrypts_sensitive_values_and_survives_a_restart(tmp_path):
    path = tmp_path / "portfolio.sqlite"
    key = _key()
    scope = _scope("tenant-a", "user-a")
    marker = "UNIQUE-HEALTH-MARKER-ALPINE-731"
    repo = persistence.PortfolioRepository(path, key)

    member = repo.add_member(
        scope, name=marker, relationship="parent", date_of_birth="1960-01-01",
        health_facts=marker)
    document = repo.add_document(
        scope, kind="BASE_WORDING", blob=(marker + " source bytes").encode("utf-8"),
        language="en", country="SG", filename="schedule.pdf")
    policy = repo.create_policy(
        scope, **_policy_payload(
            household_id=member["household_id"],
            insured_member_ids=[member["id"]],
            source_document_ids=[document["id"]],
            coverage_items=[{
                "mechanism_code": "HEALTH.WAITING_PERIOD",
                "value": "125000.50",
                "unit": "SGD",
                "evidence_span_ids": ["span-1"],
                "source_document_id": document["id"],
            }],
        ))

    assert policy["coverage_items"][0]["value"] == "125000.50"
    assert policy["coverage_items"][0]["evidence_class"] == "PROVEN"
    assert marker.encode("utf-8") not in _all_database_bytes(path)

    # A fresh repository object has no shared connection or in-memory cache to consult.
    restarted = persistence.PortfolioRepository(path, key)
    loaded = restarted.get_policy(scope, policy["id"])
    assert loaded["insurer"] == "Lotus General (Sandbox)"
    assert loaded["coverage_items"][0]["value"] == "125000.50"
    assert restarted.get_document(scope, document["id"])["blob"] == (
        marker + " source bytes").encode("utf-8")


def test_portfolio_crypto_shredding_deletes_the_tenant_key_not_the_ciphertext(tmp_path):
    path = tmp_path / "portfolio.sqlite"
    key = _key()
    scope = _scope("tenant-shred", "user-shred")
    repo = persistence.PortfolioRepository(path, key)
    document = repo.add_document(
        scope, kind="BASE_WORDING", blob=b"private policy bytes", language="en", country="SG")

    with sqlite3.connect(path) as conn:
        wrapped_key = bytes(conn.execute(
            "SELECT wrapped_key FROM tenant_key WHERE tenant_id=?", (scope.tenant_id,)
        ).fetchone()[0])

    assert repo.crypto_shred(scope)["shredded"] is True
    # Complete local destruction means the wrapped tenant key is absent from the primary
    # database and every SQLite sidecar, not merely absent from the live table.
    assert wrapped_key not in _all_database_bytes(path)
    with pytest.raises(persistence.TenantShredded):
        repo.get_document(scope, document["id"])

    # The encrypted record remains, but the key needed to authenticate/decrypt it is gone.
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM document").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tenant_key").fetchone()[0] == 0


def test_portfolio_tenant_queries_cannot_cross_the_boundary(tmp_path):
    repo = persistence.PortfolioRepository(tmp_path / "portfolio.sqlite", _key())
    tenant_a, tenant_b = _scope("tenant-a", "user-a"), _scope("tenant-b", "user-b")
    member = repo.add_member(tenant_a, name="A member", relationship="self")
    document = repo.add_document(
        tenant_a, kind="BASE_WORDING", blob=b"A document", language="en", country="SG")
    policy = repo.create_policy(
        tenant_a, **_policy_payload(
            household_id=member["household_id"], source_document_ids=[document["id"]]))

    assert repo.list_members(tenant_b) == []
    assert repo.list_policies(tenant_b) == []
    with pytest.raises(persistence.PortfolioNotFound):
        repo.get_document(tenant_b, document["id"])
    with pytest.raises(persistence.PortfolioNotFound):
        repo.get_policy(tenant_b, policy["id"])


def test_portfolio_rejects_floats_and_caller_selected_evidence_classes(tmp_path):
    repo = persistence.PortfolioRepository(tmp_path / "portfolio.sqlite", _key())
    scope = _scope("tenant-money", "user-money")
    document = repo.add_document(
        scope, kind="BASE_WORDING", blob=b"wording", language="en", country="SG")

    with pytest.raises(persistence.PortfolioValidationError, match="never float"):
        repo.create_policy(
            scope, **_policy_payload(source_document_ids=[document["id"]], coverage_items=[{
                "mechanism_code": "HEALTH.LIMIT", "value": 12.5,
                "source_document_id": document["id"],
            }]))
    with pytest.raises(persistence.PortfolioValidationError, match="derived"):
        repo.create_policy(
            scope, **_policy_payload(source_document_ids=[document["id"]], coverage_items=[{
                "mechanism_code": "HEALTH.LIMIT", "value": "12.50",
                "source_document_id": document["id"], "evidence_class": "PROVEN",
            }]))


def test_sme_analysis_is_wrapped_without_retaining_case_findings(tmp_path):
    repo = persistence.PortfolioRepository(tmp_path / "portfolio.sqlite", _key())
    scope = _scope("tenant-sme", "user-sme")
    state = {
        "case_id": "case-existing-sme",
        "brief": {
            "business": "Existing SME Business",
            "carrier": "Lotus General (Sandbox)",
            "jurisdiction": {"detected": "SG"},
            "findings": [{"id": "not-copied"}],
        },
    }
    wrapped = repo.wrap_sme_analysis(scope, state)
    assert wrapped is not None
    assert wrapped["source"] == "SME_ANALYSIS"
    assert wrapped["line_of_business"] == "SME_PACKAGE"
    assert wrapped["coverage_items"] == []
    assert repo.wrap_sme_analysis(scope, state)["id"] == wrapped["id"]


def test_simulated_accounting_amounts_use_decimal_and_serialise_as_text():
    """The existing SME feed is part of the durable bundle, so it follows money rules."""
    from decimal import Decimal

    from proven import connectors

    signal = connectors.signal("SIG-001")
    assert signal is not None
    assert isinstance(signal.amount_sgd, Decimal)
    assert signal.as_dict()["amount_sgd"] == "62000"


def _as_owner(client: TestClient, tenant_id: str, user_id: str) -> None:
    client.get("/api/session")
    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    session.state["_principal"] = auth.Principal(
        id=user_id,
        email=f"{user_id}@example.invalid",
        name=f"{user_id} owner",
        role=auth.SME,
        organisation_id=tenant_id,
        organisation_name=f"{tenant_id} organisation",
        job_title="Owner",
    )


def test_portfolio_routes_are_authenticated_and_tenant_scoped(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.sqlite"
    monkeypatch.setenv("VERIS_PORTFOLIO_DB_PATH", str(path))
    monkeypatch.setenv("VERIS_MASTER_KEY", base64.urlsafe_b64encode(_key()).decode("ascii"))
    api.SESSIONS.clear()
    try:
        with TestClient(api.app) as first, TestClient(api.app) as second:
            # Each new durable route requires an authenticated owner principal.
            assert first.get("/api/portfolio").status_code == 401
            _as_owner(first, "tenant-first", "user-first")
            _as_owner(second, "tenant-second", "user-second")

            created_member = first.post("/api/household/members", json={
                "name": "First tenant member", "relationship": "self",
            }).json()["member"]
            created_asset = first.post("/api/assets", json={
                "kind": "CAR", "name": "First tenant car",
                "household_id": created_member["household_id"],
            }).json()["asset"]
            created_business = first.post("/api/businesses", json={
                "name": "First tenant business", "country": "SG",
            }).json()["business"]
            created_policy_response = first.post("/api/policies", json=_policy_payload(
                business_id=created_business["id"], insured_asset_ids=[created_asset["id"]],
            ))
            assert created_policy_response.status_code == 200
            created_policy = created_policy_response.json()["policy"]

            # GET collections expose only the caller's tenant, and the item route refuses
            # an identifier issued in the other tenant without revealing whether it exists.
            second_portfolio = second.get("/api/portfolio").json()
            assert second_portfolio["members"] == []
            assert second_portfolio["assets"] == []
            assert second_portfolio["businesses"] == []
            assert second_portfolio["policies"] == []
            assert second.get("/api/household/members").json()["members"] == []
            assert second.get("/api/assets").json()["assets"] == []
            assert second.get("/api/businesses").json()["businesses"] == []
            assert second.get("/api/policies").json()["policies"] == []
            assert second.get(f"/api/policies/{created_policy['id']}").status_code == 404

            # Every POST is tenant-guarded too: foreign parent ids cannot be attached.
            assert second.post("/api/household/members", json={
                "name": "Cross tenant", "relationship": "self",
                "household_id": created_member["household_id"],
            }).status_code == 404
            assert second.post("/api/assets", json={
                "kind": "CAR", "name": "Cross tenant car",
                "household_id": created_member["household_id"],
            }).status_code == 404
            assert second.post("/api/policies", json=_policy_payload(
                business_id=created_business["id"],
            )).status_code == 404

            own_business = second.post("/api/businesses", json={
                "name": "Second tenant business", "country": "SG",
            })
            assert own_business.status_code == 200
            assert own_business.json()["business"]["id"] != created_business["id"]

            # Extra input is forbidden, so a caller cannot set an evidence class directly.
            invalid = first.post("/api/policies", json=_policy_payload(coverage_items=[{
                "mechanism_code": "HEALTH.LIMIT", "evidence_class": "PROVEN",
            }]))
            assert invalid.status_code == 422
    finally:
        api.SESSIONS.clear()
