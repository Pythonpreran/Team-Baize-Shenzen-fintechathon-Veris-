"""T06 household portfolio flow: real upload, durable cards and cited wording."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pdfbuild_household
from proven import api, household


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIS_PORTFOLIO_DB_PATH", str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(
        "VERIS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    api.SESSIONS.clear()
    with TestClient(api.app) as test_client:
        login = test_client.post("/api/auth/login", json={
            "email": "owner@hockleefoods.demo", "password": "Demo@123",
        })
        assert login.status_code == 200
        yield test_client
    api.SESSIONS.clear()


def _household_parts() -> list:
    return [
        ("files", (name, builder(), "application/pdf"))
        for name, builder in pdfbuild_household.BUILDERS.items()
    ]


def _load_household(client: TestClient) -> dict:
    # The portfolio API wraps the existing SME case before the real household upload
    # replaces that transient case. This is the same sequence the one-click UI takes.
    assert client.post("/api/reset").status_code == 200
    assert client.get("/api/portfolio").status_code == 200
    upload = client.post("/api/bundle", files=_household_parts(), data={
        "business_name": "Rao household (synthetic)",
        "kinds": json.dumps([None, "SCHEDULE", "SCHEDULE", "SCHEDULE", "SCHEDULE", "SCHEDULE"]),
    })
    assert upload.status_code == 200, upload.text
    loaded = client.post("/api/portfolio/example-household")
    assert loaded.status_code == 200, loaded.text
    return loaded.json()["portfolio"]


def _remove_household_fixture_metadata(client: TestClient, household_id: str) -> None:
    """Create the exact durable shape written before T06 persisted seed metadata."""
    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    with session.lock:
        scope = api._portfolio_scope(session)
    repository = api._portfolio_repository()
    with repository._connection() as conn:
        key = repository._ensure_scope(conn, scope)
        household_row = repository._row_for_tenant(
            conn, "household", household_id, scope.tenant_id)
        household_detail = repository._open_json(
            key, scope.tenant_id, f"household:{household_id}",
            household_row["details_encrypted"])
        household_detail.pop("provenance", None)
        conn.execute(
            "UPDATE household SET details_encrypted=? WHERE id=? AND tenant_id=?",
            (repository._seal_json(key, scope.tenant_id, f"household:{household_id}",
                                   household_detail), household_id, scope.tenant_id),
        )
        policy_rows = conn.execute(
            "SELECT * FROM policy WHERE tenant_id=? AND household_id=?",
            (scope.tenant_id, household_id),
        ).fetchall()
        for policy in policy_rows:
            detail = repository._open_json(
                key, scope.tenant_id, f"policy:{policy['id']}", policy["details_encrypted"])
            detail.pop("_seed_binding", None)
            conn.execute(
                "UPDATE policy SET details_encrypted=? WHERE id=? AND tenant_id=?",
                (repository._seal_json(key, scope.tenant_id, f"policy:{policy['id']}", detail),
                 policy["id"], scope.tenant_id),
            )


def _make_unmarked_legacy_sme_bundle(client: TestClient) -> None:
    """Recreate the source-less durable demo shape written before T06 metadata."""
    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    with session.lock:
        scope = api._portfolio_scope(session)
    repository = api._portfolio_repository()
    with repository._connection() as conn:
        key = repository._ensure_scope(conn, scope)
        row = conn.execute(
            "SELECT p.id AS policy_id, p.details_encrypted AS policy_details_encrypted, "
            "b.id AS business_id, b.details_encrypted AS business_details_encrypted "
            "FROM policy p JOIN business b ON b.id=p.business_id AND b.tenant_id=p.tenant_id "
            "WHERE p.tenant_id=? AND p.line_of_business='SME_PACKAGE'",
            (scope.tenant_id,),
        ).fetchone()
        assert row is not None
        business_detail = repository._open_json(
            key, scope.tenant_id, f"business:{row['business_id']}",
            row["business_details_encrypted"])
        policy_detail = repository._open_json(
            key, scope.tenant_id, f"policy:{row['policy_id']}",
            row["policy_details_encrypted"])
        business_detail.pop("_sme_bundle_origin", None)
        policy_detail.pop("_sme_bundle_origin", None)
        legacy_case_id = "legacy-sme-demo-case"
        conn.execute(
            "UPDATE business SET source_case_id=?, details_encrypted=? WHERE id=? AND tenant_id=?",
            (legacy_case_id, repository._seal_json(
                key, scope.tenant_id, f"business:{row['business_id']}", business_detail),
             row["business_id"], scope.tenant_id),
        )
        conn.execute(
            "UPDATE policy SET source_case_id=?, details_encrypted=? WHERE id=? AND tenant_id=?",
            (legacy_case_id, repository._seal_json(
                key, scope.tenant_id, f"policy:{row['policy_id']}", policy_detail),
             row["policy_id"], scope.tenant_id),
        )


@pytest.mark.requires_corpus("sg-chubb-selectplus-wording")
def test_household_example_uses_the_uploaded_pdfs_and_is_idempotent(client):
    manifest = client.get("/api/example/household/manifest").json()
    assert [item["name"] for item in manifest["documents"]] == list(household.document_names())
    assert manifest["count"] == len(pdfbuild_household.BUILDERS)
    for item in manifest["documents"]:
        served = client.get("/api/example/household/file/" + item["name"])
        assert served.status_code == 200
        assert served.content == pdfbuild_household.BUILDERS[item["name"]]()

    first = _load_household(client)
    assert {member["name"] for member in first["members"]} == {
        "Arjun Rao", "Meera Rao", "Anya Rao", "Raghavan", "Lakshmi",
    }
    assert first["household"]["provenance"] == {
        "kind": "SYNTHETIC_EXAMPLE", "seed_id": household.SEED_ID,
    }
    assert {asset["kind"] for asset in first["assets"]} == {"CAR", "HOME_CONTENTS"}
    assert len(first["policies"]) == len(household.POLICIES) + 1

    # The current session contains the household upload, but it is never wrapped as a
    # business. The business card remains the separately wrapped existing SME analysis.
    durable = client.get("/api/portfolio").json()
    assert [business["name"] for business in durable["businesses"]] == ["Hock Lee Foods Pte Ltd"]
    assert len(durable["policies"]) == len(household.POLICIES) + 1

    second = client.post("/api/portfolio/example-household")
    assert second.status_code == 200
    repeated = second.json()["portfolio"]
    assert len(repeated["members"]) == len(first["members"])
    assert len(repeated["assets"]) == len(first["assets"])
    assert len(repeated["policies"]) == len(first["policies"])

    # This is the final reset the actual browser flow performs after persisting the
    # household. It restores the SME case without making a second SME bundle.
    assert client.post("/api/reset").status_code == 200
    after_reset = client.get("/api/portfolio").json()
    assert [business["name"] for business in after_reset["businesses"]] == ["Hock Lee Foods Pte Ltd"]
    assert len(after_reset["policies"]) == len(household.POLICIES) + 1
    assert sum(policy["source"] == "SME_ANALYSIS" for policy in after_reset["policies"]) == 1


@pytest.mark.requires_corpus("sg-chubb-selectplus-wording")
def test_household_evidence_endpoint_returns_only_the_cited_synthetic_wording(client):
    portfolio = _load_household(client)
    policy = next(policy for policy in portfolio["policies"]
                  if policy["masked_number"] == "****-HEALTH6")
    item = next(item for item in policy["coverage_items"]
                if item["mechanism_code"] == "LIMIT.SUM_INSURED")
    evidence = client.get(
        f"/api/policies/{policy['id']}/coverage/{item['id']}/evidence")
    assert evidence.status_code == 200
    body = evidence.json()
    assert body["available"] is True
    assert body["evidence_class"] in {
        "PROVEN", "DECLARED", "MARKET_CONVENTION", "UNESTABLISHED",
    }
    assert body["document"] == "rao-india-health-schedule.pdf"
    assert body["quote"] == "Synthetic family-floater sum insured: INR 500,000."

    # An ordinary caller can copy visible policy fields, but cannot create the internal
    # fixture binding or point the resulting card at unrelated synthetic wording.
    other_document_id = next(
        candidate["source_document_id"]
        for candidate_policy in portfolio["policies"]
        for candidate in candidate_policy["coverage_items"]
        if candidate.get("source_document_id") != item["source_document_id"]
    )
    imitation = client.post("/api/policies", json={
        "country": policy["country"],
        "line_of_business": policy["line_of_business"],
        "insurer": policy["insurer"],
        "product": "Ordinary policy",
        "masked_number": policy["masked_number"],
        "status": "ACTIVE",
        "source_document_ids": [other_document_id],
        "coverage_items": [{
            "mechanism_code": item["mechanism_code"],
            "source_document_id": other_document_id,
        }],
    })
    assert imitation.status_code == 200, imitation.text
    imitation_policy = imitation.json()["policy"]
    assert imitation_policy["source"] == "PORTFOLIO"
    imitation_item = imitation_policy["coverage_items"][0]
    refused = client.get(
        f"/api/policies/{imitation_policy['id']}/coverage/{imitation_item['id']}/evidence")
    assert refused.status_code == 404


def test_household_synthetic_provenance_survives_a_fresh_session(client):
    _load_household(client)
    client.cookies.clear()
    login = client.post("/api/auth/login", json={
        "email": "owner@hockleefoods.demo", "password": "Demo@123",
    })
    assert login.status_code == 200
    # A new session has no browser-local household suppression marker. The durable
    # server-stamped demo origin keeps the SME demonstration to one bundle.
    assert client.post("/api/reset").status_code == 200
    fresh_portfolio = client.get("/api/portfolio")
    assert fresh_portfolio.status_code == 200
    body = fresh_portfolio.json()
    assert body["household"]["provenance"] == {
        "kind": "SYNTHETIC_EXAMPLE", "seed_id": household.SEED_ID,
    }
    assert [business["name"] for business in body["businesses"]] == ["Hock Lee Foods Pte Ltd"]
    assert sum(policy["source"] == "SME_ANALYSIS" for policy in body["policies"]) == 1


def test_unmarked_legacy_demo_requests_reseed_without_changing_records(client):
    _load_household(client)
    _make_unmarked_legacy_sme_bundle(client)
    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    with session.lock:
        scope = api._portfolio_scope(session)
    repository = api._portfolio_repository()
    before = repository.portfolio(scope)
    before_business_ids = {business["id"] for business in before["businesses"]}
    before_policy_ids = {policy["id"] for policy in before["policies"]}

    # A new browser session has no household-flow suppression marker, so it exercises
    # the same demo restoration route that encounters the durable legacy bundle.
    client.cookies.clear()
    login = client.post("/api/auth/login", json={
        "email": "owner@hockleefoods.demo", "password": "Demo@123",
    })
    assert login.status_code == 200
    assert client.post("/api/reset").status_code == 200
    rejected = client.get("/api/portfolio")
    assert rejected.status_code == 409
    error = rejected.json()["error"]
    assert error["code"] == "LEGACY_SME_BUNDLE_REQUIRES_RESEED"
    message = error["message"]
    assert "left unchanged" in message
    assert "new local portfolio" in message
    assert "reload the synthetic household" in message

    after = repository.portfolio(scope)
    assert {business["id"] for business in after["businesses"]} == before_business_ids
    assert {policy["id"] for policy in after["policies"]} == before_policy_ids


def test_complete_legacy_household_backfills_trusted_fixture_metadata(client):
    portfolio = _load_household(client)
    household_id = portfolio["household"]["id"]
    _remove_household_fixture_metadata(client, household_id)

    legacy = client.get("/api/portfolio")
    assert legacy.status_code == 200
    assert legacy.json()["household"]["provenance"] is None
    assert all(policy["source"] == "PORTFOLIO"
               for policy in legacy.json()["policies"] if policy["household_id"] == household_id)

    upgraded = client.post("/api/portfolio/example-household")
    assert upgraded.status_code == 200, upgraded.text
    rebuilt = upgraded.json()["portfolio"]
    assert rebuilt["household"]["provenance"] == {
        "kind": "SYNTHETIC_EXAMPLE", "seed_id": household.SEED_ID,
    }
    policy = next(policy for policy in rebuilt["policies"]
                  if policy["masked_number"] == "****-HEALTH6")
    item = next(item for item in policy["coverage_items"]
                if item["mechanism_code"] == "LIMIT.SUM_INSURED")
    evidence = client.get(f"/api/policies/{policy['id']}/coverage/{item['id']}/evidence")
    assert evidence.status_code == 200
    assert evidence.json()["quote"] == "Synthetic family-floater sum insured: INR 500,000."


def test_household_seed_refuses_bytes_that_do_not_match_the_generated_documents(client):
    upload = client.post("/api/bundle", files=_household_parts(), data={
        "business_name": "Rao household (synthetic)",
        "kinds": json.dumps([None, "SCHEDULE", "SCHEDULE", "SCHEDULE", "SCHEDULE", "SCHEDULE"]),
    })
    assert upload.status_code == 200
    session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
    assert session is not None
    first = next(row for row in session.state["intake"] if row["accepted"])
    session.state[api._SOURCE_BYTES_KEY][first["document_id"]] = b"not the generated PDF"
    refused = client.post("/api/portfolio/example-household")
    assert refused.status_code == 409


def test_portfolio_screen_has_a_standard_card_and_only_real_routes():
    root = Path(__file__).resolve().parents[1]
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    for view in ("sme-portfolio", "sme-ask", "sme-claims"):
        assert f'"{view}"' in script
    assert "function planLayoutCard(" in script
    assert "function loadHouseholdExample(" in script
    assert 'data-act="portfolio-evidence"' in script
    assert "portfolioSyntheticBanner(portfolio)" in script
    assert "HOUSEHOLD_EXAMPLE_FLAG" not in script
    for section in ("covered", "limits", "waiting", "share", "exclusions", "renewal", "evidence"):
        assert f"portfolio.section.{section}" in script
    assert '"/api/portfolio"' in script
    assert '"/api/portfolio/example-household"' in script


@pytest.mark.requires_corpus(
    "in-arogya-sanjeevani-policy-wording",
    "sg-medishield-life-information-booklet",
    "sg-singlife-shield-standard-plan-wording",
)
def test_household_health_cards_are_mapped_from_their_cited_wordings(client):
    _load_household(client)
    portfolio_response = client.get("/api/portfolio")
    assert portfolio_response.status_code == 200, portfolio_response.text
    portfolio = portfolio_response.json()
    india = next(policy for policy in portfolio["policies"]
                 if policy["masked_number"] == "****-HEALTH6")
    assert india["health_mapping"]["mapped"] is True
    terms = {item["mechanism_code"]: item for item in india["health_items"]}
    assert terms["WAITING.PED"]["value"] == "2029-04-01"
    assert terms["WAITING.PED"]["member_scope"] == [{
        "id": india["insured_member_ids"][1], "name": "Lakshmi", "age": "63",
    }]
    assert {operand["quote"] for operand in terms["WAITING.PED"]["operand_citations"]} == {
        "Period: 1 April 2026 to 31 March 2027", "Lakshmi has declared diabetes.",
    }
    assert terms["LIMIT.ROOM_RENT"]["value"] == "5000"
    assert terms["LIMIT.ICU"]["value"] == "10000"
    assert terms["SHARE.CO_PAYMENT"]["value"] == "5"
    assert [member["name"] for member in terms["SHARE.CO_PAYMENT"]["member_scope"]] == [
        "Raghavan", "Lakshmi",
    ]
    assert terms["CONDITION.MORATORIUM"]["value"] == "2031-04-01"
    assert terms["LIMIT.FAMILY_FLOATER_SHARED"]["scope_reason_key"] == "health.scope.family_floater"
    assert all(item["citation"]["page"] >= 1 for item in terms.values())

    singlife = next(policy for policy in portfolio["policies"]
                    if policy["masked_number"] == "****-ISP2")
    singlife_codes = {item["mechanism_code"] for item in singlife["health_items"]}
    assert {"CONDITION.WARD_ENTITLEMENT", "LIMIT.ROOM_BOARD", "LIMIT.ICU",
            "SHARE.DEDUCTIBLE", "SHARE.CO_INSURANCE", "CONDITION.PRO_RATION"} <= singlife_codes
