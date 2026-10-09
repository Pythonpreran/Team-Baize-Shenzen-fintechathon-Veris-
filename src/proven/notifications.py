"""Notifications: the record of what we told the owner, and the decision to tell them.

WHAT THIS MODULE OWNS, and just as importantly what it does not.

  owns        the notification record, the per-business inbox, read/unread state, the
              preference rules, and the deduplication that stops one piece of news being
              sent twelve times.
  does NOT    write the wording (that is `presenter`, so alert prose goes through the same
              gates as screen prose) or send anything (that is `mailer`). Both are handed
              in. Keeping them out means this module has no network and no model, so its
              behaviour is entirely testable.

WHY DEDUPLICATION IS STRUCTURAL RATHER THAN A NICETY. The scheduler re-checks every few
minutes, and a change stays visible on the website until the policy is actually updated. So
"two branches are not on your schedule" is true at 09:00, still true at 09:05, and still
true at 09:10. Without a durable fingerprint the owner gets the same email twelve times an
hour, stops reading any of them, and the one that matters arrives into a folder they have
learned to ignore. `watchlist` supplies the fingerprint; this module remembers it.

WHY A SUPPRESSED NOTIFICATION STILL EXISTS. When a preference blocks an email the
notification is still recorded, with the reason attached. A product that silently drops
messages because of a setting the owner forgot they changed is indistinguishable from a
product that is broken. "We found this and did not email you, because you turned off team
size alerts" is a sentence the interface can show; silence is not.

SEVERITY IS DECIDED HERE, NOT COPIED. A discovered fact carries the severity Discovery gave
it, which is about the fact. What matters for interrupting somebody is the consequence, and
that is a product judgement made once, in `CODE_SEVERITY`, with the reasoning written down.
The higher of the two wins, so a source that already thinks something is serious is never
quietly downgraded.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

MAX_BUSINESSES = 32
MAX_PER_BUSINESS = 200

# ── the notifiable vocabulary ────────────────────────────────────────────────
#
# Closed on purpose, and it mirrors `watchlist.ALERTABLE_TYPES`. Only changes whose values
# are structured enough that a difference means a difference can reach an inbox; reworded
# prose is recorded by the watchlist and never arrives here.

CODE_LABELS: dict[str, str] = {
    "NEW_LOCATION": "A new address appears in your public information",
    "CHANGED_LOCATION": "An address in your public information changes",
    "NEW_HEADCOUNT": "A team size appears where we had none recorded",
    "CHANGED_HEADCOUNT": "Your published team size changes",
    "NEW_STATUS": "A registration status appears where we had none recorded",
    "CHANGED_STATUS": "Your company registration status changes",
    # ── carrier-initiated ────────────────────────────────────────────────────
    #
    # Everything above is something VERIS noticed. These three are something the INSURER
    # asked for, and the distinction has to survive all the way to the inbox: an owner
    # deciding whether to act needs to know whether the request came from a monitoring
    # agent or from the company that would pay the claim.
    #
    # They carry no case detail by construction. A carrier is book-level only, so the
    # message names the business, the reason the account surfaced on the carrier's own
    # book, and a call to action — never a figure or a clause.
    "CARRIER_REVIEW_REQUESTED": "Your insurer has asked you to review your cover",
    "CARRIER_RENEWAL_REMINDER": "Your insurer says your renewal is approaching",
    "CARRIER_INFORMATION_NEEDED": "Your insurer needs a document from you",
}

# Codes whose sender is the carrier rather than VERIS. Used by the interface to label them
# differently, and by `mailer` to choose the sending brand.
CARRIER_CODES: frozenset[str] = frozenset({
    "CARRIER_REVIEW_REQUESTED", "CARRIER_RENEWAL_REMINDER",
    "CARRIER_INFORMATION_NEEDED",
})

NOTIFIABLE_CODES: tuple[str, ...] = tuple(CODE_LABELS)

# The consequence of the news, not the confidence in the fact.
#
# NEW_LOCATION is HIGH because premises being operated but absent from the schedule is the
# single strongest finding in this corpus — it is the Grace Electrical failure, where cover
# was refused over premises use the insurer had not been told about. CHANGED_STATUS is HIGH
# because a registration lapsing affects whether there is an insurable entity at all. A team
# size moving is MODERATE: it matters for compulsory work injury cover, and it is a number
# on a form rather than a coverage cliff.
CODE_SEVERITY: dict[str, str] = {
    "NEW_LOCATION": "HIGH",
    "CHANGED_LOCATION": "HIGH",
    "NEW_STATUS": "HIGH",
    "CHANGED_STATUS": "HIGH",
    "NEW_HEADCOUNT": "MODERATE",
    "CHANGED_HEADCOUNT": "MODERATE",
    # A direct request from the insurer is HIGH because ignoring it has consequences the
    # owner did not choose. A renewal reminder is MODERATE: it is a date, not a problem.
    "CARRIER_REVIEW_REQUESTED": "HIGH",
    "CARRIER_INFORMATION_NEEDED": "HIGH",
    "CARRIER_RENEWAL_REMINDER": "MODERATE",
}

SEVERITY_ORDER: dict[str, int] = {"LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}

# ── email status vocabulary ──────────────────────────────────────────────────

NOT_ATTEMPTED = "NOT_ATTEMPTED"
SENT = "SENT"
HELD_FOR_DIGEST = "HELD_FOR_DIGEST"
SUPPRESSED = "SUPPRESSED_BY_PREFERENCE"
NO_MAIL_SERVER = "NO_MAIL_SERVER"
FAILED = "FAILED"

IMMEDIATE = "IMMEDIATE"
DAILY = "DAILY"

DIGEST_INTERVAL_HOURS = 24


def default_preferences() -> dict:
    """Sensible defaults: tell me about it, by email, unless it is trivial.

    `minimum_severity` is MODERATE rather than LOW because LOW is where the recorded-but-
    unremarkable things live. Defaulting to LOW would mean the first thing a new user
    experiences is noise.
    """
    return {
        "email_enabled": True,
        "minimum_severity": "MODERATE",
        "digest": IMMEDIATE,
        "codes": {code: True for code in NOTIFIABLE_CODES},
    }


def severity_for(code: str, change_severity: Optional[str] = None) -> str:
    """The higher of the product judgement and whatever the source already thought."""
    a = CODE_SEVERITY.get(code, "MODERATE")
    b = (change_severity or "").upper()
    if SEVERITY_ORDER.get(b, 0) > SEVERITY_ORDER.get(a, 0):
        return b
    return a


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@dataclass
class Notification:
    """One thing we noticed, in the words the owner will read."""

    id: str
    business_id: str
    created_at: str
    code: str
    severity: str
    dedupe_key: str
    title: str
    body: str
    what_to_do: str
    evidence: dict = field(default_factory=dict)
    wording_by: str = "TEMPLATE"          # MODEL | TEMPLATE — same badge as the screens
    read: bool = False
    read_at: Optional[str] = None
    email_status: str = NOT_ATTEMPTED
    email_detail: str = ""
    emailed_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "business_id": self.business_id,
            "created_at": self.created_at,
            "code": self.code,
            "code_label": CODE_LABELS.get(self.code, self.code),
            "severity": self.severity,
            "dedupe_key": self.dedupe_key,
            "title": self.title,
            "body": self.body,
            "what_to_do": self.what_to_do,
            "evidence": self.evidence,
            "wording_by": self.wording_by,
            "read": self.read,
            "read_at": self.read_at,
            "email_status": self.email_status,
            "email_detail": self.email_detail,
            "emailed_at": self.emailed_at,
        }


def should_email(notification: Notification, preferences: dict) -> tuple[bool, str]:
    """Whether this notification may be emailed, and in plain words why not.

    The reason is returned rather than logged because it is shown on screen. An owner who
    turned something off six weeks ago needs to be told that is why they heard nothing.
    """
    prefs = preferences or default_preferences()

    if not prefs.get("email_enabled", True):
        return False, "Email is switched off in your notification settings."

    floor = str(prefs.get("minimum_severity") or "MODERATE").upper()
    if SEVERITY_ORDER.get(notification.severity, 0) < SEVERITY_ORDER.get(floor, 2):
        return False, (
            f"This is a {notification.severity.lower()} priority item and you asked to be "
            f"emailed about {floor.lower()} and above.")

    codes = prefs.get("codes") or {}
    # Unknown codes default to allowed, so a newly added alert type reaches people instead
    # of being silently dropped by a preference nobody has ever seen.
    if codes.get(notification.code, True) is False:
        label = CODE_LABELS.get(notification.code, notification.code)
        return False, f"You turned off emails for: {label.lower()}."

    return True, ""


class Notifications:
    """Per-business inboxes. In-memory, bounded, thread-safe."""

    def __init__(self, max_businesses: int = MAX_BUSINESSES,
                 max_per_business: int = MAX_PER_BUSINESS):
        self._items: dict[str, list[Notification]] = {}
        self._sent_keys: dict[str, set[str]] = {}
        self._prefs: dict[str, dict] = {}
        self._last_digest: dict[str, str] = {}
        self._seq = 0
        self._lock = threading.RLock()
        self.max_businesses = max_businesses
        self.max_per_business = max_per_business

    # ── writing ────────────────────────────────────────────────────────────
    def already_notified(self, business_id: str, dedupe_key: str) -> bool:
        with self._lock:
            return dedupe_key in self._sent_keys.get(str(business_id), set())

    def add(self, business_id: str, *, code: str, dedupe_key: str, title: str,
            body: str, what_to_do: str = "", evidence: Optional[dict] = None,
            change_severity: Optional[str] = None, wording_by: str = "TEMPLATE",
            now: Optional[datetime] = None) -> Optional[Notification]:
        """Record a notification, or return None if this exact news was already recorded.

        The duplicate check happens under the same lock as the insert. The scheduler thread
        and a manual "check now" request can arrive at the same moment, and two threads each
        finding "not yet notified" before either writes is how the owner gets the same email
        twice.
        """
        bid = str(business_id)
        stamp = now or _now()
        with self._lock:
            if dedupe_key and dedupe_key in self._sent_keys.get(bid, set()):
                return None

            self._seq += 1
            notification = Notification(
                id=f"NTF-{self._seq:04d}",
                business_id=bid,
                created_at=_iso(stamp),
                code=code,
                severity=severity_for(code, change_severity),
                dedupe_key=dedupe_key,
                title=title,
                body=body,
                what_to_do=what_to_do,
                evidence=evidence or {},
                wording_by=wording_by,
            )
            self._items.setdefault(bid, []).append(notification)
            if dedupe_key:
                self._sent_keys.setdefault(bid, set()).add(dedupe_key)
            self._trim_locked(bid)
            self._evict_locked()
            return notification

    # ── reading ────────────────────────────────────────────────────────────
    def list(self, business_id: str, *, unread_only: bool = False,
             limit: int = 50) -> list[dict]:
        """Newest first, because the reason someone opens an inbox is the newest thing."""
        with self._lock:
            items = list(self._items.get(str(business_id), []))
        if unread_only:
            items = [n for n in items if not n.read]
        items.sort(key=lambda n: n.created_at, reverse=True)
        return [n.as_dict() for n in items[:max(0, limit)]]

    def get(self, business_id: str, notification_id: str) -> Optional[Notification]:
        with self._lock:
            for n in self._items.get(str(business_id), []):
                if n.id == notification_id:
                    return n
        return None

    def unread_count(self, business_id: str) -> int:
        with self._lock:
            return sum(1 for n in self._items.get(str(business_id), []) if not n.read)

    def counts(self, business_id: str) -> dict:
        with self._lock:
            items = list(self._items.get(str(business_id), []))
        return {
            "total": len(items),
            "unread": sum(1 for n in items if not n.read),
            "emailed": sum(1 for n in items if n.email_status == SENT),
            "suppressed": sum(1 for n in items if n.email_status == SUPPRESSED),
            "held": sum(1 for n in items if n.email_status == HELD_FOR_DIGEST),
            "by_severity": {
                sev: sum(1 for n in items if n.severity == sev)
                for sev in ("HIGH", "MODERATE", "LOW")
            },
        }

    # ── read state ─────────────────────────────────────────────────────────
    def mark_read(self, business_id: str, notification_id: str,
                  *, now: Optional[datetime] = None) -> bool:
        with self._lock:
            for n in self._items.get(str(business_id), []):
                if n.id == notification_id:
                    if not n.read:
                        n.read = True
                        n.read_at = _iso(now or _now())
                    return True
        return False

    def mark_all_read(self, business_id: str, *,
                      now: Optional[datetime] = None) -> int:
        stamp = _iso(now or _now())
        changed = 0
        with self._lock:
            for n in self._items.get(str(business_id), []):
                if not n.read:
                    n.read, n.read_at = True, stamp
                    changed += 1
        return changed

    # ── email bookkeeping (the mailer performs the send, this records it) ───
    def set_email_status(self, business_id: str, notification_id: str, status: str,
                         detail: str = "", *, now: Optional[datetime] = None) -> bool:
        with self._lock:
            for n in self._items.get(str(business_id), []):
                if n.id == notification_id:
                    n.email_status = status
                    n.email_detail = detail
                    if status == SENT:
                        n.emailed_at = _iso(now or _now())
                    return True
        return False

    def held_for_digest(self, business_id: str) -> list[Notification]:
        with self._lock:
            return [n for n in self._items.get(str(business_id), [])
                    if n.email_status == HELD_FOR_DIGEST]

    def digest_due(self, business_id: str, *,
                   now: Optional[datetime] = None) -> bool:
        """True when a daily digest may go out: never sent, or the interval has elapsed."""
        stamp = now or _now()
        with self._lock:
            last = self._last_digest.get(str(business_id))
        if not last:
            return True
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            return True
        return stamp - when >= timedelta(hours=DIGEST_INTERVAL_HOURS)

    def record_digest(self, business_id: str, *,
                      now: Optional[datetime] = None) -> None:
        with self._lock:
            self._last_digest[str(business_id)] = _iso(now or _now())

    def last_digest_at(self, business_id: str) -> Optional[str]:
        with self._lock:
            return self._last_digest.get(str(business_id))

    # ── preferences ────────────────────────────────────────────────────────
    def preferences(self, business_id: str) -> dict:
        with self._lock:
            prefs = self._prefs.get(str(business_id))
            return dict(prefs) if prefs else default_preferences()

    def set_preferences(self, business_id: str, changes: dict) -> dict:
        """Merge a partial update. Unknown keys and unknown codes are discarded rather
        than stored — the preference vocabulary is closed, the same way the event
        vocabulary is."""
        bid = str(business_id)
        with self._lock:
            prefs = self._prefs.get(bid) or default_preferences()
            incoming = changes or {}

            if "email_enabled" in incoming:
                prefs["email_enabled"] = bool(incoming["email_enabled"])

            if "minimum_severity" in incoming:
                want = str(incoming["minimum_severity"] or "").upper()
                if want in SEVERITY_ORDER:
                    prefs["minimum_severity"] = want

            if "digest" in incoming:
                want = str(incoming["digest"] or "").upper()
                if want in (IMMEDIATE, DAILY):
                    prefs["digest"] = want

            codes = incoming.get("codes")
            if isinstance(codes, dict):
                for code, on in codes.items():
                    key = str(code).upper()
                    if key in NOTIFIABLE_CODES:
                        prefs.setdefault("codes", {})[key] = bool(on)

            self._prefs[bid] = prefs
            return dict(prefs)

    # ── housekeeping ───────────────────────────────────────────────────────
    def forget(self, business_id: Optional[str] = None) -> None:
        with self._lock:
            if business_id is None:
                self._items.clear()
                self._sent_keys.clear()
                self._prefs.clear()
                self._last_digest.clear()
            else:
                bid = str(business_id)
                self._items.pop(bid, None)
                self._sent_keys.pop(bid, None)
                self._prefs.pop(bid, None)
                self._last_digest.pop(bid, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                "businesses": len(self._items),
                "total_notifications": sum(len(v) for v in self._items.values()),
                "notifiable_codes": list(NOTIFIABLE_CODES),
                "notice": (
                    "Held in memory for this process only. Notifications are a record of "
                    "what we told you, not evidence — the findings they point at live in "
                    "the case and the audit record."),
            }

    def _trim_locked(self, business_id: str) -> None:
        items = self._items.get(business_id)
        if items and len(items) > self.max_per_business:
            # Oldest first out. Their dedupe keys stay remembered, so trimming an inbox
            # cannot cause old news to be re-sent as if it were new.
            del items[:len(items) - self.max_per_business]

    def _evict_locked(self) -> None:
        while len(self._items) > self.max_businesses:
            oldest = min(
                self._items.items(),
                key=lambda kv: (kv[1][-1].created_at if kv[1] else ""))
            bid = oldest[0]
            self._items.pop(bid, None)
            self._sent_keys.pop(bid, None)
            self._prefs.pop(bid, None)
            self._last_digest.pop(bid, None)


# Module-level singleton, owned by the scheduler in the same way the watchlist is.
NOTIFICATIONS = Notifications()
