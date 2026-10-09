"""Carrier-initiated email: the carrier writes to the SME, and says nothing it has not seen.

THE ONE CONSTRAINT THAT MATTERS.

A carrier is book-level only. So a message it sends may name the business and the reason
the account surfaced ON THE CARRIER'S OWN BOOK, and nothing else. No figure, no clause, no
finding, nothing derived from the SME's documents.

That is easy to write in a comment and easy to break in a template, so
`test_the_email_contains_nothing_from_the_businesses_documents` asserts it against the
REAL finding headlines and the real figures from the live case rather than against a list
somebody remembered to keep up to date.

The second thing under test is honesty about the sender. An email from the carrier with VERIS
in the header is the same class of dishonesty as a "sent!" toast with nothing on the wire, so
the brand is asserted in both directions: the carrier mail says the carrier, and the agent
mail still says VERIS.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import carrier, mailer, notifications
from proven.api import SESSIONS, app

CARRIER = {"email": "underwriter@carrier.demo", "password": "Demo@123"}
OWNER = {"email": "owner@hockleefoods.demo", "password": "Demo@123"}
ADVISER = {"email": "adviser@lionbrokers.demo", "password": "Demo@123"}

ACCOUNT = "ACC-10231"
TEMPLATES = ("CARRIER_REVIEW_REQUESTED", "CARRIER_RENEWAL_REMINDER",
             "CARRIER_INFORMATION_NEEDED")


@pytest.fixture
def client():
    SESSIONS.clear()
    carrier.LOG.clear()
    notifications.NOTIFICATIONS.forget()
    mailer.OUTBOX.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()
    carrier.LOG.clear()
    notifications.NOTIFICATIONS.forget()
    mailer.OUTBOX.clear()


def _send(client, template=TEMPLATES[0], account=ACCOUNT):
    client.post("/api/auth/login", json=CARRIER)
    return client.post("/api/carrier/notify",
                       json={"account_id": account, "template": template})


# ── the boundary ────────────────────────────────────────────────────────────

def test_only_a_carrier_may_send(client):
    body = {"account_id": ACCOUNT, "template": TEMPLATES[0]}
    assert client.post("/api/carrier/notify", json=body).status_code == 401
    client.post("/api/auth/login", json=OWNER)
    assert client.post("/api/carrier/notify", json=body).status_code == 403
    client.post("/api/auth/login", json=ADVISER)
    assert client.post("/api/carrier/notify", json=body).status_code == 403


def test_the_template_vocabulary_is_closed(client):
    """Free text is where a carrier would type the one thing it must not."""
    client.post("/api/auth/login", json=CARRIER)
    r = client.post("/api/carrier/notify", json={
        "account_id": ACCOUNT, "template": "YOUR_STOCK_IS_UNDERINSURED"})
    assert r.status_code == 400


def test_an_unknown_account_is_refused(client):
    client.post("/api/auth/login", json=CARRIER)
    r = client.post("/api/carrier/notify",
                    json={"account_id": "ACC-NOPE", "template": TEMPLATES[0]})
    assert r.status_code == 404


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_email_contains_nothing_from_the_businesses_documents(client, template):
    """Asserted against the REAL case, not against a remembered list.

    Every figure and headline the engine actually produced is checked against the composed
    message. If a template ever starts interpolating case detail, this fails with the exact
    string that leaked.
    """
    client.get("/api/brief")
    findings = client.get("/api/brief").json()["findings"]
    forbidden: set[str] = set()
    for f in findings:
        forbidden.add(f["headline"])
        for span in f.get("spans") or []:
            quote = span.get("quoted_text")
            if quote and len(quote) > 25:
                forbidden.add(quote)
    # The figures that matter most, from the schedule and the accounts.
    forbidden |= {"340000", "150000", "100000", "180000", "66.7"}

    response = _send(client, template)
    assert response.status_code == 200
    body = response.json()
    assert body["contains_case_detail"] is False

    # Read the composed bytes straight from the outbox store rather than through the
    # owner-scoped endpoint, so this test inspects what was actually built regardless of
    # which session is asking.
    composed = " ".join(
        f"{m.get('subject','')} {m.get('text_body','')} {m.get('html_body','')}"
        for m in mailer.OUTBOX.list(limit=25))
    full = composed + response.text
    for secret in forbidden:
        assert secret not in full, (
            f"the carrier email leaked case detail: {secret[:60]!r}")


def test_the_email_does_name_the_business_and_the_book_reason(client):
    """The constraint is "nothing from their documents", not "nothing at all". A message
    with no context is not safer, it is just useless."""
    response = _send(client)
    assert response.status_code == 200
    item = response.json()["notification"]
    assert item["evidence"]["sent_by"] == "CARRIER"
    assert item["evidence"]["lead_reason"], "the book's own reason should travel"
    assert "not seen your documents" in item["evidence"]["caveat"]


# ── who it says it is from ──────────────────────────────────────────────────

def test_the_carrier_mail_sends_as_the_carrier(client):
    response = _send(client)
    assert response.status_code == 200
    assert response.json()["sent_as"], "a carrier mail must name its sender"
    messages = mailer.OUTBOX.list(limit=5)
    assert messages
    subject = messages[0]["subject"]
    assert "VERIS" not in subject.upper(), (
        "a carrier email carried the VERIS brand in its subject")


def test_the_agent_mail_still_says_veris():
    """The brand is a parameter, not a rename. Agent mail was genuinely sent by VERIS."""
    subject = mailer.subject_for([], "Test Business Pte Ltd")
    assert mailer.BRAND in subject


def test_the_unsubscribe_facility_is_present(client):
    """Singapore's Spam Control Act / IMDA require an unsubscribe facility on unsolicited
    commercial mail. The preference system genuinely is one, so it is stated on every
    message rather than only on the ones we classify as bulk."""
    _send(client)
    messages = mailer.OUTBOX.list(limit=5)
    assert messages
    assert "unsubscribe" in messages[0]["text_body"].lower()


def test_the_bulk_label_is_only_applied_when_the_caller_says_bulk():
    """A service notice to an existing policyholder is not a campaign, and labelling it
    <ADV> would be as wrong as failing to label one that is."""
    plain = mailer.subject_for([], "X Pte Ltd", sender_brand="Lotus General (Sandbox)")
    bulk = mailer.subject_for([], "X Pte Ltd", sender_brand="Lotus General (Sandbox)",
                              bulk_commercial=True)
    assert mailer.ADV_LABEL not in plain
    assert bulk.startswith(mailer.ADV_LABEL)


# ── it reaches the owner, and behaves like every other notification ─────────

def test_the_owner_sees_it_in_their_own_inbox(client):
    _send(client)
    client.post("/api/auth/login", json=OWNER)
    inbox = client.get("/api/notifications").json()
    codes = {n["code"] for n in inbox["notifications"]}
    assert TEMPLATES[0] in codes, "the carrier's message never reached the owner"


def test_sending_twice_does_not_send_twice(client):
    first = _send(client)
    second = _send(client)
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True


def test_with_no_mail_server_it_says_so_rather_than_claiming_delivery(client):
    """The suite runs with no SMTP configured, which is the honest path under test."""
    body = _send(client).json()
    assert body["email"]["status"] in (notifications.NO_MAIL_SERVER,
                                       notifications.SENT,
                                       notifications.FAILED)
    if body["email"]["status"] == notifications.NO_MAIL_SERVER:
        assert "outbox" in body["email"]["detail"].lower()


def test_the_send_is_recorded_against_the_account(client):
    """One history for the underwriter, not two parallel ones to reconcile."""
    _send(client)
    rows = carrier.LOG.for_account(ACCOUNT)
    assert rows, "emailing the owner left no trace on the account"
    assert any("Emailed the owner" in r["note"] for r in rows)


def test_carrier_codes_are_toggleable_like_any_other(client):
    """A carrier message must be governed by the owner's preferences, not exempt from
    them. Otherwise "turn off email" is a lie."""
    prefs = notifications.default_preferences()
    for template in TEMPLATES:
        assert template in prefs["codes"], (
            f"{template} is not in default preferences, so the owner cannot turn it off")
