"""The notification endpoints.

The tests that matter here are about RESTRAINT, not delivery. Anyone can build something
that sends an email. The properties that decide whether an owner keeps the feature switched
on are: the first check tells nobody anything, the same change is never sent twice, and a
message withheld by a preference is still recorded with the reason it was withheld.

Offline throughout. `conftest` forces PROVEN_LIVE_SOURCES=0 and strips the model keys, so
Discovery reads its fixtures and the wording is the deterministic template.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proven import discovery, mailer, notifications as notif, watchlist
from proven.api import SESSIONS, app


def _reset_stores():
    watchlist.WATCHLIST.forget()
    notif.NOTIFICATIONS.forget()
    mailer.OUTBOX.clear()


@pytest.fixture
def client():
    """A clean client, a stopped scheduler, and clean shared stores.

    Stopping the scheduler is the part that matters. Entering `TestClient(app)` runs the
    app's startup hook, which starts the real background scheduler, and its very first act
    is a Discovery run — so by the time a test made its first assertion the watchlist had
    already been observed and notifications already existed. That was harmless while
    Discovery wrote nowhere; now that it feeds the watchlist, a test asserting "the inbox is
    empty" was racing a live agent.

    So: let the lifecycle run properly (it is worth exercising), then stop the thread and
    reset the stores, and let the test drive the checks itself.
    """
    SESSIONS.clear()
    with TestClient(app) as c:
        from proven import scheduler
        scheduler.SCHEDULER.stop()
        _reset_stores()
        yield c
    SESSIONS.clear()
    _reset_stores()


def _fact(fact_type, value, idx=1):
    return {
        "id": f"DISC-WEB-{fact_type}-{idx}",
        "fact_type": fact_type,
        "label": f"{fact_type} on the website",
        "value": value,
        "previous_value": "Unit 03-11, 21 Tai Seng Avenue",
        "claim_scope": "DISCOVERY_HINT",
        "severity": "MODERATE",
        "grounded": True,
        "extraction": "PATTERN",
        "source": {"id": "disc-web", "name": "Company's own website",
                   "author_class": "SELF"},
    }


def _result(*facts):
    return {
        "facts": list(facts), "demo_mode": True,
        "fetch_log": [{"source": "Website", "url": "http://localhost", "ok": True}],
        "extraction": {"model_used": False, "pattern_facts": len(facts),
                       "model_facts": 0, "discarded_ungrounded": 0},
    }


ONE = _result(_fact("LOCATION", "#03-11, 21 Tai Seng Avenue", 1))
TWO = _result(_fact("LOCATION", "#03-11, 21 Tai Seng Avenue", 1),
              _fact("LOCATION", "#05-01, 9 Kaki Bukit Road 1", 2))


def _pin(monkeypatch, result):
    monkeypatch.setattr(discovery, "discover_business", lambda **k: result)


# ── the empty state ──────────────────────────────────────────────────────────

def test_a_new_session_has_an_empty_inbox(client):
    body = client.get("/api/notifications").json()
    assert body["notifications"] == []
    assert body["unread"] == 0
    assert body["counts"]["total"] == 0
    assert "Nothing here has changed your policy" in body["notice"]


def test_the_inbox_reports_the_mail_mode_without_leaking_a_secret(client,
                                                                 monkeypatch):
    monkeypatch.setenv("PROVEN_SMTP_PASSWORD", "do-not-leak-me")
    body = client.get("/api/notifications").json()
    assert "do-not-leak-me" not in str(body)
    assert body["mail"]["mode"] in ("SMTP", "OUTBOX_ONLY")


# ── check now ────────────────────────────────────────────────────────────────

def test_check_now_reports_what_it_concluded_not_that_it_started(client, monkeypatch):
    """A button that says a check began but cannot say what it found is indistinguishable
    from one that does nothing."""
    _pin(monkeypatch, ONE)
    body = client.post("/api/notifications/check-now").json()

    assert body["ok"] is True
    assert body["ran_at"]
    assert body["facts_found"] == 1
    assert body["summary"]
    assert body["duration_ms"] >= 0


def test_the_first_check_tells_nobody_anything(client, monkeypatch):
    """On a cold start everything looks new. Reporting it would mean announcing premises
    that have been there for years."""
    _pin(monkeypatch, TWO)
    body = client.post("/api/notifications/check-now").json()

    assert body["first_observation"] is True
    assert body["notifications_created"] == 0
    assert body["unread"] == 0
    assert client.get("/api/notifications").json()["notifications"] == []
    assert client.get("/api/notifications/outbox").json()["count"] == 0


def test_a_new_address_found_on_a_later_check_reaches_the_inbox(client, monkeypatch):
    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")

    _pin(monkeypatch, TWO)
    body = client.post("/api/notifications/check-now").json()

    assert body["first_observation"] is False
    assert body["notifications_created"] == 1
    assert body["unread"] == 1

    inbox = client.get("/api/notifications").json()
    item = inbox["notifications"][0]
    assert item["code"] == "NEW_LOCATION"
    assert item["severity"] == "HIGH"
    assert item["code_label"]
    assert item["title"]
    assert item["what_to_do"]
    assert item["evidence"]["caveat"], "an inbox has no evidence panel, so it must say so"
    assert inbox["last_checked"]
    assert inbox["facts_watched"] == 2


def test_checking_again_with_no_change_adds_nothing(client, monkeypatch):
    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")
    _pin(monkeypatch, TWO)
    client.post("/api/notifications/check-now")

    again = client.post("/api/notifications/check-now").json()

    assert again["notifications_created"] == 0
    assert len(client.get("/api/notifications").json()["notifications"]) == 1


def test_the_same_change_is_never_emailed_twice(client, monkeypatch):
    """The scheduler re-checks every few minutes and the address stays on the website until
    the policy is updated."""
    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")
    _pin(monkeypatch, TWO)
    for _ in range(4):
        client.post("/api/notifications/check-now")

    assert client.get("/api/notifications/outbox").json()["count"] == 1


# ── read state ───────────────────────────────────────────────────────────────

def test_marking_one_read_lowers_the_unread_count(client, monkeypatch):
    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")
    _pin(monkeypatch, TWO)
    client.post("/api/notifications/check-now")

    item_id = client.get("/api/notifications").json()["notifications"][0]["id"]
    body = client.post(f"/api/notifications/{item_id}/read").json()

    assert body["ok"] is True
    assert body["unread"] == 0
    assert client.get("/api/notifications", params={"unread_only": True}
                      ).json()["notifications"] == []


def test_marking_an_unknown_notification_read_is_a_404(client):
    assert client.post("/api/notifications/NTF-9999/read").status_code == 404


def test_read_all_reports_how_many_it_changed(client, monkeypatch):
    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")
    _pin(monkeypatch, TWO)
    client.post("/api/notifications/check-now")

    body = client.post("/api/notifications/read-all").json()
    assert body["marked_read"] == 1
    assert body["unread"] == 0
    assert client.post("/api/notifications/read-all").json()["marked_read"] == 0


# ── preferences ──────────────────────────────────────────────────────────────

def test_preferences_come_with_the_vocabulary_needed_to_render_them(client):
    body = client.get("/api/notifications/preferences").json()

    assert body["preferences"]["email_enabled"] is True
    assert body["preferences"]["minimum_severity"] == "MODERATE"
    assert body["severities"] == ["LOW", "MODERATE", "HIGH"]
    assert body["digests"] == [notif.IMMEDIATE, notif.DAILY]
    codes = {c["code"] for c in body["available_codes"]}
    assert "NEW_LOCATION" in codes
    assert all(c["label"] for c in body["available_codes"])
    assert "stops the email, not the check" in body["notice"]


def test_preferences_round_trip(client):
    client.post("/api/notifications/preferences",
                json={"minimum_severity": "HIGH", "digest": "DAILY"})
    prefs = client.get("/api/notifications/preferences").json()["preferences"]
    assert prefs["minimum_severity"] == "HIGH"
    assert prefs["digest"] == "DAILY"


def test_preferences_reject_values_outside_the_vocabulary(client):
    saved = client.post("/api/notifications/preferences",
                        json={"minimum_severity": "BANANA",
                              "codes": {"NOT_A_CODE": False}}).json()["preferences"]
    assert saved["minimum_severity"] == "MODERATE"
    assert "NOT_A_CODE" not in saved["codes"]


def test_turning_email_off_still_records_the_finding_and_says_why(client, monkeypatch):
    """Silently dropping a message because of a setting is indistinguishable from being
    broken."""
    client.post("/api/notifications/preferences", json={"email_enabled": False})

    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")
    _pin(monkeypatch, TWO)
    body = client.post("/api/notifications/check-now").json()

    assert body["notifications_created"] == 1
    item = client.get("/api/notifications").json()["notifications"][0]
    assert item["email_status"] == notif.SUPPRESSED
    assert "switched off" in item["email_detail"].lower()
    assert client.get("/api/notifications/outbox").json()["count"] == 0


# ── the outbox ───────────────────────────────────────────────────────────────

def test_the_outbox_holds_the_message_that_would_have_been_sent(client, monkeypatch):
    """With no mail server the message is composed in full and shown, never claimed as
    delivered."""
    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")
    _pin(monkeypatch, TWO)
    client.post("/api/notifications/check-now")

    box = client.get("/api/notifications/outbox").json()
    assert box["count"] == 1
    msg = box["messages"][0]
    assert msg["status"] == notif.NO_MAIL_SERVER
    assert msg["transport"] == "OUTBOX"
    assert msg["subject"]

    full = client.get(f"/api/notifications/outbox/{msg['id']}").json()
    assert full["text_body"]
    assert full["html_body"]
    assert "not told your insurer" in full["text_body"]


def test_an_unknown_message_is_a_404(client):
    assert client.get("/api/notifications/outbox/MSG-9999").status_code == 404


# ── the watch status panel ───────────────────────────────────────────────────

def test_the_watch_panel_says_what_is_watched_and_how_memory_behaves(client):
    body = client.get("/api/notifications/watch").json()

    assert body["website_watched"]
    assert body["checks_every_seconds"] > 0
    assert "LOCATION" in body["watchlist"]["alertable_types"]
    assert "restart" in body["watchlist"]["notice"].lower()
    assert "reworded" in body["alertable_note"]


# ── the role boundary applies here too ───────────────────────────────────────

def test_an_adviser_may_not_read_the_owners_inbox(client):
    """Same asymmetry as the rest of the business workspace: an adviser reads the consented
    disclosure, not the owner's notifications."""
    client.post("/api/auth/login",
                json={"email": "adviser@lionbrokers.demo", "password": "Demo@123"})
    assert client.get("/api/notifications").status_code == 403
    assert client.post("/api/notifications/check-now").status_code == 403


def test_the_owner_may(client):
    client.post("/api/auth/login",
                json={"email": "owner@hockleefoods.demo", "password": "Demo@123"})
    assert client.get("/api/notifications").status_code == 200


# ── starting again once the policy has actually been updated ─────────────────
#
# The deduplication that stops repeat emails also means a HANDLED item can never be
# cleared, so without this the inbox only ever grows and a rehearsed demo can never be
# repeated. The honest framing is "I have dealt with these": the schedule is updated, the
# alerts that prompted it are resolved, and today's state becomes the new normal.

def test_restart_watch_clears_the_list_and_rebaselines(client, monkeypatch):
    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")
    _pin(monkeypatch, TWO)
    client.post("/api/notifications/check-now")
    assert len(client.get("/api/notifications").json()["notifications"]) == 1

    body = client.post("/api/notifications/restart-watch").json()
    assert body["ok"] is True
    assert body["unread"] == 0
    assert "no finding" in body["notice"].lower()

    after = client.get("/api/notifications").json()
    assert after["notifications"] == []
    assert after["unread"] == 0
    assert after["last_checked"] is None, "the baseline is gone, not just the list"
    # The mail log goes with it. Otherwise the interface shows an email about a change the
    # user cannot find anywhere, which reads as a bug rather than as history.
    assert client.get("/api/notifications/outbox").json()["count"] == 0


def test_the_next_check_after_restarting_raises_nothing(client, monkeypatch):
    """Today's state becomes the new normal, so the branch already on the website is not
    re-reported as though it had just appeared."""
    _pin(monkeypatch, ONE)
    client.post("/api/notifications/check-now")
    _pin(monkeypatch, TWO)
    client.post("/api/notifications/check-now")
    client.post("/api/notifications/restart-watch")

    fresh = client.post("/api/notifications/check-now").json()
    assert fresh["first_observation"] is True
    assert fresh["notifications_created"] == 0


def test_a_genuine_change_after_restarting_is_still_reported(client, monkeypatch):
    """Clearing must not switch the watching off."""
    _pin(monkeypatch, TWO)
    client.post("/api/notifications/check-now")
    client.post("/api/notifications/restart-watch")
    client.post("/api/notifications/check-now")          # new baseline

    three = _result(*TWO["facts"],
                    _fact("LOCATION", "#02-14, 15 Jurong Port Road", 3))
    _pin(monkeypatch, three)
    out = client.post("/api/notifications/check-now").json()

    assert out["notifications_created"] == 1


def test_an_adviser_may_not_restart_the_owners_watch(client):
    client.post("/api/auth/login",
                json={"email": "adviser@lionbrokers.demo", "password": "Demo@123"})
    assert client.post("/api/notifications/restart-watch").status_code == 403
