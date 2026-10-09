"""The adviser can actually do something to a case that was shared with them.

Before this, the adviser workspace was read-only in practice: the queue navigated, the
review rendered, and the only control anywhere was on the file note. Worse, the share
itself did not work — see `test_sharing_grants_document_kinds_the_gate_can_match` — so a
"shared" case arrived with every finding withheld and there was nothing to act on even in
principle.

These tests pin the two halves:

  1. THE SHARE LANDS. `/api/consent/status` publishes the document kinds the case holds,
     and granting those makes findings visible to the adviser.
  2. THE ADVISER CAN MOVE THINGS. Information requests record against a named finding, and
     the correction requests advance through the transitions `actions.py` reserves to the
     actor "adviser" — with the actor taken from the session, never the body.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import actions
from proven.api import app

ADVISER = {"email": "adviser@lionbrokers.demo", "password": "Demo@123"}
CARRIER = {"email": "underwriter@carrier.demo", "password": "Demo@123"}
RECIPIENT = "Licensed adviser"


@pytest.fixture()
def client() -> TestClient:
    c = TestClient(app)
    c.post("/api/reset")
    return c


def _share_everything(client: TestClient) -> list[str]:
    """Grant the adviser every document kind the case actually holds."""
    status = client.get("/api/consent/status").json()
    kinds = [k["kind"] for k in status["shareable"]]
    assert kinds, "the demo case holds no documents to share"
    r = client.post("/api/consent", json={"recipient": RECIPIENT, "fields": kinds})
    assert r.status_code == 200, r.text
    return kinds


# ── 1. the share lands ───────────────────────────────────────────────────────

def test_consent_status_publishes_the_documents_the_case_holds(client):
    """The consent screen must be able to offer values the gate can match.

    REGRESSION, and it made the whole adviser flow inert. The screen offered "findings",
    "facts" and "score"; `disclosed_brief` compares the granted list against DOCUMENT
    KINDS, so those matched nothing, every span-backed finding was withheld, and the
    adviser opened a case that said `No findings in scope`. The grant succeeded and
    disclosed nothing.
    """
    d = client.get("/api/consent/status").json()
    assert d["adviser_recipient"] == RECIPIENT
    assert d["shareable"], "nothing to share means nothing to render"
    kinds = {k["kind"] for k in d["shareable"]}
    assert "BASE_WORDING" in kinds and "SCHEDULE" in kinds
    for k in d["shareable"]:
        assert k["label"] and k["count"] >= 1
        assert k["author_class"]
    # And the values are exactly what the gate compares against.
    granted = client.post("/api/consent",
                          json={"recipient": RECIPIENT,
                                "fields": sorted(kinds)}).json()
    assert granted["granted"] is True
    brief = client.get("/api/disclosure/brief",
                       params={"recipient": RECIPIENT}).json()
    assert brief["findings"], "granting every document must disclose findings"
    assert brief["findings_withheld_count"] == 0


def test_sharing_grants_document_kinds_the_gate_can_match(client):
    """The old client vocabulary is still refused by arithmetic, not by validation."""
    client.post("/api/consent", json={"recipient": RECIPIENT,
                                      "fields": ["findings", "facts", "score"]})
    brief = client.get("/api/disclosure/brief",
                       params={"recipient": RECIPIENT}).json()
    # The grant is live, and it discloses nothing that rests on a document. That is the
    # bug the new consent screen avoids by naming documents instead.
    assert brief["findings_withheld_count"] > 0
    assert brief["accounting_withheld"] is True


def test_the_adviser_reads_the_shared_case(client):
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    q = client.get("/api/adviser/queue").json()
    assert q["awaiting_consent"] is False
    assert q["live_case_count"] == 1
    review = client.get("/api/adviser/review").json()
    assert review["findings"]
    assert review["findings_withheld_count"] == 0


# ── 2. information requests ──────────────────────────────────────────────────

def test_the_adviser_raises_a_request_against_a_named_finding(client):
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    rule_id = client.get("/api/adviser/review").json()["findings"][0]["rule_id"]
    r = client.post("/api/adviser/request-information",
                    json={"rule_id": rule_id,
                          "message": "Please send the current stock valuation."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    raised = body["requests"][-1]
    assert raised["rule_id"] == rule_id
    assert raised["requested_by"] == "Priya Raman"
    assert raised["adviser_organisation"] == "Lion Brokers (Singapore)"
    assert raised["state"] == "INFORMATION_REQUESTED"


def test_a_request_does_not_move_the_finding_it_is_raised_against(client):
    """An adviser's question is not evidence, and must not read as any."""
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    before = client.get("/api/adviser/review").json()["findings"][0]
    client.post("/api/adviser/request-information",
                json={"rule_id": before["rule_id"], "message": "Please confirm."})
    after = next(f for f in client.get("/api/adviser/review").json()["findings"]
                 if f["rule_id"] == before["rule_id"])
    assert after["state"] == before["state"]
    assert after["class"] == before["class"]


def test_requests_are_readable_by_the_owner_and_carry_the_fields_the_ui_renders(client):
    """The requests screen rendered blank cards because it read `document`, `title`,
    `status` and `reason` — none of which are on this response."""
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    rule_id = client.get("/api/adviser/review").json()["findings"][0]["rule_id"]
    client.post("/api/adviser/request-information",
                json={"rule_id": rule_id, "message": "Please send the schedule."})
    rows = client.get("/api/adviser/requests").json()["requests"]
    assert rows
    for key in ("rule_id", "message", "requested_by", "state"):
        assert key in rows[0], f"the requests screen renders {key}"


# ── 3. the correction requests ───────────────────────────────────────────────

def _limit_action(client: TestClient) -> dict:
    for a in client.get("/api/actions").json()["actions"]:
        if a["action_type"] == actions.LIMIT_INCREASE:
            return a
    pytest.skip("demo case raised no limit-increase request")


def test_the_adviser_gets_moves_the_state_machine_permits_them(client):
    """The UI renders buttons FROM `allowed_moves`, so it cannot offer a refused move."""
    aid = _limit_action(client)["id"]
    client.post(f"/api/actions/{aid}/advance",
                json={"to_state": actions.SENT_TO_ADVISER})
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    a = client.get(f"/api/actions/{aid}").json()["action"]
    assert a["state"] == actions.SENT_TO_ADVISER
    mine = [m for m in a["allowed_moves"] if m["by"] == "adviser"]
    assert [m["to"] for m in mine] == [actions.ADVISER_REVIEWING]
    assert all(m["label"] for m in mine)


def test_the_adviser_drives_a_request_to_bound_and_closes_the_exposure(client):
    aid = _limit_action(client)["id"]
    client.post(f"/api/actions/{aid}/advance",
                json={"to_state": actions.SENT_TO_ADVISER})
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    for state in (actions.ADVISER_REVIEWING, actions.QUOTE_RECEIVED, actions.BOUND):
        r = client.post(f"/api/actions/{aid}/advance",
                        json={"to_state": state, "note": "terms confirmed"})
        assert r.status_code == 200, r.text
    final = client.get(f"/api/actions/{aid}").json()["action"]
    assert final["state"] == actions.BOUND
    assert final["closes_exposure"] is True
    assert final["adviser_note"] == "terms confirmed"
    out = client.get("/api/outcomes").json()
    assert out["exposure_closed_sgd"] == final["exposure_sgd"]
    assert out["exposure_pending_sgd"] == "0"


def test_a_refused_move_names_what_the_adviser_may_do_instead(client):
    """409 with the permitted moves, because "you cannot do that" without naming what you
    can is a dead end for whoever is driving the interface."""
    aid = _limit_action(client)["id"]
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    r = client.post(f"/api/actions/{aid}/advance",
                    json={"to_state": actions.BOUND})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["state"] == actions.DRAFTED
    assert detail["actor"] == "adviser"
    assert detail["allowed_moves"]


def test_the_adviser_still_cannot_release_the_owner_own_request(client):
    """DRAFTED -> SENT_TO_ADVISER belongs to the owner. The adviser asking for it is a
    WRONG_ACTOR refusal, not a permission the UI can grant itself."""
    aid = _limit_action(client)["id"]
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    r = client.post(f"/api/actions/{aid}/advance",
                    json={"to_state": actions.SENT_TO_ADVISER})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "WRONG_ACTOR"


def test_revoking_consent_stops_the_adviser_acting(client):
    """Revocation is what makes consent consent, and it has to reach the action loop."""
    aid = _limit_action(client)["id"]
    client.post(f"/api/actions/{aid}/advance",
                json={"to_state": actions.SENT_TO_ADVISER})
    _share_everything(client)
    client.post("/api/auth/login", json=ADVISER)
    assert client.get("/api/actions").status_code == 200
    client.post("/api/consent/revoke", json={})
    assert client.get("/api/actions").status_code == 403
    assert client.post(f"/api/actions/{aid}/advance",
                       json={"to_state": actions.ADVISER_REVIEWING}).status_code == 403
