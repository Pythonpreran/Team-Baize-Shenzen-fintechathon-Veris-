"""Tests for the autonomous scheduler.

The scheduler makes the agents run without human trigger — which is what makes
'real-time picture' true. These tests verify it runs, records, and stops cleanly.
"""
import time

import pytest

from proven.scheduler import Scheduler, RunEntry


@pytest.fixture
def sched():
    s = Scheduler()
    yield s
    s.stop()


def test_scheduler_starts_and_runs_all_three_agents(sched):
    """On start, all three agents should run immediately so the demo has history."""
    sched.start()
    time.sleep(2.0)  # Give the initial runs time to complete
    sched.stop()
    history = sched.history()
    agents_run = {e["agent"] for e in history}
    assert "connect" in agents_run
    assert "sentinel" in agents_run
    assert "discovery" in agents_run


def test_scheduler_records_timestamps(sched):
    sched.start()
    time.sleep(1.5)
    sched.stop()
    for entry in sched.history():
        assert entry["timestamp"]
        assert "T" in entry["timestamp"]  # ISO format


def test_scheduler_records_signal_counts(sched):
    sched.start()
    time.sleep(1.5)
    sched.stop()
    for entry in sched.history():
        assert entry["signals_produced"] >= 0
        assert isinstance(entry["elevated"], bool)


def test_scheduler_stops_cleanly(sched):
    sched.start()
    assert sched.running is True
    sched.stop()
    assert sched.running is False


def test_scheduler_stats_reports_state(sched):
    sched.start()
    time.sleep(1.5)
    sched.stop()
    stats = sched.stats()
    assert stats["running"] is False
    assert stats["total_runs"] >= 3  # at least one of each
    assert "connect" in stats["by_agent"]
    assert "intervals" in stats


def test_scheduler_history_is_bounded(sched):
    """Memory must not grow unbounded on a long-running demo."""
    # Fake a lot of entries
    for i in range(300):
        sched._record(RunEntry(
            agent="test", timestamp=f"2026-08-14T{i:05d}", mode="TEST",
            signals_produced=0, elevated=False, summary="test"))
    assert len(sched._history) <= 200


def test_scheduler_does_not_crash_on_agent_error():
    """A failing agent must not take down the scheduler."""
    import proven.connectors as conn_mod
    s = Scheduler()
    # Monkey-patch connectors.timeline to raise
    original = conn_mod.timeline
    conn_mod.timeline = lambda **kw: (_ for _ in ()).throw(RuntimeError("simulated"))
    try:
        s.start()
        time.sleep(2.0)
        s.stop()
    finally:
        conn_mod.timeline = original
    # Should still have entries from the other agents, and connect recorded the error
    history = s.history()
    assert any(e["agent"] == "sentinel" for e in history)
    assert any(e["agent"] == "discovery" for e in history)
    connect_runs = [e for e in history if e["agent"] == "connect"]
    assert connect_runs
    assert "Error" in connect_runs[0]["summary"]


def test_discovery_marks_elevated_when_verified_facts_found(sched):
    sched.start()
    time.sleep(1.5)
    sched.stop()
    disc_runs = [e for e in sched.history() if e["agent"] == "discovery"]
    assert disc_runs
    # The demo discovery always finds the ACRA branch, so it should be elevated
    assert disc_runs[0]["elevated"] is True
    assert "verified" in disc_runs[0]["summary"].lower()


# ── the watch step: noticing changes and telling the owner ───────────────────
#
# The scheduler is the ONLY writer to the watchlist, so these tests are also what proves the
# store isolation works. Each `Scheduler()` gets its own stores; if it did not, the first test
# to run would establish a baseline and every later one would see phantom changes.

from proven import discovery, mailer, notifications as notif, watchlist  # noqa: E402

BID = "T09HL1234A"


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
        "facts": list(facts),
        "demo_mode": True,
        "fetch_log": [{"source": "Website", "url": "http://localhost", "ok": True}],
        "extraction": {"model_used": False, "pattern_facts": len(facts),
                       "model_facts": 0, "discarded_ungrounded": 0},
    }


@pytest.fixture
def isolated():
    s = Scheduler(watch=watchlist.Watchlist(), notices=notif.Notifications())
    mailer.OUTBOX.clear()
    yield s
    s.stop()
    mailer.OUTBOX.clear()


def test_a_bare_scheduler_does_not_share_the_global_stores():
    """Otherwise one test's baseline becomes another test's phantom change."""
    a, b = Scheduler(), Scheduler()
    assert a._watchlist is not watchlist.WATCHLIST
    assert a._notifications is not notif.NOTIFICATIONS
    assert a._watchlist is not b._watchlist


def test_the_production_singleton_does_share_them():
    """The API reads these; only the scheduler writes to them."""
    from proven.scheduler import SCHEDULER
    assert SCHEDULER._watchlist is watchlist.WATCHLIST
    assert SCHEDULER._notifications is notif.NOTIFICATIONS


def test_run_discovery_now_returns_the_run_it_recorded(isolated):
    """A "Check now" button should be able to show the outcome, not say "started"."""
    run = isolated.run_discovery_now()
    assert run["agent"] == "discovery"
    assert run["timestamp"]
    assert "watch" in run["detail"]


def test_the_first_run_records_a_baseline_and_tells_nobody(isolated, monkeypatch):
    monkeypatch.setattr(discovery, "discover_business",
                        lambda **k: _result(_fact("LOCATION", "#03-11, 21 Tai Seng Ave")))

    run = isolated.run_discovery_now()
    watch = run["detail"]["watch"]

    assert watch["first_observation"] is True
    assert watch["notifications_created"] == 0
    assert "baseline" in run["summary"]
    assert isolated._notifications.list(BID) == []
    assert mailer.OUTBOX.count(BID) == 0


def test_a_new_address_on_the_second_run_becomes_a_notification(isolated, monkeypatch):
    one = _result(_fact("LOCATION", "#03-11, 21 Tai Seng Ave", 1))
    two = _result(_fact("LOCATION", "#03-11, 21 Tai Seng Ave", 1),
                  _fact("LOCATION", "#05-01, 9 Kaki Bukit Road 1", 2))

    monkeypatch.setattr(discovery, "discover_business", lambda **k: one)
    isolated.run_discovery_now()

    monkeypatch.setattr(discovery, "discover_business", lambda **k: two)
    run = isolated.run_discovery_now()
    watch = run["detail"]["watch"]

    assert watch["notifications_created"] == 1
    assert watch["changes"][0]["code"] == "NEW_LOCATION"
    assert run["elevated"] is True
    assert "reported to the owner" in run["summary"]

    inbox = isolated._notifications.list(BID)
    assert len(inbox) == 1
    assert inbox[0]["severity"] == "HIGH"
    assert mailer.OUTBOX.count(BID) == 1


def test_running_again_with_no_change_is_silent(isolated, monkeypatch):
    two = _result(_fact("LOCATION", "#03-11, 21 Tai Seng Ave", 1),
                  _fact("LOCATION", "#05-01, 9 Kaki Bukit Road 1", 2))
    monkeypatch.setattr(discovery, "discover_business", lambda **k: two)

    isolated.run_discovery_now()          # baseline
    isolated.run_discovery_now()          # nothing new
    run = isolated.run_discovery_now()

    assert run["detail"]["watch"]["notifications_created"] == 0
    assert run["elevated"] is False
    assert "nothing new" in run["summary"]
    assert mailer.OUTBOX.count(BID) == 0


def test_the_same_change_is_never_emailed_twice(isolated, monkeypatch):
    """The interval is minutes and a new branch stays on the website until the policy is
    updated, so this is the difference between a useful alert and a spam folder."""
    one = _result(_fact("LOCATION", "#03-11, 21 Tai Seng Ave", 1))
    two = _result(_fact("LOCATION", "#03-11, 21 Tai Seng Ave", 1),
                  _fact("LOCATION", "#05-01, 9 Kaki Bukit Road 1", 2))

    monkeypatch.setattr(discovery, "discover_business", lambda **k: one)
    isolated.run_discovery_now()
    monkeypatch.setattr(discovery, "discover_business", lambda **k: two)
    isolated.run_discovery_now()
    isolated.run_discovery_now()
    isolated.run_discovery_now()

    assert len(isolated._notifications.list(BID)) == 1
    assert mailer.OUTBOX.count(BID) == 1


def test_a_notification_failure_does_not_destroy_the_run_record(isolated, monkeypatch):
    """A failed fetch means we learned nothing. A failed notification means we learned
    something and could not pass it on. The second must not look like the first, and must
    not erase the evidence that the check happened."""
    monkeypatch.setattr(discovery, "discover_business",
                        lambda **k: _result(_fact("LOCATION", "#03-11, 21 Tai Seng")))

    def _boom(*a, **k):
        raise RuntimeError("watchlist exploded")

    monkeypatch.setattr(isolated._watchlist, "observe", _boom)

    run = isolated.run_discovery_now()

    assert run["agent"] == "discovery"
    assert run["signals_produced"] == 1, "the facts were still found"
    assert "RuntimeError" in run["detail"]["watch"]["error"]
    assert "failed" in run["detail"]["watch"]["note"].lower()


def test_a_discovery_failure_still_records_a_run(isolated, monkeypatch):
    def _boom(**k):
        raise RuntimeError("network gone")

    monkeypatch.setattr(discovery, "discover_business", _boom)
    run = isolated.run_discovery_now()

    assert "Error" in run["summary"]
    assert run["elevated"] is False


def test_the_run_detail_carries_the_extraction_provenance(isolated, monkeypatch):
    """The activity feed shows how facts were pulled out, so it has to travel with the run."""
    monkeypatch.setattr(discovery, "discover_business",
                        lambda **k: _result(_fact("LOCATION", "#03-11, 21 Tai Seng")))
    run = isolated.run_discovery_now()
    assert run["detail"]["extraction"]["pattern_facts"] == 1
