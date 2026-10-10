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
    fresh_portfolio = client.get("/api/portfolio")
    assert fresh_portfolio.status_code == 200
    assert fresh_portfolio.json()["household"]["provenance"] == {
        "kind": "SYNTHETIC_EXAMPLE", "seed_id": household.SEED_ID,
    }


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
