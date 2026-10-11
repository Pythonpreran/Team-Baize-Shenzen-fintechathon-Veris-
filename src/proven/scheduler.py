"""VERIS SCHEDULER — autonomous agent execution without human trigger.

THE PROBLEM THIS SOLVES. Until now, every agent ran only when a human clicked a button
or hit an endpoint. That means the SME had to remember to check — which is the exact
failure the problem statement calls out: "gaps go unnoticed until a claim is denied."

This module runs the three agents on a schedule:
  - Connect (internal)   — event-driven: fires when a connector signal arrives
  - Sentinel (external)  — periodic: checks the environment every SENTINEL_INTERVAL
  - Discovery (public)   — periodic: checks public registries every DISCOVERY_INTERVAL

Each run is RECORDED with a timestamp, the signals it produced, and whether any of them
raised the orchestrator's attention level. This makes the timeline REAL — every entry was
an actual check, not a reconstructed history.

IMPLEMENTATION. A background thread with a simple loop. Not celery, not redis, not a
separate process. The scheduler starts with the app (via FastAPI's `on_event("startup")`)
and stops with it. For a demo running on one machine, this is the right amount of
machinery: enough to show autonomy is real, honest about not being distributed.

HONESTY. In DEMO mode the scheduler runs the agents against the fixture data rather than
making outbound HTTP calls. `mode` is recorded on every run entry. A live deployment would
swap the fixture for real connectors + real HTTP and change nothing below the scheduler.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import (alerts, config, connectors, discovery, notifications, sentinel,
               watchlist)

# ── configuration ────────────────────────────────────────────────────────────

# How often each agent runs, in seconds. These are demonstration intervals — a real
# deployment would use hours/days, not seconds. Set short so a demo can show the
# autonomy loop without waiting.
CONNECT_INTERVAL = 60          # 1 minute (event-driven in production; polled here)
SENTINEL_INTERVAL = 120        # 2 minutes
DISCOVERY_INTERVAL = 300       # 5 minutes

# Maximum history entries kept in memory. Bounded so a long-running demo does not
# accumulate unbounded state.
MAX_HISTORY = 200


@dataclass
class RunEntry:
    """One scheduled execution of an agent.

    `detail` carries the auditable execution facts the activity feed surfaces: what the
    agent planned to check, which URLs it actually called and their status, how many
    facts it extracted, how many survived grounding and how many were rejected. Every
    field is a real record of what happened, not a reconstruction.
    """

    agent: str                       # connect | sentinel | discovery
    timestamp: str                   # ISO datetime
    mode: str                        # DEMO_FIXTURE | LIVE
    signals_produced: int
    elevated: bool                   # did this run raise attention?
    summary: str
    duration_ms: int = 0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "agent": self.agent,
            "timestamp": self.timestamp,
            "mode": self.mode,
            "signals_produced": self.signals_produced,
            "elevated": self.elevated,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


class Scheduler:
    """Background agent runner. Starts and stops with the FastAPI app."""

    def __init__(self, watch: Optional[watchlist.Watchlist] = None,
                 notices: Optional[notifications.Notifications] = None):
        """The stores are injectable, and default to FRESH ones rather than the shared
        singletons.

        Deliberate. The scheduler is the only writer to the watchlist, so a test that
        constructs `Scheduler()` must not inherit another test's baseline — the first run
        would establish it and every later test would see phantom changes. Production wiring
        is explicit at the bottom of this module, where SCHEDULER is given the singletons the
        API reads from.
        """
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._history: list[RunEntry] = []
        self._lock = threading.Lock()
        self._running = False
        self._last_connect = 0.0
        self._last_sentinel = 0.0
        self._last_discovery = 0.0
        self._watchlist = watch if watch is not None else watchlist.Watchlist()
        self._notifications = (notices if notices is not None
                               else notifications.Notifications())

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="veris-scheduler")
        self._thread.start()
        self._running = True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._running = False

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [e.as_dict() for e in self._history[-limit:]]

    def stats(self) -> dict:
        with self._lock:
            total = len(self._history)
            elevated = sum(1 for e in self._history if e.elevated)
            by_agent = {}
            for e in self._history:
                by_agent[e.agent] = by_agent.get(e.agent, 0) + 1
            last_run = self._history[-1].as_dict() if self._history else None
        return {
            "running": self._running,
            "total_runs": total,
            "elevated_runs": elevated,
            "by_agent": by_agent,
            "last_run": last_run,
            "intervals": {
                "connect_seconds": CONNECT_INTERVAL,
                "sentinel_seconds": SENTINEL_INTERVAL,
                "discovery_seconds": DISCOVERY_INTERVAL,
            },
            "notice": (
                "The scheduler runs all three agents autonomously in a background "
                "thread. Each run is recorded with its timestamp and results. In this "
                "demo build, agents run against fixture data — swapping for live "
                "connectors changes the data source and nothing below it."
            ),
        }

    def _record(self, entry: RunEntry) -> None:
        with self._lock:
            self._history.append(entry)
            if len(self._history) > MAX_HISTORY:
                self._history = self._history[-MAX_HISTORY:]

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _loop(self) -> None:
        """The autonomy loop. Observe → check → record → sleep → repeat."""
        # Run all three once immediately on startup, so the demo shows history
        # without waiting for the first interval to elapse.
        self._run_connect()
        self._run_sentinel()
        self._run_discovery()

        while not self._stop_event.is_set():
            now = time.time()

            if now - self._last_connect >= CONNECT_INTERVAL:
                self._run_connect()

            if now - self._last_sentinel >= SENTINEL_INTERVAL:
                self._run_sentinel()

            if now - self._last_discovery >= DISCOVERY_INTERVAL:
                self._run_discovery()

            # Sleep in short increments so stop() is responsive.
            self._stop_event.wait(timeout=5.0)

    def _run_connect(self) -> None:
        self._last_connect = time.time()
        t0 = time.time()
        try:
            timeline = connectors.timeline()
            signals = timeline.get("events", [])
            elevated = len(signals) > 0
            self._record(RunEntry(
                agent="connect",
                timestamp=self._now(),
                # Connect reads a SIMULATED internal accounting feed — genuinely not a
                # live external call, so it stays DEMO_FIXTURE honestly rather than
                # borrowing a LIVE badge it has not earned.
                mode="DEMO_FIXTURE",
                signals_produced=len(signals),
                elevated=elevated,
                summary=f"{len(signals)} internal signal(s) from connected sources",
                duration_ms=int((time.time() - t0) * 1000),
                detail={
                    "planned_check": "Read the connected accounting/ERP feed for "
                                     "business-change signals",
                    "calls": [{"source": "internal-connectors", "url": "in-process "
                               "simulated feed", "status": "OK"}],
                    "facts_extracted": len(signals),
                    "facts_grounded": len(signals),
                    "facts_rejected": 0,
                    "result": f"{len(signals)} internal signal(s)",
                },
            ))
        except Exception as e:
            self._record(RunEntry(
                agent="connect", timestamp=self._now(), mode="DEMO_FIXTURE",
                signals_produced=0, elevated=False,
                summary=f"Error: {type(e).__name__}: {e}",
                duration_ms=int((time.time() - t0) * 1000),
                detail={"planned_check": "Read the connected accounting/ERP feed",
                        "calls": [], "facts_extracted": 0, "facts_grounded": 0,
                        "facts_rejected": 0, "result": f"error: {type(e).__name__}"},
            ))

    def _run_sentinel(self) -> None:
        self._last_sentinel = time.time()
        t0 = time.time()
        live = config.live_sources_enabled()
        try:
            result = sentinel.feed(live=live)
            signals = result.get("signals", [])
            env = result.get("environment", {})
            level = env.get("level", "LOW")
            elevated = level in ("ELEVATED", "HIGH")
            live_info = result.get("live", {})
            fetch_log = live_info.get("fetch_log", [])
            any_live = bool(live_info.get("any_live"))
            mode = "LIVE" if any_live else "DEMO_FIXTURE"
            self._record(RunEntry(
                agent="sentinel",
                timestamp=self._now(),
                mode=mode,
                signals_produced=len(signals),
                elevated=elevated,
                summary=f"{len(signals)} external signal(s), environment: {level} "
                        f"[{mode}]",
                duration_ms=int((time.time() - t0) * 1000),
                detail={
                    "planned_check": "Fetch rainfall + 24h forecast (data.gov.sg) and "
                                     "construction cost index (SingStat M211251)",
                    "calls": [{"source": f.get("source"), "url": f.get("url"),
                               "status": "OK" if f.get("ok") else
                               (f.get("error") or "FAILED")}
                              for f in fetch_log],
                    "facts_extracted": len(signals),
                    "facts_grounded": len(signals),
                    "facts_rejected": 0,
                    "result": f"environment level {level}",
                },
            ))
        except Exception as e:
            self._record(RunEntry(
                agent="sentinel", timestamp=self._now(), mode="DEMO_FIXTURE",
                signals_produced=0, elevated=False,
                summary=f"Error: {type(e).__name__}: {e}",
                duration_ms=int((time.time() - t0) * 1000),
                detail={"planned_check": "Fetch weather + cost index", "calls": [],
                        "facts_extracted": 0, "facts_grounded": 0,
                        "facts_rejected": 0, "result": f"error: {type(e).__name__}"},
            ))

    def run_discovery_now(self, *, business_id: Optional[str] = None,
                          business_name: Optional[str] = None) -> dict:
        """Run Discovery immediately for one tenant-scoped notification namespace."""
        return self._run_discovery(business_id=business_id, business_name=business_name)

    def _observe_and_notify(self, result: dict, *, business_id: Optional[str] = None,
                            business_name: Optional[str] = None) -> dict:
        """Compare this look against the previous one and raise anything new.

        Kept in its own try/except, separate from the fetch, because these are two different
        failures with two different meanings. A failed fetch means we learned nothing this
        run. A failed notification means we learned something and could not pass it on. The
        second must not be allowed to masquerade as the first, and must never destroy the run
        record that proves the check actually happened.
        """
        biz = discovery.demo_business()
        watched_business_id = str(business_id or biz.get("uen") or "unknown")
        watched_business_name = str(business_name or biz.get("name") or "your business")
        try:
            observation = self._watchlist.observe(
                watched_business_id, result.get("facts") or [])
            raised = alerts.raise_alerts(
                observation,
                business_id=watched_business_id,
                business_name=watched_business_name,
                store=self._notifications)
            email = raised.get("email") or {}
            return {
                "business_id": watched_business_id,
                "first_observation": observation["first_observation"],
                "facts_tracked": observation["facts_tracked"],
                "previous_check": observation.get("previous_observed_at"),
                "changes_detected": len(observation["changes"]),
                "changes_worth_raising": len(observation["alertable_changes"]),
                "changes": [
                    {"code": s["code"], "count": s["count"],
                     "severity": s["severity"], "values": s["values"][:3]}
                    for s in observation.get("summary") or []],
                "notifications_created": raised["created"],
                "already_reported": raised["already_known"],
                "email_status": email.get("status"),
                "email_message_id": email.get("message_id"),
                "note": raised["note"],
            }
        except Exception as e:
            return {
                "business_id": watched_business_id,
                "error": f"{type(e).__name__}: {e}",
                "notifications_created": 0,
                "note": ("The check itself ran. Comparing it against the previous look "
                         "failed, so nothing was raised from this run."),
            }

    def _run_discovery(self, *, business_id: Optional[str] = None,
                       business_name: Optional[str] = None) -> dict:
        self._last_discovery = time.time()
        t0 = time.time()
        live = config.live_sources_enabled()
        try:
            result = discovery.discover_business(live=live)
            facts = result.get("facts", [])
            verified = sum(1 for f in facts
                          if f.get("claim_scope") == discovery.DISCOVERY_VERIFIED)
            rejected = sum(1 for f in facts if not f.get("grounded"))
            watch = self._observe_and_notify(
                result, business_id=business_id, business_name=business_name)
            raised = int(watch.get("notifications_created") or 0)
            # A run is "elevated" when it found something that stands up, or when it had
            # something new to tell the owner. The second is the stronger signal: a verified
            # fact may be one we reported months ago.
            elevated = verified > 0 or raised > 0
            mode = "DEMO_FIXTURE" if result.get("demo_mode") else "LIVE"
            summary = f"{len(facts)} fact(s) discovered, {verified} verified"
            if raised:
                summary += f", {raised} new change(s) reported to the owner"
            elif watch.get("first_observation"):
                summary += ", baseline recorded"
            elif not watch.get("error"):
                summary += ", nothing new since the last check"
            entry = RunEntry(
                agent="discovery",
                timestamp=self._now(),
                mode=mode,
                signals_produced=len(facts),
                elevated=elevated,
                summary=summary + f" [{mode}]",
                duration_ms=int((time.time() - t0) * 1000),
                detail={
                    "planned_check": "Look up the business in public registries (ACRA) "
                                     "and on its own website, compare against the policy "
                                     "schedule, and report anything new since last time",
                    "calls": [{"source": f.get("source"), "url": f.get("url"),
                               "status": "OK" if f.get("ok") else
                               (f.get("error") or "FAILED")}
                              for f in result.get("fetch_log", [])],
                    "facts_extracted": len(facts),
                    "facts_grounded": sum(1 for f in facts if f.get("grounded")),
                    "facts_rejected": rejected,
                    "extraction": result.get("extraction"),
                    "watch": watch,
                    "result": f"{verified} verified, {rejected} ungrounded, "
                              f"{raised} reported",
                },
            )
            self._record(entry)
            return entry.as_dict()
        except Exception as e:
            entry = RunEntry(
                agent="discovery", timestamp=self._now(), mode="DEMO_FIXTURE",
                signals_produced=0, elevated=False,
                summary=f"Error: {type(e).__name__}: {e}",
                duration_ms=int((time.time() - t0) * 1000),
                detail={"planned_check": "Public registry lookup", "calls": [],
                        "facts_extracted": 0, "facts_grounded": 0,
                        "facts_rejected": 0, "result": f"error: {type(e).__name__}"},
            )
            self._record(entry)
            return entry.as_dict()


# Module-level singleton. The API starts and stops it with the app lifecycle.
#
# The shared stores are wired in HERE and nowhere else. The scheduler is the only writer to
# the watchlist — the API route for Discovery runs with the case's policy state and would
# produce a different fact set for the same business, so letting both observe would flip the
# same premises between "new" and "gone" forever. The API reads these singletons; only this
# object writes to them.
SCHEDULER = Scheduler(watch=watchlist.WATCHLIST,
                      notices=notifications.NOTIFICATIONS)
