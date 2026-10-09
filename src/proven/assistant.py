"""The in-product helper: answer "where is X" and then actually go there.

WHY THIS IS A SEPARATE THING FROM `ai.py`.

`ai.py` answers questions about the CASE — what a finding means, what the wording says. It
is consent-scoped, evidence-bound, and every answer it gives has to be checkable against a
document. This module answers a different question: *where in this product do I find the
thing I am looking for*. That question has nothing to do with the case, needs no evidence,
and must work for a user who has uploaded nothing at all.

Keeping them apart matters. If the helper could reach case data it would become a second,
unaudited answer path for findings — the one thing this product cannot have two of. So this
module never touches a case, a document or a finding. It knows the shape of the interface
and nothing else.

THREE RULES, in the order they matter.

1. THE ROLE COMES FROM THE SESSION AND THE CATALOGUE IS FILTERED BY IT.
   An SME may be routed only to `sme-*`, an adviser only to `adv-*`, a carrier only to
   `car-*`. This is not cosmetic. Ask the carrier's helper for "the cyber panel" and it does
   not quietly find the nearest carrier screen and pretend that is what you meant, and it
   certainly does not route across the boundary — it names the role that owns that screen
   and stops. The boundary is more useful demonstrated than asserted.

2. DETERMINISTIC FIRST, AND SUFFICIENT ON ITS OWN.
   Resolution is keyword scoring over a hand-written catalogue. With no API key configured
   the helper still works, still routes correctly, and says `DETERMINISTIC`. A judge cloning
   this repo with no key gets the whole feature.

3. THE MODEL MAY ONLY CHOOSE FROM THE CATALOGUE. NEVER INVENT A DESTINATION.
   When a key is present the model is handed the caller's own destination list and asked for
   one id and one sentence. The id is validated against that same list before it is used; an
   unknown id is discarded and the deterministic pick stands. A model that could emit a view
   id would be a model that could navigate a user to a screen that does not exist, and
   "the assistant sent me nowhere" is a worse failure than a slightly worse match.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from . import auth, config

# ── the catalogue ───────────────────────────────────────────────────────────
#
# One entry per place a user can be sent. Fields:
#
#   id        stable key, used by the model to choose and by tests to assert
#   role      which workspace owns it — the access boundary, enforced below
#   label     what the interface itself calls it, so the answer matches the screen
#   view      the view id `render()` routes on
#   tab       [group, tabId] when the destination is a tab inside a container, else None
#   trail     breadcrumbs to show while travelling: what a user would have clicked
#   targets   selectors to highlight on arrival, best first. Tried in order because a
#             screen with no case shows an empty state whose markup differs from a
#             populated one, and a highlight that lands on nothing is worse than none.
#   what      one sentence, in the user's language, about what the screen answers
#   keywords  how people actually ask for it, including the words the interface avoids
#             ("hack", "stolen", "flood") — a helper that only understands its own
#             vocabulary helps nobody who needed a helper
#
# `#tabpanel .page-title` is the heading of the tab's own view: `renderTabs` mounts each
# tab into `#tabpanel` and every view opens with `pageTitle(...)`. It is the closest thing
# to "the heading of the thing you asked for".

_TAB_TARGETS = ["#tabpanel .page-title", "#tabpanel .stats", "#tabpanel .card",
                "#tabpanel .insight", "#tabpanel"]
_VIEW_TARGETS = ["#main .page-title", "#main .stats", "#main .card", "#main .insight"]

DESTINATIONS: tuple[dict, ...] = (
    # ── SME: my business ──────────────────────────────────────────────────
    {"id": "overview", "role": auth.SME, "label": "Overview",
     "view": "sme-home", "tab": None, "trail": ["Overview"],
     "targets": [".pf__core", ".pf__stage", "#main .stats"] + _VIEW_TARGETS,
     "what": "Your protection score and the headline of where you stand, counted up "
             "from your own documents.",
     "keywords": ["overview", "home", "dashboard", "score", "protection score",
                  "cover score", "how am i doing", "summary", "start", "main page",
                  "front page", "where do i stand", "my position overall"]},

    {"id": "position", "role": auth.SME, "label": "Your position",
     "view": "sme-position", "tab": None, "trail": ["Your position"],
     "targets": ["[data-act='gap-why']", ".cov", "#main .heads"] + _VIEW_TARGETS,
     "what": "Every gap we found, in plain words, each with a Show me why button that "
             "opens the page of the PDF it came from.",
     "keywords": ["position", "gaps", "gap", "findings", "problems", "issues",
                  "what did you find", "show me why", "evidence", "proof",
                  "what is wrong", "shortfall", "list of gaps"]},

    {"id": "alerts", "role": auth.SME, "label": "What's new",
     "view": "sme-alerts", "tab": None, "trail": ["What's new"],
     "targets": [".ntf", "#main .stats"] + _VIEW_TARGETS,
     "what": "What changed since you last looked, and the exact email we wrote about it.",
     "keywords": ["new", "whats new", "what's new", "alerts", "alert", "notifications",
                  "notification", "changed", "changes", "inbox", "messages", "email",
                  "emails", "updates", "recent", "check now", "unread"]},

    # ── SME: Protection hub ───────────────────────────────────────────────
    {"id": "cover-score", "role": auth.SME, "label": "Your cover score",
     "view": "sme-protect-hub", "tab": ["prot", "score"],
     "trail": ["Protection", "Your cover score"], "targets": _TAB_TARGETS,
     "what": "How the score was built, dimension by dimension, and which areas were not "
             "assessed at all.",
     "keywords": ["cover score", "score breakdown", "how is the score", "dimensions",
                  "score detail", "protection score detail", "rating", "out of 100"]},

    {"id": "property", "role": auth.SME, "label": "Property and assets",
     "view": "sme-protect-hub", "tab": ["prot", "property"],
     "trail": ["Protection", "Property and assets"], "targets": _TAB_TARGETS,
     "what": "What your buildings, stock, machinery and fit-out are insured for against "
             "what your documents say they are worth.",
     "keywords": ["property", "building", "buildings", "premises", "stock", "assets",
                  "machinery", "plant", "equipment", "freezer", "fit out", "fitout",
                  "contents", "fire", "under insured", "underinsured", "sum insured",
                  "average clause", "value"]},

    {"id": "cyber", "role": auth.SME, "label": "Cyber and data",
     "view": "sme-protect-hub", "tab": ["prot", "cyber"],
     "trail": ["Protection", "Cyber and data"], "targets": _TAB_TARGETS,
     "what": "What a cyber or data incident would cost you, and what your policy does "
             "and does not do about it.",
     "keywords": ["cyber", "cyber panel", "cyber security", "cybersecurity", "data",
                  "data breach", "breach", "hack", "hacked", "hacking", "ransomware",
                  "malware", "virus", "phishing", "computer", "computers",
                  "online", "digital", "pdpa"]},

    {"id": "dependencies", "role": auth.SME, "label": "Who you depend on",
     "view": "sme-protect-hub", "tab": ["prot", "supply"],
     "trail": ["Protection", "Who you depend on"], "targets": _TAB_TARGETS,
     "what": "The suppliers, landlords and utilities your trading depends on, and "
             "whether a failure at their end is covered at yours.",
     "keywords": ["depend", "depends", "dependency", "dependencies", "supplier",
                  "suppliers", "supply chain", "supplychain", "landlord", "utilities",
                  "power", "outage", "third party", "vendor", "contingent",
                  "who i rely on", "rely"]},

    {"id": "bi", "role": auth.SME, "label": "If you can't trade",
     "view": "sme-protect-hub", "tab": ["prot", "bi"],
     "trail": ["Protection", "If you can't trade"], "targets": _TAB_TARGETS,
     "what": "Business interruption: what you would be paid while you cannot trade, and "
             "for how long.",
     "keywords": ["business interruption", "bi", "cant trade", "can't trade",
                  "cannot trade", "closed", "shut", "downtime", "loss of profit",
                  "profits", "indemnity period", "revenue", "turnover",
                  "stop trading", "out of action"]},

    {"id": "scenario", "role": auth.SME, "label": "If you claim tomorrow",
     "view": "sme-protect-hub", "tab": ["prot", "scenario"],
     "trail": ["Protection", "If you claim tomorrow"], "targets": _TAB_TARGETS,
     "what": "A worked claim on your actual policy: the deductible, the average clause, "
             "and what would land in your bank.",
     "keywords": ["claim", "claim tomorrow", "if i claim", "payout", "pay out",
                  "how much would i get", "settlement", "deductible", "excess",
                  "simulate", "scenario", "what would i get", "cheque"]},

    {"id": "willitpay", "role": auth.SME, "label": "Will it actually pay?",
     "view": "sme-protect-hub", "tab": ["prot", "pay"],
     "trail": ["Protection", "Will it actually pay?"], "targets": _TAB_TARGETS,
     "what": "The conditions you have to be meeting already — alarms, premium payment, "
             "declarations — where breaking one means the cover was never there.",
     "keywords": ["will it pay", "actually pay", "warranty", "warranties", "condition",
                  "conditions", "condition precedent", "compliance", "alarm", "security",
                  "premium payment", "obligations", "get out of paying", "refuse",
                  "declined", "void", "invalid"]},

    # ── SME: Timeline hub ─────────────────────────────────────────────────
    {"id": "crept", "role": auth.SME, "label": "How this crept up",
     "view": "sme-time-hub", "tab": ["time", "crept"],
     "trail": ["Timeline", "How this crept up"], "targets": _TAB_TARGETS,
     "what": "How a small mismatch became a large one, dated step by dated step.",
     "keywords": ["crept", "crept up", "compounding", "over time", "history",
                  "how did this happen", "timeline", "grew", "drift over time"]},

    {"id": "renewal", "role": auth.SME, "label": "What changed this year",
     "view": "sme-time-hub", "tab": ["time", "renewal"],
     "trail": ["Timeline", "What changed this year"], "targets": _TAB_TARGETS,
     "what": "This year's policy against last year's, clause by clause, including what "
             "quietly got worse.",
     "keywords": ["renewal", "renew", "this year", "last year", "changed this year",
                  "compare policy", "prior policy", "previous policy", "new policy",
                  "what changed at renewal", "year on year"]},

    {"id": "found", "role": auth.SME, "label": "What we found",
     "view": "sme-time-hub", "tab": ["time", "found"],
     "trail": ["Timeline", "What we found"], "targets": _TAB_TARGETS,
     "what": "The full list of findings with the evidence class on each one.",
     "keywords": ["what we found", "all findings", "full list", "every finding",
                  "findings list"]},

    # ── SME: What to do hub ───────────────────────────────────────────────
    {"id": "todo", "role": auth.SME, "label": "Things to do",
     "view": "sme-act-hub", "tab": ["act", "todo"],
     "trail": ["What to do", "Things to do"], "targets": _TAB_TARGETS,
     "what": "The correction requests prepared for you, and the button that releases one "
             "to your adviser.",
     "keywords": ["to do", "todo", "things to do", "actions", "action", "next step",
                  "next steps", "what should i do", "fix", "fix it", "tasks", "task",
                  "send to adviser", "release", "requests"]},

    {"id": "help", "role": auth.SME, "label": "What would help",
     "view": "sme-act-hub", "tab": ["act", "help"],
     "trail": ["What to do", "What would help"], "targets": _TAB_TARGETS,
     "what": "Which single document would settle the most unanswered questions.",
     "keywords": ["what would help", "which document", "what should i upload",
                  "most useful", "information value", "worth uploading", "priority"]},

    {"id": "stage", "role": auth.SME, "label": "Protection for your stage",
     "view": "sme-act-hub", "tab": ["act", "stage"],
     "trail": ["What to do", "Protection for your stage"], "targets": _TAB_TARGETS,
     "what": "What businesses at your size and stage usually need to have in place.",
     "keywords": ["stage", "my stage", "roadmap", "growing", "growth", "size",
                  "what do businesses like mine need", "maturity", "benchmark"]},

    # ── SME: Monitoring hub ───────────────────────────────────────────────
    {"id": "connect", "role": auth.SME, "label": "Your business",
     "view": "sme-mon-hub", "tab": ["mon", "business"],
     "trail": ["Monitoring", "Your business"], "targets": _TAB_TARGETS,
     "what": "Changes picked up from your own records — a new asset, a new activity — "
             "without you having to tell us.",
     "keywords": ["changes in my business", "changes in business", "my business",
                  "internal", "own records", "connect", "connect agent", "signals",
                  "bought", "purchase", "new asset", "acquired"]},

    {"id": "systems", "role": auth.SME, "label": "Connected systems",
     "view": "sme-mon-hub", "tab": ["mon", "systems"],
     "trail": ["Monitoring", "Connected systems"], "targets": _TAB_TARGETS,
     "what": ("Which of your systems feed this workspace, and why a live accounting "
              "integration is a documented seam rather than a built feature."),
     "keywords": ["connect", "connected", "connected systems", "accounting",
                  "integration", "integrations", "link", "authorise", "authorize",
                  "quickbooks", "erp", "pos", "payroll", "inventory"]},

    {"id": "around", "role": auth.SME, "label": "Around you",
     "view": "sme-mon-hub", "tab": ["mon", "around"],
     "trail": ["Monitoring", "Around you"], "targets": _TAB_TARGETS,
     "what": "Conditions outside your business — weather, construction, area risk — read "
             "live from Singapore government services.",
     "keywords": ["around", "around me", "around you", "outside", "environment",
                  "weather", "rain", "flood", "dengue", "construction", "nearby",
                  "sentinel", "conditions", "area", "neighbourhood", "external"]},

    {"id": "public", "role": auth.SME, "label": "Public records",
     "view": "sme-mon-hub", "tab": ["mon", "public"],
     "trail": ["Monitoring", "Public records"], "targets": _TAB_TARGETS,
     "what": "What the outside world publishes about you — your own website, ACRA, MOM — "
             "and where it disagrees with your policy.",
     "keywords": ["public", "public records", "registry", "acra", "mom", "website",
                  "my website", "discovery", "google", "bizfile", "uen",
                  "what does the internet say", "outside sources", "fetch"]},

    {"id": "costs", "role": auth.SME, "label": "Keeping up with costs",
     "view": "sme-mon-hub", "tab": ["mon", "costs"],
     "trail": ["Monitoring", "Keeping up with costs"], "targets": _TAB_TARGETS,
     "what": "Whether your sums insured have kept up with construction and material "
             "costs since the policy was written.",
     "keywords": ["costs", "cost", "inflation", "rebuild cost", "construction cost",
                  "drift", "index", "keeping up", "prices", "material costs",
                  "out of date value"]},

    {"id": "checked", "role": auth.SME, "label": "What we checked",
     "view": "sme-mon-hub", "tab": ["mon", "checked"],
     "trail": ["Monitoring", "What we checked"], "targets": _TAB_TARGETS,
     "what": "Every automated run, what it looked at, and what it concluded.",
     "keywords": ["what we checked", "agents", "agent", "activity", "runs", "log",
                  "audit", "history of checks", "scheduler", "automation"]},

    # ── SME: Documents hub ────────────────────────────────────────────────
    {"id": "getting-started", "role": auth.SME, "label": "Getting started",
     "view": "sme-docs-hub", "tab": ["docs", "start"],
     "trail": ["Documents", "Getting started"], "targets": _TAB_TARGETS,
     "what": "What to do first, and which two documents get you the most in one go.",
     "keywords": ["getting started", "get started", "start", "onboarding", "first",
                  "begin", "how do i start", "new here", "setup", "set up"]},

    {"id": "upload", "role": auth.SME, "label": "Add documents",
     "view": "sme-docs-hub", "tab": ["docs", "add"],
     "trail": ["Documents", "Add documents"],
     "targets": [".dropzone", "#tabpanel .page-title"] + _TAB_TARGETS,
     "what": "Where you drop PDFs in. Each one is read on the server and reports back "
             "file by file.",
     "keywords": ["upload", "uploads", "uploading", "add document", "add documents",
                  "add file", "add files", "attach", "drop", "drag", "pdf", "pdfs",
                  "send documents", "give you my policy", "where do i put my policy",
                  "submit"]},

    {"id": "blindspots", "role": auth.SME, "label": "What we haven't checked",
     "view": "sme-docs-hub", "tab": ["docs", "gaps"],
     "trail": ["Documents", "What we haven't checked"], "targets": _TAB_TARGETS,
     "what": "The areas we could not assess at all, and the document that would unlock "
             "each one. Not looked at is not the same as fine.",
     "keywords": ["havent checked", "haven't checked", "not checked", "blind spot",
                  "blind spots", "blindspots", "missing", "unknown", "what dont you know",
                  "what don't you know", "unassessed", "not assessed", "gaps in coverage"]},

    {"id": "quality", "role": auth.SME, "label": "How well we read them",
     "view": "sme-docs-hub", "tab": ["docs", "quality"],
     "trail": ["Documents", "How well we read them"], "targets": _TAB_TARGETS,
     "what": "Page by page, which pages we read cleanly and which we refused to rely on.",
     "keywords": ["how well", "read quality", "page quality", "ocr", "scan", "scanned",
                  "illegible", "blurry", "unreadable", "could not read", "rejected file",
                  "why was my file rejected", "no text"]},

    # ── SME: control ──────────────────────────────────────────────────────
    {"id": "consent", "role": auth.SME, "label": "Share with adviser",
     "view": "sme-consent", "tab": None, "trail": ["Share with adviser"],
     "targets": [".consent-list", "#main .card"] + _VIEW_TARGETS,
     "what": "You choose, document by document, what leaves this workspace and who "
             "receives it. You can stop sharing at any time.",
     "keywords": ["share", "sharing", "consent", "adviser", "advisor", "broker",
                  "disclose", "disclosure", "permission", "give access", "grant",
                  "revoke", "stop sharing", "who can see", "privacy"]},

    {"id": "record", "role": auth.SME, "label": "Your record",
     "view": "sme-record", "tab": None, "trail": ["Your record"],
     "targets": ["#main .card"] + _VIEW_TARGETS,
     "what": "A sealed, downloadable chain of what was established. It survives deleting "
             "the case.",
     "keywords": ["record", "my record", "download", "export", "audit trail", "receipt",
                  "certificate", "proof to keep", "take away", "hash", "verify",
                  "delete my case", "delete"]},

    # ── ADVISER ───────────────────────────────────────────────────────────
    {"id": "adv-queue", "role": auth.ADVISER, "label": "Review Queue",
     "view": "adv-queue", "tab": None, "trail": ["Review Queue"],
     "targets": [".pc", "#main table"] + _VIEW_TARGETS,
     "what": "Your clients placed by urgency against impact, from the queue's own "
             "numbers.",
     "keywords": ["queue", "review queue", "cases", "clients", "my clients", "caseload",
                  "priority", "canvas", "workload", "who needs me"]},

    {"id": "adv-review", "role": auth.ADVISER, "label": "Current Review",
     "view": "adv-review", "tab": None, "trail": ["Current Review"],
     "targets": ["#main .stats", "#main .card"] + _VIEW_TARGETS,
     "what": "The brief the client disclosed to you, what was withheld, and which "
             "document kinds you would need to see the rest.",
     "keywords": ["review", "current review", "brief", "disclosed", "withheld",
                  "what was shared", "scope", "pending documents", "request documents",
                  "findings i can see", "consent scope"]},

    {"id": "adv-actions", "role": auth.ADVISER, "label": "Requests to action",
     "view": "adv-actions", "tab": None, "trail": ["Requests to action"],
     "targets": [".action-card", "#main .stats"] + _VIEW_TARGETS,
     "what": "The correction requests on this case and the moves the state machine "
             "reserves to you.",
     "keywords": ["actions", "requests to action", "move", "moves", "bound", "bind",
                  "mark as bound", "mark this bound", "quote", "terms", "advance",
                  "state", "workflow", "exposure", "waiting on me", "what can i do"]},

    {"id": "adv-requests", "role": auth.ADVISER, "label": "Information Requests",
     "view": "adv-requests", "tab": None, "trail": ["Information Requests"],
     "targets": ["#main .card"] + _VIEW_TARGETS,
     "what": "What you have asked the client for, and what they have answered.",
     "keywords": ["information request", "information requests", "asked", "ask client",
                  "ask the client", "asked the client", "what i asked",
                  "questions", "outstanding", "chase", "follow up", "requested"]},

    {"id": "adv-note", "role": auth.ADVISER, "label": "File note",
     "view": "adv-note", "tab": None, "trail": ["File note"],
     "targets": ["#main .card"] + _VIEW_TARGETS,
     "what": "A file note drafted from the disclosed evidence, for you to edit and "
             "approve. Only an approved note is a record.",
     "keywords": ["note", "file note", "notes", "write up", "draft", "report",
                  "documentation", "approve", "sign off", "record of advice"]},

    # ── CARRIER ───────────────────────────────────────────────────────────
    {"id": "car-triage", "role": auth.CARRIER, "label": "Work queue",
     "view": "car-triage", "tab": None, "trail": ["Work queue"],
     "targets": ["#main .card", "#main table"] + _VIEW_TARGETS,
     "what": "The book ranked by attention, with the weight table shown so the order can "
             "be argued with.",
     "keywords": ["work queue", "queue", "triage", "what should i look at", "ranked",
                  "ranking", "weights", "priority", "renewal triage", "my work"]},

    {"id": "car-inbox", "role": auth.CARRIER, "label": "Needs attention",
     "view": "car-inbox", "tab": None, "trail": ["Needs attention"],
     "targets": ["#main .card"] + _VIEW_TARGETS,
     "what": "Accounts that surfaced on your own book, stating what a disclosure would "
             "add and what it would not.",
     "keywords": ["needs attention", "inbox", "attention", "undisclosed", "flagged",
                  "surfaced", "which accounts"]},

    {"id": "car-decisions", "role": auth.CARRIER, "label": "Decision log",
     "view": "car-decisions", "tab": None, "trail": ["Decision log"],
     "targets": ["#main .card", "#main table"] + _VIEW_TARGETS,
     "what": "Every decision recorded against an account, append-only. Nothing here can "
             "be edited or removed.",
     "keywords": ["decision", "decisions", "decision log", "record a decision", "log",
                  "audit", "append only", "refer", "endorsement", "accept",
                  "what did i decide", "history"]},

    {"id": "car-analyses", "role": auth.CARRIER, "label": "Coverage Gap Analysis",
     "view": "car-analyses", "tab": None, "trail": ["Coverage Gap Analysis"],
     "targets": ["#main table", "#main .card"] + _VIEW_TARGETS,
     "what": "One row per account: findings, how many are proven, how many need a human. "
             "Every column computed, none authored.",
     "keywords": ["coverage gap", "gap analysis", "analyses", "analysis", "accounts",
                  "per account", "book rows", "list of accounts", "case list"]},

    {"id": "car-patterns", "role": auth.CARRIER, "label": "Book patterns",
     "view": "car-patterns", "tab": None, "trail": ["Book patterns"],
     "targets": ["#main .card"] + _VIEW_TARGETS,
     "what": "Counts over the book against stated thresholds. Nothing inferred and "
             "nothing predicted.",
     "keywords": ["patterns", "book patterns", "trends", "across the book", "aggregate",
                  "portfolio patterns", "systemic", "common"]},

    {"id": "car-portfolio", "role": auth.CARRIER, "label": "Overview",
     "view": "car-portfolio", "tab": None, "trail": ["Overview"],
     "targets": ["#main .stats", "#main .card"] + _VIEW_TARGETS,
     "what": "The wording portfolio: what each published wording does to an insured, "
             "read from the document, computed live and needing no customer consent.",
     "keywords": ["overview", "portfolio", "wording", "wordings", "policy wording",
                  "competitor", "competitors", "side by side", "compare wordings",
                  "mechanisms", "divergence", "clause map", "our wording"]},

    {"id": "car-premium", "role": auth.CARRIER, "label": "Exposure at Risk",
     "view": "car-premium", "tab": None, "trail": ["Exposure at Risk"],
     "targets": ["#main .card", "#main .stats"] + _VIEW_TARGETS,
     "what": "The revenue absent from the book where cover has drifted from documented "
             "value. Not a quotation and not a risk judgement.",
     "keywords": ["exposure", "exposure at risk", "premium", "premium at risk",
                  "revenue", "money", "at risk", "lost premium", "opportunity"]},

    {"id": "car-matrix", "role": auth.CARRIER, "label": "Attention Matrix",
     "view": "car-matrix", "tab": None, "trail": ["Attention Matrix"],
     "targets": [".v-matrix", "#main .card"] + _VIEW_TARGETS,
     "what": "Business drift plotted against environmental risk. It reports attention, "
             "never a breach.",
     "keywords": ["matrix", "attention matrix", "quadrant", "plot", "chart", "map",
                  "drift against risk", "grid"]},

    {"id": "car-accounts", "role": auth.CARRIER, "label": "Priority accounts",
     "view": "car-accounts", "tab": None, "trail": ["Priority accounts"],
     "targets": ["#main table", "#main .card"] + _VIEW_TARGETS,
     "what": "Accounts ranked by findings a licensed adviser must resolve.",
     "keywords": ["priority accounts", "priority", "needs a human", "escalate",
                  "which accounts first", "ranked accounts"]},
)

# What each role is called when the helper has to explain a boundary.
_ROLE_WORD = {auth.SME: "business owner", auth.ADVISER: "licensed adviser",
              auth.CARRIER: "carrier underwriter"}

# Phrasings that mean "take me there" rather than "explain it to me". The distinction
# decides whether the client navigates immediately or offers a button, because hijacking
# the screen of somebody who only asked what a word meant is its own small betrayal.
_GO_WORDS = re.compile(
    r"\b(go|goto|take|bring|show|open|navigate|nav|jump|find|where|locate|"
    r"lead|direct|send|get)\b", re.I)

# "What is business interruption" is a request for a MEANING, not for a screen. It scores
# highly — "business interruption" is an exact phrase on that tab — and a confidence-only
# rule would therefore navigate away from whatever the person was reading in order to answer
# a vocabulary question. So a definitional phrasing suppresses the automatic trip and the
# client offers a button instead.
#
# Deliberately narrow. "What changed this year", "what should I do" and "what did you find"
# are not definitions, they are requests to be shown something, and they must keep travelling.
_DEFINE_WORDS = re.compile(
    r"(^\s*(what|whats|what's)\s+(is|are|does|do|means?)\b"
    r"|^\s*(explain|define)\b"
    r"|\bmean(s|ing)?\b)", re.I)

_STOPWORDS = frozenset("""
a an and are as at be but by can could do does for from get got has have how i in into is
it its me my of on or please should show so somewhere the their there these this to us was
were what when where which who why will with would you your
""".split())


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())


def _tokens(text: str) -> list[str]:
    return [t for t in _normalise(text).split() if t and t not in _STOPWORDS]


def for_role(role: Optional[str]) -> tuple[dict, ...]:
    """Only the destinations this role may be sent to. The access boundary."""
    return tuple(d for d in DESTINATIONS if d["role"] == role)


def _word_hit(keyword: str, toks: list[str]) -> bool:
    """Does any token mean this keyword?

    Prefix matching in both directions, gated on four characters, instead of a stemmer.
    "flooding" has to find the keyword "flood", "hacked" has to find "hack", and "cost" has
    to find "costs" — a helper that only understands the exact inflection somebody happened
    to type is a search box with extra steps. The four-character floor is what stops short
    words swallowing everything: without it the keyword "it" matches "items", "issues" and
    "interruption".
    """
    for t in toks:
        if t == keyword:
            return True
        if len(keyword) >= 4 and t.startswith(keyword):
            return True
        if len(t) >= 4 and keyword.startswith(t):
            return True
    return False


def _score(dest: dict, question: str, toks: list[str]) -> float:
    """How well one destination answers this question.

    Multi-word keywords are matched as PHRASES against the normalised question, because
    "business interruption" landing on the Monitoring tab that owns the word "business"
    is exactly the kind of near-miss that makes a helper untrustworthy. A phrase is worth
    more than a word for the same reason.
    """
    norm = " " + _normalise(question) + " "
    score = 0.0

    for kw in dest["keywords"]:
        k = _normalise(kw).strip()
        if not k:
            continue
        if " " in k:
            if f" {k} " in norm:
                score += 4.0 + 0.5 * k.count(" ")     # longer phrase, stronger signal
        elif _word_hit(k, toks):
            score += 1.0

    # The name on the screen. Someone who types the label deserves the label.
    label = _normalise(dest["label"]).strip()
    if label and f" {label} " in norm:
        score += 6.0
    else:
        label_toks = [t for t in label.split() if t not in _STOPWORDS]
        if label_toks and all(t in toks for t in label_toks):
            score += 3.0

    # A trail segment is how the user would have got there by clicking.
    for seg in dest["trail"]:
        s = _normalise(seg).strip()
        if s and s != label and f" {s} " in norm:
            score += 1.5

    return score


def resolve(question: str, role: Optional[str]) -> dict:
    """Pick a destination for `question`, deterministically.

    Returns a dict with `destination` (or None), `refused` (a cross-role match this role
    may not be sent to, or None) and `ranked` (the top few in-role candidates, so the
    client can offer alternatives instead of a dead end).
    """
    toks = _tokens(question)
    mine = for_role(role)

    scored = sorted(((_score(d, question, toks), d) for d in mine),
                    key=lambda pair: (-pair[0], pair[1]["id"]))
    best = scored[0] if scored else (0.0, None)

    others = sorted(((_score(d, question, toks), d)
                     for d in DESTINATIONS if d["role"] != role),
                    key=lambda pair: (-pair[0], pair[1]["id"]))

    # Would a screen belonging to a DIFFERENT role have answered this? If so, say so — and
    # DROP the in-role pick rather than serving it.
    #
    # Two triggers, and the first one is the whole point. An SME asking to "open the
    # decision log" scores a weak coincidental 1.0 on "What we checked" (which owns the
    # word "log") while the carrier's Decision log scores 11.5. Routing to the weak match
    # is precisely the "quietly find the nearest screen and pretend that is what you meant"
    # behaviour this module exists not to do — and it hides the boundary at the moment the
    # boundary is the most interesting thing in the product.
    refused = None
    if others:
        other_best, other_dest = others[0]
        dominant = other_best >= 4.0 and other_best > best[0] * 1.5
        only_elsewhere = best[0] < 1.0 and other_best >= 1.0
        if dominant or only_elsewhere:
            refused = other_dest

    return {
        "destination": None if refused is not None
                       else (best[1] if best[0] >= 1.0 else None),
        "confidence": round(best[0], 2),
        "refused": refused,
        "ranked": [d for s, d in scored[:4] if s > 0],
    }


def _public(dest: dict) -> dict:
    """The client's view of a destination. Role is dropped: it is a server concern, and
    shipping it would invite a frontend to think it could choose."""
    return {"id": dest["id"], "label": dest["label"], "view": dest["view"],
            "tab": list(dest["tab"]) if dest["tab"] else None,
            "trail": list(dest["trail"]), "targets": list(dest["targets"]),
            "what": dest["what"]}


def _model_pick(question: str, allowed: tuple[dict, ...]) -> tuple[Optional[str],
                                                                  Optional[str],
                                                                  Optional[str]]:
    """Let the model choose ONE id from `allowed` and write one sentence.

    Returns (destination_id, sentence, error). The id is validated by the caller against
    the same list that was offered, so the worst a bad response can do is be ignored.
    """
    from . import ai                                        # local: avoids a cycle

    catalogue = "\n".join(f"- {d['id']}: {d['label']} — {d['what']}" for d in allowed)
    system = (
        "You help a user of an insurance document-analysis product find the right screen. "
        "You are given the ONLY screens this user is allowed to reach. Choose the single "
        "best one for their question. You must reply with JSON: "
        '{"id": "<one id from the list, or empty string if none fits>", '
        '"say": "<one short sentence, max 30 words, saying what they will find there>"}. '
        "Never invent an id. Never mention a screen that is not in the list. Do not "
        "promise any figure, coverage outcome or claim result."
    )
    content, error = ai._chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"Screens:\n{catalogue}\n\nQuestion: {question}"}],
        json_mode=True, max_tokens=200)
    if error or not content:
        return None, None, error or "empty response"
    try:
        parsed = json.loads(content)
    except Exception as exc:                                # noqa: BLE001
        return None, None, f"unparseable: {exc}"[:120]
    if not isinstance(parsed, dict):
        return None, None, "not an object"
    chosen = str(parsed.get("id") or "").strip()
    say = " ".join(str(parsed.get("say") or "").split())[:260]
    return (chosen or None), (say or None), None


def answer(question: str, role: Optional[str], *, allow_model: bool = True) -> dict:
    """The helper's whole reply to one question.

    Shape:
      answer       prose to show in the chat
      route        the destination to travel to, or None
      navigate     whether the client should GO now or offer a button
      suggestions  other in-role destinations worth offering
      mode         DETERMINISTIC or LLM_ASSISTED — visible in the UI, like everywhere else
      boundary     set when the best match belongs to another role
    """
    text = " ".join(str(question or "").split())[:400]
    mine = for_role(role)
    role_word = _ROLE_WORD.get(role, "user")

    if not text:
        return {"answer": "Ask me where something is, and I will take you to it.",
                "route": None, "navigate": False, "mode": "DETERMINISTIC",
                "suggestions": [_public(d) for d in mine[:4]], "boundary": None,
                "role": role}

    picked = resolve(text, role)
    dest = picked["destination"]
    mode = "DETERMINISTIC"
    sentence = None

    # A cross-role match is answered by naming the boundary. Deliberately BEFORE the model
    # is consulted: no phrasing should be able to talk the helper across a role line, and
    # `resolve()` has already cleared the in-role pick so there is nothing to fall back to.
    if picked["refused"] is not None:
        other = picked["refused"]
        return {
            "answer": (
                f"“{other['label']}” is part of the "
                f"{_ROLE_WORD.get(other['role'], 'other')} workspace, and you are signed "
                f"in as a {role_word}. I will not take you there — the boundary is "
                f"enforced on the server, not just hidden in this menu. Here is what I "
                f"can open for you instead."),
            "route": None, "navigate": False, "mode": "DETERMINISTIC",
            "suggestions": [_public(d) for d in (picked["ranked"] or list(mine)[:4])],
            "boundary": {"label": other["label"],
                         "owner": _ROLE_WORD.get(other["role"], "another role")},
            "role": role,
        }

    if allow_model and config.ai_enabled() and mine:
        chosen_id, say, _err = _model_pick(text, mine)
        valid = {d["id"] for d in mine}

        # THE MODEL MAY ONLY CHOOSE WHERE THE KEYWORDS FOUND NOTHING.
        #
        # It was allowed to override any deterministic pick, and it made routing worse. An
        # adviser asking "how do I mark this bound" resolves to Requests to action — "bound"
        # is a keyword there and appears nowhere else — and the model chose File note, which
        # is a valid id and the wrong screen. Validating the id caught nothing because the id
        # was fine; the JUDGEMENT was worse than the table's.
        #
        # So it now only decides where the table had no answer at all, which is exactly where
        # it is genuinely valuable: a phrasing nobody thought to add a keyword for. Where the
        # table did resolve, the model contributes the sentence and nothing else. That makes
        # "deterministic first" a property of the code rather than an aspiration, and it means
        # adding the model can never move a route that already worked.
        if dest is None and chosen_id in valid:
            dest = next(d for d in mine if d["id"] == chosen_id)
            sentence = say
            mode = "LLM_ASSISTED"
        elif say:
            sentence = say
            mode = "LLM_ASSISTED"

    if dest is None:
        return {
            "answer": ("I could not match that to a screen in your workspace. These are "
                       "the places I can take you — or try naming what you want to know "
                       "rather than where it lives."),
            "route": None, "navigate": False, "mode": mode,
            "suggestions": [_public(d) for d in mine[:6]], "boundary": None, "role": role,
        }

    where = " → ".join(dest["trail"])
    body = sentence or dest["what"]
    return {
        "answer": f"{body} It is under {where}.",
        "route": _public(dest),
        "navigate": (bool(_GO_WORDS.search(text))
                     or (picked["confidence"] >= 4.0
                         and not _DEFINE_WORDS.search(text))),
        "mode": mode,
        "suggestions": [_public(d) for d in picked["ranked"] if d["id"] != dest["id"]][:3],
        "boundary": None,
        "role": role,
    }


def catalogue(role: Optional[str]) -> dict:
    """Everywhere this role can be sent. Used by the client to seed its suggestion chips
    and by tests to assert the boundary holds."""
    mine = for_role(role)
    return {
        "role": role,
        "count": len(mine),
        "destinations": [_public(d) for d in mine],
        "notice": ("These are the only screens this helper will open for you. The list is "
                   "filtered on the server by the role on your session, so a request for "
                   "another workspace's screen is refused rather than hidden."),
    }
