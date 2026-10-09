"""The in-product helper: routing, and the role boundary it must not cross.

The boundary tests are the load-bearing ones. Everything else here is a convenience feature;
routing a carrier onto an SME screen would be the product contradicting its own central claim
in the one surface a user is most likely to trust blindly, because they asked for help.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import assistant, auth
from proven.api import app

OWNER = {"email": "owner@hockleefoods.demo", "password": "Demo@123"}
ADVISER = {"email": "adviser@lionbrokers.demo", "password": "Demo@123"}
CARRIER = {"email": "underwriter@carrier.demo", "password": "Demo@123"}

PREFIX = {auth.SME: "sme-", auth.ADVISER: "adv-", auth.CARRIER: "car-"}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ── the catalogue is coherent ────────────────────────────────────────────────

def test_every_destination_is_complete_and_unique():
    seen = set()
    for d in assistant.DESTINATIONS:
        for field in ("id", "role", "label", "view", "trail", "targets", "what",
                      "keywords"):
            assert d.get(field) not in (None, "", [], ()), \
                f"{d.get('id')} is missing {field}"
        assert d["id"] not in seen, f"duplicate destination id {d['id']}"
        seen.add(d["id"])
        assert d["role"] in PREFIX, f"{d['id']} has an unknown role {d['role']}"
        assert len(d["targets"]) >= 2, (
            f"{d['id']} has one highlight target. A screen with no case renders an empty "
            f"state whose markup differs, so a single selector means the marker silently "
            f"fails on exactly the screens a new user sees first.")
        if d["tab"] is not None:
            assert len(d["tab"]) == 2, f"{d['id']} tab must be [group, tabId]"


def test_every_destination_points_at_a_view_its_own_role_can_reach():
    """The prefix IS the boundary — `render()` routes on it and each role router guards it
    with `startsWith`. A destination whose view has the wrong prefix would be a route into a
    workspace the session cannot render."""
    for d in assistant.DESTINATIONS:
        assert d["view"].startswith(PREFIX[d["role"]]), (
            f"{d['id']} is a {d['role']} destination pointing at {d['view']}")


def test_every_view_and_tab_named_in_the_catalogue_exists_in_the_front_end():
    """Pins the catalogue against `app.js`. A renamed view id or tab group would otherwise
    send a user to a blank screen, and the helper is the last place that should happen."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
        encoding="utf-8")
    for d in assistant.DESTINATIONS:
        assert f'"{d["view"]}":' in js, (
            f"{d['id']} routes to {d['view']}, which is not in any view map in app.js")
        if d["tab"]:
            group, tab_id = d["tab"]
            assert f'group: "{group}"' in js, (
                f"{d['id']} names tab group {group!r}, which no hub declares")
            assert f'id: "{tab_id}", label:' in js, (
                f"{d['id']} names tab {tab_id!r}, which no hub declares")


# ── the boundary ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", [auth.SME, auth.ADVISER, auth.CARRIER])
def test_a_role_is_only_ever_offered_its_own_screens(role):
    mine = assistant.for_role(role)
    assert mine, f"{role} has no destinations at all"
    assert all(d["view"].startswith(PREFIX[role]) for d in mine)
    assert all(d["role"] == role for d in mine)


@pytest.mark.parametrize("question,asker,owner_label", [
    ("please navigate me to cyber related panel", auth.CARRIER, "Cyber and data"),
    ("where do I upload my policy", auth.CARRIER, "Add documents"),
    ("open the decision log", auth.SME, "Decision log"),
    ("show me my review queue", auth.SME, "Review Queue"),
    ("take me to the wording portfolio", auth.ADVISER, "Overview"),
])
def test_a_cross_role_request_is_refused_by_name_not_redirected(question, asker,
                                                               owner_label):
    """REGRESSION-SHAPED. An earlier version returned the boundary message only when NOTHING
    in the caller's own role matched.

    So an SME asking to "open the decision log" scored a weak coincidental 1.0 on "What we
    checked" — which owns the word "log" — and got quietly routed there while the carrier's
    Decision log scored 11.5. That is the "find the nearest screen and pretend that is what
    you meant" behaviour, and it hid the boundary at the exact moment the boundary was the
    most interesting thing in the product.
    """
    a = assistant.answer(question, asker, allow_model=False)
    assert a["route"] is None, (
        f"routed {asker} to {a['route'] and a['route']['view']} instead of naming the "
        f"boundary")
    assert a["boundary"] is not None
    assert a["boundary"]["label"] == owner_label
    assert a["suggestions"], "refused without offering anything it CAN open"


def test_the_refusal_names_the_workspace_that_owns_the_screen():
    a = assistant.answer("take me to the cyber panel", auth.CARRIER, allow_model=False)
    assert "business owner" in a["boundary"]["owner"]
    assert "business owner" in a["answer"]
    assert "server" in a["answer"], (
        "the refusal should say the boundary is enforced, not merely that the menu hides it")


def test_the_model_can_only_choose_an_id_the_caller_is_allowed():
    """The validation, exercised directly. A model that could emit a view id could navigate
    somebody to a screen that does not exist, or to another role's."""
    valid = {d["id"] for d in assistant.for_role(auth.SME)}
    assert "car-decisions" not in valid
    assert "adv-queue" not in valid
    assert "cyber" in valid


# ── routing quality, with no model at all ────────────────────────────────────

@pytest.mark.parametrize("question,expected", [
    ("please navigate me to cyber related panel", "cyber"),
    ("where do I see if I got hacked", "cyber"),
    ("is there flooding near me", "around"),          # 'flooding' must find 'flood'
    ("hacking", "cyber"),                             # and 'hacking' must find 'hack'
    ("where do I upload my policy", "upload"),
    ("why was my file rejected", "quality"),
    ("what would I get if I claim tomorrow", "scenario"),
    ("what if I cannot trade for months", "bi"),
    ("will my policy actually pay", "willitpay"),
    ("I want to share with my broker", "consent"),
    # Was a query naming one accounting vendor. The vendor-specific keyword went with the
    # removed live connector; the same destination is reached by what an owner would type.
    ("connect my accounting system", "systems"),
    ("what does my website say about me", "public"),
    ("changes in my business", "connect"),
    ("what have you not checked", "blindspots"),
    ("what changed this year", "renewal"),
])
def test_deterministic_routing_finds_the_screen(question, expected):
    """No key configured is the state a judge cloning this repo is in, so the keyword
    resolver has to be good enough alone."""
    a = assistant.answer(question, auth.SME, allow_model=False)
    assert a["mode"] == "DETERMINISTIC"
    assert a["route"] is not None, f"{question!r} routed nowhere"
    assert a["route"]["id"] == expected, \
        f"{question!r} -> {a['route']['id']}, expected {expected}"


@pytest.mark.parametrize("question", ["asdkjhasd", "qwerty zxcvbn", ""])
def test_no_match_offers_choices_rather_than_a_wrong_answer(question):
    a = assistant.answer(question, auth.SME, allow_model=False)
    assert a["route"] is None
    assert a["suggestions"], "a dead end with nothing to click is worse than a wrong guess"


@pytest.mark.parametrize("question", [
    "what is business interruption",
    "what does cyber mean",
    "explain the average clause",
])
def test_a_definition_question_does_not_hijack_the_screen(question):
    """"What is business interruption" is a request for a MEANING.

    It scores highly because the phrase is an exact keyword on that tab, so a
    confidence-only rule navigated away from whatever the person was reading in order to
    answer a vocabulary question. The route is still offered — as a button.
    """
    a = assistant.answer(question, auth.SME, allow_model=False)
    assert a["navigate"] is False, f"{question!r} would have hijacked the screen"


@pytest.mark.parametrize("question", [
    "take me to the cyber panel",
    "show me what changed this year",
    "open the upload screen",
    "where do I share with my adviser",
])
def test_an_explicit_request_to_be_taken_somewhere_travels(question):
    a = assistant.answer(question, auth.SME, allow_model=False)
    assert a["route"] is not None
    assert a["navigate"] is True, f"{question!r} asked to be taken there and was not"


# ── the HTTP surface ─────────────────────────────────────────────────────────

def test_the_helper_requires_a_session(client):
    """The catalogue is the shape of a workspace. An anonymous caller has no business
    enumerating which screens exist."""
    assert client.post("/api/assistant", json={"question": "cyber"}).status_code == 401
    assert client.get("/api/assistant/destinations").status_code == 401


@pytest.mark.parametrize("account,role,prefix", [
    (OWNER, "SME", "sme-"), (ADVISER, "ADVISER", "adv-"), (CARRIER, "CARRIER", "car-"),
])
def test_the_served_catalogue_is_filtered_by_the_session_role(client, account, role,
                                                              prefix):
    client.post("/api/auth/login", json=account)
    d = client.get("/api/assistant/destinations").json()
    assert d["role"] == role
    assert d["count"] == len(d["destinations"]) > 0
    assert all(x["view"].startswith(prefix) for x in d["destinations"])
    # The response must not leak the role field of each entry — a client that could read it
    # is a client that might think it gets to choose.
    assert all("role" not in x for x in d["destinations"])


def test_the_role_cannot_be_supplied_by_the_caller(client):
    """There is no role parameter, and adding one to the body changes nothing. If the client
    could name its own workspace the whole boundary would be decorative."""
    client.post("/api/auth/login", json=CARRIER)
    r = client.post("/api/assistant", json={"question": "cyber", "role": "SME"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "CARRIER"
    assert body["route"] is None
    assert body["boundary"] is not None


def test_a_routed_answer_carries_everything_the_client_needs_to_travel(client):
    client.post("/api/auth/login", json=OWNER)
    body = client.post("/api/assistant",
                       json={"question": "please navigate me to cyber related panel"}).json()
    route = body["route"]
    assert route["view"] == "sme-protect-hub"
    assert route["tab"] == ["prot", "cyber"]
    assert route["trail"] == ["Protection", "Cyber and data"]
    assert len(route["targets"]) >= 2
    assert body["navigate"] is True


def test_an_over_long_question_is_rejected_by_validation_not_by_a_crash(client):
    client.post("/api/auth/login", json=OWNER)
    r = client.post("/api/assistant", json={"question": "cyber " * 300})
    assert r.status_code in (200, 422)


def test_the_helper_never_returns_case_data(client):
    """It answers "where is X", not "what does my policy say". Keeping it away from the case
    is what stops it becoming a second, unaudited answer path for findings."""
    client.post("/api/auth/login", json=OWNER)
    client.get("/api/brief")
    body = client.post("/api/assistant",
                       json={"question": "am I underinsured and by how much"}).json()
    assert set(body) == {"answer", "route", "navigate", "mode", "suggestions",
                         "boundary", "role"}
    blob = str(body)
    for leak in ("rule_id", "span_id", "char_start", "PROVEN", "sum_insured",
                 "findings"):
        assert leak not in blob, f"the helper returned case data: {leak}"


def test_the_model_cannot_move_a_route_the_keywords_already_resolved(monkeypatch):
    """REGRESSION. The model was allowed to override any deterministic pick, and it made
    routing worse.

    An adviser asking "how do I mark this bound" resolves to Requests to action — "bound" is
    a keyword there and appears nowhere else — and the live model chose File note. That is a
    VALID id, so validating the id caught nothing; the judgement was simply worse than the
    table's. So the model now only decides where the table had no answer at all, which is
    where it is actually useful: a phrasing nobody thought to add a keyword for.

    Simulated with a model that always picks the wrong screen, which is the only way to test
    the rule without depending on a network call or a key.
    """
    wrong = "adv-note"
    monkeypatch.setattr(assistant.config, "ai_enabled", lambda: True)
    monkeypatch.setattr(assistant, "_model_pick",
                        lambda q, allowed: (wrong, "a sentence from the model", None))

    a = assistant.answer("how do I mark this bound", auth.ADVISER)
    assert a["route"]["id"] == "adv-actions", (
        "the model overrode a route the keywords had already resolved correctly")
    # It still supplied the wording, and the response still says a model was involved.
    assert a["mode"] == "LLM_ASSISTED"
    assert "a sentence from the model" in a["answer"]


def test_the_model_still_decides_where_the_keywords_found_nothing(monkeypatch):
    """The other half of the same rule: it must not be neutered into doing nothing."""
    monkeypatch.setattr(assistant.config, "ai_enabled", lambda: True)
    monkeypatch.setattr(assistant, "_model_pick",
                        lambda q, allowed: ("cyber", "Model chose this.", None))

    # A phrasing with no keyword anywhere in the catalogue.
    assert assistant.answer("zzz qqq", auth.SME, allow_model=False)["route"] is None
    a = assistant.answer("zzz qqq", auth.SME)
    assert a["route"] is not None and a["route"]["id"] == "cyber"
    assert a["mode"] == "LLM_ASSISTED"


def test_an_invented_destination_id_from_the_model_is_discarded(monkeypatch):
    """A model that could emit its own id could navigate somebody nowhere."""
    monkeypatch.setattr(assistant.config, "ai_enabled", lambda: True)
    monkeypatch.setattr(assistant, "_model_pick",
                        lambda q, allowed: ("sme-does-not-exist", "hello", None))
    a = assistant.answer("zzz qqq", auth.SME)
    assert a["route"] is None, "an unknown id was accepted as a destination"


def test_the_model_cannot_reach_across_a_role_even_with_a_valid_id(monkeypatch):
    """`car-decisions` is a real id — just not one this caller may be sent to. The model is
    only ever handed its own role's list, and the validation is against that same list."""
    monkeypatch.setattr(assistant.config, "ai_enabled", lambda: True)
    monkeypatch.setattr(assistant, "_model_pick",
                        lambda q, allowed: ("car-decisions", "hello", None))
    a = assistant.answer("zzz qqq", auth.SME)
    assert a["route"] is None
