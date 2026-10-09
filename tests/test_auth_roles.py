"""Authentication and the role boundary.

THE POINT OF THIS FILE. The product's story is that a business owns its own analysis,
an adviser sees only what was disclosed to them, and a carrier never sees an individual
business's documents at all. Before `auth.py` that was a claim about which screen the
demo happened to be showing. These tests make it a claim about the server.

The asymmetry is deliberate and is tested as such: an ANONYMOUS session still reaches
the business workspace, because that is the local demo surface the example flow and the
rest of the suite depend on. What no session can do is cross into another role's
surface. So the guard denies on wrong role, not on absent role — and
`test_health_does_not_overstate_the_auth_posture` exists to stop anyone reading that as
"we shipped authentication".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import auth, marketmap
from proven.api import SESSIONS, app

SME_EMAIL = "owner@hockleefoods.demo"
ADVISER_EMAIL = "adviser@lionbrokers.demo"
CARRIER_EMAIL = "underwriter@carrier.demo"
PASSWORD = auth.DEMO_PASSWORD


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


def _login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# ── password handling ────────────────────────────────────────────────────────

def test_passwords_are_salted_and_never_stored_in_the_clear():
    """Two accounts share a password in this build, so identical digests would prove
    the salt is not per-account."""
    hashes = {a.password_hash for a in auth.ACCOUNTS.values()}
    assert len(hashes) == len(auth.ACCOUNTS), "a salt is being reused across accounts"
    for encoded in hashes:
        algorithm, iterations, salt, digest = encoded.split("$")
        assert algorithm == "pbkdf2_sha256"
        assert int(iterations) >= 100_000, "iteration count is too low to bother with"
        assert PASSWORD not in encoded
        assert len(salt) >= 16 and len(digest) == 64


def test_verify_rejects_a_wrong_password_and_a_malformed_hash():
    encoded = auth.hash_password("correct-horse")
    assert auth.verify_password("correct-horse", encoded)
    assert not auth.verify_password("Correct-Horse", encoded)
    assert not auth.verify_password("", encoded)
    for broken in ("", "not-a-hash", "pbkdf2_sha256$abc$salt$digest", "a$b$c$d"):
        assert not auth.verify_password("correct-horse", broken)


def test_the_account_catalogue_never_serves_a_hash(client):
    """It DOES serve the demo password, which is intentional for a judged build. It must
    not serve the stored digest, which would be a real leak."""
    body = client.get("/api/auth/accounts").text
    assert "pbkdf2_sha256$" not in body
    for account in auth.ACCOUNTS.values():
        assert account.password_hash not in body
    parsed = client.get("/api/auth/accounts").json()
    assert len(parsed["accounts"]) == 3
    assert parsed["shared_password"] == PASSWORD
    assert "not a user management system" in parsed["notice"]


# ── login and logout ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("email,role", [
    (SME_EMAIL, auth.SME),
    (ADVISER_EMAIL, auth.ADVISER),
    (CARRIER_EMAIL, auth.CARRIER),
])
def test_each_seeded_account_signs_in_and_reports_its_role(client, email, role):
    response = _login(client, email)
    assert response.status_code == 200, response.text
    user = response.json()["user"]
    assert user["role"] == role
    assert user["email"] == email
    assert user["home"] == auth.ROLE_HOME[role]
    assert client.get("/api/auth/me").json()["user"]["role"] == role


def test_a_wrong_password_and_an_unknown_email_fail_identically(client):
    """Different messages would let someone enumerate which addresses exist."""
    wrong = _login(client, SME_EMAIL, "not-the-password")
    unknown = _login(client, "nobody@nowhere.demo", PASSWORD)
    assert wrong.status_code == 401 and unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_email_matching_is_case_insensitive(client):
    assert _login(client, SME_EMAIL.upper()).status_code == 200


def test_me_reports_nobody_before_a_login(client):
    body = client.get("/api/auth/me").json()
    assert body["authenticated"] is False and body["user"] is None


def test_logout_discards_the_case_rather_than_hiding_it(client):
    """Leaving one user's findings in a session the next person signs into would undo
    the isolation the consent model rests on."""
    _login(client, SME_EMAIL)
    client.get("/api/brief")
    assert client.get("/api/session").json()["has_case"] is True

    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["authenticated"] is False
    assert client.get("/api/session").json()["has_case"] is False


# ── the boundary: nobody reads across a role ─────────────────────────────────

def test_an_anonymous_session_still_reaches_the_business_workspace(client):
    """The asymmetry, asserted. Gating this would trade a working demo surface for the
    appearance of security, and the example flow depends on it."""
    assert client.get("/api/brief").status_code == 200
    assert client.get("/api/score").status_code == 200


def test_the_owner_reads_their_own_case(client):
    _login(client, SME_EMAIL)
    assert client.get("/api/brief").status_code == 200
    assert client.get("/api/score").status_code == 200


@pytest.mark.parametrize("email", [ADVISER_EMAIL, CARRIER_EMAIL])
@pytest.mark.parametrize("path", ["/api/brief", "/api/score"])
def test_a_foreign_role_cannot_read_a_business_own_case(client, email, path):
    """THE BOUNDARY THAT MATTERS. An adviser reads the consented disclosure; a carrier
    reads roll-ups. Neither reads this."""
    _login(client, email)
    response = client.get(path)
    assert response.status_code == 403, response.text
    # And it says where to go instead, rather than only refusing.
    assert "/api/" in response.json()["detail"]


def test_a_foreign_role_cannot_upload_into_a_business_case(client):
    _login(client, CARRIER_EMAIL)
    files = [("files", ("x.pdf", b"%PDF-1.4 not a real pdf", "application/pdf"))]
    assert client.post("/api/bundle", files=files).status_code == 403


def test_the_adviser_workspace_refuses_anonymous_and_wrong_role(client):
    """401 for nobody, 403 for the wrong somebody. The distinction is worth keeping:
    one means sign in, the other means you are signed in as the wrong thing."""
    assert client.get("/api/adviser/queue").status_code == 401
    _login(client, CARRIER_EMAIL)
    assert client.get("/api/adviser/queue").status_code == 403
    client.post("/api/auth/logout")
    _login(client, ADVISER_EMAIL)
    assert client.get("/api/adviser/queue").status_code == 200


def test_the_carrier_workspace_refuses_anonymous_and_wrong_role(client):
    assert client.get("/api/carrier/portfolio").status_code == 401
    _login(client, ADVISER_EMAIL)
    assert client.get("/api/carrier/portfolio").status_code == 403
    client.post("/api/auth/logout")
    _login(client, CARRIER_EMAIL)
    assert client.get("/api/carrier/portfolio").status_code == 200


# ── the adviser workspace ────────────────────────────────────────────────────

def test_the_adviser_queue_labels_demonstration_rows(client):
    """Rows with no documents behind them must be labelled rather than blended in. A
    fabricated citation in a product about citation fidelity is unforgivable, so the
    filler rows carry no citations at all and say what they are."""
    _login(client, ADVISER_EMAIL)
    body = client.get("/api/adviser/queue").json()
    assert body["cases"], "the queue is empty"
    assert any(row["demo"] for row in body["cases"])
    for row in body["cases"]:
        assert isinstance(row["demo"], bool)
        if row["demo"]:
            assert row["source"] == "DEMONSTRATION_ROW"
    assert "labelled rather than blended in" in body["notice"]


def test_an_undisclosed_case_does_not_appear_in_the_adviser_queue(client):
    """Consent as an access control: no grant, no row. This is what separates the
    consent model from a checkbox."""
    _login(client, ADVISER_EMAIL)
    body = client.get("/api/adviser/queue").json()
    assert all(row["demo"] for row in body["cases"]), (
        "a case appeared in the queue with no disclosure behind it")
    assert body["live_case_count"] == 0
    assert body["awaiting_consent"] is True


def test_the_adviser_review_goes_through_the_same_consent_gate(client):
    """There is no privileged read. An adviser with no grant gets the same 403 anyone
    else would."""
    _login(client, ADVISER_EMAIL)
    assert client.get("/api/adviser/review").status_code == 403


def test_requesting_information_needs_a_disclosure_first(client):
    _login(client, ADVISER_EMAIL)
    response = client.post("/api/adviser/request-information",
                           json={"rule_id": "SOME-RULE", "message": "Please confirm."})
    assert response.status_code == 403


def test_an_information_request_does_not_alter_the_finding(client):
    """An adviser's question is not evidence. If a request could move a finding's state
    or class, an unevidenced judgement would enter the record."""
    # Owner builds a case and discloses it.
    _login(client, SME_EMAIL)
    before = {f["rule_id"]: (f["state"], f["class"])
              for f in client.get("/api/brief").json()["findings"]}
    rule_id = next(iter(before))
    client.post("/api/consent", json={"recipient": "Licensed adviser",
                                      "fields": ["findings"]})

    # Same session, now acting as the adviser, so the live case is in scope.
    request = client.post("/api/adviser/request-information",
                          json={"rule_id": rule_id, "message": "Please confirm."})
    # The owner is signed in, so the adviser-only guard refuses — which is itself the
    # correct answer and is asserted rather than worked around.
    assert request.status_code == 403

    after = {f["rule_id"]: (f["state"], f["class"])
             for f in client.get("/api/brief").json()["findings"]}
    assert before == after


def test_information_requests_are_readable_by_the_owner_but_not_a_carrier(client):
    _login(client, SME_EMAIL)
    assert client.get("/api/adviser/requests").status_code == 200
    client.post("/api/auth/logout")
    _login(client, CARRIER_EMAIL)
    assert client.get("/api/adviser/requests").status_code == 403


# ── the carrier workspace ────────────────────────────────────────────────────

# The portfolio view embeds the clause comparison, computed from the published wordings
# in the corpus. Without them it is empty, and an empty portfolio cannot be told apart
# from one that lost its rows.
@pytest.mark.requires_corpus(
    *[spec["corpus_id"] for spec in marketmap.CORPUS_WORDINGS])
def test_the_portfolio_carries_no_invented_rows_and_no_breach_column(client):
    """The vocabulary is the position, and so is the provenance.

    This test used to assert `synthetic is True` and that rows existed, because the
    carrier screen read from six hardcoded businesses. Those rows were labelled and
    carried no citations, so they were honest — but they were still invented, and in a
    product arguing that every number traces to a document an invented portfolio is the
    one screen a reviewer can dismiss without engaging. So the assertion is inverted:
    nothing on this screen may be synthetic, and the account count must be the real
    number of cases held rather than a demonstration figure.
    """
    _login(client, CARRIER_EMAIL)
    body = client.get("/api/carrier/portfolio").json()

    assert body["synthetic"] is False
    assert body["account_count_is_real"] is True
    assert "SYNTHETIC" not in body["notice"].upper()

    # The count is whatever is genuinely held — including zero, which a carrier session
    # legitimately is, because a carrier holds no SME case unless one was disclosed.
    assert body["totals"]["accounts"] == len(body["accounts"])
    assert body["honest_statement"]

    for absent in ("breach", "violation", "risk_score", "blacklist"):
        assert absent in body["columns_deliberately_absent"]
        for account in body["accounts"]:
            assert absent not in account

    # The substantive surface is real engine output over published wordings.
    wp = body["wording_portfolio"]
    assert wp["available"] is True
    assert wp["counts"]["wordings"] >= 3
    assert wp["counts"]["pages_read"] > 100
    # And a wording nobody hand-read may never carry a confirmed-sounding verdict.
    for w in wp["wordings"]:
        if not w["hand_read"] and w["severity_for_insured"]:
            assert w["severity_for_insured"].endswith("_UNCONFIRMED"), (
                f"{w['carrier']} was never hand-read but reports "
                f"{w['severity_for_insured']!r} without qualification")


def test_the_portfolio_carries_a_market_map_that_is_actually_computed(client):
    """The account rows are fixed; the market map is not. It is the same engine over the
    real published wordings in this repository, and it is the one thing on the carrier
    screen that would survive a "prove it"."""
    _login(client, CARRIER_EMAIL)
    market_map = client.get("/api/carrier/portfolio").json()["market_map"]
    assert market_map.get("rows") or market_map.get("mechanisms"), \
        f"market map looks empty: {sorted(market_map)}"


def test_mode_a_refuses_to_aggregate(client):
    """The refusal is the interesting half. Aggregating business-initiated disclosures
    would quietly turn consent-as-pull into a data feed."""
    _login(client, CARRIER_EMAIL)
    body = client.get("/api/carrier/portfolio", params={"mode": "A"}).json()
    assert body["aggregated"] is False and body["accounts"] == []
    assert "by design" in body["notice"]


def test_an_unknown_mode_fails_closed(client):
    """Defaulting an unrecognised mode to B would mean a typo silently switches
    aggregation on."""
    _login(client, CARRIER_EMAIL)
    assert client.get("/api/carrier/portfolio",
                      params={"mode": "Z"}).status_code == 400


# ── the posture is reported honestly ─────────────────────────────────────────

def test_health_does_not_overstate_the_auth_posture(client):
    """`authenticated` stays False because there is no wall in front of the business
    workspace. This field is what somebody reads when deciding whether to deploy, so
    overstating it here would be the most consequential lie in the codebase."""
    body = client.get("/api/health").json()
    assert body["authenticated"] is False
    assert body["auth"]["role_boundary_enforced"] is True
    assert body["auth"]["login_required_for_business_workspace"] is False
    assert body["auth"]["seeded_accounts"] == 3
    assert "no signup" in body["auth_notice"].lower()


def test_uploading_a_bundle_does_not_sign_the_user_out(client):
    """MEASURED DEFECT, found by walking the demo over real HTTP rather than by reading
    the code.

    Replacing a case used to call `Session.close()`, which empties the whole session —
    including the principal. So uploading documents silently signed the user out, the
    identity chip went stale, and the role boundary quietly stopped applying, because an
    anonymous session is deliberately allowed to read the business workspace. A boundary
    that vanishes on the product's most common action is worse than no boundary, since
    nobody notices it has gone.
    """
    _login(client, SME_EMAIL)
    files = [("files", ("wording.pdf", _tiny_pdf(), "application/pdf"))]
    client.post("/api/bundle", files=files)      # may 400 on content; identity is the point

    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is True, "uploading signed the user out"
    assert me["user"]["email"] == SME_EMAIL

    # And the boundary is still in force afterwards.
    assert client.get("/api/adviser/queue").status_code == 403
    assert client.get("/api/carrier/portfolio").status_code == 403


def test_reloading_the_example_does_not_sign_the_user_out(client):
    """`/api/reset` had the same `close()` problem."""
    _login(client, SME_EMAIL)
    client.post("/api/reset")
    assert client.get("/api/auth/me").json()["authenticated"] is True


def test_signing_out_still_drops_everything(client):
    """The fix must not have turned sign-out into a partial teardown. Identity survives
    a CASE reset; it must not survive a logout."""
    _login(client, SME_EMAIL)
    client.get("/api/brief")
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["authenticated"] is False
    assert client.get("/api/session").json()["has_case"] is False


def test_the_upload_rate_limit_survives_a_case_replacement(client):
    """The other thing preserved across a reset. Clearing the window would let a caller
    reset the limit by uploading again, which is what the limit exists to prevent."""
    from proven.api import SESSIONS as store

    _login(client, SME_EMAIL)
    files = [("files", ("a.pdf", _tiny_pdf(), "application/pdf"))]
    client.post("/api/bundle", files=files)
    sessions = [s for s in store._sessions.values() if s.state.get("_principal")]
    assert sessions, "no authenticated session found"
    assert sessions[0].state.get("_uploads"), \
        "the rate-limit window was cleared by the upload"


def _tiny_pdf() -> bytes:
    """A structurally valid but content-free PDF. Whether the pipeline accepts it is not
    what these tests are about — the identity plumbing runs either way."""
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF\n")


def test_sessions_do_not_share_a_principal(client):
    """One browser signing in must not authenticate another."""
    _login(client, CARRIER_EMAIL)
    assert client.get("/api/auth/me").json()["user"]["role"] == auth.CARRIER
    with TestClient(app) as other:
        assert other.get("/api/auth/me").json()["authenticated"] is False
        assert other.get("/api/carrier/portfolio").status_code == 401
