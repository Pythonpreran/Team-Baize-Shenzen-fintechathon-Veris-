"""Per-session case isolation.

The API used to hold ONE module-level `_STATE` dict, so every caller shared one
case. Two people using the demo at once broke each other: A's "Delete case" emptied
B's findings, and A's consent grant applied to B's viewer. That is not only a
deployment concern — it meant the demo could not be shown to two judges at the same
time, and it made the consent model untestable.

Each browser session now gets its own sqlite connection, its own case and its own
lock. Sessions expire and are capped, because an unbounded map of open connections
keyed on a client-supplied cookie is a memory-exhaustion surface.

Concurrency contract:
  * `SessionStore` guards its own map with a store-level lock.
  * Each `Session` carries an RLock, and every endpoint that touches the session's
    connection holds it. sqlite3 is opened with check_same_thread=False so a
    connection can cross FastAPI threadpool workers at all; the per-session lock is
    what makes that safe rather than merely permitted.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# A session id is a 256-bit random token. It is a bearer credential for one case, so
# it must not be guessable — a sequential or timestamp-derived id would let anyone
# read anyone else's Brief by counting.
_TOKEN_BYTES = 32

DEFAULT_TTL_SECONDS = 2 * 60 * 60      # 2 hours idle
DEFAULT_MAX_SESSIONS = 64

# State that belongs to the SESSION rather than to the case, and therefore survives
# replacing one case with another. Kept deliberately short: anything added here outlives
# a delete, so it must be something the user would be surprised to lose — not analysis.
#   _principal  who is signed in. Losing it silently signs the user out mid-upload.
#   _uploads    the rate-limit window. Clearing it would make the limit bypassable by
#               uploading again, which is precisely what it exists to prevent.
_PRESERVED_ACROSS_CASES: tuple[str, ...] = ("_principal", "_uploads")


@dataclass
class Session:
    id: str
    created_at: float
    last_seen: float
    state: dict = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def touch(self) -> None:
        self.last_seen = time.time()

    def close(self) -> None:
        """Full teardown: release the connection and drop EVERYTHING, identity included.

        This is what eviction and sign-out want. It is not what replacing a case wants —
        see `reset_case`.
        """
        conn = self.state.get("conn")
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass                     # already closed; nothing to recover
        self.state = {}

    def reset_case(self) -> None:
        """Drop the case but keep who is signed in.

        MEASURED DEFECT, found by walking the three-role demo over real HTTP rather than
        by reading the code. Uploading a bundle called `close()` to discard the previous
        case, and `close()` empties `state` — which also deleted `_principal`. So
        uploading a document silently signed the user out: the identity chip went stale,
        and the role boundary quietly stopped applying because an anonymous session is
        deliberately allowed to read the business workspace. A security boundary that
        disappears on the product's most common action is worse than not having one,
        because nobody notices it has gone.

        Identity is not case data, so it is preserved explicitly here rather than being
        left to survive by accident.
        """
        preserved = {key: self.state[key]
                     for key in _PRESERVED_ACROSS_CASES if key in self.state}
        self.close()
        self.state.update(preserved)


class SessionStore:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_sessions: int = DEFAULT_MAX_SESSIONS):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions

    # ── lifecycle ──────────────────────────────────────────────────────────
    def new_id(self) -> str:
        return secrets.token_urlsafe(_TOKEN_BYTES)

    def get(self, sid: Optional[str]) -> Optional[Session]:
        if not sid:
            return None
        with self._lock:
            self._expire_locked()
            session = self._sessions.get(sid)
            if session is not None:
                session.touch()
            return session

    def create(self) -> Session:
        with self._lock:
            self._expire_locked()
            self._enforce_capacity_locked()
            now = time.time()
            session = Session(id=self.new_id(), created_at=now, last_seen=now)
            self._sessions[session.id] = session
            return session

    def get_or_create(self, sid: Optional[str]) -> tuple[Session, bool]:
        """Returns (session, created). `created` tells the caller to set the cookie."""
        session = self.get(sid)
        if session is not None:
            return session, False
        return self.create(), True

    def rotate(self, sid: str) -> Session:
        """Replace a pre-authentication session id without losing its locked state.

        Authentication must not turn a session id obtained before login into a durable
        credential. Moving the same Session object preserves its lock and any upload
        state while making the old bearer token immediately unknown to the store.
        """
        with self._lock:
            session = self._sessions.pop(sid, None)
            if session is None:
                return self.create()
            session.id = self.new_id()
            session.touch()
            self._sessions[session.id] = session
            return session

    def drop(self, sid: str) -> None:
        with self._lock:
            session = self._sessions.pop(sid, None)
        if session is not None:
            session.close()

    def clear(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.close()

    # ── housekeeping ───────────────────────────────────────────────────────
    def _expire_locked(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        stale = [sid for sid, s in self._sessions.items() if s.last_seen < cutoff]
        for sid in stale:
            session = self._sessions.pop(sid, None)
            if session is not None:
                session.close()

    def _enforce_capacity_locked(self) -> None:
        """Evict least-recently-seen sessions down to the cap. Bounded memory is not
        optional when the key comes from the client."""
        while len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
            self._sessions.pop(oldest.id, None)
            oldest.close()

    # ── introspection ──────────────────────────────────────────────────────
    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def ensure_state(
        self, session: Session, build: Callable[[], dict],
    ) -> dict:
        """Return the session's analysis state, building it on first use. Held under
        the session lock so two concurrent requests cannot both run the pipeline and
        leave one orphaned connection behind."""
        with session.lock:
            if "conn" not in session.state:
                session.state.update(build())
            return session.state
