"""Sending the message — or being visibly honest that we did not.

THE DESIGN PROBLEM. A notification feature demonstrated without a mail server is usually
faked: a toast that says "email sent!" and nothing on the wire. That is the same dishonesty
this whole product exists to refuse, so it is not available here.

Instead there are two modes and the interface always says which one it is in:

  SMTP configured        the message is really sent, and the transcript records the server
                         and the recipient.
  no server configured   the message is COMPOSED in full and written to an outbox that the
                         owner can open and read, marked NO_MAIL_SERVER. Nothing claims to
                         have been delivered.

The outbox is not a consolation prize. It is the more useful artefact for a demonstration,
because you can read the exact bytes the owner would have received, and it is the only way
to show the wording without owning a mailbox.

WHY THE HTML IS ESCAPED WITH SOME CARE. The body contains text scraped from a third-party
website — an address, a company description, a status. Interpolating that into HTML without
escaping would put a cross-site scripting payload in somebody's mail client, delivered by
us, signed by us. `esc()` is applied to every interpolated value on the HTML path, and the
plain-text part carries no markup at all.

NOTHING HERE RAISES. A mail server being unreachable must never take down the agent run that
produced the notification. Every failure returns a status and a sentence.
"""
from __future__ import annotations

import html
import smtplib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

from . import config, notifications as notif

MAX_OUTBOX = 100

BRAND = "VERIS"
_SUPPORT_NOTE = ("You are receiving this because monitoring is switched on for your "
                 "business. You can change what we email you, or turn it off, in your "
                 "notification settings.")


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OutboxEntry:
    """A message we composed, and what became of it."""

    id: str
    created_at: str
    business_id: str
    notification_ids: list[str]
    to: str
    sender: str
    subject: str
    text_body: str
    html_body: str
    status: str
    detail: str = ""
    transport: str = "OUTBOX"          # SMTP | OUTBOX
    wording_by: str = "TEMPLATE"
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "business_id": self.business_id,
            "notification_ids": list(self.notification_ids),
            "to": self.to,
            "from": self.sender,
            "subject": self.subject,
            "text_body": self.text_body,
            "html_body": self.html_body,
            "status": self.status,
            "detail": self.detail,
            "transport": self.transport,
            "wording_by": self.wording_by,
            "meta": self.meta,
        }


class Outbox:
    """Every message we composed, in order, bounded."""

    def __init__(self, limit: int = MAX_OUTBOX):
        self._entries: list[OutboxEntry] = []
        self._lock = threading.RLock()
        self._seq = 0
        self.limit = limit

    def add(self, **kw) -> OutboxEntry:
        with self._lock:
            self._seq += 1
            entry = OutboxEntry(id=f"MSG-{self._seq:04d}", created_at=_now_iso(), **kw)
            self._entries.append(entry)
            if len(self._entries) > self.limit:
                del self._entries[:len(self._entries) - self.limit]
            return entry

    def list(self, business_id: Optional[str] = None, *, limit: int = 25) -> list[dict]:
        with self._lock:
            items = list(self._entries)
        if business_id:
            items = [e for e in items if e.business_id == str(business_id)]
        items.reverse()
        return [e.as_dict() for e in items[:max(0, limit)]]

    def get(self, message_id: str) -> Optional[OutboxEntry]:
        with self._lock:
            for e in self._entries:
                if e.id == message_id:
                    return e
        return None

    def count(self, business_id: Optional[str] = None) -> int:
        with self._lock:
            if business_id is None:
                return len(self._entries)
            return sum(1 for e in self._entries if e.business_id == str(business_id))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def forget(self, business_id: Optional[str] = None) -> int:
        """Drop one business's messages, or all of them. Returns how many went.

        Used by "start fresh". Without it, clearing the inbox would leave a mail log full of
        messages describing notifications that no longer exist — an interface showing an
        email about a change the user cannot find anywhere.
        """
        if business_id is None:
            with self._lock:
                n = len(self._entries)
                self._entries.clear()
            return n
        bid = str(business_id)
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.business_id != bid]
            return before - len(self._entries)


OUTBOX = Outbox()


# ── composing ────────────────────────────────────────────────────────────────

# ── who is sending, and the Singapore rules that follow from it ──────────────
#
# WHY `sender_brand` EXISTS RATHER THAN A GLOBAL RENAME.
#
# Two different senders now use this module. VERIS emails what its own agents noticed; a
# carrier emails a request of its own. An email from the carrier with VERIS in the header
# is wrong, and so is the reverse — so the brand is a parameter, and `BRAND` stays the
# default because the agent mails were genuinely sent by VERIS.
#
# THE SPAM CONTROL ACT DISTINCTION, AND WHY IT IS ENCODED RATHER THAN ASSUMED.
#
# Singapore's Spam Control Act 2007 governs unsolicited commercial messages sent IN BULK,
# and IMDA requires a subject-line label plus an unsubscribe facility on those. A service
# message to an existing policyholder about their own account is a different thing from a
# marketing campaign to a cohort, and treating them identically would either label
# transactional mail as advertising or fail to label a campaign that is one.
#
# So the caller states which it is. `bulk_commercial=True` adds the label; the unsubscribe
# line is present on EVERY message regardless, because the preference system genuinely is
# one and there is no reason to withhold it.
ADV_LABEL = "<ADV>"
UNSUBSCRIBE_NOTE = (
    "To stop these emails, open your VERIS notification settings and turn email off, or "
    "reply to this message asking to unsubscribe.")


def subject_for(items: list[notif.Notification], business_name: str, *,
                sender_brand: Optional[str] = None,
                bulk_commercial: bool = False) -> str:
    """A subject line that says what happened, not that something happened.

    "Update from VERIS" is the subject line of an email nobody opens. The count and the
    thing that changed go in the subject, because for most recipients the subject IS the
    notification.
    """
    brand = sender_brand or BRAND
    prefix = f"{ADV_LABEL} " if bulk_commercial else ""
    if not items:
        return f"{prefix}{brand}: nothing to report for {business_name}"
    if len(items) == 1:
        return f"{prefix}{brand}: {items[0].title}"
    high = sum(1 for i in items if i.severity == "HIGH")
    lead = f"{len(items)} things we noticed"
    if high:
        lead = f"{len(items)} things we noticed, {high} worth doing now"
    return f"{prefix}{brand}: {lead} about {business_name}"


def _text_block(item: notif.Notification) -> str:
    # The headline is underlined rather than upper-cased. Section LABELS are shouted because
    # they are furniture; the sentence itself was written with some care and putting it in
    # capitals just makes the product sound like it is shouting at the owner.
    lines = [item.title, "~" * min(len(item.title), 60), ""]
    if item.body:
        lines += [item.body, ""]
    points = (item.evidence or {}).get("supporting_points") or []
    for p in points:
        lines.append(f"  - {p}")
    if points:
        lines.append("")
    if item.what_to_do:
        lines += ["WHAT WOULD HELP", item.what_to_do, ""]
    caveat = (item.evidence or {}).get("caveat")
    if caveat:
        lines += [f"How sure are we? {caveat}", ""]
    where = (item.evidence or {}).get("source_name")
    if where:
        lines.append(f"Where we saw it: {where}")
    seen = (item.evidence or {}).get("observed_at")
    if seen:
        lines.append(f"When we checked: {seen}")
    return "\n".join(lines)


def render_text(items: list[notif.Notification], business_name: str, *,
                sender_brand: Optional[str] = None) -> str:
    brand = sender_brand or BRAND
    from_carrier = brand != BRAND
    parts = [
        (f"{brand} — a message about your cover for {business_name}" if from_carrier
         else f"{brand} — what changed for {business_name}"),
        "=" * 60,
        "",
        # The opening line is the one place the reader learns WHO is writing, and the two
        # senders are making different claims. VERIS says "we looked and something moved".
        # A carrier says "we are asking you to look" — and it deliberately does not say
        # what it found, because it has not read the business's documents.
        (("Your insurer has asked us to pass this on. It concerns your account with them, "
          "and it does not include anything from your own documents — they have not seen "
          "them.") if from_carrier else
         ("We check your public information on a schedule. This is what is different "
          "since we last looked.")),
        "",
    ]
    for item in items:
        parts.append(_text_block(item))
        parts.append("-" * 60)
        parts.append("")
    parts += [
        "WHAT WE DID NOT DO",
        (("Your insurer has not seen your documents, your findings or your protection "
          "score. Nothing has been shared with them, and nothing happens until you "
          "decide it should.") if from_carrier else
         ("We have not changed anything on your policy and we have not told your insurer. "
          "Nothing happens until you decide it should.")),
        "",
        _SUPPORT_NOTE,
        "",
        UNSUBSCRIBE_NOTE,
    ]
    return "\n".join(parts)


def _html_block(item: notif.Notification) -> str:
    ev = item.evidence or {}
    colour = {"HIGH": "#b42318", "MODERATE": "#b54708"}.get(item.severity, "#475467")
    points = "".join(
        f'<li style="margin:4px 0;color:#344054">{esc(p)}</li>'
        for p in (ev.get("supporting_points") or [])[:3])
    rows = ""
    if item.what_to_do:
        rows += (
            '<tr><td style="padding:14px 0 0"><div style="font-size:11px;'
            'text-transform:uppercase;letter-spacing:.08em;color:#667085;'
            'font-weight:700">What would help</div>'
            f'<div style="color:#344054;margin-top:4px">{esc(item.what_to_do)}</div>'
            "</td></tr>")
    if ev.get("caveat"):
        rows += (
            '<tr><td style="padding:14px 0 0"><div style="background:#f8f9fb;'
            'border-left:3px solid #d0d5dd;padding:10px 14px;font-size:13px;'
            f'color:#475467">{esc(ev["caveat"])}</div></td></tr>')
    where = ev.get("source_name")
    if where:
        rows += (
            '<tr><td style="padding:12px 0 0;font-size:12px;color:#667085">'
            f'Where we saw it: {esc(where)}'
            + (f' &middot; checked {esc(ev.get("observed_at"))}'
               if ev.get("observed_at") else "")
            + "</td></tr>")
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid #e4e7ec;border-radius:10px;margin:0 0 16px">
  <tr><td style="padding:20px 22px">
    <div style="font-size:11px;font-weight:700;letter-spacing:.08em;
                text-transform:uppercase;color:{colour}">
      {esc(item.severity)} &middot; {esc(notif.CODE_LABELS.get(item.code, item.code))}
    </div>
    <div style="font-size:19px;font-weight:700;color:#101828;margin:6px 0 8px;
                line-height:1.35">{esc(item.title)}</div>
    <div style="color:#344054">{esc(item.body)}</div>
    {f'<ul style="margin:12px 0 0;padding-left:18px">{points}</ul>' if points else ""}
    <table role="presentation" width="100%">{rows}</table>
  </td></tr>
</table>"""


def render_html(items: list[notif.Notification], business_name: str, *,
                sender_brand: Optional[str] = None) -> str:
    blocks = "".join(_html_block(i) for i in items)
    brand = sender_brand or BRAND
    from_carrier = brand != BRAND
    strap = ("A message about your cover" if from_carrier
             else f"What changed for {business_name}")
    lede = (("Your insurer has asked us to pass this on. It concerns your account with "
             "them, and it contains nothing from your own documents — they have not seen "
             "them.") if from_carrier else
            ("We check your public information on a schedule. This is what is different "
             "since we last looked."))
    didnt = (("Your insurer has not seen your documents, your findings or your protection "
              "score. Nothing has been shared with them, and nothing happens until you "
              "decide it should.") if from_carrier else
             ("We have not changed anything on your policy and we have not told your "
              "insurer. Nothing happens until you decide it should."))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
  content="width=device-width,initial-scale=1"><title>{esc(brand)}</title></head>
<body style="margin:0;padding:0;background:#f4f5f7;
             font-family:-apple-system,'Segoe UI',Roboto,Helvetica,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
 <tr><td align="center" style="padding:28px 14px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:620px;background:#fff;border-radius:14px;overflow:hidden;
                border:1px solid #e4e7ec">
   <tr><td style="background:#101828;padding:22px 24px">
     <div style="color:#fff;font-size:17px;font-weight:800;letter-spacing:-.2px">
       {esc(brand)}</div>
     <div style="color:#98a2b3;font-size:12.5px;margin-top:2px">
       {esc(strap)}</div>
   </td></tr>
   <tr><td style="padding:24px">
     <div style="color:#475467;font-size:14.5px;margin-bottom:18px">
       {esc(lede)}</div>
     {blocks}
     <table role="presentation" width="100%"
            style="background:#f8f9fb;border-radius:10px;margin-top:4px">
       <tr><td style="padding:16px 18px">
         <div style="font-size:11px;font-weight:700;letter-spacing:.08em;
                     text-transform:uppercase;color:#667085">What we did not do</div>
         <div style="color:#475467;font-size:13.5px;margin-top:5px">
           {esc(didnt)}</div>
       </td></tr>
     </table>
     <div style="color:#98a2b3;font-size:12px;margin-top:16px">
       {esc(UNSUBSCRIBE_NOTE)}</div>
   </td></tr>
   <tr><td style="padding:16px 24px 22px;border-top:1px solid #e4e7ec;
                  color:#98a2b3;font-size:12px">{esc(_SUPPORT_NOTE)}</td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""


# ── sending ──────────────────────────────────────────────────────────────────

def _send_smtp(to: str, subject: str, text_body: str,
               html_body: str) -> tuple[bool, str]:
    """Really send it. Returns (sent, detail). Never raises."""
    s = config.smtp_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["sender"]
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        if s["port"] == 465:
            server = smtplib.SMTP_SSL(s["host"], s["port"], timeout=20)
        else:
            server = smtplib.SMTP(s["host"], s["port"], timeout=20)
        with server:
            if s["use_tls"] and s["port"] != 465:
                server.starttls()
            if s["user"] and s["password"]:
                server.login(s["user"], s["password"])
            server.send_message(msg)
        return True, f"Sent via {s['host']}:{s['port']} to {to}"
    except Exception as e:
        # The exception text can carry the server's reply; it never carries our password,
        # because the password is not interpolated into any message we build.
        return False, f"{type(e).__name__}: {e}"


def deliver(items: list[notif.Notification], *, business_id: str,
            business_name: str, to: Optional[str] = None,
            wording_by: str = "TEMPLATE",
            sender_brand: Optional[str] = None,
            bulk_commercial: bool = False) -> OutboxEntry:
    """Compose one message covering `items`, send it if we can, and record what happened.

    Always returns an outbox entry, whatever the outcome, so the interface can show the
    exact message that was or was not delivered.

    `sender_brand` lets a carrier send as itself rather than as VERIS. `bulk_commercial`
    controls the Spam Control Act subject label — see the note above `ADV_LABEL`; it is
    the caller's statement about what the message IS, because only the caller knows
    whether this is a service notice to an existing policyholder or a campaign.
    """
    settings = config.smtp_settings()
    # Precedence is deliberate: an explicit `to` for a real recipient, then the demo
    # override, then nothing. `PROVEN_NOTIFY_TO` wins over the account address in a demo
    # because the seeded accounts use a `.demo` domain that can never receive mail.
    recipient = (config.notify_override() or to or "").strip()
    subject = subject_for(items, business_name, sender_brand=sender_brand,
                          bulk_commercial=bulk_commercial)
    text_body = render_text(items, business_name, sender_brand=sender_brand)
    html_body = render_html(items, business_name, sender_brand=sender_brand)

    if not items:
        status, detail, transport = notif.NOT_ATTEMPTED, "Nothing to send.", "OUTBOX"
    elif not config.smtp_configured():
        status = notif.NO_MAIL_SERVER
        transport = "OUTBOX"
        detail = ("No mail server is configured, so this was written to your outbox "
                  "instead of being sent. Set PROVEN_SMTP_HOST and PROVEN_SMTP_FROM to "
                  "deliver it for real.")
    elif not recipient:
        status = notif.NO_MAIL_SERVER
        transport = "OUTBOX"
        detail = ("No recipient address is configured. The demo accounts use a .demo "
                  "domain that cannot receive mail, so set PROVEN_NOTIFY_TO to a real "
                  "address.")
    else:
        sent, detail = _send_smtp(recipient, subject, text_body, html_body)
        status = notif.SENT if sent else notif.FAILED
        transport = "SMTP"

    return OUTBOX.add(
        business_id=str(business_id),
        notification_ids=[i.id for i in items],
        to=recipient or "(not configured)",
        sender=settings["sender"] or "(not configured)",
        subject=subject, text_body=text_body, html_body=html_body,
        status=status, detail=detail, transport=transport, wording_by=wording_by,
        meta={"item_count": len(items),
              "severities": sorted({i.severity for i in items}),
              "sender_brand": sender_brand or BRAND,
              "bulk_commercial": bool(bulk_commercial)},
    )


def status() -> dict:
    """What the interface is told about mail. Reports presence, never secrets."""
    s = config.smtp_settings()
    configured = config.smtp_configured()
    return {
        "configured": configured,
        "mode": "SMTP" if configured else "OUTBOX_ONLY",
        "host": s["host"] or None,
        "port": s["port"] if configured else None,
        "sender": s["sender"] or None,
        "credentials_present": bool(s["user"] and s["password"]),
        "use_tls": s["use_tls"],
        "recipient_override": config.notify_override() or None,
        "outbox_count": OUTBOX.count(),
        "notice": (
            "Mail is really sent. The outbox still records every message so you can read "
            "exactly what went out."
            if configured else
            "No mail server is configured, so messages are composed in full and written "
            "to the outbox rather than sent. Nothing here claims to have been delivered."),
    }
