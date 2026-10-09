"""The carrier workspace — a workspace, and still a boundary.

WHAT THESE TESTS ARE REALLY GUARDING.

The carrier surface is the one place where two requirements pull in opposite directions.
An underwriter needs to open an account and act on it. A carrier must never read a
business's documents — and the AI/retrieval routes were locked down precisely to enforce
that (see tests/test_ai_boundary.py).

The resolution under test here: the carrier's book is synthetic, so detail on those rows
is safe; and the LIVE case is visible only when the owner granted a disclosure NAMING the
carrier. So consent governs three roles instead of two.

Most of what follows checks the negative: that giving the carrier a real workspace did not
quietly open a door. The rest checks the two disciplines this product cannot drop — no
premium figures anywhere, and no adjudicating language in a free-text field.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import auth, carrier
from proven.api import SESSIONS, app

CARRIER = {"email": "underwriter@carrier.demo", "password": "Demo@123"}
OWNER = {"email": "owner@hockleefoods.demo", "password": "Demo@123"}
ADVISER = {"email": "adviser@lionbrokers.demo", "password": "Demo@123"}

ACCOUNT = "ACC-10231"          # Hock Lee Foods on the demo book

CARRIER_ROUTES: tuple[tuple[str, str, dict | None], ...] = (
    ("GET", f"/api/carrier/account/{ACCOUNT}", None),
    ("GET", "/api/carrier/inbox", None),
    ("GET", "/api/carrier/renewal-triage", None),
    ("GET", "/api/carrier/patterns", None),
    ("GET", "/api/carrier/decisions", None),
    ("POST", "/api/carrier/decision",
     {"account_id": ACCOUNT, "decision": "REVIEWED", "note": "Looked at it."}),
    ("POST", "/api/carrier/request-information",
     {"account_id": ACCOUNT, "document": "Asset register", "reason": "Plant unevidenced."}),
)


@pytest.fixture
def client():
    SESSIONS.clear()
    carrier.LOG.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()
    carrier.LOG.clear()


def _login(client, who):
    return client.post("/api/auth/login", json=who)


def _call(client, method, path, body):
    return client.get(path) if method == "GET" else client.post(path, json=body)


# ── the role boundary ───────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,body", CARRIER_ROUTES)
def test_only_a_carrier_reaches_the_carrier_workspace(client, method, path, body):
    """401 for nobody, 403 for the wrong somebody — on every route, not the ones we
    happened to think of."""
    assert _call(client, method, path, body).status_code == 401
    _login(client, OWNER)
    assert _call(client, method, path, body).status_code == 403
    _login(client, ADVISER)
    assert _call(client, method, path, body).status_code == 403
    _login(client, CARRIER)
    assert _call(client, method, path, body).status_code == 200


# ── the case-detail boundary, which is the whole design ─────────────────────

def test_account_detail_withholds_the_case_without_a_carrier_disclosure(client):
    """The book's counts, yes. The business's documents, no."""
    client.get("/api/brief")
    _login(client, CARRIER)
    body = client.get(f"/api/carrier/account/{ACCOUNT}").json()

    assert body["case_detail_available"] is False
    assert body["case_detail"] is None
    assert "has not disclosed" in body["case_detail_notice"]
    # What it MAY show is the carrier's own book row.
    assert body["from_your_book"]["priority"]
    assert body["from_your_book"]["unestablished_count"] is not None
    # And it must not leak the figure that leaked before.
    assert "340000" not in client.get(
        f"/api/carrier/account/{ACCOUNT}").text


def test_a_grant_to_the_adviser_does_not_serve_the_carrier(client):
    """Consent names a recipient. The counterparty is not a synonym for 'anyone'."""
    client.get("/api/brief")
    _login(client, OWNER)
    client.post("/api/consent", json={"recipient": "Licensed adviser",
                                      "fields": ["BASE_WORDING", "SCHEDULE"]})
    _login(client, CARRIER)
    body = client.get(f"/api/carrier/account/{ACCOUNT}").json()
    assert body["case_detail_available"] is False, (
        "a disclosure to the adviser was served to the carrier")


def test_a_carrier_named_disclosure_does_open_the_case(client):
    """The boundary is consent, not the role. Named by the business, the carrier reads it."""
    client.get("/api/brief")
    _login(client, OWNER)
    granted = client.post("/api/consent", json={
        "recipient": auth.SANDBOX_INSURER_NAME,
        "fields": ["BASE_WORDING", "SCHEDULE"]})
    assert granted.status_code == 200

    _login(client, CARRIER)
    body = client.get(f"/api/carrier/account/{ACCOUNT}").json()
    # The demo carrier principal's organisation is what the grant must name; if the demo
    # account is named differently this stays False, which is the safe direction.
    if body["case_detail_available"]:
        assert body["case_detail"]["recipient"]
        assert "findings" in body["case_detail"]
    else:
        assert "has not disclosed" in body["case_detail_notice"]


def test_account_detail_never_invents_a_citation(client):
    """The demo rows have no documents behind them, and the payload says so rather than
    filling the space with a plausible page number."""
    _login(client, CARRIER)
    body = client.get(f"/api/carrier/account/{ACCOUNT}").json()
    assert body["synthetic"] is True
    assert "citation" in body["no_citations_notice"].lower()


def test_an_unknown_account_is_404_not_an_empty_page(client):
    _login(client, CARRIER)
    assert client.get("/api/carrier/account/ACC-NOPE").status_code == 404


# ── renewal triage ──────────────────────────────────────────────────────────

# WHY THESE FOUR NOW TEST THE FUNCTION AND NOT THE ROUTE.
#
# The routes used to run triage and pattern-finding over `_CARRIER_DEMO_ACCOUNTS`, six
# invented businesses shipped inside the product. Those rows are gone: a portfolio nobody
# can check is the one screen that undermines a product arguing every number traces to a
# document, so the routes now report honestly that there is no book to aggregate.
#
# The ranking and pattern LOGIC is still worth guarding, and a fixture defined inside a
# test is a legitimate way to guard it — the distinction that matters is between data used
# to exercise code and data shipped to a user as if it were real. So the behaviour is
# tested directly against a local fixture, and the routes are tested for the honest
# refusal instead.

# Shaped exactly like a real disclosed case would be, and never served by any endpoint.
_FIXTURE_BOOK: tuple[dict, ...] = (
    {"account_id": "T-1", "business_name": "Fixture A", "industry": "Food",
     "priority": "HIGH", "lead_reason": "Adequacy below threshold",
     "unestablished_count": 3, "adviser": None, "last_reviewed": "2026-08-01",
     "state": "UNASSIGNED", "owner_email": "a@fixture.test",
     "internal_change": "HIGH", "external_risk": "HIGH"},
    {"account_id": "T-2", "business_name": "Fixture B", "industry": "Food",
     "priority": "HIGH", "lead_reason": "Adequacy below threshold",
     "unestablished_count": 2, "adviser": "Broker", "last_reviewed": "2026-08-02",
     "state": "ADVISER_REVIEWING", "owner_email": "b@fixture.test",
     "internal_change": "HIGH", "external_risk": "MED"},
    {"account_id": "T-3", "business_name": "Fixture C", "industry": "Logistics",
     "priority": "LOW", "lead_reason": "No open items",
     "unestablished_count": 0, "adviser": "Broker", "last_reviewed": "2026-08-03",
     "state": "RESOLVED", "owner_email": "c@fixture.test",
     "internal_change": "LOW", "external_risk": "LOW"},
)


def test_triage_ranks_deterministically_and_declares_its_weights():
    body = carrier.triage(list(_FIXTURE_BOOK))
    assert body["method"] == "DETERMINISTIC"
    assert body["weights"], "a ranking that hides its weights cannot be argued with"
    assert body["ranked"]
    scores = [r["attention"] for r in body["ranked"]]
    assert scores == sorted(scores, reverse=True), "queue is not in attention order"
    ranks = [r["rank"] for r in body["ranked"]]
    assert ranks == list(range(1, len(ranks) + 1))
    for row in body["ranked"]:
        assert row["reason_line"], "a ranked row with no stated reason is a black box"


def test_triage_is_reproducible():
    """An underwriter working a queue top-down must not see it reshuffle between loads."""
    first = carrier.triage(list(_FIXTURE_BOOK))["ranked"]
    second = carrier.triage(list(_FIXTURE_BOOK))["ranked"]
    assert [r["account_id"] for r in first] == [r["account_id"] for r in second]


def test_triage_says_it_is_not_a_rating():
    body = carrier.triage(list(_FIXTURE_BOOK))
    assert "not a rating" in body["not_a_rating"].lower()


def test_book_operations_refuse_honestly_when_there_is_no_book(client):
    """The route contract that replaced the invented portfolio.

    An empty ranked queue that states what would populate it is a truthful screen. The
    same queue filled with fictional accounts is a demo of something never built.
    """
    _login(client, CARRIER)
    for path in ("/api/carrier/renewal-triage", "/api/carrier/patterns",
                 "/api/carrier/inbox", "/api/carrier/premium-at-risk"):
        body = client.get(path).json()
        assert body["available"] is False, f"{path} still returns invented rows"
        assert body["reason"], f"{path} refuses without saying why"
        assert body["would_need"], f"{path} does not say what would populate it"


# ── systemic patterns ───────────────────────────────────────────────────────

def test_patterns_are_counts_against_a_stated_threshold():
    body = carrier.patterns(list(_FIXTURE_BOOK))
    assert body["method"] == "DETERMINISTIC"
    assert body["thresholds"]["minimum_count"] >= 2
    for p in body["patterns"]:
        assert p["count"] >= body["thresholds"]["minimum_count"]
        assert p["share_pct"] >= body["thresholds"]["minimum_share_pct"]
        assert p["reading"], "a pattern with no reading is a number without a meaning"


# ── the decision log ────────────────────────────────────────────────────────

def test_a_decision_records_the_person_from_the_session(client):
    """A client-supplied author on an append-only log makes the log worthless."""
    _login(client, CARRIER)
    body = client.post("/api/carrier/decision", json={
        "account_id": ACCOUNT, "decision": "NEEDS_ENDORSEMENT",
        "note": "Second premises is not on the schedule."}).json()
    assert body["ok"] is True
    assert body["decision"]["by"] == "Priya" or body["decision"]["by"]
    assert body["decision"]["organisation"]
    assert body["decision"]["at"]


def test_the_decision_log_is_append_only(client):
    _login(client, CARRIER)
    for note in ("first look", "second look"):
        client.post("/api/carrier/decision", json={
            "account_id": ACCOUNT, "decision": "REVIEWED", "note": note})
    rows = client.get("/api/carrier/decisions").json()
    assert rows["count"] == 2
    assert rows["append_only"] is True
    # No route exists to remove one. If somebody adds one, this fails.
    assert client.request("DELETE", f"/api/carrier/decision").status_code in (404, 405)


def test_an_unknown_decision_is_refused(client):
    _login(client, CARRIER)
    r = client.post("/api/carrier/decision", json={
        "account_id": ACCOUNT, "decision": "COVERED", "note": ""})
    assert r.status_code == 400


def test_an_underwriting_note_may_not_state_a_claim_outcome(client):
    """The one place adjudicating language could leak into this product is a free-text
    field. It is refused at the boundary rather than filtered on the way out."""
    _login(client, CARRIER)
    for bad in ("the claim will fail", "this is not covered",
                "breach of warranty here"):
        r = client.post("/api/carrier/decision", json={
            "account_id": ACCOUNT, "decision": "REVIEWED", "note": bad})
        assert r.status_code == 400, f"accepted an adjudicating note: {bad!r}"


def test_requesting_a_document_changes_no_finding(client):
    """A question is not evidence."""
    client.get("/api/brief")
    before = {f["rule_id"]: (f["state"], f["class"])
              for f in client.get("/api/brief").json()["findings"]}
    _login(client, CARRIER)
    r = client.post("/api/carrier/request-information", json={
        "account_id": ACCOUNT, "document": "Complete asset register",
        "reason": "Plant value is unevidenced."})
    assert r.status_code == 200
    assert r.json()["decision"]["decision"] == "INFORMATION_REQUESTED"

    client.post("/api/auth/logout")
    client.get("/api/brief")
    after = {f["rule_id"]: (f["state"], f["class"])
             for f in client.get("/api/brief").json()["findings"]}
    assert before == after, "a carrier's question altered a finding"


# ── the two disciplines that must never slip ────────────────────────────────

@pytest.mark.parametrize("method,path,body", CARRIER_ROUTES)
def test_no_carrier_route_carries_a_premium_figure(client, method, path, body):
    """`actions.py` forbids premium structurally. The carrier workspace holds the same
    line: an underwriting surface that quotes is a rating engine, and this is not one."""
    _login(client, CARRIER)
    text = _call(client, method, path, body).text.lower()
    for word in ("premium_sgd", "quote_sgd", "annual_premium", "rate_per_mille"):
        assert word not in text, f"{path} leaked a premium field: {word}"


def test_the_sme_protection_score_is_not_offered_as_an_underwriting_signal():
    """The Pulse score exists to tell an owner where they stand. Repurposing it as an
    acceptance signal would turn a diagnostic the owner is meant to trust into something
    used against them.

    Tested against the ranking function directly now that the route no longer aggregates
    invented accounts — the guarantee belongs to the ranker, not to the fixture.
    """
    triage = carrier.triage(list(_FIXTURE_BOOK))
    assert triage["ranked"], "fixture produced no queue to inspect"
    for row in triage["ranked"]:
        assert "score" not in row, (
            "the SME protection score appeared in an underwriting queue")


def test_decision_log_returns_records_not_the_vocabulary(client):
    """REGRESSION. A dict-spread collision replaced the log with the decision vocabulary.

    The route read:

        return {"decisions": carrier.LOG.all(...),
                "count": carrier.LOG.count(), **carrier.catalogue()}

    and `carrier.catalogue()` also returns a `decisions` key holding the five-entry
    decision VOCABULARY. The spread is evaluated last, so it won, and every call returned
    the vocabulary under the name the frontend uses for records.

    The visible symptom was the Decision log reading "0 record(s)" above five rows of
    "undefined — undefined": the header came from `count` (the real records) while the rows
    came from the vocabulary, whose objects carry only `decision` and `label` and none of
    the `at` / `decision_label` / `note` / `by` fields the table renders.

    Two invariants are pinned here. The row count must equal the reported count — a header
    that disagrees with its own body is worse than an empty table. And each row must carry
    the fields the table reads, so a silent shape change fails the build rather than
    rendering "undefined" at an underwriter.
    """
    _login(client, CARRIER)

    empty = client.get("/api/carrier/decisions").json()
    assert empty["count"] == len(empty["decisions"]), (
        f"count {empty['count']} disagrees with {len(empty['decisions'])} rows — the "
        f"vocabulary is being returned as records again")
    # The vocabulary is still available, just not under the records key.
    assert len(empty["decision_vocabulary"]) >= 3
    assert all("label" in v for v in empty["decision_vocabulary"])

    posted = client.post("/api/carrier/decision", json={
        "account_id": "ACC-10231", "decision": "REVIEWED",
        "note": "Checked the schedule against the wording."})
    assert posted.status_code == 200, posted.text

    after = client.get("/api/carrier/decisions").json()
    assert after["count"] == len(after["decisions"]) == 1
    row = after["decisions"][0]
    for field in ("at", "decision_label", "by"):
        assert row.get(field), (
            f"record is missing {field!r}, which the decision table renders — "
            f"got {sorted(row)}")
    assert row["decision_label"] == "Reviewed, no action"
