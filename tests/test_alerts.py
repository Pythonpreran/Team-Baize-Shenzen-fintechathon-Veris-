"""Alert wording, the mail transport, and the pipeline that joins them.

Fully offline. `conftest` strips the model keys from the process, so the presenter takes its
deterministic path here and the wording is the written template — which is exactly the floor
that has to be correct, because it is what an owner receives whenever the model is
unavailable, rate-limited, or rejected by its own gates.
"""
from datetime import datetime, timedelta, timezone

import pytest

from proven import alerts, mailer, notifications as notif, presenter

BID = "T09HL1234A"
BNAME = "Hock Lee Foods Pte Ltd"
T0 = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
OBSERVED = {"observed_at": "2026-08-16T09:00:00+00:00"}


@pytest.fixture(autouse=True)
def _clean_outbox():
    mailer.OUTBOX.clear()
    presenter.clear_cache()
    yield
    mailer.OUTBOX.clear()


def new_locations(count=2, **kw):
    entry = {
        "code": "NEW_LOCATION", "kind": "NEW", "fact_type": "LOCATION",
        "count": count,
        "values": ["#05-01, 9 Kaki Bukit Road 1, Singapore 417818",
                   "#02-14, 15 Jurong Port Road, Singapore 619092"][:count],
        "policy_says": "Unit 03-11, 21 Tai Seng Avenue",
        "source_name": "Company's own website", "author_class": "SELF",
        "claim_scope": "DISCOVERY_HINT", "severity": "MODERATE",
        "dedupe_key": "loc-key",
    }
    entry.update(kw)
    return entry


def headcount_change(**kw):
    entry = {
        "code": "CHANGED_HEADCOUNT", "kind": "CHANGED", "fact_type": "HEADCOUNT",
        "count": 1, "values": ["21"], "previous_value": "14", "new_value": "21",
        "policy_says": "11", "source_name": "Company's own website",
        "author_class": "SELF", "claim_scope": "DISCOVERY_HINT",
        "severity": "MODERATE", "dedupe_key": "head-key",
    }
    entry.update(kw)
    return entry


def observation(*entries, first=False):
    return {
        "first_observation": first,
        "observed_at": OBSERVED["observed_at"],
        "summary": list(entries),
        "changes": list(entries),
        "alertable_changes": list(entries),
    }


# ── the presenter purpose ────────────────────────────────────────────────────

def test_the_alert_purpose_is_registered():
    assert alerts.ALERT_PURPOSE in presenter.PURPOSES


def test_new_premises_wording_names_the_count_and_the_schedule():
    pres, mode = alerts.compose_wording(new_locations(2), OBSERVED)
    assert mode == "TEMPLATE"
    assert "2 addresses" in pres["headline"]
    assert "Unit 03-11" in pres["subheadline"]
    assert pres["what_next"]
    assert pres["visual_emphasis"]["severity"] == "HIGH"


def test_one_new_premises_reads_as_singular():
    pres, _ = alerts.compose_wording(new_locations(1), OBSERVED)
    assert "An address" in pres["headline"] or "is not on your policy" in pres["headline"]
    assert "2 addresses" not in pres["headline"]


def test_headcount_wording_carries_both_numbers_and_the_declared_figure():
    pres, _ = alerts.compose_wording(headcount_change(), OBSERVED)
    assert "21" in pres["headline"]
    assert "14" in pres["headline"]
    assert "11" in pres["subheadline"]


def test_status_change_wording_is_about_the_registered_entity():
    pres, _ = alerts.compose_wording({
        "code": "CHANGED_STATUS", "kind": "CHANGED", "fact_type": "STATUS",
        "count": 1, "values": ["Struck off"], "previous_value": "Live",
        "new_value": "Struck off", "source_name": "ACRA BizFile+",
        "author_class": "AUTHORITY", "severity": "HIGH", "dedupe_key": "s"},
        OBSERVED)
    assert "Struck off" in pres["headline"]
    assert "Live" in pres["subheadline"]


def test_an_unknown_code_still_produces_safe_wording():
    pres, _ = alerts.compose_wording(
        {"code": "SOMETHING_NEW", "count": 1, "values": ["x"],
         "source_name": "Company's own website", "author_class": "SELF",
         "dedupe_key": "u"}, OBSERVED)
    assert pres["headline"]
    assert pres["why_it_matters"]


# ── the caveat is engine-owned, because an inbox has no evidence panel ───────

def test_a_self_authored_source_is_flagged_as_a_hint():
    pres, _ = alerts.compose_wording(new_locations(2), OBSERVED)
    assert "your own website" in pres["caveat"].lower()


def test_an_authority_source_says_so_differently():
    pres, _ = alerts.compose_wording(
        new_locations(1, author_class="AUTHORITY", source_name="ACRA"), OBSERVED)
    assert "government register" in pres["caveat"].lower()


def test_the_caveat_is_not_a_field_the_model_may_rewrite():
    """The model may reword the news. How far a source can be trusted is not news."""
    assert "caveat" not in presenter._MODEL_OWNS


def test_alert_wording_avoids_claim_outcome_language():
    """The same line the screen wording holds: never predict the outcome of a claim."""
    for entry in (new_locations(2), headcount_change()):
        pres, _ = alerts.compose_wording(entry, OBSERVED)
        blob = " ".join(str(v) for v in pres.values()).lower()
        for banned in presenter._FORBIDDEN_PHRASES:
            assert banned not in blob, f"{banned!r} appeared in alert wording"


# ── the mail transport ───────────────────────────────────────────────────────

def test_status_reports_presence_and_never_a_secret(monkeypatch):
    monkeypatch.setenv("PROVEN_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PROVEN_SMTP_FROM", "veris@example.com")
    monkeypatch.setenv("PROVEN_SMTP_USER", "someone")
    monkeypatch.setenv("PROVEN_SMTP_PASSWORD", "super-secret-value")

    blob = str(mailer.status())
    assert "super-secret-value" not in blob
    assert mailer.status()["credentials_present"] is True


def test_with_no_mail_server_the_message_is_still_composed_in_full():
    """The outbox is not a consolation prize — it is how the wording is shown at all."""
    store = notif.Notifications()
    item = store.add(BID, code="NEW_LOCATION", dedupe_key="k", title="2 addresses",
                     body="why", what_to_do="do this",
                     evidence={"supporting_points": ["a", "b"], "caveat": "careful"},
                     now=T0)

    entry = mailer.deliver([item], business_id=BID, business_name=BNAME)

    assert entry.status == notif.NO_MAIL_SERVER
    assert entry.transport == "OUTBOX"
    assert "PROVEN_SMTP_HOST" in entry.detail
    assert "2 addresses" in entry.subject
    assert "2 addresses" in entry.text_body
    assert "careful" in entry.text_body
    assert len(entry.html_body) > 500


def test_the_subject_says_what_happened_not_that_something_happened():
    store = notif.Notifications()
    one = store.add(BID, code="NEW_LOCATION", dedupe_key="a",
                    title="2 addresses on your website are not on your policy",
                    body="b", now=T0)
    assert "addresses" in mailer.subject_for([one], BNAME)

    two = store.add(BID, code="CHANGED_HEADCOUNT", dedupe_key="b",
                    title="Team grew", body="b", now=T0)
    subject = mailer.subject_for([one, two], BNAME)
    assert "2 things" in subject
    assert BNAME in subject


def test_the_email_says_what_we_did_not_do():
    """The strongest trust signal available: we noticed, and we changed nothing."""
    store = notif.Notifications()
    item = store.add(BID, code="NEW_LOCATION", dedupe_key="k", title="t", body="b",
                     now=T0)
    body = mailer.render_text([item], BNAME)
    assert "not changed anything on your policy" in body
    assert "not told your insurer" in body


def test_scraped_text_is_escaped_on_the_html_path():
    """The body carries text scraped from a third-party website. Interpolating that into
    HTML unescaped would post a script payload into somebody's mail client, from us."""
    store = notif.Notifications()
    nasty = '<script>alert("x")</script>'
    item = store.add(BID, code="NEW_LOCATION", dedupe_key="k",
                     title=f"New address {nasty}", body=f"body {nasty}",
                     what_to_do=nasty,
                     evidence={"supporting_points": [nasty], "caveat": nasty,
                               "source_name": nasty},
                     now=T0)

    html_body = mailer.render_html([item], BNAME)

    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body


def test_a_business_name_is_escaped_too():
    assert "<b>" not in mailer.render_html([], "Acme <b>Foods</b>")


def test_nothing_to_send_is_not_an_attempt():
    entry = mailer.deliver([], business_id=BID, business_name=BNAME)
    assert entry.status == notif.NOT_ATTEMPTED


def test_a_broken_mail_server_fails_without_raising(monkeypatch):
    """A mail outage must never take down the agent run that produced the notification."""
    monkeypatch.setenv("PROVEN_SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("PROVEN_SMTP_FROM", "veris@example.com")
    monkeypatch.setenv("PROVEN_NOTIFY_TO", "owner@example.com")

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer.smtplib, "SMTP", _boom)

    store = notif.Notifications()
    item = store.add(BID, code="NEW_LOCATION", dedupe_key="k", title="t", body="b",
                     now=T0)
    entry = mailer.deliver([item], business_id=BID, business_name=BNAME)

    assert entry.status == notif.FAILED
    assert "OSError" in entry.detail
    assert entry.text_body, "the composed message is still kept"


def test_a_configured_server_really_gets_the_message(monkeypatch):
    monkeypatch.setenv("PROVEN_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PROVEN_SMTP_FROM", "veris@example.com")
    monkeypatch.setenv("PROVEN_NOTIFY_TO", "owner@example.com")
    sent = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, msg):
            sent["to"] = msg["To"]
            sent["subject"] = msg["Subject"]

    monkeypatch.setattr(mailer.smtplib, "SMTP", _FakeSMTP)

    store = notif.Notifications()
    item = store.add(BID, code="NEW_LOCATION", dedupe_key="k", title="t", body="b",
                     now=T0)
    entry = mailer.deliver([item], business_id=BID, business_name=BNAME)

    assert entry.status == notif.SENT
    assert entry.transport == "SMTP"
    assert sent["to"] == "owner@example.com"
    assert sent["tls"] is True


def test_the_outbox_is_bounded():
    box = mailer.Outbox(limit=3)
    for i in range(7):
        box.add(business_id=BID, notification_ids=[], to="a", sender="b",
                subject=f"s{i}", text_body="t", html_body="h", status=notif.SENT)
    assert box.count() == 3


# ── the pipeline ─────────────────────────────────────────────────────────────

def test_a_first_observation_tells_nobody_anything():
    store = notif.Notifications()
    out = alerts.raise_alerts(observation(new_locations(), first=True),
                              business_id=BID, business_name=BNAME, store=store,
                              now=T0)
    assert out["created"] == 0
    assert out["email"] is None
    assert store.list(BID) == []
    assert "baseline" in out["note"].lower()


def test_a_change_becomes_a_notification_with_wording_and_evidence():
    store = notif.Notifications()
    out = alerts.raise_alerts(observation(new_locations(2)), business_id=BID,
                              business_name=BNAME, store=store, now=T0)

    assert out["created"] == 1
    item = store.list(BID)[0]
    assert item["severity"] == "HIGH"
    assert "2 addresses" in item["title"]
    assert item["what_to_do"]
    assert item["evidence"]["caveat"]
    assert item["evidence"]["policy_says"] == "Unit 03-11, 21 Tai Seng Avenue"
    assert item["wording_by"] == "TEMPLATE"


def test_the_same_change_on_the_next_run_is_silent():
    """The scheduler re-checks every few minutes and the branch stays on the website."""
    store = notif.Notifications()
    obs = observation(new_locations(2))
    first = alerts.raise_alerts(obs, business_id=BID, business_name=BNAME,
                                store=store, now=T0)
    second = alerts.raise_alerts(obs, business_id=BID, business_name=BNAME,
                                 store=store, now=T0 + timedelta(minutes=5))

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["already_known"] == 1
    assert len(store.list(BID)) == 1
    assert mailer.OUTBOX.count(BID) == 1, "a second email must not go out"


def test_several_changes_in_one_run_produce_one_email():
    """Three emails arriving in the same second is how a product teaches people to filter
    it."""
    store = notif.Notifications()
    out = alerts.raise_alerts(observation(new_locations(2), headcount_change()),
                              business_id=BID, business_name=BNAME, store=store,
                              now=T0)

    assert out["created"] == 2
    assert mailer.OUTBOX.count(BID) == 1
    assert out["email"]["item_count"] == 2


def test_the_email_outcome_is_written_back_onto_each_notification():
    store = notif.Notifications()
    alerts.raise_alerts(observation(new_locations()), business_id=BID,
                        business_name=BNAME, store=store, now=T0)
    item = store.list(BID)[0]
    assert item["email_status"] == notif.NO_MAIL_SERVER
    assert item["email_detail"]


def test_a_suppressed_notification_is_still_recorded_with_the_reason():
    """Silently dropping a message because of a setting is indistinguishable from being
    broken."""
    store = notif.Notifications()
    store.set_preferences(BID, {"email_enabled": False})

    out = alerts.raise_alerts(observation(new_locations()), business_id=BID,
                              business_name=BNAME, store=store, now=T0)

    assert out["created"] == 1
    item = store.list(BID)[0]
    assert item["email_status"] == notif.SUPPRESSED
    assert "switched off" in item["email_detail"].lower()
    assert mailer.OUTBOX.count(BID) == 0
    assert out["email"]["suppressed"]


def test_a_severity_floor_lets_the_serious_one_through_and_holds_the_rest():
    store = notif.Notifications()
    store.set_preferences(BID, {"minimum_severity": "HIGH"})

    out = alerts.raise_alerts(observation(new_locations(2), headcount_change()),
                              business_id=BID, business_name=BNAME, store=store,
                              now=T0)

    assert out["created"] == 2
    by_code = {i["code"]: i for i in store.list(BID)}
    assert by_code["NEW_LOCATION"]["email_status"] == notif.NO_MAIL_SERVER
    assert by_code["CHANGED_HEADCOUNT"]["email_status"] == notif.SUPPRESSED
    assert out["email"]["item_count"] == 1


def test_a_daily_digest_holds_the_second_batch_rather_than_emailing_it():
    store = notif.Notifications()
    store.set_preferences(BID, {"digest": notif.DAILY})

    alerts.raise_alerts(observation(new_locations()), business_id=BID,
                        business_name=BNAME, store=store, now=T0)
    out = alerts.raise_alerts(observation(headcount_change()), business_id=BID,
                              business_name=BNAME, store=store,
                              now=T0 + timedelta(hours=1))

    assert out["email"]["status"] == notif.HELD_FOR_DIGEST
    assert store.list(BID)[0]["email_status"] == notif.HELD_FOR_DIGEST
    assert mailer.OUTBOX.count(BID) == 1, "still only the first message"


def test_a_daily_digest_sends_held_items_together_when_it_is_due():
    store = notif.Notifications()
    store.set_preferences(BID, {"digest": notif.DAILY})

    alerts.raise_alerts(observation(new_locations()), business_id=BID,
                        business_name=BNAME, store=store, now=T0)
    alerts.raise_alerts(observation(headcount_change()), business_id=BID,
                        business_name=BNAME, store=store,
                        now=T0 + timedelta(hours=1))
    out = alerts.raise_alerts(
        observation({**new_locations(1), "dedupe_key": "third",
                     "values": ["#07-07, 3 Somewhere Road, Singapore 100200"]}),
        business_id=BID, business_name=BNAME, store=store,
        now=T0 + timedelta(hours=26))

    assert out["email"]["status"] == notif.NO_MAIL_SERVER   # composed, no server
    assert out["email"]["item_count"] >= 2, "the held item travels with the new one"


def test_email_can_be_skipped_entirely_by_the_caller():
    store = notif.Notifications()
    out = alerts.raise_alerts(observation(new_locations()), business_id=BID,
                              business_name=BNAME, store=store, now=T0,
                              send_email=False)
    assert out["created"] == 1
    assert out["email"] is None
    assert mailer.OUTBOX.count(BID) == 0


def test_nothing_changed_says_so_plainly():
    store = notif.Notifications()
    out = alerts.raise_alerts(observation(), business_id=BID, business_name=BNAME,
                              store=store, now=T0)
    assert out["created"] == 0
    assert "nothing changed" in out["note"].lower()
