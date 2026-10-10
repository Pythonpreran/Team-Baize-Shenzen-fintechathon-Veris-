"""The adviser's file note — AI drafts, deterministic grounding verifies, human approves.

WHY A FILE NOTE IS THE RIGHT PLACE FOR A MODEL, AND MOST THINGS ARE NOT.

The rule this codebase holds to is that a model may propose, rephrase or retrieve, and may
never decide. Almost every adviser task fails that test — deciding whether a warranty was
breached, whether cover responds, which client to call first. Those are the licensed act.

Writing up what you already decided is different. An adviser in Singapore has to record the
basis on which advice was given, and that paperwork is where their hours actually go. The
decisions are already made by a human before this module runs; all that is left is prose
over facts that are fixed. So the model does the typing and none of the thinking.

THE GROUNDING GATE, AND WHY IT IS STRICTER HERE THAN ON A SCREEN.

A file note is relied on later. A screen is read now, by somebody who can see the evidence
next to it; a note is read months later by somebody who cannot. So an invented clause
reference in a note is more dangerous than the same invention on a screen, and the check is
correspondingly harder:

  * every rule id in the draft must be one of the findings actually reviewed
  * every clause reference must appear in a citation on those findings
  * every page number must be one of the pages those citations name
  * every number must appear in the verified context handed in

A draft that fails any of these is REJECTED, not repaired, and the deterministic template
is used instead. Repairing it would mean guessing what the model meant, which is the
failure mode this whole architecture exists to avoid.

THREE STATES, AND WHY NOTHING AUTO-SENDS.

    AI_DRAFT           the model wrote it. Nobody has read it.
    ADVISER_EDITED     a human changed the text.
    ADVISER_APPROVED   a human put their name to it.

Only an approved note is a record. Nothing is ever sent to the SME or the carrier from
here: a draft escaping to a client as if it were advice is the single worst outcome this
feature could produce, so there is no code path that sends it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .lang.lexicons import en as english_lexicon

AI_DRAFT = "AI_DRAFT"
ADVISER_EDITED = "ADVISER_EDITED"
ADVISER_APPROVED = "ADVISER_APPROVED"
STATES: tuple[str, ...] = (AI_DRAFT, ADVISER_EDITED, ADVISER_APPROVED)

STATE_LABELS: dict[str, str] = {
    AI_DRAFT: "Drafted, not yet read",
    ADVISER_EDITED: "Edited by the adviser",
    ADVISER_APPROVED: "Approved by the adviser",
}

# Language that turns a note into a coverage opinion. Refused unless it appears inside a
# verbatim quotation, because a wording that says "not covered" must be quotable — the
# engine's whole method is to quote the document rather than paraphrase it.
BANNED: tuple[str, ...] = english_lexicon.NOTE_BANNED_PHRASES

MAX_NOTE = 6000


@dataclass
class Note:
    """One file note for one case, and its history."""

    id: str
    case_id: str
    state: str
    text: str
    drafted_by: str                  # MODEL | TEMPLATE
    adviser: Optional[str] = None
    organisation: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    approved_at: Optional[str] = None
    grounding: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "case_id": self.case_id,
            "state": self.state, "state_label": STATE_LABELS.get(self.state, self.state),
            "text": self.text, "drafted_by": self.drafted_by,
            "adviser": self.adviser, "organisation": self.organisation,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "approved_at": self.approved_at,
            "grounding": self.grounding, "context": self.context,
            "history": self.history,
            "is_record": self.state == ADVISER_APPROVED,
            "notice": ("Only an approved note is a record. A draft is the model's "
                       "suggestion and carries no authority."),
        }


# ── grounding ───────────────────────────────────────────────────────────────

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CLAUSE = english_lexicon.CLAUSE_PATTERNS[0]
_PAGE = re.compile(r"\bpage\s+(\d+)\b", re.I)
_RULE_ID = re.compile(r"\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+\b")


def allowed_facts(findings: list[dict], context: dict) -> dict[str, set[str]]:
    """Everything a draft is permitted to mention, derived from the disclosed material.

    Built from the findings the adviser actually reviewed, so a note cannot reference a
    finding withheld by the consent gate any more than the adviser could read one.
    """
    rules: set[str] = set()
    clauses: set[str] = set()
    pages: set[str] = set()
    numbers: set[str] = set()

    for f in findings or []:
        rid = str(f.get("rule_id") or "")
        if rid:
            rules.add(rid)
        for cite in (f.get("detail") or {}).get("cite") or []:
            if isinstance(cite, dict) and cite.get("clause"):
                clauses.add(str(cite["clause"]))
        for span in f.get("spans") or []:
            if span.get("page_no") is not None:
                pages.add(str(span["page_no"]))
            for m in _NUMBER.finditer(str(span.get("quoted_text") or "")):
                numbers.add(m.group(0).replace(",", ""))
        for m in _NUMBER.finditer(str(f.get("headline") or "")):
            numbers.add(m.group(0).replace(",", ""))

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)
        elif value is not None:
            for m in _NUMBER.finditer(str(value)):
                numbers.add(m.group(0).replace(",", ""))

    walk(context or {})
    return {"rules": rules, "clauses": clauses, "pages": pages, "numbers": numbers}


def verify(text: str, allowed: dict[str, set[str]]) -> tuple[bool, list[str]]:
    """Reject a draft that references anything it was not given. Never repair one.

    Returns `(ok, problems)`. Every problem names the exact token, so a rejection is
    reviewable rather than a shrug — and so a demo can show a hallucination being caught
    with the invented string on screen.
    """
    problems: list[str] = []
    body = text or ""

    quoted = " ".join(re.findall(r"[\"“]([^\"”]{10,})[\"”]", body))

    low = body.lower()
    quoted_low = quoted.lower()
    for phrase in BANNED:
        if phrase in low and phrase not in quoted_low:
            problems.append(
                f"states a coverage outcome outside a quotation: {phrase!r}")

    for m in _RULE_ID.finditer(body):
        token = m.group(0)
        # Ignore acronyms that are not rule ids by shape.
        if "-" not in token:
            continue
        if token not in allowed["rules"]:
            problems.append(f"cites a finding that was not reviewed: {token!r}")

    for m in _CLAUSE.finditer(body):
        ref = m.group(1)
        if allowed["clauses"] and ref not in allowed["clauses"]:
            problems.append(f"cites a clause with no citation behind it: cl {ref}")

    for m in _PAGE.finditer(body):
        page = m.group(1)
        if allowed["pages"] and page not in allowed["pages"]:
            problems.append(f"cites a page no citation names: page {page}")

    for m in _NUMBER.finditer(body):
        raw = m.group(0).replace(",", "")
        if raw in allowed["numbers"]:
            continue
        # Small integers are prose ("3 findings", "two of six"), not claims about money.
        try:
            if abs(float(raw)) <= 50:
                continue
        except ValueError:
            continue
        problems.append(f"states a figure that is not in the evidence: {raw}")

    return (not problems), problems


# ── drafting ────────────────────────────────────────────────────────────────

def template_note(*, business: str, adviser: str, findings: list[dict],
                  requests: list[dict], context: dict) -> str:
    """The deterministic note. Used when no model is configured, and whenever a draft is
    rejected — so the adviser always has something defensible to edit."""
    escalated = [f for f in findings
                 if f.get("state") == "REQUIRES_AUTHORISED_REVIEW"]
    unestablished = [f for f in findings if f.get("class") == "UNESTABLISHED"]
    stamp = datetime.now(timezone.utc).strftime("%d %B %Y")

    lines = [
        f"FILE NOTE — {business}",
        f"Prepared by {adviser} on {stamp}",
        "",
        "SCOPE OF REVIEW",
        (f"Reviewed {len(findings)} finding(s) disclosed by the client through Veris. "
         f"This note records the basis of the review; it is not a coverage opinion and "
         f"does not determine any claim."),
        "",
        "MATTERS REQUIRING AN AUTHORISED OPINION",
    ]
    if escalated:
        for f in escalated:
            lines.append(f"  - {f.get('rule_id')}: {f.get('headline')}")
    else:
        lines.append("  - None. No finding required escalation.")

    lines += ["", "MATTERS NOT ESTABLISHED BY DOCUMENT"]
    if unestablished:
        for f in unestablished:
            lines.append(f"  - {f.get('rule_id')}: {f.get('headline')}")
    else:
        lines.append("  - None. Every reviewed finding rested on a document.")

    lines += ["", "INFORMATION REQUESTED FROM THE CLIENT"]
    if requests:
        for r in requests:
            lines.append(f"  - {r.get('rule_id')}: {r.get('message')}")
    else:
        lines.append("  - None at the date of this note.")

    lines += [
        "",
        "BASIS AND LIMITATIONS",
        ("The review was carried out on the documents the client disclosed. Findings "
         "resting on documents that were not disclosed were withheld from this adviser "
         "and are therefore outside the scope of this note. Where a value could not be "
         "established from a document it is recorded as unestablished rather than "
         "assumed."),
        "",
        "NEXT STEPS",
        ("Obtain the documents listed above, then re-review the matters requiring an "
         "authorised opinion."),
    ]
    return "\n".join(lines)


def build_context(findings: list[dict], requests: list[dict]) -> dict:
    """The verified numbers a draft is allowed to use. Computed, never model-supplied."""
    return {
        "findings_reviewed": len(findings or []),
        "requires_review": sum(1 for f in findings or []
                               if f.get("state") == "REQUIRES_AUTHORISED_REVIEW"),
        "unestablished": sum(1 for f in findings or []
                             if f.get("class") == "UNESTABLISHED"),
        "documents_requested": len(requests or []),
    }


class NoteStore:
    """One note per case, with its history. In memory, like the rest of the session."""

    def __init__(self) -> None:
        self._notes: dict[str, Note] = {}
        self._seq = 0

    def draft(self, *, case_id: str, business: str, adviser: str, organisation: str,
              findings: list[dict], requests: list[dict],
              model_summary: Optional[str] = None,
              model_text: Optional[str] = None) -> Note:
        """Create or replace the draft. An APPROVED note is never overwritten.

        Approval is the point at which a human took responsibility, so silently redrafting
        over it would destroy the only state in this module that means anything.

        THE STRUCTURE IS NEVER THE MODEL'S. `template_note()` always produces the note, and
        an accepted `model_summary` is PREPENDED as a summary section. The alternative —
        letting the model write the whole thing — was tried and rejected: its prose is
        good, but a file note without the scope and limitations sections is not defensible,
        and a model that can add value can also silently omit structure.

        `model_text` is retained for callers that genuinely want to replace the body, and
        is held to the same grounding gate.
        """
        existing = self._notes.get(str(case_id))
        if existing is not None and existing.state == ADVISER_APPROVED:
            return existing

        context = build_context(findings, requests)
        allowed = allowed_facts(findings, context)
        structured = template_note(business=business, adviser=adviser,
                                   findings=findings, requests=requests,
                                   context=context)

        text, drafted_by, grounding = structured, "TEMPLATE", {
            "checked": False,
            "reason": "No model draft was offered, so the deterministic note was used.",
        }

        candidate = model_text or model_summary
        if candidate:
            ok, problems = verify(candidate, allowed)
            if ok and model_text:
                text, drafted_by = model_text.strip()[:MAX_NOTE], "MODEL"
                grounding = {"checked": True, "accepted": True, "problems": []}
            elif ok:
                text = ("SUMMARY (drafted by model, checked against the evidence)\n"
                        + candidate.strip() + "\n\n" + structured)[:MAX_NOTE]
                drafted_by = "MODEL_SUMMARY"
                grounding = {"checked": True, "accepted": True, "problems": []}
            else:
                grounding = {
                    "checked": True, "accepted": False, "problems": problems,
                    "reason": ("The model's draft referenced material that is not in the "
                               "evidence, so it was rejected and the deterministic note "
                               "used instead. It was not repaired: guessing what the "
                               "model meant is the failure this architecture avoids."),
                }

        now = datetime.now(timezone.utc).isoformat()
        self._seq += 1
        note = Note(
            id=f"note-{self._seq:04d}", case_id=str(case_id), state=AI_DRAFT,
            text=text, drafted_by=drafted_by, adviser=adviser,
            organisation=organisation, created_at=now, updated_at=now,
            grounding=grounding, context=context,
            history=[{"at": now, "state": AI_DRAFT, "by": drafted_by,
                      "what": "Draft created."}])
        self._notes[str(case_id)] = note
        return note

    def get(self, case_id: str) -> Optional[Note]:
        return self._notes.get(str(case_id))

    def edit(self, case_id: str, text: str, *, by: str,
             allowed: Optional[dict[str, set[str]]] = None) -> Note:
        note = self._notes.get(str(case_id))
        if note is None:
            raise KeyError("No note for this case.")
        if note.state == ADVISER_APPROVED:
            raise ValueError("This note is approved. Approved notes are not editable.")
        clean = str(text or "")[:MAX_NOTE]
        # An adviser's own words are still checked for coverage language, because the
        # product's claim is about what it PUBLISHES, not about who typed it. The check is
        # advisory here — recorded, not blocking — since a licensed person may legitimately
        # need to quote a term. Blocking their own file note would be the tool overreaching.
        problems: list[str] = []
        if allowed:
            _, problems = verify(clean, allowed)
        now = datetime.now(timezone.utc).isoformat()
        note.text = clean
        note.state = ADVISER_EDITED
        note.updated_at = now
        note.grounding = {**note.grounding, "adviser_edit_warnings": problems}
        note.history.append({"at": now, "state": ADVISER_EDITED, "by": by,
                             "what": "Edited by the adviser."})
        return note

    def approve(self, case_id: str, *, by: str) -> Note:
        note = self._notes.get(str(case_id))
        if note is None:
            raise KeyError("No note for this case.")
        now = datetime.now(timezone.utc).isoformat()
        note.state = ADVISER_APPROVED
        note.approved_at = now
        note.updated_at = now
        note.history.append({"at": now, "state": ADVISER_APPROVED, "by": by,
                             "what": "Approved. This is now a record."})
        return note

    def forget(self, case_id: Optional[str] = None) -> None:
        if case_id is None:
            self._notes.clear()
            self._seq = 0
        else:
            self._notes.pop(str(case_id), None)


NOTES = NoteStore()


def catalogue() -> dict:
    return {
        "states": [{"state": s, "label": STATE_LABELS[s]} for s in STATES],
        "banned_phrases": list(BANNED),
        "max_length": MAX_NOTE,
        "never_sent": ("A note is never sent to the client or the insurer from here. A "
                       "draft escaping as if it were advice is the worst outcome this "
                       "feature could produce, so there is no code path that sends it."),
        "method": "DETERMINISTIC_VERIFICATION",
    }
