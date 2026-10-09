"""PRIORITY 3 — the agent activity feed.

An autonomous agent the judge cannot watch is indistinguishable from a hardcoded
response. This feed makes the runs legible, and — the important part — shows what the
system REJECTED, not only what it found.
"""
import time

import pytest
from fastapi.testclient import TestClient

from proven.api import SESSIONS, app
from proven.scheduler import Scheduler, RunEntry


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


def test_activity_endpoint_answers(client):
    body = client.get("/api/agents/activity").json()
    assert "activity" in body
    assert "total_rejected" in body
    assert "grounding gate" in body["notice"]


def test_activity_records_real_runs_with_modes():
    """Sourced from the scheduler's real run history, with per-run mode and the
    auditable execution detail the feed surfaces."""
    s = Scheduler()
    s.start()
    time.sleep(1.5)
    s.stop()
    runs = s.history()
    assert runs
    for r in runs:
        assert r["mode"] in ("LIVE", "DEMO_FIXTURE", "TEST")
        assert "detail" in r
        detail = r["detail"]
        # the fields the activity feed exposes are present
        assert "planned_check" in detail
        assert "facts_rejected" in detail


def test_failed_call_is_recorded_not_hidden():
    """A failing agent must still leave an honest row rather than vanishing."""
    import proven.connectors as conn_mod
    s = Scheduler()
    original = conn_mod.timeline
    conn_mod.timeline = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        s.start()
        time.sleep(1.5)
        s.stop()
    finally:
        conn_mod.timeline = original
    connect_runs = [r for r in s.history() if r["agent"] == "connect"]
    assert connect_runs
    assert "Error" in connect_runs[0]["summary"]


def test_demo_mode_is_reported_accurately_offline():
    """With live sources off (the test default), sentinel/discovery report
    DEMO_FIXTURE — never a borrowed LIVE badge."""
    s = Scheduler()
    s.start()
    time.sleep(1.5)
    s.stop()
    for r in s.history():
        if r["agent"] in ("sentinel", "discovery", "connect"):
            assert r["mode"] == "DEMO_FIXTURE"


def test_rejection_count_is_exposed(client):
    """The feed must surface rejected/ungrounded claims prominently — the whole point
    of the screen. A demo discovery run grounds all its facts, so rejected is 0, but
    the field is always present and summed."""
    body = client.get("/api/agents/activity").json()
    assert isinstance(body["total_rejected"], int)
    for row in body["activity"]:
        assert "facts_rejected" in row
