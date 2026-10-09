"""Noticing that something CHANGED — a different question from finding a fact.

WHY THIS IS A SEPARATE CONCERN. Discovery answers "what does the outside world say about
this business right now". That is enough to render a screen and not enough to send anybody a
message. "You have two branches" is a fact. "You have opened two branches since we last
looked, and neither is on your policy" is a reason to interrupt someone's day. The second
one requires memory, and nothing in the pipeline had any.

So this module holds one thing per business: the last set of facts we saw. Every new
observation is diffed against it, and the diff — not the fact list — is what a notification
is built from.

FOUR DECISIONS THAT ARE LOAD-BEARING, each from a specific way this goes wrong:

1. IDENTITY IS NOT THE FACT ID. `DiscoveredFact.id` is built as
   `f"DISC-WEB-LOCATION-{len(facts)+1}"` — an index into the list. Add one unrelated fact
   near the front and every id after it shifts, so a naive id diff reports the entire page
   as new every single run. Identity here is (source, fact_type, normalised value), which
   depends only on what the page actually said.

2. THE FIRST LOOK IS A BASELINE, NEVER AN ALERT. On a cold start every fact is "new",
   because there is nothing to compare against. Reporting them would mean the first run
   after every restart shouts "3 new branches!" about premises that have been there for
   years. First observation records and stays silent.

3. ONLY STRUCTURED VALUES MAY RAISE AN ALERT. A language model is in the extraction path,
   so prose comes back phrased differently between runs — "Manufacture of prepared meals and
   dishes" one time, "prepared meals manufacturing" the next. That is a rewording, not a
   business change, and alerting on it would train the owner to ignore us. Addresses, head
   counts and registration statuses are structured enough that a difference means a
   difference; activities and equipment descriptions are not. The latter are recorded and
   shown, never emailed.

4. DISAPPEARANCE IS WEAK EVIDENCE. Presence is grounded — we re-located the words in the
   fetched bytes. Absence is not: a fact vanishes when a branch closes, and equally when a
   redesign defeats a regex or the model simply does not mention it this time. So GONE is
   recorded, and is not alertable. Telling an owner "your branch has closed" because an
   extractor got quieter is worse than saying nothing.

THE SOLE WRITER IS THE SCHEDULER, and that is not incidental. `scheduler._run_discovery()`
calls `discover_business()` with no policy state; the API route calls it with policy state
from the case. The two produce different fact sets for the same business, so if both wrote
here the watchlist would flip-flop NEW and GONE forever. What the outside world publishes
does not depend on what the policy says, so exactly one observer records it.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# Bounded on purpose. This is an in-process cache keyed on a business identifier, and an
# unbounded one is a memory leak with a long fuse.
MAX_BUSINESSES = 32
MAX_TRACKED_FACTS = 400

# Fact types whose values are structured enough that a change means a change. See decision
# 3 above: everything else is recorded and displayed, never alerted on.
ALERTABLE_TYPES: tuple[str, ...] = ("LOCATION", "HEADCOUNT", "STATUS")

# Types where only ONE value can be current per source, so a different value is a CHANGE
# rather than an addition. A head count of 17 replaces a head count of 14; a new address
# does not replace an old one.
#
# Scoped per source, not globally, because two sources legitimately report different numbers
# for the same type: the website says "a team of 14" (everybody) while MOM says 4 (foreign
# work permits). Those are not competing values, they are different measurements.
SINGLETON_TYPES: tuple[str, ...] = ("HEADCOUNT", "STATUS", "NAME")

# Below this length, containment is meaningless — the head count value "4" is a substring of
# half the addresses in Singapore. Same floor and same reasoning as
# discovery.is_duplicate_fact().
_MIN_CONTAINMENT = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise_value(fact_type: str, value) -> str:
    """A comparable form of a fact's value, by type.

    Type-aware because the failure modes differ. Addresses arrive punctuated inconsistently
    — "#03-11, 21 Tai Seng Avenue" from a regex, "#03-11 21 Tai Seng Avenue, Singapore
    534054" from a model — so punctuation is dropped entirely. Head counts arrive wrapped in
    prose ("Total Foreign Workers: 4"), so only the number survives.
    """
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if fact_type == "HEADCOUNT":
        nums = re.findall(r"\d[\d,]*", s)
        return nums[0].replace(",", "") if nums else s
    if fact_type == "LOCATION":
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s+", " ", s)


def slot_of(fact: dict) -> tuple[str, str]:
    """(source, fact_type) — the bucket a fact competes in."""
    src = ((fact.get("source") or {}).get("id")
           or (fact.get("source") or {}).get("name") or "?")
    return (str(src), str(fact.get("fact_type") or "?"))


def key_of(fact: dict) -> tuple[str, str, str]:
    src, ftype = slot_of(fact)
    return (src, ftype, normalise_value(ftype, fact.get("value")))


def _tracked(fact: dict) -> dict:
    """The minimum worth remembering about a fact between runs."""
    source = fact.get("source") or {}
    return {
        "fact_type": fact.get("fact_type"),
        "label": fact.get("label") or "",
        "value": fact.get("value"),
        "policy_says": fact.get("previous_value"),
        "source_id": source.get("id"),
        "source_name": source.get("name"),
        "author_class": source.get("author_class"),
        "claim_scope": fact.get("claim_scope"),
        "severity": fact.get("severity"),
        "extraction": fact.get("extraction"),
    }


def _same_thing_reworded(a: str, b: str) -> bool:
    """Whether two normalised values are the same fact said differently.

    The regex and the model capture different amounts of the same address — one stops at a
    comma, the other runs to the postcode. Without this, a run where the regex fails and the
    model succeeds reports a brand-new branch at an address the business has always had.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= _MIN_CONTAINMENT and (a in b or b in a)


@dataclass(frozen=True)
class Change:
    """One difference between what we see now and what we saw last time."""

    kind: str                      # NEW | CHANGED | GONE
    fact_type: str
    label: str
    value: Optional[str]
    previous_value: Optional[str]  # what WE saw last time
    policy_says: Optional[str]     # what the policy schedule says, per Discovery
    source_id: str
    source_name: str
    author_class: str
    claim_scope: str
    severity: str
    observed_at: str
    alertable: bool
    not_alertable_because: str = ""

    @property
    def code(self) -> str:
        return f"{self.kind}_{self.fact_type}"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "code": self.code,
            "fact_type": self.fact_type,
            "label": self.label,
            "value": self.value,
            "previous_value": self.previous_value,
            "policy_says": self.policy_says,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "author_class": self.author_class,
            "claim_scope": self.claim_scope,
            "severity": self.severity,
            "observed_at": self.observed_at,
            "alertable": self.alertable,
            "not_alertable_because": self.not_alertable_because,
        }


def _alertability(kind: str, fact_type: str) -> tuple[bool, str]:
    if kind == "GONE":
        return False, (
            "Something we saw before is not on the page now. That happens when a site is "
            "redesigned or worded differently, not only when a business changes, so we "
            "record it and do not raise it.")
    if fact_type not in ALERTABLE_TYPES:
        return False, (
            "Descriptions get reworded between checks, so a difference here does not "
            "reliably mean anything changed. Recorded, not raised.")
    return True, ""


def diff(previous: Optional[dict], facts: list[dict], *,
         now: Optional[str] = None) -> list[Change]:
    """Compare a fresh fact list against the last observation.

    `previous` is a snapshot dict as stored by `Watchlist`, or None for a cold start — in
    which case there is nothing to compare and no changes exist, by definition rather than
    by suppression.
    """
    stamp = now or _now_iso()
    if not previous:
        return []

    old_by_key: dict = dict(previous.get("facts") or {})
    old_by_slot: dict = {}
    for key, rec in old_by_key.items():
        old_by_slot.setdefault((key[0], key[1]), []).append((key[2], rec))

    changes: list[Change] = []
    seen_keys: set = set()
    seen_slots: set = set()

    for fact in facts or []:
        key = key_of(fact)
        slot = (key[0], key[1])
        # Defence in depth. Extractors have shipped the same address twice from one page
        # (see discovery's website location loop), and this count goes into an email, so a
        # duplicate upstream must not become "2 new branches" here.
        if key in seen_keys:
            continue
        seen_keys.add(key)
        seen_slots.add(slot)
        if key in old_by_key:
            continue                       # unchanged, and silence is correct

        prior = old_by_slot.get(slot) or []

        # Same fact, different words. Not a change.
        if any(_same_thing_reworded(key[2], old_norm) for old_norm, _ in prior):
            continue

        rec = _tracked(fact)
        if key[1] in SINGLETON_TYPES and prior:
            # One value may be current in this slot, so this replaces what was there.
            kind, previous_value = "CHANGED", prior[0][1].get("value")
        else:
            kind, previous_value = "NEW", None

        alertable, why = _alertability(kind, key[1])
        changes.append(Change(
            kind=kind, fact_type=key[1], label=rec["label"], value=rec["value"],
            previous_value=previous_value, policy_says=rec["policy_says"],
            source_id=str(rec["source_id"] or ""),
            source_name=str(rec["source_name"] or ""),
            author_class=str(rec["author_class"] or ""),
            claim_scope=str(rec["claim_scope"] or ""),
            severity=str(rec["severity"] or "MODERATE"),
            observed_at=stamp, alertable=alertable, not_alertable_because=why,
        ))

    # Anything we held before and cannot see now. Recorded, never alerted — see decision 4.
    for key, rec in old_by_key.items():
        if key in seen_keys:
            continue
        slot = (key[0], key[1])
        if slot not in seen_slots:
            # The whole source went quiet — a failed fetch, not a closed branch. Reporting
            # every fact from an unreachable page as "gone" would be pure noise.
            continue
        if key[1] in SINGLETON_TYPES:
            continue                       # already reported as CHANGED above
        if any(_same_thing_reworded(key[2], key_of(f)[2])
               for f in facts or [] if slot_of(f) == slot):
            continue
        alertable, why = _alertability("GONE", key[1])
        changes.append(Change(
            kind="GONE", fact_type=key[1], label=rec.get("label") or "",
            value=rec.get("value"), previous_value=rec.get("value"),
            policy_says=rec.get("policy_says"),
            source_id=str(rec.get("source_id") or ""),
            source_name=str(rec.get("source_name") or ""),
            author_class=str(rec.get("author_class") or ""),
            claim_scope=str(rec.get("claim_scope") or ""),
            severity="LOW",
            observed_at=stamp, alertable=alertable, not_alertable_because=why,
        ))
    return changes


def summarise(changes: list[Change]) -> list[dict]:
    """Group changes into the units a person would recognise.

    Two new addresses is one piece of news — "two places you work from are not on your
    policy" — not two notifications. Grouping happens here rather than in the notifier so
    that the count is derived from the data instead of being written into a sentence.
    """
    groups: dict[str, list[Change]] = {}
    for c in changes:
        if not c.alertable:
            continue
        groups.setdefault(c.code, []).append(c)

    out: list[dict] = []
    for code, items in groups.items():
        first = items[0]
        sev = max((c.severity for c in items),
                  key=lambda s: _SEVERITY_ORDER.get(s, 0))
        entry = {
            "code": code,
            "kind": first.kind,
            "fact_type": first.fact_type,
            "count": len(items),
            "values": [c.value for c in items],
            "policy_says": first.policy_says,
            "source_name": first.source_name,
            "author_class": first.author_class,
            "claim_scope": first.claim_scope,
            "severity": sev,
            "dedupe_key": _dedupe_key(code, items),
        }
        if first.kind == "CHANGED":
            entry["previous_value"] = first.previous_value
            entry["new_value"] = first.value
        out.append(entry)
    out.sort(key=lambda e: (-_SEVERITY_ORDER.get(e["severity"], 0), e["code"]))
    return out


_SEVERITY_ORDER = {"LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}


def _dedupe_key(code: str, items: list[Change]) -> str:
    """A stable fingerprint for "this exact piece of news".

    The scheduler re-runs every few minutes and a change stays visible until the policy is
    updated, so without this the same two branches would be emailed twelve times an hour.
    Built from the normalised values so that a rewording does not produce a fresh key.
    """
    parts = sorted(normalise_value(c.fact_type, c.value) for c in items)
    raw = code + "|" + "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class Watchlist:
    """Remembers the last observation per business. In-memory, bounded, thread-safe."""

    def __init__(self, max_businesses: int = MAX_BUSINESSES):
        self._snapshots: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.max_businesses = max_businesses

    def observe(self, business_id: str, facts: list[dict], *,
                now: Optional[str] = None) -> dict:
        """Record what we can see now, and report what changed since last time."""
        stamp = now or _now_iso()
        bid = str(business_id or "unknown")
        facts = list(facts or [])[:MAX_TRACKED_FACTS]

        with self._lock:
            previous = self._snapshots.get(bid)
            changes = diff(previous, facts, now=stamp)

            self._snapshots[bid] = {
                "business_id": bid,
                "observed_at": stamp,
                "facts": {key_of(f): _tracked(f) for f in facts},
            }
            self._evict_locked()

        alertable = [c for c in changes if c.alertable]
        first = previous is None
        return {
            "business_id": bid,
            "observed_at": stamp,
            "previous_observed_at": (previous or {}).get("observed_at"),
            "first_observation": first,
            "facts_tracked": len(facts),
            "changes": [c.as_dict() for c in changes],
            "alertable_changes": [c.as_dict() for c in alertable],
            "summary": summarise(changes),
            "note": (
                "First look at this business, so there is nothing to compare against yet. "
                "What we can see has been recorded as the baseline and nothing has been "
                "raised — everything looks new on a first look, and reporting that would "
                "mean claiming a change we have no evidence for."
                if first else
                f"Compared against the previous check. {len(alertable)} change(s) worth "
                f"raising, {len(changes) - len(alertable)} recorded without raising."),
        }

    def last_seen(self, business_id: str) -> Optional[str]:
        with self._lock:
            snap = self._snapshots.get(str(business_id))
            return snap.get("observed_at") if snap else None

    def tracked_count(self, business_id: str) -> int:
        with self._lock:
            snap = self._snapshots.get(str(business_id))
            return len(snap.get("facts") or {}) if snap else 0

    def forget(self, business_id: Optional[str] = None) -> None:
        """Drop memory, so the next look is a fresh baseline. Used by tests and by a
        deliberate "start watching again" action."""
        with self._lock:
            if business_id is None:
                self._snapshots.clear()
            else:
                self._snapshots.pop(str(business_id), None)

    def stats(self) -> dict:
        with self._lock:
            return {
                "businesses_watched": len(self._snapshots),
                "max_businesses": self.max_businesses,
                "watching": [
                    {"business_id": bid, "observed_at": s.get("observed_at"),
                     "facts_tracked": len(s.get("facts") or {})}
                    for bid, s in self._snapshots.items()
                ],
                "alertable_types": list(ALERTABLE_TYPES),
                "notice": (
                    "Held in memory for this process only. A restart forgets what was "
                    "seen, so the next check is a baseline rather than a wave of alerts "
                    "about premises that have not moved."),
            }

    def _evict_locked(self) -> None:
        while len(self._snapshots) > self.max_businesses:
            oldest = min(self._snapshots.items(),
                         key=lambda kv: kv[1].get("observed_at") or "")
            self._snapshots.pop(oldest[0], None)


# Module-level singleton. The scheduler owns it; see the module docstring.
WATCHLIST = Watchlist()
