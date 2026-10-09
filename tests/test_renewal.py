"""PRIORITY 2 — the renewal comparison engine.

The differentiator: notice what changed between last year's policy and this year's, and
refuse to guess when the significance cannot be safely determined. UNKNOWN escalates.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdfbuild_renewal as R  # noqa: E402

from proven import ingest, renewal  # noqa: E402
from proven.api import SESSIONS, app  # noqa: E402


def _version(pages_bytes, doc_id, label):
    pages = ingest.extract_pdf_pages_cached(pages_bytes, doc_id + ".pdf")
    return renewal.PolicyVersion.from_pages(pages, doc_id, label)


@pytest.fixture
def prior():
    return _version(R.prior_policy_pdf(), "prior", "Prior")


@pytest.fixture
def current():
    return _version(R.current_policy_pdf(), "current", "Current")


# ── the core diff behaviours ─────────────────────────────────────────────────

def test_identical_policies_produce_zero_changes(prior):
    """Comparing a version with itself is the falsifiable baseline."""
    changes = renewal.compare_terms(prior, prior)
    assert changes == []


def test_added_exclusion_is_one_material_change(prior, current):
    changes = renewal.compare_terms(prior, current)
    material = [c for c in changes if c.materiality == renewal.MATERIAL]
    assert len(material) == 1
    change = material[0]
    assert change.change_type == renewal.EXCLUSION_ADDED
    # sourced from BOTH sides: prior has no quote (absent), current quotes the page
    assert change.prior_quote == ""
    assert "Communicable Disease" in change.current_quote
    assert change.current_page == 1
    assert change.escalates is True


def test_unreadable_endorsement_is_unknown_and_escalates():
    """An endorsement whose text could not be read cannot be classified — UNKNOWN,
    and UNKNOWN must escalate rather than being dropped."""
    prior = renewal.PolicyVersion("p", "Prior", terms=[])
    current = renewal.PolicyVersion("c", "Current", terms=[
        renewal.PolicyTerm(kind=renewal.ENDORSEMENT, key="endorsement 4",
                           document_id="c", page=2, quote="",
                           mechanism=renewal.MECH_UNREADABLE,
                           title="Endorsement No. 4")])
    changes = renewal.compare_terms(prior, current)
    assert len(changes) == 1
    assert changes[0].materiality == renewal.UNKNOWN
    assert changes[0].escalates is True


def test_reduced_limit_is_material_increased_is_administrative():
    prior = renewal.PolicyVersion("p", "P", terms=[
        renewal.PolicyTerm(renewal.LIMIT, "limit of liability", "p", 1,
                           "Limit of Liability: SGD 1,000,000", value="1000000")])
    lower = renewal.PolicyVersion("c", "C", terms=[
        renewal.PolicyTerm(renewal.LIMIT, "limit of liability", "c", 1,
                           "Limit of Liability: SGD 500,000", value="500000")])
    change = renewal.compare_terms(prior, lower)[0]
    assert change.direction == renewal.DECREASED
    assert change.materiality == renewal.MATERIAL

    higher = renewal.PolicyVersion("c", "C", terms=[
        renewal.PolicyTerm(renewal.LIMIT, "limit of liability", "c", 1,
                           "Limit of Liability: SGD 2,000,000", value="2000000")])
    change2 = renewal.compare_terms(prior, higher)[0]
    assert change2.direction == renewal.INCREASED
    assert change2.materiality == renewal.ADMINISTRATIVE


def test_increased_deductible_is_material():
    prior = renewal.PolicyVersion("p", "P", terms=[
        renewal.PolicyTerm(renewal.DEDUCTIBLE, "deductible", "p", 1,
                           "Deductible: SGD 1,000", value="1000")])
    current = renewal.PolicyVersion("c", "C", terms=[
        renewal.PolicyTerm(renewal.DEDUCTIBLE, "deductible", "c", 1,
                           "Deductible: SGD 5,000", value="5000")])
    change = renewal.compare_terms(prior, current)[0]
    assert change.change_type == renewal.DEDUCTIBLE_CHANGED
    assert change.materiality == renewal.MATERIAL


def test_form_number_change_is_unknown_and_escalates():
    prior = renewal.PolicyVersion("p", "P", terms=[
        renewal.PolicyTerm(renewal.ENDORSEMENT, "form cg 40 46", "p", 1,
                           "Form CG 40 46 Liability", title="Form CG 40 46 Liability")])
    current = renewal.PolicyVersion("c", "C", terms=[
        renewal.PolicyTerm(renewal.ENDORSEMENT, "form cg 40 47", "c", 1,
                           "Form CG 40 47 Liability", title="Form CG 40 47 Liability")])
    changes = renewal.compare_terms(prior, current)
    form_changes = [c for c in changes if c.change_type == renewal.FORM_NUMBER_CHANGED]
    assert form_changes
    assert form_changes[0].materiality == renewal.UNKNOWN
    assert form_changes[0].escalates is True


def test_every_change_carries_both_pages_and_quotes(prior, current):
    for c in renewal.compare_terms(prior, current):
        d = c.as_dict()
        assert "prior_quote" in d and "current_quote" in d
        assert "prior_page" in d and "current_page" in d
        assert d["materiality"] in (renewal.MATERIAL, renewal.ADMINISTRATIVE,
                                    renewal.UNKNOWN)


def test_material_signals_only_escalating(prior, current):
    changes = renewal.compare_terms(prior, current)
    signals = renewal.material_signals(changes)
    assert signals
    for s in signals:
        assert s["materiality"] in (renewal.MATERIAL, renewal.UNKNOWN)
        assert s["agent"] == "renewal"
        assert s["claim_scope"] == "RISK_SIGNAL"   # never a coverage verdict


# ── the endpoint ─────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


def test_endpoint_reports_the_added_exclusion(client):
    body = client.get("/api/renewal/compare").json()
    assert body["available"] is True
    assert body["by_materiality"]["MATERIAL"] == 1
    material = [c for c in body["changes"] if c["materiality"] == "MATERIAL"][0]
    assert material["change_type"] == "EXCLUSION_ADDED"
    assert "Communicable Disease" in material["current_quote"]


def test_endpoint_feeds_orchestrator_under_existing_precedence(client):
    body = client.get("/api/renewal/compare").json()
    assert "orchestration" in body
    # a material change on stock, where the demo case documents a stock finding,
    # reaches ACTION_REQUIRED via the existing precedence rules
    decisions = body["orchestration"]["decisions"]
    assert any(d["decision"] == "ACTION_REQUIRED" for d in decisions)


def test_endpoint_refuses_a_carrier(client):
    """Role isolation: a carrier may not read a business's own renewal comparison."""
    client.post("/api/auth/login",
                json={"email": "underwriter@carrier.demo", "password": "Demo@123"})
    r = client.get("/api/renewal/compare")
    assert r.status_code == 403
