"""T26 home and travel, India and Singapore: structural mapping, findings, boundaries."""
from __future__ import annotations

import base64
import dataclasses
import json
import os
from decimal import Decimal
from pathlib import Path

import pdfbuild_household
import pytest
from fastapi.testclient import TestClient

from proven import (align, api, auth, corpus, health, home_travel, household,
                    household_loader, ingest, motor, persistence, protection_map, sandbox)

HOME_SG = "sg-aig-homes-advantage-wording"
HOME_IN = "in-hdfc-ergo-bharat-griha-raksha-wording"
TRAVEL_SG = "sg-aig-travel-guard-direct-wording"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def portfolio(tmp_path, monkeypatch):
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    repository = persistence.repository_from_environment()
    scope = persistence.TenantScope("tenant-t26", "user-t26", role="SME")
    loaded = household_loader.load_example(
        repository, scope, {name: build() for name, build in pdfbuild_household.BUILDERS.items()})
    return home_travel.enrich_portfolio(motor.enrich_portfolio(health.enrich_portfolio(loaded)))


def _policy(portfolio: dict, masked: str) -> dict:
    return next(policy for policy in portfolio["policies"] if policy["masked_number"] == masked)


def _items(policy: dict) -> dict:
    return {item["mechanism_code"]: item for item in policy["property_travel_items"]}


def test_every_fixture_quote_is_verbatim_in_its_generated_pdf():
    texts = {name: "\n".join(ingest.extract_pdf_pages(build(), name))
             for name, build in pdfbuild_household.BUILDERS.items()}
    for spec in household.POLICIES:
        text = texts[spec.document_filename]
        for coverage in spec.coverage:
            assert coverage.wording in text, (spec.masked_number, coverage.wording)
        for _key, quote in spec.schedule_quotes:
            assert quote in text, (spec.masked_number, quote)
    contents = household.CONTENTS_LIST
    for line in (*contents.item_lines, contents.total_line):
        assert line in texts[contents.document_filename]


@pytest.mark.requires_corpus(HOME_SG)
def test_singapore_home_terms_are_bound_from_sentence_structure(portfolio):
    home = _policy(portfolio, "****-HOME3")
    assert home["property_travel_mapping"]["mapped"] is True
    items = _items(home)
    assert {"COVERED.HOME_CONTENTS_CLAUSE", "LIMIT.SINGLE_ARTICLE", "CONDITION.SUM_INSURED_BASIS",
            "CONDITION.INSURANCE_TO_VALUE", "CONDITION.THEFT_FORCIBLE_ENTRY", "CONDITION.UNOCCUPANCY",
            "DEADLINE.CLAIM_NOTICE", "CONDITION.POLICE_REPORT"} <= set(items)
    assert items["CONDITION.UNOCCUPANCY"]["value"] == "60"
    assert items["CONDITION.UNOCCUPANCY"]["structural_mechanism"] == "COND.UNOCCUPANCY"
    # The existing forcible-entry theft mechanism, reused rather than reinvented.
    assert items["CONDITION.THEFT_FORCIBLE_ENTRY"]["taxonomy_code"] == "EXCL.THEFT_FORCIBLE_ENTRY"
    assert items["DEADLINE.CLAIM_NOTICE"]["value"] == "7"
    assert (items["LIMIT.SINGLE_ARTICLE"]["value"], items["LIMIT.SINGLE_ARTICLE"]["unit"]) == ("10000", "SGD")
    pages = [text for _page, text in health._pages(HOME_SG)]
    flat = [" ".join(text.split()) for text in pages]
    for item in items.values():
        citation = item["citation"]
        assert citation["quote"] in flat[citation["page"] - 1], item["mechanism_code"]
        assert item["evidence_class"] in {"PROVEN", "DECLARED", "UNESTABLISHED"}


@pytest.mark.requires_corpus(HOME_SG)
def test_contents_below_the_owners_list_is_a_declared_illustration_with_both_operands(portfolio):
    gap = _items(_policy(portfolio, "****-HOME3"))["LIMIT.CONTENTS_BELOW_LIST"]
    assert gap["state"] == "DOCUMENTED"
    assert gap["evidence_class"] == "DECLARED", "one operand is the owner's own list"
    assert (gap["value"], gap["unit"]) == ("120000", "SGD")
    assert gap["params"] == {"sum_insured": "300000", "contents_list": "420000",
                             "proportion_percent": "71.43"}
    assert gap["illustration"] is True
    assert Decimal(gap["params"]["contents_list"]) - Decimal(gap["params"]["sum_insured"]) == Decimal(gap["value"])
    documents = {citation["document"] for citation in gap["operand_citations"]}
    assert documents == {"rao-home-contents-schedule.pdf", "rao-household-profile.pdf"}
    assert "proportion of the sum insured to the actual replacement value" in gap["citation"]["quote"]


@pytest.mark.requires_corpus(HOME_SG)
def test_a_list_whose_lines_disagree_with_its_total_yields_no_number(portfolio, monkeypatch):
    broken = dataclasses.replace(household.CONTENTS_LIST,
                                 total_line="Contents list total (self-declared): SGD 999,000.")
    monkeypatch.setattr(household, "CONTENTS_LIST", broken)
    home = _policy(portfolio, "****-HOME3")
    result = home_travel.assess_policy(home)
    gap = {item["mechanism_code"]: item for item in result["items"]}["LIMIT.CONTENTS_BELOW_LIST"]
    assert gap["state"] == "INFORMATION_MISSING"
    assert gap["value"] is None and gap["params"] == {}
    assert gap["missing_document_key"] == "home.document.contents_list"


@pytest.mark.requires_corpus(HOME_IN)
def test_india_home_terms_and_the_built_in_contents_cover(portfolio):
    india = _items(_policy(portfolio, "****-HOME8"))
    assert {"CONDITION.SUM_INSURED_BASIS", "COVERED.HOME_CONTENTS_CLAUSE",
            "CONDITION.UNDERINSURANCE_WAIVED", "CONDITION.THEFT_AFTER_INSURED_EVENT",
            "DEADLINE.CLAIM_NOTICE", "LIMIT.CONTENTS_BUILT_IN"} <= set(india)
    # This standard wording states neither an unoccupancy trigger nor a forcible-entry
    # theft condition, so neither appears: absence is not filled from another wording.
    assert "CONDITION.UNOCCUPANCY" not in india and "CONDITION.THEFT_FORCIBLE_ENTRY" not in india
    built_in = india["LIMIT.CONTENTS_BUILT_IN"]
    # min(20% x INR 45,00,000, 10 lakh) = INR 9,00,000, from the wording's own share and cap.
    assert (built_in["value"], built_in["unit"]) == ("900000", "INR")
    assert built_in["params"] == {"percent": "20", "cap": "1000000", "building_sum_insured": "4500000"}
    assert built_in["evidence_class"] == "PROVEN"
    assert india["CONDITION.THEFT_AFTER_INSURED_EVENT"]["value"] == "7"


@pytest.mark.requires_corpus(TRAVEL_SG)
def test_singapore_travel_terms_thresholds_and_withheld_plan_limits(portfolio):
    travel = _policy(portfolio, "****-TRAVEL5")
    items = _items(travel)
    assert items["CONDITION.BAGGAGE_DELAY_THRESHOLD"]["value"] == "6"
    assert items["CONDITION.BAGGAGE_DELAY_THRESHOLD"]["unit"] == "hours"
    assert items["DEADLINE.CLAIM_NOTICE"]["value"] == "30"
    assert items["COVERED.OVERSEAS_MEDICAL"]["value"] == "90"
    assert "property irregularity report" in items["CONDITION.CARRIER_REPORT"]["citation"]["quote"]
    assert "existing health conditions" in items["EXCLUSION.EXISTING_CONDITION"]["citation"]["quote"]
    for limit in ("LIMIT.TRIP_CANCELLATION", "LIMIT.OVERSEAS_MEDICAL"):
        assert items[limit]["state"] == "INFORMATION_MISSING"
        assert items[limit]["value"] is None
        assert items[limit]["missing_document_key"] == "travel.document.selected_plan"
    names = [member["name"] for member in items["COVERED.TRAVELLERS"]["member_scope"]]
    assert names == ["Arjun Rao", "Meera Rao", "Anya Rao"]
    # The notice quote stops at its sentence and does not carry the next one's consequence.
    assert "invalidate" not in items["DEADLINE.CLAIM_NOTICE"]["citation"]["quote"]


def test_a_lookalike_owner_policy_inherits_no_home_or_travel_terms():
    lookalike = {
        "id": "owner", "source": "PORTFOLIO", "country": "SG", "line_of_business": "HOME",
        "product": "AIG Homes Advantage (synthetic schedule)", "masked_number": "****-HOME3",
        "period": {"start": "2026-02-01", "end": "2027-01-31"}, "source_document_ids": ["doc"],
        "coverage_items": [],
    }
    assert home_travel.assess_policy(lookalike) == {"mapped": False, "items": []}
    enriched = home_travel.enrich_portfolio({"policies": [lookalike], "members": []})
    assert "property_travel_items" not in enriched["policies"][0]


def test_an_absent_wording_is_reported_not_papered_over(portfolio, monkeypatch):
    def missing(_ref):
        raise corpus.CorpusMissing("not fetched", document_id=TRAVEL_SG)
    monkeypatch.setattr(corpus, "resolve", missing)
    result = home_travel.assess_policy(_policy(portfolio, "****-TRAVEL5"))
    assert result["mapped"] is False and result["items"] == []
    assert result["notice_key"] == "home_travel.notice.wording_missing"


@pytest.mark.requires_corpus(HOME_SG, HOME_IN, TRAVEL_SG, "sg-chubb-selectplus-lite-wording",
                             "sg-msig-sumo-wording", "sg-liberty-smecare-wording")
def test_new_mechanisms_bind_only_in_the_wordings_they_were_written_for():
    new = [key for key in align.mechanisms() if key.startswith(("HOME.", "TRAVEL."))]
    assert len(new) == 18
    for doc_id in ("sg-chubb-selectplus-lite-wording", "sg-msig-sumo-wording",
                   "sg-liberty-smecare-wording", "in-arogya-sanjeevani-policy-wording"):
        if corpus.missing([doc_id]):
            continue
        pages = [text for _page, text in health._pages(doc_id)]
        bound = align.align_pages(pages, carrier=doc_id, only=new).alignments
        assert bound == [], f"{doc_id}: {[a.mechanism for a in bound]}"


@pytest.mark.requires_corpus(HOME_SG, HOME_IN, TRAVEL_SG)
def test_protection_map_shows_the_contents_condition_india_home_and_each_traveller(portfolio):
    mapped = protection_map.build(portfolio)
    cells = {(cell["entity"], cell["risk"]): cell for cell in mapped["cells"]}
    contents = cells[("Singapore home contents", "home_contents")]
    assert contents["state"] == protection_map.CONDITIONAL
    assert contents["evidence_class"] == "DECLARED"
    assert cells[("Bengaluru family home", "home_structure")]["state"] == protection_map.DOCUMENTED
    for name in ("Arjun Rao", "Meera Rao", "Anya Rao"):
        assert cells[(name, "travel")]["state"] == protection_map.DOCUMENTED


def test_home_and_travel_text_never_decides_cover_or_advises():
    catalogue = json.loads((ROOT / "web" / "i18n" / "en.json").read_text(encoding="utf-8"))
    text = json.dumps({key: catalogue[key] for key in ("home", "home_travel", "travel")}).lower()
    source = (ROOT / "src" / "proven" / "home_travel.py").read_text(encoding="utf-8").lower()
    for phrase in ("not covered", "will be paid", "will not pay", "you should", "we recommend",
                   "increase your sum insured", "you are underinsured", "breach"):
        assert phrase not in text, phrase
    for phrase in ("not covered", "will be paid", "you should", "we recommend", "breach"):
        assert phrase not in source.replace("never decides cover", ""), phrase


@pytest.mark.requires_corpus(HOME_SG, HOME_IN, TRAVEL_SG)
def test_the_portfolio_route_serves_mapped_home_and_travel_cards(tmp_path, monkeypatch):
    key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv(persistence.PORTFOLIO_DB_ENV, str(tmp_path / "portfolio.sqlite"))
    monkeypatch.setenv(persistence.MASTER_KEY_ENV, key)
    monkeypatch.setenv(sandbox.SANDBOX_DB_ENV, str(tmp_path / "sandbox.sqlite"))
    monkeypatch.setenv(sandbox.SANDBOX_KEY_ENV, key)
    api.SESSIONS.clear()
    with TestClient(api.app) as client:
        assert client.post("/api/auth/login", json={
            "email": "owner@hockleefoods.demo", "password": auth.DEMO_PASSWORD}).status_code == 200
        session = api.SESSIONS.get(client.cookies.get(api.SESSION_COOKIE))
        with session.lock:
            scope = api._portfolio_scope(session)
        household_loader.load_example(api._portfolio_repository(), scope,
                                      {name: build() for name, build in pdfbuild_household.BUILDERS.items()})
        body = client.get("/api/portfolio").json()
        mapped = {policy["masked_number"]: policy for policy in body["policies"]
                  if policy.get("property_travel_items")}
        assert set(mapped) == {"****-HOME3", "****-TRAVEL5", "****-HOME8"}
        assert all(policy["property_travel_mapping"]["mapped"] for policy in mapped.values())
        map_body = client.get("/api/protection-map").json()
        assert any(cell["risk"] == "travel" for cell in map_body["cells"])
    api.SESSIONS.clear()
