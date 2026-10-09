"""The notification record, the inbox, and the decision to interrupt somebody.

The behaviours locked here are the ones that decide whether an owner trusts the product:
never send the same thing twice, never silently drop something because of a setting, and
never quietly downgrade how serious a source said something was.
"""
from datetime import datetime, timedelta, timezone

from proven import notifications as n

BID = "T09HL1234A"
T0 = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)


def store():
    return n.Notifications()


def add(store_, *, code="NEW_LOCATION", key="k1", title="A new address",
        body="b", severity=None, when=T0, **kw):
    return store_.add(BID, code=code, dedupe_key=key, title=title, body=body,
                      change_severity=severity, now=when, **kw)


# ── recording ────────────────────────────────────────────────────────────────

def test_a_notification_is_recorded_with_a_readable_id():
    s = store()
    item = add(s)
    assert item is not None
    assert item.id.startswith("NTF-")
    assert item.read is False
    assert item.email_status == n.NOT_ATTEMPTED
    assert s.unread_count(BID) == 1


def test_the_inbox_reads_newest_first():
    s = store()
    add(s, key="a", title="first", when=T0)
    add(s, key="b", title="second", when=T0 + timedelta(minutes=5))
    titles = [x["title"] for x in s.list(BID)]
    assert titles == ["second", "first"]


def test_a_code_label_travels_with_the_record():
    """The interface should never have to translate a machine code itself."""
    s = store()
    add(s, code="CHANGED_HEADCOUNT", key="h")
    assert s.list(BID)[0]["code_label"] == CODE_LABEL_HEADCOUNT


CODE_LABEL_HEADCOUNT = n.CODE_LABELS["CHANGED_HEADCOUNT"]


# ── deduplication: the property that decides whether email is read at all ────

def test_the_same_news_is_only_recorded_once():
    """The scheduler re-checks every few minutes and a new branch stays on the website until
    the policy is updated, so this exact news is true every run."""
    s = store()
    assert add(s, key="same") is not None
    assert add(s, key="same") is None
    assert add(s, key="same") is None
    assert len(s.list(BID)) == 1


def test_different_news_is_recorded_separately():
    s = store()
    assert add(s, key="one") is not None
    assert add(s, key="two") is not None
    assert len(s.list(BID)) == 2


def test_already_notified_can_be_asked_before_composing():
    """Composing wording costs a model call, so callers check first."""
    s = store()
    assert s.already_notified(BID, "k9") is False
    add(s, key="k9")
    assert s.already_notified(BID, "k9") is True


def test_dedupe_is_per_business():
    s = store()
    s.add(BID, code="NEW_LOCATION", dedupe_key="shared", title="t", body="b", now=T0)
    other = s.add("OTHER-UEN", code="NEW_LOCATION", dedupe_key="shared",
                  title="t", body="b", now=T0)
    assert other is not None


def test_trimming_an_inbox_does_not_let_old_news_resend():
    """Dedupe keys outlive the records they belong to. Otherwise a busy inbox rolls over and
    last month's alerts arrive again as if they were new."""
    s = n.Notifications(max_per_business=3)
    for i in range(5):
        s.add(BID, code="NEW_LOCATION", dedupe_key=f"k{i}", title=f"t{i}",
              body="b", now=T0)
    assert len(s.list(BID, limit=99)) == 3
    assert s.add(BID, code="NEW_LOCATION", dedupe_key="k0", title="t0",
                 body="b", now=T0) is None


# ── severity is decided, not copied ──────────────────────────────────────────

def test_a_new_premises_is_high_severity_regardless_of_the_fact():
    """Premises operated but absent from the schedule is the strongest failure in this
    corpus. Discovery labels the website fact MODERATE; the consequence is not."""
    assert n.severity_for("NEW_LOCATION", "MODERATE") == "HIGH"


def test_a_source_that_thinks_something_is_worse_is_never_downgraded():
    assert n.severity_for("CHANGED_HEADCOUNT", "HIGH") == "HIGH"


def test_severity_falls_back_sanely_for_an_unknown_code():
    assert n.severity_for("SOMETHING_NEW", None) == "MODERATE"


def test_headcount_change_is_moderate():
    assert n.severity_for("CHANGED_HEADCOUNT", "MODERATE") == "MODERATE"


# ── preferences, and saying why nothing was sent ─────────────────────────────

def test_by_default_a_new_business_gets_emailed():
    s = store()
    item = add(s)
    ok, why = n.should_email(item, s.preferences(BID))
    assert ok is True
    assert why == ""


def test_turning_email_off_suppresses_and_explains():
    s = store()
    s.set_preferences(BID, {"email_enabled": False})
    ok, why = n.should_email(add(s), s.preferences(BID))
    assert ok is False
    assert "switched off" in why.lower()


def test_a_severity_floor_suppresses_and_names_the_floor():
    s = store()
    s.set_preferences(BID, {"minimum_severity": "HIGH"})
    item = add(s, code="CHANGED_HEADCOUNT", key="h")   # MODERATE
    ok, why = n.should_email(item, s.preferences(BID))
    assert ok is False
    assert "moderate" in why.lower()


def test_a_severity_floor_still_lets_serious_things_through():
    s = store()
    s.set_preferences(BID, {"minimum_severity": "HIGH"})
    ok, _ = n.should_email(add(s, code="NEW_LOCATION", key="L"), s.preferences(BID))
    assert ok is True


def test_turning_off_one_code_says_which_one_in_plain_words():
    """An owner who changed a setting six weeks ago has to be told that is why they heard
    nothing. Silence is indistinguishable from a broken product."""
    s = store()
    s.set_preferences(BID, {"codes": {"CHANGED_HEADCOUNT": False}})
    ok, why = n.should_email(add(s, code="CHANGED_HEADCOUNT", key="h"),
                             s.preferences(BID))
    assert ok is False
    assert "team size" in why.lower()

    ok2, _ = n.should_email(add(s, code="NEW_LOCATION", key="L"), s.preferences(BID))
    assert ok2 is True, "turning off one code must not silence the others"


def test_an_unknown_code_is_allowed_rather_than_silently_dropped():
    s = store()
    item = add(s, code="NEW_KIND_OF_THING", key="x")
    ok, _ = n.should_email(item, s.preferences(BID))
    assert ok is True


def test_preferences_ignore_junk():
    """The preference vocabulary is closed, the same way the event vocabulary is."""
    s = store()
    saved = s.set_preferences(BID, {
        "minimum_severity": "BANANA",
        "digest": "HOURLY",
        "codes": {"NOT_A_REAL_CODE": False},
        "some_unknown_key": "ignored",
    })
    assert saved["minimum_severity"] == "MODERATE"
    assert saved["digest"] == n.IMMEDIATE
    assert "NOT_A_REAL_CODE" not in saved["codes"]
    assert "some_unknown_key" not in saved


def test_preferences_merge_rather_than_replace():
    s = store()
    s.set_preferences(BID, {"minimum_severity": "HIGH"})
    s.set_preferences(BID, {"digest": n.DAILY})
    prefs = s.preferences(BID)
    assert prefs["minimum_severity"] == "HIGH"
    assert prefs["digest"] == n.DAILY


def test_preferences_are_per_business():
    s = store()
    s.set_preferences(BID, {"email_enabled": False})
    assert s.preferences("OTHER")["email_enabled"] is True


# ── read state ───────────────────────────────────────────────────────────────

def test_marking_one_read_moves_the_count():
    s = store()
    item = add(s, key="a")
    add(s, key="b")
    assert s.unread_count(BID) == 2
    assert s.mark_read(BID, item.id) is True
    assert s.unread_count(BID) == 1
    assert s.list(BID, unread_only=True)[0]["dedupe_key"] == "b"


def test_marking_read_twice_is_harmless():
    s = store()
    item = add(s)
    s.mark_read(BID, item.id, now=T0)
    first = s.get(BID, item.id).read_at
    s.mark_read(BID, item.id, now=T0 + timedelta(hours=1))
    assert s.get(BID, item.id).read_at == first


def test_mark_all_read_reports_how_many_it_changed():
    s = store()
    add(s, key="a")
    add(s, key="b")
    assert s.mark_all_read(BID) == 2
    assert s.mark_all_read(BID) == 0
    assert s.unread_count(BID) == 0


def test_marking_an_unknown_id_read_is_false_not_an_error():
    assert store().mark_read(BID, "NTF-9999") is False


# ── email bookkeeping ────────────────────────────────────────────────────────

def test_email_status_is_recorded_with_its_reason():
    s = store()
    item = add(s)
    s.set_email_status(BID, item.id, n.SUPPRESSED, "You turned email off.")
    got = s.get(BID, item.id)
    assert got.email_status == n.SUPPRESSED
    assert got.email_detail
    assert got.emailed_at is None


def test_a_sent_email_gets_a_timestamp():
    s = store()
    item = add(s)
    s.set_email_status(BID, item.id, n.SENT, "to owner@example.com", now=T0)
    assert s.get(BID, item.id).emailed_at == T0.isoformat()


def test_counts_summarise_the_inbox():
    s = store()
    a = add(s, key="a")
    b = add(s, key="b", code="CHANGED_HEADCOUNT")
    s.set_email_status(BID, a.id, n.SENT)
    s.set_email_status(BID, b.id, n.SUPPRESSED)
    s.mark_read(BID, a.id)

    c = s.counts(BID)
    assert c["total"] == 2
    assert c["unread"] == 1
    assert c["emailed"] == 1
    assert c["suppressed"] == 1
    assert c["by_severity"]["HIGH"] == 1
    assert c["by_severity"]["MODERATE"] == 1


# ── the daily digest ─────────────────────────────────────────────────────────

def test_a_digest_is_due_when_none_has_ever_been_sent():
    assert store().digest_due(BID, now=T0) is True


def test_a_digest_is_not_due_again_immediately():
    s = store()
    s.record_digest(BID, now=T0)
    assert s.digest_due(BID, now=T0 + timedelta(hours=1)) is False


def test_a_digest_is_due_again_after_the_interval():
    s = store()
    s.record_digest(BID, now=T0)
    assert s.digest_due(BID, now=T0 + timedelta(hours=25)) is True


def test_held_notifications_can_be_collected_for_the_digest():
    s = store()
    a = add(s, key="a")
    add(s, key="b")
    s.set_email_status(BID, a.id, n.HELD_FOR_DIGEST)
    held = s.held_for_digest(BID)
    assert [x.id for x in held] == [a.id]


# ── housekeeping ─────────────────────────────────────────────────────────────

def test_forget_clears_one_business_only():
    s = store()
    add(s, key="a")
    s.add("OTHER", code="NEW_LOCATION", dedupe_key="z", title="t", body="b", now=T0)
    s.forget(BID)
    assert s.list(BID) == []
    assert len(s.list("OTHER")) == 1


def test_forgetting_a_business_also_forgets_its_dedupe_keys():
    """"Start watching again" has to mean a genuinely fresh start."""
    s = store()
    add(s, key="a")
    s.forget(BID)
    assert add(s, key="a") is not None


def test_the_store_is_bounded_by_business():
    s = n.Notifications(max_businesses=3)
    for i in range(8):
        s.add(f"BIZ-{i:02d}", code="NEW_LOCATION", dedupe_key="k",
              title="t", body="b", now=T0)
    assert s.stats()["businesses"] <= 3


def test_stats_do_not_claim_to_be_evidence():
    s = store()
    add(s)
    assert "evidence" in s.stats()["notice"].lower()
