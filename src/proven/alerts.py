"""From "something changed" to "the owner has been told", in one place.

This is the only module that knows the whole alert pipeline, and it sits above every part of
it so that none of them has to know about the others:

    watchlist      what is different since last time        (no idea anyone gets emailed)
    presenter      how to say it in plain language          (no idea it is an email)
    notifications  the record, and whether to send at all   (no model, no network)
    mailer         put it on the wire, or in the outbox     (no idea what a change is)

WHY THE WORDING GOES THROUGH THE PRESENTER. It would be quicker to write the sentences here.
But the presenter is where the four measured gates live — no invented figures, no predicting
a claim outcome, no generic filler, no confusing commercial cover with home cover — and an
email is the worst possible place to skip them. A dashboard sentence is corrected on the
next render. An email is forwarded to a broker, printed, and read back in six months.

WHY A DECISION AND A DELIVERY ARE SEPARATE. `should_email` decides, `deliver` sends, and the
answer to "why did I not get an email" is recorded on the notification either way. The three
outcomes an owner can see are: sent, held for the daily digest, or suppressed with the
preference that suppressed it named in plain words.

ONE EMAIL PER RUN, NOT ONE PER FINDING. If a check turns up two new branches and a head
count change, that is one message with three items. Three separate emails arriving in the
same second is how a product teaches people to filter it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import mailer, notifications as notif, presenter

ALERT_PURPOSE = "discovery_alert"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _context_for(entry: dict, observation: dict) -> dict:
    """The verified facts the wording may be built from, and nothing else.

    Only values that came out of the watchlist go in here. That matters because the
    presenter's grounding gate checks every numeral in the model's prose against this
    dictionary — anything not present is treated as invented and the wording is thrown away.
    """
    return {
        "code": entry.get("code"),
        "kind": entry.get("kind"),
        "fact_type": entry.get("fact_type"),
        "count": entry.get("count"),
        "values": entry.get("values") or [],
        "policy_says": entry.get("policy_says"),
        "previous_value": entry.get("previous_value"),
        "new_value": entry.get("new_value"),
        "source_name": entry.get("source_name"),
        "author_class": entry.get("author_class"),
        "claim_scope": entry.get("claim_scope"),
        "severity": entry.get("severity"),
        "observed_at": observation.get("observed_at"),
    }


def compose_wording(entry: dict, observation: dict) -> tuple[dict, str]:
    """Plain-language wording for one change. Returns (presentation, "MODEL"|"TEMPLATE")."""
    result = presenter.compose(ALERT_PURPOSE, _context_for(entry, observation))
    pres = result.presentation or {}
    return pres, ("MODEL" if result.mode == "MODEL" else "TEMPLATE")


def raise_alerts(observation: dict, *, business_id: str, business_name: str,
                 store: Optional[notif.Notifications] = None,
                 now: Optional[datetime] = None,
                 send_email: bool = True) -> dict:
    """Turn one watchlist observation into notifications, and email them if allowed.

    Safe to call on every scheduler run. A first observation raises nothing, and news that
    has already been recorded is skipped rather than repeated.
    """
    store = store if store is not None else notif.NOTIFICATIONS
    stamp = now or _now()
    bid = str(business_id)

    if observation.get("first_observation"):
        return {
            "raised": [], "created": 0, "already_known": 0,
            "email": None,
            "note": ("First look at this business, so there is a baseline and nothing to "
                     "report. Nobody was emailed."),
        }

    summary = observation.get("summary") or []
    created: list[notif.Notification] = []
    already_known = 0
    wording_modes: set[str] = set()

    for entry in summary:
        key = entry.get("dedupe_key") or ""
        if key and store.already_notified(bid, key):
            already_known += 1
            continue

        pres, mode = compose_wording(entry, observation)
        wording_modes.add(mode)

        item = store.add(
            bid,
            code=str(entry.get("code") or "UNKNOWN"),
            dedupe_key=key,
            title=str(pres.get("headline") or "Something changed"),
            body=str(pres.get("why_it_matters") or ""),
            what_to_do=str(pres.get("what_next") or ""),
            change_severity=entry.get("severity"),
            wording_by=mode,
            evidence={
                "subheadline": pres.get("subheadline") or "",
                "supporting_points": pres.get("supporting_points") or [],
                "caveat": pres.get("caveat") or "",
                "recommended_action": pres.get("recommended_action") or "",
                "values": entry.get("values") or [],
                "policy_says": entry.get("policy_says"),
                "previous_value": entry.get("previous_value"),
                "new_value": entry.get("new_value"),
                "count": entry.get("count"),
                "source_name": entry.get("source_name"),
                "author_class": entry.get("author_class"),
                "claim_scope": entry.get("claim_scope"),
                "observed_at": observation.get("observed_at"),
            },
            now=stamp,
        )
        if item is not None:
            created.append(item)
        else:
            already_known += 1

    email_result = None
    if created and send_email:
        email_result = _handle_email(
            created, business_id=bid, business_name=business_name, store=store,
            wording_by=("MODEL" if "MODEL" in wording_modes else "TEMPLATE"),
            now=stamp)

    return {
        "raised": [i.as_dict() for i in created],
        "created": len(created),
        "already_known": already_known,
        "email": email_result,
        "note": _note_for(len(created), already_known, email_result),
    }


def _handle_email(items: list[notif.Notification], *, business_id: str,
                  business_name: str, store: notif.Notifications,
                  wording_by: str, now: datetime) -> dict:
    """Apply the preferences, then send one message covering whatever survives them."""
    prefs = store.preferences(business_id)
    sendable: list[notif.Notification] = []
    suppressed: list[dict] = []

    for item in items:
        allowed, why = notif.should_email(item, prefs)
        if allowed:
            sendable.append(item)
        else:
            store.set_email_status(business_id, item.id, notif.SUPPRESSED, why,
                                   now=now)
            suppressed.append({"id": item.id, "title": item.title, "reason": why})

    if not sendable:
        return {"attempted": False, "status": notif.SUPPRESSED,
                "suppressed": suppressed, "message_id": None,
                "detail": ("Everything found was suppressed by your notification "
                           "settings.")}

    # The daily digest: hold until the interval has elapsed, then send everything held
    # together with whatever is new.
    if str(prefs.get("digest")) == notif.DAILY and not store.digest_due(
            business_id, now=now):
        for item in sendable:
            store.set_email_status(
                business_id, item.id, notif.HELD_FOR_DIGEST,
                "Saved for your daily summary rather than emailed on its own.", now=now)
        return {"attempted": False, "status": notif.HELD_FOR_DIGEST,
                "suppressed": suppressed, "message_id": None,
                "detail": (f"Held for your daily summary. {len(sendable)} item(s) "
                           f"waiting.")}

    batch = sendable
    if str(prefs.get("digest")) == notif.DAILY:
        held = [h for h in store.held_for_digest(business_id)
                if h.id not in {s.id for s in sendable}]
        batch = held + sendable

    entry = mailer.deliver(batch, business_id=business_id,
                           business_name=business_name, wording_by=wording_by)

    for item in batch:
        store.set_email_status(business_id, item.id, entry.status, entry.detail,
                               now=now)
    if str(prefs.get("digest")) == notif.DAILY:
        store.record_digest(business_id, now=now)

    return {
        "attempted": True,
        "status": entry.status,
        "transport": entry.transport,
        "message_id": entry.id,
        "to": entry.to,
        "subject": entry.subject,
        "detail": entry.detail,
        "item_count": len(batch),
        "suppressed": suppressed,
    }


def _note_for(created: int, already_known: int,
              email: Optional[dict]) -> str:
    if not created and not already_known:
        return "Nothing changed since the last check."
    if not created:
        return (f"{already_known} change(s) still showing, all of them already reported. "
                f"Nothing new was sent.")
    bits = [f"{created} new change(s) recorded"]
    if already_known:
        bits.append(f"{already_known} already reported and skipped")
    if email is None:
        bits.append("email not attempted")
    elif email.get("status") == notif.SENT:
        bits.append(f"emailed to {email.get('to')}")
    elif email.get("status") == notif.NO_MAIL_SERVER:
        bits.append("written to the outbox, because no mail server is configured")
    elif email.get("status") == notif.HELD_FOR_DIGEST:
        bits.append("held for the daily summary")
    elif email.get("status") == notif.SUPPRESSED:
        bits.append("not emailed, because of your notification settings")
    elif email.get("status") == notif.FAILED:
        bits.append("email failed, and the message is in your outbox")
    return ". ".join(bits) + "."
