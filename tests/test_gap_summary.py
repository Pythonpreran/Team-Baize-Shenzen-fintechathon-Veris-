"""The gap summary is a projection, and the language rules are non-negotiable.

TWO THINGS ARE UNDER TEST, AND THE SECOND IS THE IMPORTANT ONE.

First, that this is composition rather than a second engine. Two surfaces that disagree is
worse than one surface that is wrong, because a reader cannot tell which one lied. So every
value here must trace back to an endpoint that already existed.

Second, and this is the part the brief calls non-negotiable: the UI must not be allowed to
convert an uncertainty into a denial. Three distinct claims get three distinct renderings —
"we can prove this", "we cannot confirm this yet", and "we did not look". Collapsing them
into one red number would make a better demo and a dishonest product.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import gapsummary
from proven.api import SESSIONS, app


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


# ── it is composition, not a second engine ──────────────────────────────────

def test_every_item_traces_back_to_an_existing_endpoint(client):
    """The guard against this becoming a rival engine.

    Findings must be findings the Brief actually reported. If a future change starts
    inventing rows, the ids stop matching and this fails.
    """
    summary = client.get("/api/gap-summary").json()
    rule_ids = {f["rule_id"] for f in client.get("/api/brief").json()["findings"]}
    area_keys = {f"area:{a['key']}"
                 for a in client.get("/api/blindspots").json()["areas"]}

    for cat in summary["categories"]:
        for item in cat["items"]:
            key = item["key"]
            derivable = (
                key in rule_ids
                or key in area_keys
                or key.startswith(("voi:", "loop:"))
                or key in ("cyber", "dependencies"))
            assert derivable, f"{key!r} is not derivable from an existing endpoint"


def test_it_declares_what_it_was_composed_from(client):
    summary = client.get("/api/gap-summary").json()
    assert summary["method"] == "COMPOSITION_ONLY"
    assert "/api/brief" in summary["composed_from"]
    assert "/api/blindspots" in summary["composed_from"]


def test_the_counts_agree_with_the_brief(client):
    """The summary may regroup findings. It may not lose or invent one."""
    summary = client.get("/api/gap-summary").json()
    findings = client.get("/api/brief").json()["findings"]
    keys = {i["key"] for c in summary["categories"] for i in c["items"]}
    for f in findings:
        assert f["rule_id"] in keys, f"{f['rule_id']} vanished from the summary"


# ── the language rules ──────────────────────────────────────────────────────

def test_three_categories_and_not_all_of_them_are_gaps(client):
    summary = client.get("/api/gap-summary").json()
    got = [c["category"] for c in summary["categories"]]
    assert got == list(gapsummary.CATEGORIES)
    labels = {c["category"]: c["label"] for c in summary["categories"]}
    assert labels[gapsummary.NEEDS_VERIFICATION] == "Needs verification"
    assert labels[gapsummary.NOT_YET_ASSESSED] == "Not yet assessed"


def test_not_yet_assessed_is_neutral_never_red(client):
    """An area we did not examine is unknown, not dangerous. Rendering it red would
    manufacture a severity the evidence does not support."""
    summary = client.get("/api/gap-summary").json()
    bucket = next(c for c in summary["categories"]
                  if c["category"] == gapsummary.NOT_YET_ASSESSED)
    assert bucket["tone"] == "neutral"
    assert bucket["colour_hint"] == "grey"
    for item in bucket["items"]:
        assert item["tone"] == "neutral"


def test_needs_verification_says_cannot_confirm_not_not_covered(client):
    summary = client.get("/api/gap-summary").json()
    bucket = next(c for c in summary["categories"]
                  if c["category"] == gapsummary.NEEDS_VERIFICATION)
    assert "cannot confirm" in bucket["plain"].lower()


def test_no_composed_item_converts_an_unknown_into_a_denial(client):
    """The `assert_language` gate, checked on the wire.

    Items copied verbatim from the engine are exempt and marked as such — see the note on
    `_item`. The reason is worth knowing: BUSINESS-INTERRUPTION-NOT-CONFIRMED's own headline
    contains the words "not covered" precisely in order to disclaim them, and a blunt phrase
    match flagged the engine's own care as carelessness.
    """
    summary = client.get("/api/gap-summary").json()
    for cat in summary["categories"]:
        for item in cat["items"]:
            if item["verbatim_from_engine"]:
                continue
            blob = f"{item['title']} {item['why']}".lower()
            for phrase in ("you are not covered", "no cover", "will not pay",
                           "claim will fail", "uninsured"):
                assert phrase not in blob, (
                    f"composed item {item['key']!r} states {phrase!r}")


def test_the_language_gate_actually_rejects_a_denial():
    """Prove the guard fires rather than trusting that it would."""
    bad = {"categories": [{"items": [{
        "key": "x", "title": "You are not covered for this",
        "why": "", "category": gapsummary.ACTION_REQUIRED, "tone": "critical",
        "verbatim_from_engine": False}]}]}
    with pytest.raises(AssertionError):
        gapsummary.assert_language(bad)


def test_the_language_gate_rejects_an_unassessed_item_rendered_as_alarming():
    bad = {"categories": [{"items": [{
        "key": "y", "title": "Not looked at", "why": "",
        "category": gapsummary.NOT_YET_ASSESSED, "tone": "critical",
        "verbatim_from_engine": False}]}]}
    with pytest.raises(AssertionError):
        gapsummary.assert_language(bad)


# ── status is never carried by colour alone ─────────────────────────────────

def test_every_status_has_an_icon_and_a_label(client):
    """A status carried by colour alone is unreadable to a colour-blind reader and invisible
    in the screenshot somebody pastes into an email."""
    summary = client.get("/api/gap-summary").json()
    for cat in summary["categories"]:
        assert cat["icon"], f"{cat['category']} has no icon"
        assert cat["label"]
        assert cat["plain"]
        for item in cat["items"]:
            assert item["icon"]
            assert item["category_label"]


# ── the default view ────────────────────────────────────────────────────────

def test_the_default_view_is_three_items_not_seventeen(client):
    """A list of seventeen is a list nobody starts."""
    summary = client.get("/api/gap-summary").json()
    assert len(summary["top_three"]) <= 3
    assert summary["uncertain_count"] >= 0
    assert summary["best_next_action"]["what"]


def test_the_next_action_is_ranked_by_what_it_settles_not_by_fear(client):
    summary = client.get("/api/gap-summary").json()
    basis = summary["best_next_action"]["basis"].lower()
    assert "settle" in basis or "evidenced" in basis or "verified" in basis


def test_ordering_puts_evidenced_first_and_unassessed_last(client):
    summary = client.get("/api/gap-summary").json()
    order = [c["category"] for c in summary["categories"]]
    assert order.index(gapsummary.ACTION_REQUIRED) == 0
    assert order.index(gapsummary.NOT_YET_ASSESSED) == len(order) - 1


def test_the_gap_age_is_a_real_day_count(client):
    """From `compounding`, which measures the distance between two document dates. Not a
    severity and not a projection."""
    summary = client.get("/api/gap-summary").json()
    assert summary["oldest_open_days"] >= 0
    for cat in summary["categories"]:
        for item in cat["items"]:
            if item.get("age_days") is not None:
                assert item["age_days"] >= 0


# ── deletion, and the role boundary ─────────────────────────────────────────

def test_it_goes_quiet_when_the_case_is_deleted(client):
    assert client.get("/api/gap-summary").status_code == 200
    client.delete("/api/case")
    body = client.get("/api/gap-summary").json()
    assert body["available"] is False
    assert body["deleted"] is True


def test_a_carrier_may_not_read_the_gap_summary(client):
    """It is case detail, however nicely grouped."""
    client.get("/api/brief")
    client.post("/api/auth/login",
                json={"email": "underwriter@carrier.demo", "password": "Demo@123"})
    assert client.get("/api/gap-summary").status_code == 403
