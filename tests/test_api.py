"""API behaviour: session isolation, the consent gate as an access control, change
events, record export, and the security headers.

The consent tests are the ones that matter most. "Consent is a pull, not a share" was
previously a checkbox that toggled a boolean while `/api/brief` served everything to
anyone who could reach the port.
"""
import pdfbuild
import pytest
from fastapi.testclient import TestClient

from proven import pipeline
from proven.api import SESSIONS, app


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


@pytest.fixture
def second_client():
    with TestClient(app) as c:
        yield c


# ── sessions ────────────────────────────────────────────────────────────────

def test_health_declares_the_limits_of_its_authentication(client):
    """This assertion used to be "no authentication", and it changed when `auth.py`
    added three seeded accounts and a server-side role boundary. What must NOT change
    is that the endpoint keeps admitting what is still missing: there is no login wall
    in front of the business workspace, and no signup, reset or rotation behind the
    three fixture accounts. `authenticated` therefore stays False.

    This field is what somebody reads when deciding whether to put the port on a
    network, so overstating it would be the most consequential lie in the codebase —
    which is why the honest limits are pinned here as well as in test_auth_roles.py.
    """
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["authenticated"] is False
    notice = body["auth_notice"].lower()
    assert "no signup" in notice
    assert "do not expose this port" in notice
    assert body["auth"]["login_required_for_business_workspace"] is False


def test_session_cookie_is_httponly_and_samesite(client):
    r = client.get("/api/brief")
    assert r.status_code == 200
    cookie_header = r.headers.get("set-cookie", "")
    assert "httponly" in cookie_header.lower()
    assert "samesite=lax" in cookie_header.lower()


def test_two_sessions_do_not_share_a_case(client, second_client):
    """The old module-level _STATE meant one caller's delete emptied everyone's
    findings, so the demo could not be shown to two judges at once."""
    a = client.get("/api/brief").json()
    b = second_client.get("/api/brief").json()
    assert a["case_id"] != b["case_id"]

    client.delete("/api/case")
    assert client.get("/api/brief").json()["findings"] == []
    # The other session is untouched.
    assert len(second_client.get("/api/brief").json()["findings"]) > 10


def test_consent_granted_in_one_session_does_not_leak(client, second_client):
    second_client.get("/api/brief")
    client.post("/api/consent", json={"recipient": "Adviser A",
                                      "fields": ["SCHEDULE"]})
    assert client.get("/api/consent/status").json()["granted"] is True
    assert second_client.get("/api/consent/status").json()["granted"] is False


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


# ── the consent gate is an access control ───────────────────────────────────

def test_disclosed_brief_refused_without_consent(client):
    client.get("/api/brief")
    r = client.get("/api/disclosure/brief?recipient=Licensed%20adviser")
    assert r.status_code == 403
    assert "disclosure" in r.json()["error"]["message"].lower()


def test_disclosed_brief_filters_to_granted_documents(client):
    client.get("/api/brief")
    client.post("/api/consent", json={"recipient": "Licensed adviser",
                                      "fields": ["SCHEDULE", "CPF_RECORD"]})
    body = client.get("/api/disclosure/brief?recipient=Licensed%20adviser").json()
    assert body["disclosed_fields"] == ["CPF_RECORD", "SCHEDULE"]
    # Findings evidenced by undisclosed documents are withheld, and COUNTED, so the
    # adviser can ask rather than be handed a silently shortened list.
    assert body["findings_withheld_count"] > 0
    assert body["withheld_pending_documents"]
    # Pinned SHAPE, not just truthiness. This is a mapping of document kind -> how many
    # withheld findings rest on it, and the Review Brief screen reads it as such. It was
    # once consumed with `.length`, which yields undefined on an object and printed the
    # word "undefined" into a stat tile. If this ever becomes a list, the two readers in
    # app.js (_wpdKinds / _wpdCount) already cope, but this assertion should be the thing
    # that tells you the contract moved.
    wpd = body["withheld_pending_documents"]
    assert isinstance(wpd, dict), f"expected a mapping, got {type(wpd).__name__}"
    assert all(isinstance(k, str) and k.isupper() for k in wpd), (
        f"document kinds should be upper-case tokens: {sorted(wpd)}")
    assert all(isinstance(v, int) and v > 0 for v in wpd.values()), (
        f"counts should be positive integers: {wpd}")
    assert body["accounting_withheld"] is True
    full = len(client.get("/api/brief").json()["findings"])
    assert len(body["findings"]) < full


def test_disclosure_named_recipient_only(client):
    client.get("/api/brief")
    client.post("/api/consent", json={"recipient": "Adviser A",
                                      "fields": ["SCHEDULE"]})
    assert client.get("/api/disclosure/brief?recipient=Adviser%20A").status_code == 200
    # A grant to A is not a grant to B.
    assert client.get("/api/disclosure/brief?recipient=Adviser%20B").status_code == 403


def test_revocation_closes_access(client):
    client.get("/api/brief")
    client.post("/api/consent", json={"recipient": "Adviser A",
                                      "fields": ["SCHEDULE"]})
    assert client.get("/api/disclosure/brief?recipient=Adviser%20A").status_code == 200
    revoked = client.post("/api/consent/revoke", json={"recipient": "Adviser A"})
    assert revoked.json()["revoked"] == 1
    assert revoked.json()["granted"] is False
    assert client.get("/api/disclosure/brief?recipient=Adviser%20A").status_code == 403


def test_empty_consent_field_list_refused(client):
    client.get("/api/brief")
    r = client.post("/api/consent", json={"recipient": "A", "fields": []})
    assert r.status_code == 422        # granular consent must name something


def test_deleting_the_case_revokes_live_disclosures(client):
    client.get("/api/brief")
    client.post("/api/consent", json={"recipient": "Adviser A",
                                      "fields": ["SCHEDULE"]})
    body = client.delete("/api/case").json()
    assert body["disclosures_remaining"] == 0
    assert client.get("/api/disclosure/brief?recipient=Adviser%20A").status_code == 403


def test_owner_can_always_see_their_own_brief(client):
    """Consent governs disclosure to a third party. Gating the owner out of their own
    analysis would be security theatre, not a guarantee."""
    body = client.get("/api/brief").json()
    assert len(body["findings"]) > 10


# ── change events ───────────────────────────────────────────────────────────

def test_event_catalogue_has_the_nine_events(client):
    body = client.get("/api/events").json()
    assert len(body["events"]) == 9
    codes = {e["code"] for e in body["events"]}
    assert "CHANGED_PREMISES_USE" in codes
    assert "STARTED_SELLING_ONLINE" in codes
    for e in body["events"]:
        assert e["relevant_rules"]
        assert e["asks_for"]


def test_recording_events_marks_relevant_findings(client):
    client.get("/api/brief")
    body = client.post("/api/events",
                       json={"codes": ["CHANGED_PREMISES_USE"]}).json()
    assert body["recorded"] == ["CHANGED_PREMISES_USE"]
    assert "ALTERATION-PREMISES-NOT-ON-SCHEDULE" in body["relevant_rules"]
    flagged = [f["rule_id"] for f in body["brief"]["findings"]
               if f.get("event_relevant")]
    assert "ALTERATION-PREMISES-NOT-ON-SCHEDULE" in flagged
    assert "CLAIM-TIME-LIMIT" not in flagged


def test_unknown_event_codes_are_dropped_not_stored(client):
    client.get("/api/brief")
    body = client.post("/api/events",
                       json={"codes": ["NOT_A_REAL_EVENT", "HEADCOUNT_CHANGE"]}).json()
    assert body["recorded"] == ["HEADCOUNT_CHANGE"]
    assert body["ignored_unknown_codes"] == ["NOT_A_REAL_EVENT"]


def test_events_never_change_a_findings_state_or_class(client):
    """An event is owner-asserted, so it may not raise evidence. It selects what is
    foregrounded and nothing else."""
    before = {f["rule_id"]: (f["state"], f["class"])
              for f in client.get("/api/brief").json()["findings"]}
    body = client.post("/api/events", json={
        "codes": [e["code"] for e in client.get("/api/events").json()["events"]]
    }).json()
    after = {f["rule_id"]: (f["state"], f["class"])
             for f in body["brief"]["findings"]}
    assert before == after


def test_events_can_be_cleared(client):
    client.get("/api/brief")
    client.post("/api/events", json={"codes": ["HEADCOUNT_CHANGE"]})
    assert client.delete("/api/events").json()["recorded"] == []


# ── record export ───────────────────────────────────────────────────────────

def test_record_export_is_downloadable_and_verifies(client):
    client.get("/api/brief")
    r = client.get("/api/record/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    doc = r.json()
    assert doc["format"] == "proven.record.v1"
    assert doc["verification"]["chain_valid"] is True
    assert client.post("/api/record/verify", json=doc).json()["chain_valid"] is True


def test_record_verify_rejects_a_non_record(client):
    assert client.post("/api/record/verify", json={"nope": 1}).status_code == 400


# ── upload through the API ──────────────────────────────────────────────────

def test_upload_replaces_the_session_case(client):
    client.get("/api/brief")
    r = client.post(
        "/api/bundle",
        files=[("files", ("schedule.pdf", pdfbuild.schedule_pdf(),
                          "application/pdf")),
               ("files", ("wording.pdf", pipeline.CHUBB_PDF.read_bytes(),
                          "application/pdf"))],
        data={"business_name": "Acme Foods Pte Ltd"},
    )
    assert r.status_code == 200
    b = r.json()["brief"]
    assert b["business"] == "Acme Foods Pte Ltd"
    assert b["synthetic"] is False
    assert client.get("/api/brief").json()["business"] == "Acme Foods Pte Ltd"


def test_upload_of_a_non_pdf_is_a_clear_400(client):
    r = client.post("/api/bundle",
                    files=[("files", ("notes.txt", b"hello", "text/plain"))])
    assert r.status_code == 400
    assert "PDF" in r.json()["error"]["message"]


def test_upload_declared_kind_is_a_hint_not_an_authority_claim(client):
    """Declaring a kind selects how the document is read. It cannot select the
    evidence class — that is derived."""
    r = client.post(
        "/api/bundle",
        files=[("files", ("mystery.pdf", pdfbuild.accounts_pdf(),
                          "application/pdf"))],
        data={"kinds": '["ACCOUNTS"]'},
    )
    assert r.status_code == 200
    entry = r.json()["intake"]["files"][0]
    assert entry["kind"] == "ACCOUNTS"
    assert entry["author_class"] == "SELF"          # never PROVEN by assertion


def test_upload_rejects_malformed_kinds(client):
    r = client.post("/api/bundle",
                    files=[("files", ("a.pdf", pdfbuild.schedule_pdf(),
                                      "application/pdf"))],
                    data={"kinds": "not json"})
    assert r.status_code == 400


def test_bundle_kinds_endpoint_explains_the_grading_consequence(client):
    body = client.get("/api/bundle/kinds").json()
    kinds = {k["kind"]: k for k in body["kinds"]}
    assert kinds["CPF_RECORD"]["author_class"] == "AUTHORITY"
    assert kinds["ACCOUNTS"]["grades_as"] == "DECLARED"


# ── book view ───────────────────────────────────────────────────────────────

def test_book_mode_a_refuses_to_aggregate(client):
    client.get("/api/brief")
    body = client.get("/api/book?mode=A").json()
    assert body["aggregated"] is False
    assert body["portfolio"] == []


def test_book_unknown_mode_fails_closed(client):
    client.get("/api/brief")
    assert client.get("/api/book?mode=Z").status_code == 400


def test_book_view_has_no_score_field(client):
    """Checks KEYS, not prose: the notice deliberately contains the word "violation"
    to invite a judge to look for the column. The guarantee is that no field is named
    after a black mark, not that the word is unsayable."""
    client.get("/api/brief")
    body = client.get("/api/book?mode=B").json()

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    for key in keys(body):
        low = key.lower()
        for banned in ("violation", "breach", "deficiency", "score"):
            assert banned not in low, f"{key} contains {banned}"
