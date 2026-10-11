"""The action loop over real HTTP, plus the three front doors.

Separate from `test_actions.py` because these assert the API contract rather than the state
machine: the actor comes from the SESSION and never from the request body, deletion reaches
every action view, and the role entry paths are honest about not being an access boundary.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import actions
from proven.api import app

ADVISER_EMAIL = "adviser@lionbrokers.demo"
DEMO_PASSWORD = "Demo@123"


@pytest.fixture()
def client() -> TestClient:
    c = TestClient(app)
    c.post("/api/reset")
    return c


def _limit_action(client: TestClient) -> dict:
    body = client.get("/api/actions").json()
    for a in body["actions"]:
        if a["action_type"] == actions.LIMIT_INCREASE:
            return a
    pytest.skip("demo case raised no limit-increase request")


def _grant_disclosure(client: TestClient) -> None:
    """The owner shares the case before the adviser touches it.

    Required since the action loop gained the consent gate `_guard_action_loop`. It is
    posted while the session is still anonymous — i.e. as the owner — because that is the
    only actor entitled to grant it.
    """
    r = client.post("/api/consent", json={"recipient": "Licensed adviser",
                                          "fields": ["BASE_WORDING", "SCHEDULE"]})
    assert r.status_code == 200, r.text
    assert r.json()["granted"] is True


def _login_adviser(client: TestClient) -> None:
    _grant_disclosure(client)
    r = client.post("/api/auth/login",
                    json={"email": ADVISER_EMAIL, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    assert client.get("/api/actions").json()["actor"] == "adviser"


def test_an_adviser_with_no_disclosure_cannot_read_the_action_loop(client):
    """The consent gate reaches the action loop, not only the Brief.

    REGRESSION. These routes carried no role guard, so an adviser with no grant — and a
    carrier, who may never see an individual case — could read every request against a
    business with its exposure figure attached. An action is derived from the Brief and is
    at least as sensitive, so it is gated the same way.
    """
    client.post("/api/auth/login", json={"email": ADVISER_EMAIL,
                                         "password": DEMO_PASSWORD})
    for path in ("/api/actions", "/api/outcomes"):
        r = client.get(path)
        assert r.status_code == 403, f"{path} leaked an undisclosed case: {r.text}"
        assert "disclosed" in r.json()["detail"]


def test_a_carrier_cannot_read_one_business_action_loop(client):
    client.post("/api/auth/login", json={"email": "underwriter@carrier.demo",
                                         "password": DEMO_PASSWORD})
    for path in ("/api/actions", "/api/outcomes"):
        assert client.get(path).status_code == 403, path


def test_the_owner_and_an_anonymous_session_still_reach_the_action_loop(client):
    """The gate is asymmetric on purpose: gating the local demo surface would trade a
    real capability for the appearance of one."""
    assert client.get("/api/actions").status_code == 200
    client.post("/api/auth/login", json={"email": "owner@hockleefoods.demo",
                                         "password": DEMO_PASSWORD})
    assert client.get("/api/actions").status_code == 200


# ── GET /api/actions ─────────────────────────────────────────────────────────

def test_actions_are_prepared_from_the_demo_case(client):
    body = client.get("/api/actions").json()
    assert body["available"] is True
    assert body["count"] >= 1
    assert body["premium_basis"] == actions.NOT_QUOTED
    assert all(a["state"] == actions.DRAFTED for a in body["actions"]), \
        "nothing may be sent without the owner"


def test_actions_response_publishes_its_vocabulary(client):
    v = client.get("/api/actions").json()["vocabulary"]
    assert set(v["closes_exposure"]) == {actions.BOUND}
    assert actions.DRAFTED in v["states"]


def test_the_stock_shortfall_is_quantified_with_a_basis(client):
    a = _limit_action(client)
    assert a["exposure_sgd"] == "50000"
    assert a["is_quantified"] is True
    assert "less documented limit" in a["exposure_basis"]
    assert a["policy_refs"], "a quantified request must cite the clause behind it"


def test_no_action_carries_a_premium(client):
    for a in client.get("/api/actions").json()["actions"]:
        assert a["premium_basis"] == actions.NOT_QUOTED
        assert "No premium is shown" in a["premium_notice"]


# ── GET /api/actions/{id} ────────────────────────────────────────────────────

def test_single_action_is_retrievable(client):
    aid = _limit_action(client)["id"]
    body = client.get(f"/api/actions/{aid}").json()
    assert body["action"]["id"] == aid
    assert body["actor"] in {"owner", "adviser"}


def test_unknown_action_is_404(client):
    assert client.get("/api/actions/ACT-NOPE").status_code == 404


# ── POST /api/actions/{id}/advance ───────────────────────────────────────────

def test_owner_may_send_but_not_bind(client):
    aid = _limit_action(client)["id"]
    bad = client.post(f"/api/actions/{aid}/advance", json={"to_state": actions.BOUND})
    assert bad.status_code == 409
    assert bad.json()["detail"]["code"] in {"TRANSITION_REFUSED", "WRONG_ACTOR"}

    ok = client.post(f"/api/actions/{aid}/advance",
                     json={"to_state": actions.SENT_TO_ADVISER})
    assert ok.status_code == 200
    assert ok.json()["action"]["state"] == actions.SENT_TO_ADVISER


def test_the_actor_comes_from_the_session_not_the_body(client):
    """An owner cannot claim to be the adviser. There is no field for it, and adding one
    would make the exposure-closed figure self-reported."""
    aid = _limit_action(client)["id"]
    client.post(f"/api/actions/{aid}/advance", json={"to_state": actions.SENT_TO_ADVISER})
    r = client.post(f"/api/actions/{aid}/advance",
                    json={"to_state": actions.ADVISER_REVIEWING, "by": "adviser"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "WRONG_ACTOR"


def test_a_refusal_names_the_moves_that_are_permitted(client):
    aid = _limit_action(client)["id"]
    detail = client.post(f"/api/actions/{aid}/advance",
                         json={"to_state": actions.BOUND}).json()["detail"]
    assert detail["allowed_moves"], "a refusal that names no alternative is a dead end"
    assert detail["state"] == actions.DRAFTED


def test_unknown_state_is_refused(client):
    aid = _limit_action(client)["id"]
    r = client.post(f"/api/actions/{aid}/advance", json={"to_state": "SORTED_OUT"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "UNKNOWN_STATE"


def test_adviser_drives_the_request_to_bound(client):
    aid = _limit_action(client)["id"]
    client.post(f"/api/actions/{aid}/advance", json={"to_state": actions.SENT_TO_ADVISER})
    _login_adviser(client)
    for state in (actions.ADVISER_REVIEWING, actions.QUOTE_RECEIVED, actions.BOUND):
        r = client.post(f"/api/actions/{aid}/advance",
                        json={"to_state": state, "note": "adviser step"})
        assert r.status_code == 200, r.text
    final = client.get(f"/api/actions/{aid}").json()["action"]
    assert final["state"] == actions.BOUND
    assert final["is_terminal"] and final["closes_exposure"]
    assert final["allowed_moves"] == []


# ── GET /api/outcomes ────────────────────────────────────────────────────────

def test_outcomes_reports_identified_but_nothing_closed(client):
    o = client.get("/api/outcomes").json()
    assert o["available"] is True
    assert o["exposure_identified_sgd"] == "50000"
    assert o["exposure_closed_sgd"] == "0"
    assert o["gaps_detected"] >= 1
    assert "once per insured interest" in o["counting_basis"]


def test_exposure_is_not_double_counted_across_two_requests(client):
    """Two rules raise two corrections against the same stock shortfall."""
    o = client.get("/api/outcomes").json()
    stock = o["exposure_by_interest"].get("stock")
    assert stock, "the demo case should quantify the stock interest"
    if len(stock["actions"]) > 1:
        assert stock["amount_sgd"] == "50000", "summing would report 100000"


def test_binding_moves_exposure_from_pending_to_closed(client):
    aid = _limit_action(client)["id"]
    client.post(f"/api/actions/{aid}/advance", json={"to_state": actions.SENT_TO_ADVISER})
    assert client.get("/api/outcomes").json()["exposure_pending_sgd"] == "50000"
    _login_adviser(client)
    for state in (actions.ADVISER_REVIEWING, actions.QUOTE_RECEIVED, actions.BOUND):
        client.post(f"/api/actions/{aid}/advance", json={"to_state": state})
    o = client.get("/api/outcomes").json()
    assert o["exposure_closed_sgd"] == "50000"
    assert o["exposure_pending_sgd"] == "0"
    assert o["actions_bound"] == 1


# ── deletion reaches every action view ───────────────────────────────────────

def test_deletion_silences_the_action_list(client):
    aid = _limit_action(client)["id"]
    client.delete("/api/case")
    body = client.get("/api/actions").json()
    assert body["available"] is False and body["deleted"] is True
    assert body["actions"] == [] and body["count"] == 0
    assert client.get(f"/api/actions/{aid}").status_code == 404


def test_deletion_silences_outcomes(client):
    client.delete("/api/case")
    o = client.get("/api/outcomes").json()
    assert o["available"] is False and o["deleted"] is True
    assert o["exposure_identified_sgd"] == "0"
    assert o["exposure_closed_sgd"] == "0"
    assert o["exposure_by_interest"] == {}


def test_deletion_refuses_further_transitions(client):
    aid = _limit_action(client)["id"]
    client.delete("/api/case")
    r = client.post(f"/api/actions/{aid}/advance",
                    json={"to_state": actions.SENT_TO_ADVISER})
    assert r.status_code == 404


# ── the three front doors ────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/login", "/app", "/adviser", "/carrier", "/reviewer"])
def test_every_entry_path_serves_the_app(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("path", ["/app/findings", "/adviser/queue/case-1",
                                  "/carrier/portfolio"])
def test_deep_links_serve_the_app(client, path):
    """Client-side routing: a refresh on a deep link must not 404."""
    assert client.get(path).status_code == 200


def test_entrypoints_are_declared_with_their_roles(client):
    body = client.get("/api/entrypoints").json()
    paths = {e["path"]: e for e in body["entrypoints"]}
    assert set(paths) == {"/app", "/adviser", "/carrier", "/reviewer"}
    assert paths["/adviser"]["role"] == "ADVISER"
    assert paths["/carrier"]["role"] == "CARRIER"
    assert paths["/reviewer"]["role"] == "REVIEWER"


def test_entrypoints_state_they_are_not_an_access_boundary(client):
    """A route that merely looks private is worse than none, so the notice says so."""
    notice = client.get("/api/entrypoints").json()["notice"]
    assert "not an access boundary" in notice
    assert "server-side" in notice


def test_an_unauthenticated_carrier_page_cannot_fetch_carrier_data(client):
    """The page loads; the data does not. That is where the boundary actually is."""
    assert client.get("/carrier").status_code == 200
    assert client.get("/api/carrier/portfolio").status_code in (401, 403)
