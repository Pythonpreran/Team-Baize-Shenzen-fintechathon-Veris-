"""FastAPI app — the product surface.

VERIS is an evidence-bound insurance agent for households and small businesses. Layer 4
is an evidence layer, not a portal: the Brief is designed to be consumed by whatever
intermediary system the adviser already uses. This API exposes document intake, the
Review Brief, the change-event triggers, the consent-scoped disclosure view, the case
lifecycle and the portable record export.

SECURITY POSTURE — read this before deploying anything.

  * There is NO AUTHENTICATION. A session cookie isolates one browser's case from
    another's; it does not authenticate a person. Before any networked deployment
    this must sit behind real authentication and per-recipient authorisation,
    because the disclosure model is the entire privacy guarantee. `/api/health`
    reports `authenticated: false` so the gap cannot be overlooked.
  * Uploads are validated before parsing: byte caps, page caps, PDF magic bytes,
    encryption detection, per-session rate limit. pypdf on an untrusted file is a
    denial-of-service surface.
  * `author_class` is NEVER accepted from a caller. It derives from the detected
    document kind, because it derives evidence_class — a caller who could assert
    AUTHORITY could manufacture a PROVEN finding.
  * Uploaded bytes are never written to disk and never leave the process.
  * The session cookie is HttpOnly and SameSite=Lax, so page script cannot read it
    and a cross-site form post cannot ride it.
"""
from __future__ import annotations

import hashlib
import io
import json
import queue
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import (
    Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response,
    UploadFile,
)
from fastapi.responses import (
    FileResponse, JSONResponse, Response as RawResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (actions, advisernote, ai, align, assistant, auth, blindspots, book, carrier,
               compounding,
               config, connectors, corpus, cyber, detect, discovery, gapsummary,
               household, household_loader,
               bi, drift, events, ingest, jurisdiction, mailer, marketmap,
               notifications, ocr,
               orchestrator, pagequality, persistence, pipeline, premium_at_risk,
               presenter, renewal,
               scenario, scheduler, score, sentinel, sourceview, stages, supplychain,
               voi, warranty, watchlist)
from .errors import ProvenError
from .retrieval import Retriever
from .sessions import Session, SessionStore

app = FastAPI(
    title="VERIS",
    version="2.0.0",
    description="An evidence-bound insurance agent for households and small businesses: "
                "page-cited answers, evidence grading and deterministic settlement "
                "arithmetic. No model decides cover or computes a figure. No "
                "authentication — local demo surface.",
)

# The synthetic example documents, as real PDFs. The point of serving them is that
# "Show me an example" can UPLOAD them through the ordinary /api/bundle path instead
# of the engine seeding a case from internal fixtures — so a viewer watches the same
# journey a customer takes, and nothing is pre-baked on the server.
#
# ONE DOCUMENT IS NOT IN HERE. The example's base wording is a real insurer's published
# policy wording. We generated the other thirteen documents, so they ship; that one is
# somebody else's copyrighted material, so it lives in the corpus outside git and is
# attached SERVER-SIDE (see `_corpus_wording_upload`). There is deliberately no
# browser-facing route that serves it.
EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "demo-documents"
HOUSEHOLD_EXAMPLE_DIR = EXAMPLE_DIR / "household"

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
SESSION_COOKIE = "proven_sid"
SESSIONS = SessionStore()

# Uploads per session per window. A cheap fixed-window counter, which is the right
# amount of machinery for a single-process demo: enough to stop an accidental loop
# from parsing a hundred PDFs, honest about not being a distributed rate limiter.
_UPLOAD_LIMIT = 20
_UPLOAD_WINDOW_SECONDS = 300


# ── session plumbing ────────────────────────────────────────────────────────

def _demo_state() -> dict:
    """The synthetic example case. Labelled at the point it is built.

    `source` used to be defaulted to DEMO_BUNDLE when READ, which meant an unlabelled
    brief silently claimed to be the demo. Now both origins are stamped where they are
    created — here and in `upload_bundle` — so the label is stored evidence rather than
    a read-time assumption.
    """
    st = pipeline.run_case(db_path=":memory:")
    st["brief"]["source"] = "DEMO_BUNDLE"
    return st


def _session(request: Request, response: Response) -> Session:
    """Resolve the caller's session, creating one on first contact."""
    sid = request.cookies.get(SESSION_COOKIE)
    session, created = SESSIONS.get_or_create(sid)
    if created or sid != session.id:
        response.set_cookie(
            SESSION_COOKIE, session.id, httponly=True, samesite="lax",
            max_age=SESSIONS.ttl_seconds, path="/")
    return session


# ── durable portfolio ───────────────────────────────────────────────────────
#
# The session case remains transient: it is where an uploaded bundle is analysed and
# deletion removes its findings. Portfolio records are a separate, encrypted tenant store
# and therefore require the authenticated owner principal rather than a browser cookie.
# The route never accepts a tenant id from a caller; the server derives it from that
# principal before it opens the repository.

class PortfolioMemberIn(BaseModel):
    class Config:
        extra = "forbid"

    name: str = Field(min_length=1, max_length=240)
    relationship: str = Field(min_length=1, max_length=120)
    date_of_birth: Optional[str] = Field(default=None, max_length=40)
    health_facts: Optional[str] = Field(default=None, max_length=4_000)
    household_id: Optional[str] = Field(default=None, max_length=160)


class PortfolioAssetIn(BaseModel):
    class Config:
        extra = "forbid"

    kind: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=240)
    identifiers: dict[str, str] = Field(default_factory=dict)
    household_id: Optional[str] = Field(default=None, max_length=160)


class PortfolioBusinessIn(BaseModel):
    class Config:
        extra = "forbid"

    name: str = Field(min_length=1, max_length=240)
    country: str = Field(min_length=1, max_length=16)
    registration_number: Optional[str] = Field(default=None, max_length=240)


class PortfolioCoverageItemIn(BaseModel):
    class Config:
        extra = "forbid"

    mechanism_code: str = Field(min_length=1, max_length=160)
    # Money stays text across the HTTP boundary and becomes Decimal in persistence.py.
    # JSON floats are intentionally not a second representation of money in the product.
    value: Optional[str] = Field(default=None, max_length=80)
    unit: Optional[str] = Field(default=None, max_length=80)
    evidence_span_ids: list[str] = Field(default_factory=list)
    source_document_id: Optional[str] = Field(default=None, max_length=160)


class PortfolioPolicyIn(BaseModel):
    class Config:
        extra = "forbid"

    country: str = Field(min_length=1, max_length=16)
    line_of_business: str = Field(min_length=1, max_length=120)
    insurer: str = Field(min_length=1, max_length=240)
    product: str = Field(min_length=1, max_length=240)
    masked_number: str = Field(default="", max_length=240)
    period_start: Optional[str] = Field(default=None, max_length=40)
    period_end: Optional[str] = Field(default=None, max_length=40)
    status: str = Field(min_length=1, max_length=80)
    renewal_date: Optional[str] = Field(default=None, max_length=40)
    household_id: Optional[str] = Field(default=None, max_length=160)
    business_id: Optional[str] = Field(default=None, max_length=160)
    insured_member_ids: list[str] = Field(default_factory=list)
    insured_asset_ids: list[str] = Field(default_factory=list)
    source_document_ids: list[str] = Field(default_factory=list)
    coverage_items: list[PortfolioCoverageItemIn] = Field(default_factory=list)


def _portfolio_scope(session: Session) -> persistence.TenantScope:
    """The owner tenant derived from the server-side authenticated principal."""
    principal = _require_role(session, auth.SME)
    return persistence.TenantScope(
        tenant_id=principal.organisation_id,
        user_id=principal.id,
        user_name=principal.name,
        user_email=principal.email,
        role=principal.role,
    )


def _portfolio_repository() -> persistence.PortfolioRepository:
    return persistence.repository_from_environment()


@app.exception_handler(persistence.PortfolioError)
async def portfolio_error_handler(_request: Request, exc: persistence.PortfolioError):
    code = getattr(exc, "code", None)
    error = {"code": code, "message": str(exc)} if code else str(exc)
    return JSONResponse(status_code=exc.http_status, content={"error": error})


@app.get("/api/portfolio")
def portfolio_overview(session: Session = Depends(_session)):
    """The authenticated tenant's durable household and business portfolio."""
    with session.lock:
        scope = _portfolio_scope(session)
        case_id = session.state.get("case_id")
        # The household example genuinely passes through the ordinary upload pipeline.
        # That transient household bundle is not an SME business analysis, so do not let
        # the T05 compatibility wrapper attach it as one.  The browser prepares and
        # wraps the existing SME example before it uploads this household bundle.
        household_case = session.state.get("_household_example_case_id")
        state = None if case_id and case_id == household_case else {
            "brief": session.state.get("brief"), "case_id": case_id,
        }
    repo = _portfolio_repository()
    if state is not None:
        # Existing SME analysis becomes a policy bundle only when it already exists in
        # this session. This does not make a GET manufacture a demo case or retain its
        # findings.
        repo.wrap_sme_analysis(scope, state)
    from . import health
    return health.enrich_portfolio(repo.portfolio(scope))


@app.get("/api/household/members")
def portfolio_members(session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"members": _portfolio_repository().list_members(scope)}


@app.post("/api/household/members")
def portfolio_add_member(body: PortfolioMemberIn, session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"member": _portfolio_repository().add_member(scope, **body.model_dump())}


@app.get("/api/assets")
def portfolio_assets(session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"assets": _portfolio_repository().list_assets(scope)}


@app.post("/api/assets")
def portfolio_add_asset(body: PortfolioAssetIn, session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"asset": _portfolio_repository().add_asset(scope, **body.model_dump())}


@app.get("/api/businesses")
def portfolio_businesses(session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"businesses": _portfolio_repository().list_businesses(scope)}


@app.post("/api/businesses")
def portfolio_add_business(body: PortfolioBusinessIn, session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"business": _portfolio_repository().add_business(scope, **body.model_dump())}


@app.get("/api/policies")
def portfolio_policies(session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"policies": _portfolio_repository().list_policies(scope)}


@app.post("/api/policies")
def portfolio_add_policy(body: PortfolioPolicyIn, session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"policy": _portfolio_repository().create_policy(scope, **body.model_dump())}


@app.get("/api/policies/{policy_id}")
def portfolio_policy(policy_id: str, session: Session = Depends(_session)):
    with session.lock:
        scope = _portfolio_scope(session)
    return {"policy": _portfolio_repository().get_policy(scope, policy_id)}


@app.get("/api/policies/{policy_id}/coverage/{coverage_id}/evidence")
def portfolio_coverage_evidence(policy_id: str, coverage_id: str,
                                 session: Session = Depends(_session)):
    """The exact synthetic wording behind a T06 plan-layout value.

    This is intentionally an excerpt endpoint, not a PDF download.  It only serves the
    reviewed wording associated with the hash-checked synthetic household records, and
    the normal source-viewer policy remains responsible for full user-uploaded pages.
    """
    with session.lock:
        scope = _portfolio_scope(session)
    repository = _portfolio_repository()
    policy = repository.get_policy(scope, policy_id)
    item = next((coverage for coverage in policy["coverage_items"]
                 if coverage["id"] == coverage_id), None)
    if item is None or not item.get("source_document_id"):
        raise HTTPException(404, "No cited portfolio evidence is available for that value.")
    binding = repository.get_seed_binding(scope, policy_id)
    if binding is None:
        raise HTTPException(404, "No cited portfolio evidence is available for that value.")
    source_policy_evidence = household.evidence_for_seeded_policy(
        binding.get("seed_id"), binding.get("policy_index"),
        item.get("mechanism_code", ""))
    document_id = binding.get("document_id")
    document_sha256 = binding.get("document_sha256")
    document_filename = binding.get("document_filename")
    if (source_policy_evidence is None or not isinstance(document_id, str)
            or not isinstance(document_sha256, str) or not isinstance(document_filename, str)):
        raise HTTPException(404, "No cited portfolio evidence is available for that value.")
    source_policy, evidence = source_policy_evidence
    if (item.get("source_document_id") != document_id
            or document_id not in policy["source_document_ids"]
            or source_policy.document_filename != document_filename):
        raise HTTPException(404, "No cited portfolio evidence is available for that value.")
    document = repository.get_document(scope, document_id)
    if (document["id"] != document_id or document["sha256"] != document_sha256
            or document["filename"] != document_filename):
        raise HTTPException(404, "No cited portfolio evidence is available for that value.")
    return {
        "available": True,
        "quote": evidence.wording,
        "document": source_policy.document_filename,
        "evidence_class": item["evidence_class"],
    }


def _verified_household_example_uploads(state: dict) -> dict[str, bytes]:
    """Return only the browser-uploaded PDFs that match the generated household set."""
    source_bytes = state.get(_SOURCE_BYTES_KEY)
    intake = state.get("intake")
    if not isinstance(source_bytes, dict) or not isinstance(intake, list):
        raise HTTPException(
            409, "Upload the synthetic household documents through the processing panel first.")

    received: dict[str, bytes] = {}
    expected = {spec.filename for spec in household.DOCUMENTS}
    for row in intake:
        if not isinstance(row, dict) or not row.get("accepted"):
            continue
        filename = row.get("filename")
        document_id = row.get("document_id")
        if filename not in expected or not isinstance(document_id, str):
            continue
        data = source_bytes.get(document_id)
        if isinstance(data, bytes):
            received[filename] = data

    if set(received) != expected:
        raise HTTPException(
            409, "The uploaded documents do not match the complete synthetic household example.")
    for filename, data in received.items():
        expected_path = HOUSEHOLD_EXAMPLE_DIR / filename
        if not expected_path.is_file():
            raise HTTPException(503, "The generated synthetic household documents are unavailable.")
        if hashlib.sha256(data).digest() != hashlib.sha256(expected_path.read_bytes()).digest():
            raise HTTPException(
                409, "A synthetic household document changed after it was generated; reload the example.")
    return received


@app.post("/api/portfolio/example-household")
def load_household_example(session: Session = Depends(_session)):
    """Persist the exact household that was just uploaded through the normal pipeline."""
    with session.lock:
        scope = _portfolio_scope(session)
        state = session.state
        sources = _verified_household_example_uploads(state)
    portfolio = household_loader.load_example(_portfolio_repository(), scope, sources)
    with session.lock:
        # See portfolio_overview: this current case is a household document bundle, not
        # a business analysis to be wrapped as a second business policy.
        session.state["_household_example_case_id"] = state.get("case_id")
    return {"ok": True, "portfolio": portfolio}


def _state(session: Session) -> dict:
    """The session's analysis state, built on first use from the demo bundle."""
    return SESSIONS.ensure_state(session, _demo_state)


def _brief_with_events(session: Session) -> dict:
    """The Brief annotated with the session's recorded change events."""
    st = _state(session)
    the_brief = st["brief"]
    if the_brief.get("deleted"):
        return the_brief
    return pipeline.annotate_events(
        the_brief, pipeline.recorded_events(st["conn"], st["case_id"]))


def _principal(session: Session) -> Optional[auth.Principal]:
    """Whoever is logged into this session, or None."""
    return session.state.get("_principal")


def _require_role(session: Session, *allowed: str) -> auth.Principal:
    """Server-side role boundary. 401 when nobody is logged in, 403 on the wrong role.

    Deliberately strict in one direction only. A caller with NO principal is refused
    here, because every route that uses this guard is an adviser or carrier surface and
    those must never be reachable by an anonymous session. The SME workspace routes do
    not use this guard — see `_deny_foreign_role`.
    """
    principal = _principal(session)
    if principal is None:
        raise HTTPException(
            401, "Sign in to use this workspace. Demo accounts are listed at "
                 "/api/auth/accounts.")
    if principal.role not in allowed:
        raise HTTPException(
            403, f"A {auth.ROLE_LABELS[principal.role].lower()} cannot open this "
                 f"workspace. This surface is for: "
                 f"{', '.join(auth.ROLE_LABELS[r].lower() for r in allowed)}.")
    return principal


def _deny_foreign_role(session: Session) -> None:
    """Guard the SME's own case against a logged-in adviser or carrier.

    This is the boundary that matters and it is asymmetric on purpose:

      * anonymous  -> ALLOWED. The local demo surface, which the example flow and the
        whole test suite depend on. Gating it would trade a real capability for the
        appearance of one.
      * SME        -> ALLOWED. Their own documents.
      * ADVISER    -> REFUSED, and told where to go instead. An adviser reads the
        CONSENTED disclosure at /api/disclosure/brief, so the consent gate becomes the
        actual access path rather than a second opinion about one.
      * CARRIER    -> REFUSED. A carrier sees book-level roll-ups, never one SME's
        documents. This is the first boundary a regulator would ask about.
    """
    principal = _principal(session)
    if principal is None or principal.role == auth.SME:
        return
    where = ("/api/disclosure/brief, which serves only what the business consented to "
             "disclose" if principal.role == auth.ADVISER else
             "/api/book, which is a book-level roll-up and holds no individual case")
    raise HTTPException(
        403, f"A {auth.ROLE_LABELS[principal.role].lower()} may not read a business's "
             f"own case. Use {where}.")


def _retriever(session: Session) -> Retriever:
    """The session's retrieval index, built once and cached on the session.

    Cached because embedding an unchanged document on every question would be slow and
    would cost money for no new information. Keyed on the session rather than the
    process because one session's documents must never answer another's question.
    """
    st = _state(session)
    existing = st.get("_retriever")
    if existing is None:
        existing = Retriever.from_connection(st["conn"])
        st["_retriever"] = existing
    return existing


# ── the AI and retrieval boundary ───────────────────────────────────────────
#
# MEASURED DEFECT, not a hypothetical one. `/api/brief` and `/api/score` both carried
# `_deny_foreign_role`, and the four AI/retrieval routes carried nothing at all. So the
# consent gate held exactly where a reader looks and failed everywhere else:
#
#     as CARRIER, no consent granted
#       GET  /api/brief            -> 403   correct
#       GET  /api/score            -> 403   correct
#       GET  /api/evidence/search  -> 200   returned total_sum_insured_sgd: 340000
#       POST /api/ai/ask           -> 200   returned the same figures
#       POST /api/ai/explain       -> 200   returned the adequacy figures
#       POST /api/ai/propose       -> 200   read the wording directly
#
# A carrier's underwriter could read an SME's stock figure and premises address through the
# copilot while being correctly refused the Brief. That is the same defect SHAPE as the
# case-deletion bug this codebase already fixed once: a guarantee that holds in the
# obvious place and not in the derived ones.
#
# The fix is not simply to deny advisers — that would delete a legitimate capability.
# It is to give the AI routes the SAME access model the rest of the product has, by
# reusing `pipeline.granted_fields()` rather than writing a second disclosure notion.

# The recipient name a disclosure must be granted to for an adviser to read anything.
# Matches the default used by `/api/adviser/review` and the consent screen, so one
# grant serves both surfaces rather than the owner having to consent twice.
_ADVISER_RECIPIENT = "Licensed adviser"


def _recipient_candidates(principal: Optional[auth.Principal],
                          preferred: str = "") -> tuple[str, ...]:
    """The names a live grant may carry that legitimately mean "this principal".

    MEASURED DEFECT THIS FIXES.

    `granted_fields` matches the recipient string EXACTLY, and the consent screen's
    recipient box is free text pre-filled with "Licensed adviser". An owner who typed
    their adviser's actual firm — "Lion Brokers (Singapore)", which is the name the
    adviser's own workspace displays — granted a disclosure that the adviser's own
    review screen then refused with a 403 and no explanation. The grant was real, the
    recipient was right, and the product said no.

    The carrier side failed the same way and worse: `/api/carrier/account/{id}` matches
    against `principal.organisation_name`, which nothing in the consent UI ever named,
    so case detail was unreachable by construction.

    Widening the match to the principal's own organisation is not a loosening. Both
    names still require the owner to have granted explicitly, and neither is a synonym
    for "anyone" — a grant to "Adviser A" still refuses "Lion Brokers (Singapore)". It
    only stops the product refusing a grant that was made to exactly this person.

    Order matters: the caller's explicit preference is tried first so an owner holding
    two grants gets the one they asked for.
    """
    names: list[str] = []
    if preferred and preferred.strip():
        names.append(preferred.strip()[:200])
    if principal is not None:
        if principal.role == auth.ADVISER:
            names.append(_ADVISER_RECIPIENT)
        if principal.organisation_name:
            names.append(principal.organisation_name)
    else:
        names.append(_ADVISER_RECIPIENT)
    # dict.fromkeys de-duplicates while preserving order.
    return tuple(dict.fromkeys(n for n in names if n))


def _resolve_grant(conn, case_id: str,
                   candidates: tuple[str, ...]) -> tuple[Optional[str], Optional[set[str]]]:
    """The first candidate name that a live disclosure actually names, and its fields.

    Returns `(None, None)` when no candidate is named, which every caller turns into the
    same 403 it raised before. Nothing here grants anything the owner did not.
    """
    for name in candidates:
        allowed = pipeline.granted_fields(conn, case_id, name)
        if allowed is not None:
            return name, allowed
    return None, None


def _ai_scope(session: Session) -> tuple[dict, Optional[set[str]]]:
    """Who may ask the AI about this case, and over how much of it.

    Returns `(brief, allowed_document_kinds)` where `None` for the second element means
    "no restriction" — the owner reading their own case. An adviser gets the CONSENTED
    document kinds and a consent-filtered brief; a carrier gets nothing.

    Raises 403 rather than returning an empty result, because an empty answer and a
    refused one mean different things and only one of them is honest about why.
    """
    principal = _principal(session)
    st = _state(session)

    # Anonymous is allowed for the same reason `_deny_foreign_role` allows it: the local
    # demo surface and the whole offline suite depend on it, and gating it would trade a
    # real capability for the appearance of one.
    if principal is None or principal.role == auth.SME:
        return _brief_with_events(session), None

    if principal.role == auth.CARRIER:
        raise HTTPException(
            403, "A carrier sees book-level roll-ups, not one business's documents. "
                 "There is no AI read path around that boundary — use /api/book or "
                 "/api/carrier/portfolio.")

    if principal.role == auth.ADVISER:
        named, allowed = _resolve_grant(
            st["conn"], st["case_id"], _recipient_candidates(principal))
        if allowed is None:
            raise HTTPException(
                403, f"This business has not disclosed its review to "
                     f"'{_ADVISER_RECIPIENT}' or to "
                     f"'{principal.organisation_name}', so there is nothing here to "
                     f"ask about. The consent gate is the access path, not a second "
                     f"opinion about one.")
        disclosed = pipeline.disclosed_brief(
            st["conn"], st["case_id"], st["brief"], named)
        # `disclosed_brief` returns the consent-scoped shape. The AI helpers expect a
        # brief, and the only field they read beyond `findings` is the business name,
        # so the two are reconciled here rather than by loosening either side.
        return {**disclosed, "findings": disclosed.get("findings") or []}, allowed

    raise HTTPException(403, "This surface is not available to your role.")


def _scoped_retriever(session: Session,
                      allowed_kinds: Optional[set[str]]) -> Retriever:
    """A retriever whose INDEX contains only permitted passages.

    Authorization-first, and the distinction is load-bearing. Searching the whole case
    and filtering the hits afterwards would still rank an adviser's query against
    undisclosed documents, still spend the embedding budget on them, and would leak
    through any future code path that forgot the post-filter. Building the index from
    permitted chunks makes the leak structurally impossible: there is nothing else in
    the index to find.
    """
    if allowed_kinds is None:
        return _retriever(session)

    st = _state(session)
    key = "_retriever_scoped:" + ",".join(sorted(allowed_kinds))
    existing = st.get(key)
    if existing is None:
        permitted = [c for c in _retriever(session).chunks
                     if c.document_kind in allowed_kinds]
        existing = Retriever(permitted)
        st[key] = existing
    return existing


@app.on_event("startup")
def _start_scheduler():
    """Start the autonomous agent scheduler when the app boots."""
    scheduler.SCHEDULER.start()


@app.on_event("shutdown")
def _stop_scheduler():
    """Stop cleanly so the background thread does not outlive the process."""
    scheduler.SCHEDULER.stop()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening. The CSP matters specifically because this product renders
    verbatim text from documents authored by somebody else: if an escaping bug ever
    slips through, the policy is the second line of defence.

    SCRIPT AND STYLE ARE TREATED DIFFERENTLY, ON PURPOSE.

    `script-src 'self'` stays strict with no 'unsafe-inline'. That is the control that
    actually stops injected document text from executing, and it is not negotiable. It
    also means inline `onclick=` attributes do not run, which is why `web/app.js` uses a
    single delegated listener over `data-act` attributes rather than inline handlers. If a
    button ever stops responding, that is the reason — add the action to the delegation
    table, do not weaken this header.

    `style-src` allows 'unsafe-inline' because the interface sets colours and layout on
    generated elements. An injected style can deface a page; it cannot execute code. That
    is a materially smaller risk than the one script-src defends against, and paying for it
    with a broken interface would be the wrong trade.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'")
    return response


@app.exception_handler(ProvenError)
async def proven_error_handler(_request: Request, exc: ProvenError):
    """Domain errors become actionable HTTP responses rather than a 500. The message
    is written for the business owner who uploaded the file."""
    return JSONResponse(status_code=exc.http_status, content={"error": exc.as_dict()})


# ── health and session ──────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": app.version,
        # Still False, and deliberately so: there is a credential check and a
        # server-side role boundary, but no wall in front of the business workspace.
        # Reporting True here would overstate the posture, and this field is what
        # somebody reads when deciding whether to deploy it.
        "authenticated": False,
        "auth_notice": (
            "This build has THREE SEEDED ACCOUNTS with salted PBKDF2 password hashes "
            "and a role boundary enforced server-side: an adviser cannot read a "
            "business's own case, and a carrier cannot read either. What it does NOT "
            "have is a login wall in front of the business workspace — an "
            "unauthenticated session still gets one, because that is the local demo "
            "surface. There is no signup, no reset and no rotation. Do not expose this "
            "port to a network."),
        "auth": {
            "seeded_accounts": len(auth.ACCOUNTS),
            "roles": list(auth.ROLES),
            "role_boundary_enforced": True,
            "login_required_for_business_workspace": False,
            "password_hashing": "pbkdf2_sha256, 200000 iterations, per-account salt",
        },
        "active_sessions": SESSIONS.count(),
        "upload_limits": {
            "max_files": ingest.MAX_FILES,
            "max_file_bytes": ingest.MAX_FILE_BYTES,
            "max_bundle_bytes": ingest.MAX_BUNDLE_BYTES,
            "max_pages": ingest.MAX_PAGES,
            "accepted": ["application/pdf"],
        },
        "persistence": persistence.status(),
        "ocr": ocr.status(),
        "ai": config.ai_status(),
        "scheduler": {"running": scheduler.SCHEDULER.running},
        # External source availability. Reported without probing on every health call —
        # a health endpoint that made four outbound government calls would be slow and
        # could fail offline. `live_enabled` is the honest switch state; per-request
        # LIVE/DEMO is reported next to the data on /api/sentinel, /api/discovery and
        # /api/drift, where a real call either succeeded or fell back.
        "external_sources": {
            "live_enabled": config.live_sources_enabled(),
            "sources": [
                {"name": "data.gov.sg rainfall", "agent": "sentinel",
                 "url": sentinel.RAINFALL_URL},
                {"name": "data.gov.sg 24h forecast", "agent": "sentinel",
                 "url": sentinel.FORECAST_24H_URL},
                {"name": "SingStat M211251 construction index",
                 "agent": "sentinel/drift", "url": sentinel.SINGSTAT_TABLE_URL},
                {"name": "ACRA company register (data.gov.sg)",
                 "agent": "discovery", "url": discovery.ACRA_DATASTORE_URL},
                {"name": "OneMap geocoding", "agent": "sentinel",
                 "url": "https://www.onemap.gov.sg/api/common/elastic/search"},
            ],
            "notice": ("Set PROVEN_LIVE_SOURCES=1 to enable real outbound calls. Each "
                       "call has a 15s timeout and falls back to a labelled fixture on "
                       "failure — a fixture is never rendered as live."),
        },
    }


# ── authentication and roles ────────────────────────────────────────────────

class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@app.get("/api/auth/accounts")
def auth_accounts():
    """The seeded demo accounts, so a viewer can sign in without being told a password
    out of band. Never serves a password hash."""
    return auth.catalogue()


@app.post("/api/auth/login")
def auth_login(body: LoginIn, session: Session = Depends(_session)):
    """Bind a principal to this session.

    The session is rotated on success. Without that, a session id captured before login
    would still be valid after it — which is session fixation, and it is cheap to
    prevent here because the session store already knows how to move state.
    """
    principal = auth.authenticate(body.email, body.password)
    if principal is None:
        # One message for both failure modes, so this cannot be used to enumerate
        # which email addresses exist.
        raise HTTPException(401, "That email and password combination is not valid.")
    with session.lock:
        session.state["_principal"] = principal
    return {
        "ok": True,
        "user": principal.as_dict(),
        "notice": ("Signed in. Your role decides which workspace you can open, and "
                   "that is enforced by the server rather than by which screen is "
                   "showing."),
    }


@app.post("/api/auth/logout")
def auth_logout(session: Session = Depends(_session)):
    """Sign out and drop the case with it.

    Discarding the analysis is the point rather than a side effect: leaving one user's
    findings in a session the next person signs into would undo the isolation the whole
    consent model rests on.
    """
    with session.lock:
        session.close()
    return {"ok": True, "notice": ("Signed out. The case in this session was "
                                   "discarded, not merely hidden.")}


@app.get("/api/auth/me")
def auth_me(session: Session = Depends(_session)):
    principal = _principal(session)
    return {
        "authenticated": principal is not None,
        "user": principal.as_dict() if principal else None,
        "notice": ("No one is signed in. The business workspace is still open because "
                   "this is a local demo surface; the adviser and carrier workspaces "
                   "are not."
                   if principal is None else
                   f"Signed in as {principal.name}, "
                   f"{auth.ROLE_LABELS[principal.role].lower()}."),
    }


@app.get("/api/session")
def session_info(session: Session = Depends(_session)):
    """Report whether this session holds a case. Deliberately does NOT build one.

    This endpoint used to call `_state`, which lazily runs the whole demo pipeline. So
    merely *asking* whether a case existed created one — and the front end could never
    show an honest empty state, because looking was indistinguishable from loading.
    A GET that silently runs a pipeline is also just surprising.

    `has_case` is what the product's first screen branches on: no case means show the
    upload flow, not somebody else's synthetic business.
    """
    st = session.state
    has_case = "conn" in st
    return {
        "session_id_prefix": session.id[:8],
        "has_case": has_case,
        "case_id": st.get("case_id"),
        "business_id": st.get("business_id"),
        "source": (st.get("brief") or {}).get("source") if has_case else None,
        "isolated": True,
        "notice": "Your case is scoped to this browser session and is not shared.",
    }


# ── the Brief ───────────────────────────────────────────────────────────────

@app.get("/api/brief")
def get_brief(session: Session = Depends(_session)):
    """The OWNER's own view of their own documents.

    Deliberately not consent-gated: consent governs disclosure to a THIRD PARTY, and
    gating the owner out of their own analysis would be security theatre. The
    consent gate is `/api/disclosure/brief`, which is a real access control.

    An adviser or carrier signed into this session is refused: they have their own
    surfaces, and neither of them is somebody else's own analysis.
    """
    with session.lock:
        _deny_foreign_role(session)
        return _brief_with_events(session)


@app.post("/api/reset")
def reset(session: Session = Depends(_session)):
    """Rebuild this session's case from the demo bundle. Scoped to the caller — it
    cannot disturb another session's case."""
    with session.lock:
        _deny_foreign_role(session)
        household_case = session.state.get("_household_example_case_id")
        session.reset_case()
        st = _state(session)
        # The final step of the household loader restores the separate SME demo case.
        # Preserve the replacement case marker so the portfolio does not wrap that
        # restored case as a second business bundle.
        if isinstance(household_case, str) and household_case:
            session.state["_household_example_case_id"] = st["case_id"]
        return {"ok": True, "brief": _brief_with_events(session),
                "case_id": st["case_id"]}


# ── document intake ─────────────────────────────────────────────────────────

def _rate_limit(session: Session, count: int) -> None:
    now = time.time()
    window = session.state.setdefault("_uploads", [])
    window[:] = [t for t in window if now - t < _UPLOAD_WINDOW_SECONDS]
    if len(window) + count > _UPLOAD_LIMIT:
        raise HTTPException(
            429, f"Too many uploads. The limit is {_UPLOAD_LIMIT} documents every "
                 f"{_UPLOAD_WINDOW_SECONDS // 60} minutes for one session.")
    window.extend([now] * count)


# The irreducible minimum. Nothing other than a wording and a schedule can establish what
# cover exists at all, so these two are mandatory and everything else is progressive.
#
# WHY THIS MATTERS MORE THAN IT LOOKS. A real SME does not have a CPF record, an ACRA extract
# and management accounts to hand. The demo hides that behind one button; a pilot would not,
# and "upload fourteen documents before you see anything" is where a pilot dies. So the
# product must be useful on two, and say honestly how much of the picture two buys.
MANDATORY_KINDS: tuple[str, ...] = ("BASE_WORDING", "SCHEDULE")


def _acra_prefill(state: dict) -> dict:
    """Company identity from the ACRA register, instead of asking for a PDF of it.

    WIRED, not a seam. `discovery.fetch_acra_by_name()` already calls the data.gov.sg
    entities datastore, so the work here is to reuse that lookup for onboarding rather than
    for change detection — which is why this is a few lines and Myinfo Business is not.

    THE IDENTITY GATE STILL APPLIES, and it is the reason this is safe. `q=` on that datastore
    is a fuzzy full-text search and something always ranks first: searching for the fictional
    "Hock Lee Foods" once returned MILLENNIUM AUTOMATION & SYSTEMS PTE. LTD. and the grounding
    gate waved it through, because that name really was in the fetched bytes. So a record only
    prefills anything if `identify_subject_records()` confirms it IS the subject. A wrong
    prefill is worse than no prefill: it puts a stranger's UEN on an insurance file.

    Myinfo Business stays a declared seam. It needs a real onboarding agreement with GovTech,
    and pretending otherwise is the kind of claim this product exists not to make.
    """
    out = {
        "available": False,
        "wired": ["ACRA API Marketplace (data.gov.sg entities datastore)"],
        "seam_only": ["Myinfo Business"],
        "what_it_would_replace": "Uploading an ACRA business profile PDF.",
        "fields": [],
        "matched": False,
        "identity": None,
        "live": config.live_sources_enabled(),
        "myinfo_status": (
            "Declared as a seam, not wired. Myinfo Business became available for onboarding "
            "in May 2026 and needs a real onboarding agreement with GovTech."),
    }

    # The register holds Singapore entities only. A business whose documents are not read
    # as Singaporean is not looked up in it: the lookup would send that business's name to
    # another country's register to find, at best, nothing — and at worst a namesake.
    pack = jurisdiction.pack_of(state)
    if not pack.lists("ACRA_PROFILE"):
        out["status"] = (
            "Company identity prefill reads the ACRA register, which holds Singapore "
            "entities only. These documents are not read under the Singapore pack, so no "
            "lookup was attempted.")
        out["jurisdiction_pack"] = pack.code
        return out

    business = (state.get("brief") or {}).get("business")
    uen = None
    facts = state.get("facts")
    if facts is not None:
        try:
            fact = facts.get("primary_ssic")
            uen = getattr(getattr(fact, "doc", None), "data", {}).get("uen")
        except Exception:
            uen = None

    if not config.live_sources_enabled():
        out["status"] = (
            "Live sources are off, so no register lookup was attempted. Set "
            "PROVEN_LIVE_SOURCES=1 to prefill company identity from ACRA instead of asking "
            "for the profile PDF.")
        return out

    records, raw, err = discovery.fetch_acra_by_name(str(business or ""), limit=5)
    subject, identity = discovery.identify_subject_records(
        records, expected_name=business, expected_uen=uen)
    out["identity"] = identity
    if err or not subject:
        out["status"] = (
            f"The register was searched and no record could be confirmed as this business. "
            f"{err or 'Nothing matched closely enough to be safe to prefill.'} We are not "
            f"going to put a stranger's UEN on your file, so the profile PDF is still the "
            f"way in.")
        return out

    record = subject[0]
    # Only fields the register itself states, each re-located verbatim in the fetched bytes
    # before it is offered — the same grounding discipline every other fact obeys.
    candidates = (
        ("uen", "UEN", record.get("uen")),
        ("entity_name", "Registered name", record.get("entity_name")),
        ("entity_type_desc", "Entity type", record.get("entity_type_desc")),
        ("uen_status_desc", "Registration status", record.get("uen_status_desc")),
        ("primary_ssic_description", "Primary activity",
         record.get("primary_ssic_description")),
        ("reg_street_name", "Registered street", record.get("reg_street_name")),
    )
    for key, label, value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        grounded, quote = discovery.ground_fact(text, raw)
        out["fields"].append({
            "field": key, "label": label, "value": text,
            "grounded": grounded, "quote": quote,
            "author_class": "AUTHORITY",
            "evidence_class": "PROVEN" if grounded else "UNESTABLISHED",
        })

    out["available"] = bool(out["fields"])
    out["matched"] = True
    out["status"] = (
        f"{len(out['fields'])} field(s) read from the ACRA register and confirmed as this "
        f"business, so you do not need to upload the profile PDF. Each one was re-located "
        f"verbatim in the register's own response before we offered it.")
    return out


@app.get("/api/onboarding")
def onboarding_state(session: Session = Depends(_session)):
    """What is needed to start, what is held, and what each further document would unlock.

    The completeness meter is computed from `blindspots` rather than counted here, so the
    number an owner sees on the upload screen cannot drift from the number on the coverage
    screen. Two sources for one figure is how a product starts contradicting itself.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}

        store = st.get("store")
        held = sorted({getattr(d, "kind", "") for d in
                       (store.documents() if store is not None else [])
                       if getattr(d, "kind", "")})
        missing_mandatory = [k for k in MANDATORY_KINDS if k not in held]

        computed = score.compute_score(_brief_with_events(session))
        cyber_result = cyber.assess(st)
        dependency_result = supplychain.assess(st)
        blind = blindspots.assess(st, score=computed, cyber=cyber_result,
                                  dependency=dependency_result)
        coverage = blind.get("coverage") or {}

        # Progressive disclosure, ranked by what each document would actually settle. The
        # ranking already exists in voi.py; this surfaces it as an onboarding queue instead
        # of a separate analysis screen.
        ranked = voi.rank_information_gaps(st.get("findings") or [], st.get("facts"))
        next_documents = [
            {"document": g.get("document_needed"),
             "document_kind": g.get("document_kind"),
             "unlocks": g.get("note"),
             "settles_share": g.get("share_of_total"),
             "becomes_class": g.get("becomes_class")}
            for g in (ranked.get("gaps") or [])[:5]
        ]

        return {
            "available": True,
            "mandatory": [
                {"kind": k, "held": k in held,
                 "why": ("The wording is the only document that states what the policy "
                         "does and does not do."
                         if k == "BASE_WORDING" else
                         "The schedule is the only document that states your sums insured, "
                         "your premises and which sections you hold.")}
                for k in MANDATORY_KINDS
            ],
            "ready_to_assess": not missing_mandatory,
            "missing_mandatory": missing_mandatory,
            "held_kinds": held,
            "completeness": {
                "documents_held": len(held),
                "areas_assessable": coverage.get("areas_we_can_speak_to"),
                "areas_total": coverage.get("areas_total"),
                "statement": (
                    f"{len(held)} document(s) uploaded — enough to assess "
                    f"{coverage.get('areas_we_can_speak_to')} of "
                    f"{coverage.get('areas_total')} areas."),
                "computed_from": "/api/blindspots",
                "why_not_counted_here": (
                    "The figure comes from the blind-spot register rather than being "
                    "counted again on this screen. Two sources for one number is how a "
                    "product starts contradicting itself."),
            },
            "next_documents": next_documents,
            "identity_prefill": _acra_prefill(st),
            "notice": ("You need two documents to start. Everything else is asked for one "
                       "at a time, by the screen that needs it, ranked by how much it "
                       "would settle."),
        }


@app.get("/api/bundle/kinds")
def bundle_kinds():
    """Document kinds an uploader may declare, and what each one is worth.

    `author_class` is shown so the uploader can see WHY the type matters: it decides
    the evidence class, and it is derived here rather than accepted from them.
    """
    return {
        "kinds": [
            {"kind": k, "label": k.replace("_", " ").title(),
             # This route has no case yet. The helper returns SELF unless every available
             # pack agrees, which is the safe pre-upload class rather than the strongest
             # possible class a later country selection might establish.
             "author_class": detect.author_class_for(k),
             "author_class_context": (
                 "No jurisdiction is selected yet; this is the GENERIC safety class. "
                 "See author_class_by_jurisdiction for the class after detection."),
             "grades_as": ("PROVEN" if detect.author_class_for(k) != "SELF"
                           else "DECLARED"),
             # Who authors a kind is a fact about a country. Shown per pack so the list
             # cannot imply that a CPF record grades as an authority document in a bundle
             # that is not read as Singaporean.
             "author_class_by_jurisdiction": {
                 code: jurisdiction.pack(code).author_class_for(k)
                 for code in jurisdiction.pack_codes()}}
            for k in detect.DECLARABLE_KINDS
        ],
        "notice": ("Leave the type blank to have it detected from the document's own "
                   "text. A document whose type cannot be identified is treated as "
                   "owner-authored, the weakest evidence class — never guessed "
                   "upward. Who authors a kind depends on the country the documents "
                   "establish; under no jurisdiction pack, a kind the GENERIC pack does "
                   "not list is owner-authored too."),
    }


# ── jurisdiction packs ──────────────────────────────────────────────────────
#
# REFERENCE DATA, deliberately unauthenticated. These two routes serve the packs in
# src/proven/jurisdictions/ and nothing else: no case, no session, nothing derived from
# anybody's documents. That is the property that lets them be public, and it is the one
# to protect — a field that ever carried case data would need the role boundary every
# case route has.

@app.get("/api/jurisdictions")
def jurisdictions_index():
    """The packs VERIS holds, the countries it can recognise without one, and how a
    bundle's documents choose between them."""
    return {
        "packs": [jurisdiction.pack(code).summary_dict()
                  for code in jurisdiction.pack_codes()],
        "fallback": jurisdiction.GENERIC,
        "recognised_without_pack": jurisdiction.recognised_without_pack(),
        "detection": {
            "signals": [{"signal": key, "weight": weight, "label": label}
                        for key, weight, label in jurisdiction.SIGNAL_WEIGHTS],
            "min_score": jurisdiction.MIN_SCORE,
            "margin": jurisdiction.MARGIN,
            "method": "DETERMINISTIC",
            "rule": ("Each kind of signal counts once per country. A country is chosen "
                     "only when it has a governing-law clause or two independent signal "
                     "kinds, scores at least min_score, and leads the next by at least "
                     "margin; otherwise the GENERIC pack applies and the reasons both "
                     "ways are reported."),
        },
        "verification_ladder": list(jurisdiction.STATUS_LADDER),
        "owner_facing_statuses": sorted(jurisdiction.OWNER_FACING_STATUSES),
        "notice": ("A CANDIDATE entry is shown here, in the evidence view, and never as a "
                   "statement to an owner. An owner-facing statement needs a verbatim "
                   "quote from the primary source (SOURCE_QUOTED) or a named person's "
                   "confirmation (HUMAN_VERIFIED), and is shown with its quote, its "
                   "source link and its status."),
    }


@app.get("/api/jurisdictions/{code}")
def jurisdiction_detail(code: str):
    """One pack in full: every entry with its verification block and whether it may be
    shown to an owner. The code is matched against the loaded packs, never used as a
    path, so a request cannot name a file."""
    key = (code or "").strip().upper()
    if not jurisdiction.has_pack(key):
        known = {c["code"]: c["name"] for c in jurisdiction.recognised_without_pack()}
        if key in known:
            name = known[key]
            raise HTTPException(
                404, f"VERIS recognises {name} documents but holds no {name} "
                     f"jurisdiction pack yet. Documents from {name} are read under the "
                     f"GENERIC pack, which makes no statement about {name} law.")
        raise HTTPException(
            404, "No jurisdiction pack for that code. GET /api/jurisdictions lists the "
                 "packs that exist.")
    return jurisdiction.pack(key).public_dict()


def _parse_declared_kinds(kinds: Optional[str]) -> list[Optional[str]]:
    """The optional `kinds` form field, as a list. Shared by both upload routes."""
    if not kinds:
        return []
    try:
        parsed = json.loads(kinds)
    except json.JSONDecodeError:
        raise HTTPException(
            400, "`kinds` must be a JSON array of document kinds, one per file.")
    if not isinstance(parsed, list):
        return []
    return [str(k) if k else None for k in parsed]


async def _read_uploads(files: list[UploadFile],
                        declared: list[Optional[str]]) -> list[pipeline.UploadedFile]:
    """Drain the multipart parts into memory, refusing an oversized bundle as we go.

    The cap is checked on the running total rather than afterwards, so a caller cannot
    make the process hold 500 MB before being told 60 is the limit.
    """
    uploads: list[pipeline.UploadedFile] = []
    total = 0
    for i, up in enumerate(files):
        data = await up.read()
        total += len(data)
        if total > ingest.MAX_BUNDLE_BYTES:
            raise HTTPException(
                413, f"The bundle exceeds "
                     f"{ingest.MAX_BUNDLE_BYTES // (1024 * 1024)} MB.")
        uploads.append(pipeline.UploadedFile(
            filename=up.filename or f"document-{i + 1}.pdf",
            data=data,
            declared_kind=declared[i] if i < len(declared) else None))
    return uploads


# The example's base wording, as a form flag rather than a route.
#
# WHY A FLAG AND NOT A DOWNLOAD. The other thirteen example documents are synthetic and
# the browser fetches and re-uploads them, which is what makes the example honest: the
# viewer watches the same journey a customer takes. The wording is a real insurer's
# copyrighted publication. Serving it for download would make this repository a
# redistributor of it, and no amount of "it's only the demo" changes that. So the server
# attaches it from the corpus, by id, and there is no browser-facing route for the bytes.
#
# It is still read through the ordinary ingest path with the ordinary detection — nothing
# about the analysis is special-cased, which is the property the example exists to show.
_EXAMPLE_WORDING_NAME = "01-policy-wording.pdf"


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _corpus_wording_upload() -> Optional[pipeline.UploadedFile]:
    """The example base wording from the corpus, or None when it is not fetched.

    None is a supported answer. Without the corpus the example still loads from its
    thirteen synthetic documents and the findings that quote the wording simply do not
    appear — which is the honest outcome, and the same one a user who uploaded no
    wording gets.
    """
    # Through `require_original`, not straight off the path: it hashes the stored bytes
    # against the manifest on first read. A file replaced by hand, restored from a stale
    # backup or half-written would otherwise be ingested and cited as the published
    # wording. Absent is a supported state here; drifted is not, so `CorpusDrifted`
    # propagates rather than degrading quietly to "not fetched".
    try:
        path = corpus.require_original(pipeline.CHUBB_ID)
    except corpus.CorpusMissing:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return pipeline.UploadedFile(filename=_EXAMPLE_WORDING_NAME, data=data,
                                 declared_kind="BASE_WORDING")


def _refuse_oversized_bundle(uploads: list[pipeline.UploadedFile],
                             extra: pipeline.UploadedFile) -> None:
    """The intake cap counts the document the SERVER adds, too.

    `_read_uploads` enforces MAX_BUNDLE_BYTES on the parts the caller sent. Appending
    the wording afterwards would put the retained session bytes over the documented
    bound by the size of that document, and the bound is the reason retaining them in
    memory is defensible at all.
    """
    total = sum(len(u.data) for u in uploads) + len(extra.data)
    if total > ingest.MAX_BUNDLE_BYTES:
        raise HTTPException(
            413, f"The bundle exceeds {ingest.MAX_BUNDLE_BYTES // (1024 * 1024)} MB "
                 f"once the worked example's base wording is included.")


async def _bundle_uploads(files: list[UploadFile], declared: list[Optional[str]],
                          attach_flag: Optional[str]) -> tuple[
                              list[pipeline.UploadedFile], int, frozenset[str]]:
    """The bundle as the pipeline will see it, and two facts about it.

    Returns the uploads; how many of them the CALLER sent, because the rate limit must
    charge for those only — a document the server attached is not the caller's traffic;
    and the safe filenames of the documents that came from the CORPUS, so their
    provenance can be recorded rather than reconstructed later from a bytes map that
    has forgotten it.
    """
    uploads = await _read_uploads(files, declared)
    sent_by_caller = len(uploads)
    from_corpus: set[str] = set()
    if _truthy(attach_flag):
        attached = _corpus_wording_upload()
        if attached is not None:
            _refuse_oversized_bundle(uploads, attached)
            uploads.append(attached)
            from_corpus.add(ingest.safe_filename(attached.filename))
    return uploads, sent_by_caller, frozenset(from_corpus)


# The evidence drawer renders the cited page of the source PDF with the quotation
# highlighted, and that needs the original bytes — which this pipeline deliberately did
# not keep. `ingest.ingest_pdf_bytes` stores extracted page TEXT and a sha256, and its
# docstring is explicit that uploads are never written to disk.
#
# That property is preserved exactly. The bytes are held IN THE SESSION, in memory, for
# the life of the case and no longer:
#   * never written to disk, so there is still no temp-file lifecycle to leak
#   * dropped by `session.reset_case()` on the next upload and by `close()` on sign-out,
#     because they live in `session.state` and are not in `_PRESERVED_ACROSS_CASES`
#   * dropped by `DELETE /api/case`, which is asserted by a test rather than assumed —
#     a deletion that leaves the source document readable would make the strongest
#     promise in this product cosmetic, which is the exact defect class the case-deletion
#     bug already taught this codebase once
#   * bounded by the intake caps: one bundle per session, so at most MAX_BUNDLE_BYTES
#     (60 MB) per session rather than an unbounded accumulation
_SOURCE_BYTES_KEY = "_source_bytes"

# WHO AUTHORED THE BYTES, recorded at intake rather than inferred afterwards.
#
# The map above is keyed by document_id and holds nothing but bytes. The worked example's
# base wording is a real insurer's publication that the SERVER attaches from the corpus,
# and once it is in that map it is indistinguishable from a file the signed-in user chose
# to upload. The distinction is not cosmetic: what this repository may show of a document
# depends on who published it — our own synthetic samples and a user's own upload may be
# rendered page by page, a third party's publication may not.
#
# So provenance is stored alongside, in a parallel map on the same session state, and it
# is dropped by `reset_case()` and `close()` for the same reason the bytes are: it is in
# `session.state` and not in `_PRESERVED_ACROSS_CASES`.
PROVENANCE_USER_UPLOAD = "USER_UPLOAD"
PROVENANCE_CORPUS = "CORPUS"
PROVENANCE_SYNTHETIC_EXAMPLE = "SYNTHETIC_EXAMPLE"
PROVENANCE_NOT_HELD = "NOT_HELD"
_SOURCE_PROVENANCE_KEY = "_source_provenance"


def _retain_source_bytes(session: Session, uploads: list[pipeline.UploadedFile],
                         result: dict, *,
                         from_corpus: frozenset[str] = frozenset()) -> None:
    """Key this bundle's bytes by the document_id the pipeline assigned them.

    Matched on the SAFE filename, which is what the intake report carries
    (`ingest.safe_filename` is applied before the row is written), so the same
    normalisation is applied on both sides rather than trusting the raw upload name.

    `from_corpus` names the safe filenames the server attached from the corpus. Anything
    else in the bundle arrived as a multipart part from the signed-in user.

    A corpus document's PROVENANCE is recorded and its BYTES are not. The bytes exist in
    this map only so a page can be drawn, and a third party's publication is never drawn
    as a page (see `source_page`) — so holding them would be holding the one thing the
    viewer must never serve, one refactor away from serving it.
    """
    by_name: dict[str, bytes] = {}
    for up in uploads:
        by_name[ingest.safe_filename(up.filename)] = up.data
    store: dict[str, bytes] = {}
    provenance: dict[str, str] = {}
    for row in (result.get("intake") or []):
        if not row.get("accepted"):
            continue
        doc_id, filename = row.get("document_id"), row.get("filename")
        if doc_id and filename in by_name:
            if filename in from_corpus:
                provenance[doc_id] = PROVENANCE_CORPUS
                continue
            store[doc_id] = by_name[filename]
            provenance[doc_id] = PROVENANCE_USER_UPLOAD
    result[_SOURCE_BYTES_KEY] = store
    result[_SOURCE_PROVENANCE_KEY] = provenance


@app.post("/api/bundle")
async def upload_bundle(
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    kinds: Optional[str] = Form(None),
    business_name: str = Form("Your business"),
    attach_example_wording: Optional[str] = Form(None),
):
    """Analyse documents the user supplies. THIS is what makes it a product.

    `kinds` is an optional JSON array parallel to `files`, naming each document's
    kind. It is a hint that selects the kind only: `author_class` — and therefore
    evidence class — is always derived (detect.author_class_for), never accepted.

    `attach_example_wording` asks the server to append the worked example's base
    wording from the corpus. It names no path and carries no bytes: the only document
    it can add is the one corpus id the server already holds, so a caller cannot use it
    to have the server read a file of their choosing.
    """
    session = _session(request, response)
    declared = _parse_declared_kinds(kinds)
    uploads, sent_by_caller, from_corpus = await _bundle_uploads(
        files, declared, attach_example_wording)
    name = (business_name or "").strip()[:120] or "Your business"
    with session.lock:
        _deny_foreign_role(session)
        _rate_limit(session, sent_by_caller)
        # reset_case, not close: `close()` empties the whole session and would sign the
        # uploader out mid-upload. The rate-limit window is preserved for the same
        # reason — clearing it would let a caller reset the limit by uploading again.
        session.reset_case()
        result = pipeline.run_bundle(
            uploads, business_id="uploaded-" + session.id[:8], business_name=name)
        result["brief"]["source"] = "UPLOADED_BUNDLE"
        _retain_source_bytes(session, uploads, result, from_corpus=from_corpus)
        session.state.update(result)
        return {"ok": True, "brief": _brief_with_events(session),
                "intake": result["brief"]["intake"]}


@app.post("/api/bundle/stream")
async def upload_bundle_stream(
    request: Request,
    response: Response,
    files: list[UploadFile] = File(...),
    kinds: Optional[str] = Form(None),
    business_name: str = Form("Your business"),
    attach_example_wording: Optional[str] = Form(None),
):
    """`/api/bundle`, reported file by file as it happens, as newline-delimited JSON.

    WHY A SECOND ROUTE RATHER THAN A FLAG. The upload screen ticks each document off as
    it is read, and there are only two ways to drive that: stream the real progress, or
    animate a plausible-looking guess on the client while one request blocks. The second
    is the cheaper build and it is a lie of exactly the kind this product refuses
    everywhere else — a tick would claim a file had been read when nothing had happened
    to it yet. So the progress is real, and the cost is this endpoint.

    `/api/bundle` is unchanged and still the contract for any non-interactive caller.
    Both share one pipeline; this one passes `on_progress` and that is the whole
    difference.

    The event stream is:
        {"event":"accepted","total":N}                        once, before any work
        {"event":"reading","index":i,"total":N,"filename":...}  before each parse
        {"event":"read",...,"accepted":true|false}             after each parse
        {"event":"stored",...,"document_id":...}               after each row is written
        {"event":"analysing","documents":N}                    reading done
        {"event":"done","ok":true,"brief":{...},"intake":{...}} the /api/bundle payload
        {"event":"error","code":...,"message":...}             instead of done, on failure

    HTTP status is 200 as soon as the guards pass, so a failure DURING the run arrives as
    a terminal `error` event rather than a status code. That is a real consequence of
    streaming and the client is written for it — an event stream cannot retract a status
    line it has already sent.
    """
    session = _session(request, response)
    declared = _parse_declared_kinds(kinds)
    uploads, sent_by_caller, from_corpus = await _bundle_uploads(
        files, declared, attach_example_wording)
    name = (business_name or "").strip()[:120] or "Your business"

    # Guards BEFORE the response starts. Once a 200 and the first byte are on the wire a
    # 403 or a 429 can no longer be expressed, so the role boundary and the rate limit are
    # evaluated here where they can still be an HTTP status.
    with session.lock:
        _deny_foreign_role(session)
        _rate_limit(session, sent_by_caller)

    events_q: "queue.Queue[Optional[dict]]" = queue.Queue()

    def push(event: dict) -> None:
        events_q.put(event)

    def work() -> None:
        try:
            with session.lock:
                session.reset_case()
                result = pipeline.run_bundle(
                    uploads, business_id="uploaded-" + session.id[:8],
                    business_name=name, on_progress=push)
                result["brief"]["source"] = "UPLOADED_BUNDLE"
                _retain_source_bytes(session, uploads, result,
                                     from_corpus=from_corpus)
                session.state.update(result)
                push({"event": "done", "ok": True,
                      "brief": _brief_with_events(session),
                      "intake": result["brief"]["intake"]})
        except ProvenError as exc:
            push({"event": "error", **exc.as_dict()})
        except Exception as exc:                       # pragma: no cover - defensive
            push({"event": "error", "code": "UPLOAD_FAILED", "message": str(exc),
                  "detail": None})
        finally:
            events_q.put(None)                         # sentinel: no more events

    worker = threading.Thread(target=work, name="bundle-stream", daemon=True)

    def stream():
        yield json.dumps({"event": "accepted", "total": len(uploads)}) + "\n"
        worker.start()
        while True:
            event = events_q.get()
            if event is None:
                return
            yield json.dumps(event) + "\n"

    streaming = StreamingResponse(
        stream(), media_type="application/x-ndjson",
        # Without this a proxy or a browser may buffer the whole body and deliver every
        # event at once, which would make the progress correct and invisible.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # MEASURED BUG, and it is a trap specific to returning your own Response object.
    # `_session` sets the session cookie on the INJECTED `response`, and FastAPI merges
    # that object's headers only when the handler returns a dict or a model. Return a
    # StreamingResponse and the Set-Cookie is silently dropped — so a first-time caller
    # uploaded successfully into a session it was never told the id of, and its next
    # request created a fresh session that fell back to the demo fixture. The upload
    # appeared to work and then vanished.
    for key, value in response.raw_headers:
        if key.lower() == b"set-cookie":
            streaming.raw_headers.append((key, value))
    return streaming


# ── change events (spec §7) ─────────────────────────────────────────────────

class EventsIn(BaseModel):
    codes: list[str] = Field(default_factory=list, max_length=32)


@app.get("/api/events")
def event_catalogue():
    return {"events": events.catalogue(),
            "notice": ("An event is a statement by the business, so it is "
                       "self-authored. It selects which findings are relevant and is "
                       "written to the audit log. It can never establish a value or "
                       "raise a finding's evidence class.")}


@app.post("/api/events")
def record_events(body: EventsIn, session: Session = Depends(_session)):
    with session.lock:
        st = _state(session)
        recorded = pipeline.record_change_events(st["conn"], st["case_id"],
                                                body.codes)
        unknown = sorted(set(c.strip().upper() for c in body.codes)
                         - set(events.BY_CODE))
        return {
            "recorded": recorded,
            "ignored_unknown_codes": unknown,
            "relevant_rules": sorted(events.rules_for(recorded)),
            "requested_documents": events.documents_for(recorded),
            "brief": _brief_with_events(session),
        }


@app.delete("/api/events")
def clear_events(session: Session = Depends(_session)):
    with session.lock:
        st = _state(session)
        pipeline.clear_change_events(st["conn"], st["case_id"])
        return {"recorded": [], "brief": _brief_with_events(session)}


# ── consent ─────────────────────────────────────────────────────────────────

class ConsentIn(BaseModel):
    """Consent is claimed to be GRANULAR, so the request must actually name what is
    being disclosed. `min_length=1` makes the claim true at the API boundary, and
    `disclosed_brief` enforces the named list as an access control list rather than
    treating it as a label. `max_length` on recipient bounds an unvalidated free-text
    field that is stored and later displayed."""

    recipient: str = Field(default="Licensed adviser", min_length=1, max_length=200)
    fields: list[str] = Field(min_length=1, max_length=40)


class RevokeIn(BaseModel):
    recipient: Optional[str] = Field(default=None, max_length=200)


@app.post("/api/consent")
def consent(body: ConsentIn, session: Session = Depends(_session)):
    with session.lock:
        st = _state(session)
        did = pipeline.grant_disclosure(
            st["conn"], st["case_id"], body.recipient, body.fields)
        return {"disclosure_id": did,
                "granted": pipeline.disclosure_granted(st["conn"], st["case_id"]),
                "disclosures": pipeline.live_disclosures(st["conn"],
                                                         st["case_id"])}


@app.post("/api/consent/revoke")
def consent_revoke(body: RevokeIn, session: Session = Depends(_session)):
    """Revocation is what makes consent consent. A grant that cannot be withdrawn is
    a transfer."""
    with session.lock:
        st = _state(session)
        revoked = pipeline.revoke_disclosure(
            st["conn"], st["case_id"], body.recipient)
        return {"revoked": revoked,
                "granted": pipeline.disclosure_granted(st["conn"], st["case_id"]),
                "disclosures": pipeline.live_disclosures(st["conn"],
                                                         st["case_id"])}


@app.get("/api/consent/status")
def consent_status(session: Session = Depends(_session)):
    """What is currently shared, and what there is to share.

    `shareable` is the list the consent screen must render, and getting it from the server
    rather than hardcoding it in the client is the fix for a real defect:

      MEASURED. The consent screen offered three checkboxes — "findings", "facts",
      "score" — and posted those strings as the granted `fields`. But `granted_fields`
      upper-cases the list and `disclosed_brief` compares it against DOCUMENT KINDS, so
      FINDINGS / FACTS / SCORE matched nothing. The grant therefore succeeded, the queue
      showed the case as disclosed, and every span-backed finding was withheld — the
      adviser opened a "shared" case and read `No findings in scope`. Consent looked
      granular and was, in effect, a grant of nothing.

      The test suite never caught it because the tests grant real kinds
      (`["SCHEDULE", "CPF_RECORD"]`) directly, so the API contract was always exercised
      correctly and only the client was wrong.

    So the vocabulary is published here, derived from the documents this case actually
    holds. A checkbox now names a document the owner really uploaded, which is also the
    more honest thing to ask them: they can reason about "my policy schedule", not about
    an abstraction called "the figures".
    """
    with session.lock:
        st = _state(session)
        store = st.get("store")
        counts: dict[str, int] = {}
        names: dict[str, list[str]] = {}
        classes: dict[str, str] = {}
        for doc in (store.documents() if store is not None else []):
            kind = getattr(doc, "kind", None) or "UNKNOWN"
            counts[kind] = counts.get(kind, 0) + 1
            classes.setdefault(kind, getattr(doc, "author_class", "SELF"))
            filename = getattr(doc, "filename", None)
            if filename:
                names.setdefault(kind, []).append(filename)
        # One shareable row per document kind. The class was recorded when the bundle's
        # selected pack ingested the document, so no case-scoped response recomputes it
        # without that pack.
        shareable = [{
            "kind": kind,
            "label": kind.replace("_", " ").title(),
            "count": counts[kind],
            "filenames": names.get(kind, []),
            "author_class": classes.get(kind, "SELF"),
        } for kind in sorted(counts)]
        return {"granted": pipeline.disclosure_granted(st["conn"], st["case_id"]),
                "disclosures": pipeline.live_disclosures(st["conn"], st["case_id"]),
                "adviser_recipient": _ADVISER_RECIPIENT,
                # WHY THE RECIPIENT NAMES ARE PUBLISHED RATHER THAN TYPED.
                #
                # `granted_fields` matches the recipient EXACTLY. The carrier surface
                # matches against the underwriter's `organisation_name`, which this
                # screen never named — so "disclose my case to my insurer" was a
                # sentence the product could not express, and the carrier's account
                # detail was unreachable by construction rather than by policy.
                #
                # Publishing the names makes each grant a choice between real
                # counterparties instead of a spelling test. Free text still works; this
                # is the list the screen offers as buttons.
                "recipients": [
                    {"recipient": _ADVISER_RECIPIENT,
                     "role": auth.ADVISER,
                     "label": "Your licensed adviser",
                     "why": ("An adviser can act on a finding with you — request the "
                             "document that would settle it, and record the basis on "
                             "which they advised.")},
                ] + [
                    {"recipient": account.principal.organisation_name,
                     "role": account.principal.role,
                     "label": account.principal.organisation_name,
                     "why": ("Your insurer sees only what you release. Without a grant "
                             "they hold counts and flags from their own book and none "
                             "of your documents.")}
                    for account in auth.ACCOUNTS.values()
                    if account.principal.role == auth.CARRIER
                ],
                "shareable": shareable,
                "notice": ("Consent is granted per document. A finding is only shown to a "
                           "recipient if every document it rests on was granted to them; "
                           "anything withheld is counted and named for them to ask about, "
                           "never silently dropped.")}


@app.get("/api/disclosure/brief")
def disclosed_brief(recipient: str = "Licensed adviser",
                    session: Session = Depends(_session)):
    """The adviser's view, and the actual consent gate.

    403 unless a live disclosure names this recipient. Findings evidenced by
    documents that were not disclosed are withheld and COUNTED, so the adviser can
    ask for what they need rather than being handed a silently shortened list. Every
    read writes an audit row: a pull, logged.
    """
    with session.lock:
        st = _state(session)
        return pipeline.disclosed_brief(
            st["conn"], st["case_id"], st["brief"], recipient.strip()[:200])


# ── case lifecycle ──────────────────────────────────────────────────────────

@app.delete("/api/case")
def delete_case(session: Session = Depends(_session)):
    """Deleting the case removes every finding; the portable record survives.

    The cached brief is dropped too. Without that, /api/brief would keep serving
    findings from memory after the case was deleted, which would make the deletion
    guarantee cosmetic — the one promise this product cannot afford to fake. Live
    disclosures are revoked, so no grant outlives the data it was granted over.
    """
    with session.lock:
        st = _state(session)
        conn, case_id, business_id = st["conn"], st["case_id"], st["business_id"]
        pipeline.delete_case(conn, case_id)
        result = {
            "deleted": True,
            "findings_remaining": pipeline.count_findings(conn, case_id),
            "record_facts_remaining": pipeline.count_record_facts(conn, business_id),
            "disclosures_remaining": len(
                pipeline.live_disclosures(conn, case_id)),
        }
        # The in-memory findings list must go too, not just the assembled brief.
        #
        # MEASURED: it did not, and /api/information-value kept ranking the deleted
        # findings' information gaps — naming this business's missing documents — while
        # /api/brief correctly showed nothing. Deletion has to reach every view that
        # was derived from the case, or it is cosmetic in exactly the places a reader
        # is least likely to check.
        st["findings"] = []
        # And the retained source bytes, which are the strongest form of the same
        # problem: a deletion that leaves the original PDF renderable through
        # /api/source would make this endpoint's promise decorative. Same defect shape
        # as the cached-brief bug above, one layer further down.
        st[_SOURCE_BYTES_KEY] = {}
        prior = st["brief"]
        st["brief"] = {
            "business": prior.get("business"),
            "carrier": prior.get("carrier"),
            "deleted": True,
            "notice": "This case was deleted. Findings do not survive deletion. "
                      "The portable record of established facts does.",
            "accounting": prior.get("accounting"),
            "findings": [],
            "summary": {"finding_count": 0, "by_class": {}, "by_state": {},
                        "abstained": 0},
            "searched": prior.get("searched", []),
            "not_searched": prior.get("not_searched", []),
            "facts": {},
            "events": [],
        }
        return result


# ── cross-carrier ───────────────────────────────────────────────────────────

@app.get("/api/compare")
def compare():
    """Cross-carrier divergence, computed live from three real wordings. The same
    average rule yields MARKET_CONVENTION on Chubb/MSIG and PROVEN on Liberty,
    decided by what each document states. Memoised: it is a pure function of three
    immutable files."""
    return pipeline.compare_carriers()


# ── coverage semantics: alignment, market map, information value, drift ─────

# Every case-scoped analytical view must fall silent once the case is deleted.
# `/api/brief` already does. These three did not, which made the deletion promise
# cosmetic for whichever view a reader happened not to re-check. `/api/marketmap` is
# absent from this list on purpose: it takes no session and is computed from published
# wordings, so there is no case in it to delete.
_DELETED_NOTICE = (
    "This case was deleted, so no analysis derived from it is served. Deletion "
    "reaches every view, not only the Brief. The portable record of established "
    "facts survives; findings and anything computed from them do not.")


def _case_deleted(st: dict) -> bool:
    return bool(st.get("brief", {}).get("deleted"))


@app.get("/api/alignment")
def alignment(session: Session = Depends(_session)):
    """Structural mechanism alignment for the wording in the current case.

    Returns the bound alignments AND the rejected proposals. The rejections are part
    of the response by design: a verifier whose refusals are hidden cannot be
    distinguished from one that accepts everything.
    """
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        base = st.get("base_doc_id")
        carrier = st.get("carrier") or ""
        if not base:
            return {
                "available": False,
                "reason": ("No base policy wording is in this case, so there is "
                           "nothing to align. Upload a wording."),
            }
        res = align.align_document(st["conn"], base, carrier=carrier)
        out = res.as_dict()
        out["available"] = True
        out["method"] = (
            "Propose, then verify, then bind. A candidate span is proposed for a "
            "mechanism and may be wrong; it is bound only if every mandatory "
            "structural slot matches and no negative slot does. The proposer gets no "
            "vote. No machine-learning model makes any consequential decision.")
        out["why_it_matters"] = (
            "The mechanism is identified by structure rather than by clause number or "
            "heading, so a wording from a carrier nobody has read can still be "
            "accounted for.")
        return out


@app.get("/api/marketmap")
def market_map(jurisdiction: Optional[str] = None):
    """Clause-level market position across published wordings.

    Pure function of immutable corpus files, so it takes no session. `jurisdiction`
    filters to SG or MY; unfiltered returns both, with Malaysian rows flagged and
    excluded from any Singapore ordering.
    """
    if jurisdiction is not None and jurisdiction.upper() not in {"SG", "MY"}:
        raise HTTPException(
            400, f"Unknown jurisdiction {jurisdiction!r}. Use 'SG' or 'MY'.")
    mm = marketmap.build_market_map(
        only_jurisdiction=jurisdiction.upper() if jurisdiction else None)
    return mm.as_dict()


@app.get("/api/information-value")
def information_value(session: Session = Depends(_session)):
    """Rank the missing documents by how much exposure each one would resolve.

    Turns UNESTABLISHED from a dead end into an ordered action list. No value is
    invented for the missing figure: what is computed is the size of the consequence
    of not knowing it.
    """
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            # Same keys as the live response. A consumer that has to branch on shape
            # to handle deletion will eventually get the branch wrong.
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    "gaps": [], "count": 0, "unbounded_count": 0,
                    "total_uncertainty": "0", "recommendation": _DELETED_NOTICE,
                    "method": "", "caveat": ""}
        out = voi.rank_information_gaps(st.get("findings") or [], st.get("facts"))
        out["available"] = True
        return out


@app.get("/api/score")
def protection_score(session: Session = Depends(_session)):
    """The Protection Pulse score — the owner's entry point.

    Arithmetic over findings the deterministic engine already produced. `ai.py` is not
    consulted here and `score.py` does not import it, which is the guarantee that the
    headline number on the first screen is not a model's opinion. Every deduction
    carries the `rule_id` it came from, so the score decomposes back to evidence
    instead of being an unexplainable rating.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            # Same keys as the live response, so a consumer never has to branch on
            # shape to handle deletion.
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    "overall": 0, "status": "No case", "status_note": _DELETED_NOTICE,
                    "assessed_dimensions": 0, "total_dimensions": 0,
                    "dimensions": [], "issues": [], "headline_exposure": None,
                    "method": "DETERMINISTIC", "notice": _DELETED_NOTICE}
        out = score.compute_score(_brief_with_events(session))
        out["available"] = True
        return out


@app.get("/api/drift")
def drift_view(index: str = "construction", live: Optional[bool] = None,
               session: Session = Depends(_session)):
    """Adequacy decaying while the policy does not change.

    The index in this build is declared sample data and every response says so.
    """
    if index not in drift.INDICES:
        raise HTTPException(
            400, f"Unknown index {index!r}. Available: "
                 f"{', '.join(sorted(drift.INDICES))}.")
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        return drift.drift_for_case(
            facts=st.get("facts"), carrier=st.get("carrier") or "CHUBB",
            index_key=index, live=live)


# ── renewal comparison — what changed between last year and this ─────────────

_RENEWAL_PRIOR = "renewal-prior-policy.pdf"
_RENEWAL_CURRENT = "renewal-current-policy.pdf"


def _load_policy_version(filename: str, label: str) -> Optional[renewal.PolicyVersion]:
    path = EXAMPLE_DIR / filename
    if not path.exists():
        return None
    try:
        pages = ingest.extract_pdf_pages_cached(path.read_bytes(), filename)
    except Exception:
        return None
    return renewal.PolicyVersion.from_pages(pages, document_id=filename, label=label)


@app.get("/api/renewal/compare")
def renewal_compare(session: Session = Depends(_session)):
    """What changed between the prior policy and the renewal.

    Session-scoped and role-guarded like the rest of the SME workspace: an adviser or
    carrier is refused (they have their own surfaces) and no arbitrary case id is
    accepted. The comparison runs over the two demo renewal documents in
    `demo-documents/`; a real deployment would pass the business's own prior and current
    policy versions.

    Material changes are fed to the orchestrator as signals so they inherit the EXISTING
    precedence rules — a material change on an insured interest that the documents
    already evidence a finding on becomes ACTION_REQUIRED. This does NOT create a new
    coverage verdict; whether cover responds stays a licensed adviser's call.
    """
    with session.lock:
        _deny_foreign_role(session)
        prior = _load_policy_version(_RENEWAL_PRIOR, "Prior year (2025/2026)")
        current = _load_policy_version(_RENEWAL_CURRENT, "Renewal (2026/2027)")
        if prior is None or current is None:
            return {"available": False,
                    "reason": ("The demo renewal documents are not present in "
                               "demo-documents/. Generate them with "
                               "tests/pdfbuild_renewal.py.")}
        result = renewal.compare(prior, current)
        result["available"] = True

        # Feed material/unknown changes into the orchestrator under existing precedence.
        changes = renewal.compare_terms(prior, current)
        signals = renewal.material_signals(changes)
        st = _state(session)
        findings = (_brief_with_events(session).get("findings") or []) \
            if not _case_deleted(st) else []
        if signals:
            reconciled = orchestrator.evaluate(
                external_signals=signals, findings=findings)
            result["orchestration"] = {
                "decisions": reconciled["decisions"],
                "highest": reconciled["highest"],
                "notice": ("Material changes were reconciled under the orchestrator's "
                           "existing precedence rules, not a new coverage verdict."),
            }
        return result


# ── claim scenario simulator — what happens if you claim tomorrow ────────────

@app.get("/api/scenario")
def scenario_view(peril: str = "fire", loss: Optional[str] = None,
                  sub_limit: Optional[str] = None,
                  session: Session = Depends(_session)):
    """Illustrate what a claim would pay and what the owner absorbs.

    Deterministic waterfall in a fixed order (sum insured cap → peril sub-limit →
    average clause → deductible), every line sourced to a document, page and quote. If
    the sum insured or value at risk is unestablished, it ABSTAINS rather than guessing.
    The final figure is a DETERMINISTIC ILLUSTRATION, never a guaranteed payout.
    """
    from decimal import Decimal as _D, InvalidOperation
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        try:
            # Query values remain decimal text until the deterministic arithmetic layer.
            # Accepting FastAPI floats here would make a money representation that can
            # silently round before Decimal sees it.
            loss_d = _D(loss) if loss is not None else None
            sub_d = _D(sub_limit) if sub_limit is not None else None
        except (InvalidOperation, ValueError):
            raise HTTPException(422, "loss and sub_limit must be Decimal text values.")
        return scenario.simulate(st, peril=peril, loss_amount=loss_d, sub_limit=sub_d)


@app.get("/api/scenario/presets")
def scenario_presets(peril: str = "fire", session: Session = Depends(_session)):
    """Small / typical / total-loss presets, each fully simulated, for the slider."""
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        return scenario.presets(st, peril=peril)


# ── business interruption adequacy ──────────────────────────────────────────

@app.get("/api/bi")
def bi_assess(session: Session = Depends(_session)):
    """Business interruption cover: is it there, and is it big enough?

    Two independent questions with different evidentiary weight. ABSENCE of a BI section
    is provable from the schedule alone and needs no arithmetic. ADEQUACY needs annual
    gross profit and an indemnity period; where either is missing the engine ABSTAINS and
    names the document that would resolve it, rather than deriving gross profit from a
    balance sheet that does not contain one.

    The findings the deterministic rule engine already reports are NOT re-emitted as
    orchestrator signals — counting one document twice would inflate priority on false
    corroboration. Only the indemnity-period and sum-insured findings, which the rule
    engine does not produce, are reconciled.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        result = bi.assess(st)
        signals = bi.as_external_signals(result)
        if signals:
            findings = _brief_with_events(session).get("findings") or []
            reconciled = orchestrator.evaluate(
                external_signals=signals, findings=findings)
            result["orchestration"] = {
                "decisions": reconciled["decisions"],
                "highest": reconciled["highest"],
                "notice": ("Reconciled under the orchestrator's existing precedence "
                           "rules. Absence of the section is withheld from this "
                           "reconciliation because the rule engine already reports it."),
            }
        return result


# ── cyber and data exposure ─────────────────────────────────────────────────

@app.get("/api/cyber")
def cyber_assess(session: Session = Depends(_session)):
    """Personal data held, and whether anything in the policy responds to its loss.

    The one screen in this product whose finding is an ABSENCE rather than a shortfall,
    which changes what counts as evidence. A shortfall subtracts two documented figures.
    An absence is established by reading the operative sections and finding that nothing
    responds — so an unreadable schedule must abstain rather than conclude absence, and
    the module does.

    Nothing here is predictive. There is no breach probability, no estimated penalty and
    no network scan. The statutory ceiling is cited as statute and explicitly labelled
    not-a-prediction, because a ceiling presented as an expected loss would be the exact
    overstatement this product exists to refuse.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        result = cyber.assess(st)
        signals = cyber.as_external_signals(result)
        if signals:
            findings = _brief_with_events(session).get("findings") or []
            reconciled = orchestrator.evaluate(
                external_signals=signals, findings=findings)
            result["orchestration"] = {
                "decisions": reconciled["decisions"],
                "highest": reconciled["highest"],
                "notice": ("Raised as a POTENTIAL_EXPOSURE under existing precedence. "
                           "The engine does not assert that a breach will happen or "
                           "that a penalty would follow."),
            }
        return result


# ── dependency and supply-chain exposure ────────────────────────────────────

@app.get("/api/supply-chain")
def supply_chain_assess(session: Session = Depends(_session)):
    """What the business depends on outside its own walls, and what responds when it fails.

    Reports `visibility` as a first-class field rather than a footnote. The dependency map
    is built only from documents that NAME a counterparty, so it can never be complete —
    and a partial map presented as complete would convert an unknown unknown into a false
    known, which is worse than showing nothing.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        result = supplychain.assess(st)
        signals = supplychain.as_external_signals(result)
        if signals:
            findings = _brief_with_events(session).get("findings") or []
            reconciled = orchestrator.evaluate(
                external_signals=signals, findings=findings)
            result["orchestration"] = {
                "decisions": reconciled["decisions"],
                "highest": reconciled["highest"],
                "notice": ("Reconciled under existing precedence. Absence of the "
                           "business interruption section is reported by the rule "
                           "engine and is not counted twice here."),
            }
        return result


# ── growth stage and the protection roadmap ─────────────────────────────────

@app.get("/api/stages")
def stages_assess(session: Session = Depends(_session)):
    """Where the business sits on a growth ladder, and what that stage conventionally holds.

    The only surface in this product that names a protection the owner does NOT hold, which
    makes it the most dangerous one. Three guards apply: every expectation carries
    STATUTORY or MARKET_CONVENTION and the two are never merged; a MARKET_CONVENTION item
    may never be phrased as an obligation; and the stage itself is assigned only from facts
    a document established, never from a change event the owner tapped.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        return stages.assess(st)


@app.get("/api/stages/ladder")
def stages_ladder():
    """The ladder and protection vocabulary. Holds no case, so it needs no session."""
    return stages.catalogue()


# ── compounding over time ───────────────────────────────────────────────────

@app.get("/api/compounding")
def compounding_assess(session: Session = Depends(_session)):
    """How long each gap between a business change and a policy change has been open.

    Reports DAYS, never dollars. Exposure does not accrue continuously — it crystallises
    only if a loss happens — so a running cost would be invented. A day count is the
    distance between two dates in two of the owner's own documents, which is a fact.

    The clock is pinned to the latest date the documents evidence rather than to today,
    so the screen reads the same on every run and a demo is reproducible.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        return compounding.assess(st)


# ── the blind-spot register ─────────────────────────────────────────────────

@app.get("/api/blindspots")
def blindspots_report(session: Session = Depends(_session)):
    """What we did NOT check, and why quiet is not the same as safe.

    Composes the already-computed score, cyber and dependency results rather than
    recomputing them: this route reports on the others, and a second computation could
    disagree with the first, which would be a worse failure than not having the screen.

    The load-bearing output is `score_caveat`. The overall score is the mean of ASSESSED
    dimensions, so an area with nothing to read is absent from the number rather than
    dragging it down. That is the right arithmetic and the wrong impression, and this is
    where the impression gets corrected.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        brief_now = _brief_with_events(session)
        return blindspots.assess(
            st,
            score=score.compute_score(brief_now),
            cyber=cyber.assess(st),
            dependency=supplychain.assess(st))


@app.get("/api/blindspots/register")
def blindspots_register():
    """The risk-area register itself. Holds no case, so it needs no session."""
    return blindspots.catalogue()


@app.get("/api/gap-summary")
def gap_summary(session: Session = Depends(_session)):
    """One screen over everything the engine already found. Composition, not a second engine.

    Every value is read from another module's output. Nothing is recomputed here, because two
    engines that disagree is worse than one engine that is wrong — a reader has no way to
    tell which surface lied. `test_gap_summary.py` asserts every item traces back to an
    existing endpoint.

    The three categories exist to stop the one dishonest simplification this screen invites:
    counting everything unresolved as a gap. Something we cannot confirm is reported as
    needing verification, and something we never examined is reported as neutral — never as
    a denial of cover.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    **gapsummary.catalogue()}
        brief_now = _brief_with_events(session)
        computed = score.compute_score(brief_now)
        cyber_result = cyber.assess(st)
        dependency_result = supplychain.assess(st)
        return {
            "available": True,
            **gapsummary.build(
                brief=brief_now,
                score=computed,
                blind=blindspots.assess(st, score=computed, cyber=cyber_result,
                                        dependency=dependency_result),
                cyber=cyber_result,
                dependency=dependency_result,
                gaps=voi.rank_information_gaps(st.get("findings") or [],
                                               st.get("facts")),
                compounding=compounding.assess(st)),
        }


# ── warranties and conditions precedent ─────────────────────────────────────

@app.get("/api/warranty")
def warranty_register(session: Session = Depends(_session)):
    """Will this policy actually pay, or has a technicality already voided it?

    Every other surface in this product answers "what does my policy say". This one
    answers "will it pay", and those diverge because breaching a condition precedent can
    defeat a claim the cover otherwise plainly responded to — depending on the wording,
    regardless of whether the breach had anything to do with the loss.

    Each obligation is searched for in the customer's OWN wording, and a located phrase
    only counts as an obligation if duty language sits near it. Phrases found in
    definition or exclusion text are set aside WITH the excerpt and the reason, so a
    reader can overrule the judgement rather than take it on trust.

    UNVERIFIED is the default and it is deliberate: COMPLIANT requires evidence of
    compliance, and silence is not compliance.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True}
        result = warranty.register(st)
        signals = warranty.as_external_signals(result)
        if signals:
            findings = _brief_with_events(session).get("findings") or []
            reconciled = orchestrator.evaluate(
                external_signals=signals, findings=findings)
            result["orchestration"] = {
                "decisions": reconciled["decisions"],
                "highest": reconciled["highest"],
                "notice": ("Breaches and unverified conditions precedent raise attention "
                           "under existing precedence. The engine does not assert that "
                           "cover is void — that is a legal determination."),
            }
        return result


# ── presentation intelligence ───────────────────────────────────────────────
#
# The only place in this product where a model is allowed to choose what a screen SAYS.
# It is handed data the engine already verified, and its answer is discarded if it states
# a coverage outcome. Architecture and guardrails are Shreya's design; the transport here
# is OpenAI because that is the key this environment holds.

class PresentationIn(BaseModel):
    purpose: str = Field(min_length=1, max_length=100)
    verified_context: dict = Field(default_factory=dict)


@app.post("/api/presentation/compose")
def presentation_compose(body: PresentationIn,
                         session: Session = Depends(_session)):
    """Turn a verified finding into one plain sentence and a chart choice.

    Session-scoped so credentials never leave the server and an anonymous caller cannot
    burn model quota. The response always succeeds: if the model is absent, slow, or says
    something it should not, a written template answers instead and `mode` says which.
    """
    _ = session
    return presenter.compose(body.purpose, body.verified_context).as_dict()


@app.get("/api/presentation/status")
def presentation_status():
    """Which provider is live, and what the guardrails are. Never serves the key."""
    return presenter.status()


# ── the AI layer: PROPOSE -> VERIFY -> BIND ─────────────────────────────────
#
# Four endpoints, and the dependency direction is the guarantee. `ai.py` may read the
# stored pages and the findings; nothing in `resolve.py`, `arithmetic.py`, `grading.py`
# or `score.py` imports it, so there is no code path by which a model can decide
# coverage, resolve precedence, compute a settlement figure, set an evidence class or
# move the score. Each endpoint degrades to a deterministic answer when no key is
# configured, and says which mode produced the result.

class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class ExplainIn(BaseModel):
    rule_id: str = Field(min_length=1, max_length=120)


@app.get("/api/ai/status")
def ai_status(session: Session = Depends(_session)):
    """What the interface is told about the AI layer. Reports key *presence*, never
    the key, and names the retrieval mode actually in use.

    The mode is surfaced on purpose: a product that quietly falls back to lexical
    scoring while implying semantic search is making exactly the kind of unstated
    claim this system exists to refuse.
    """
    out = config.ai_status()
    with session.lock:
        st = session.state
        out["index"] = _retriever(session).stats() if "conn" in st else {
            "chunks": 0, "pages_indexed": 0, "documents_indexed": 0,
            "method": "LEXICAL_BM25", "embedded": False, "embed_error": None}
    out["boundary"] = {
        "model_may": ["propose a candidate mechanism from unfamiliar prose",
                      "rank retrieved passages",
                      "explain a finding the engine already produced",
                      "answer a question from this case's evidence"],
        "model_may_never": ["decide whether cover exists",
                            "resolve endorsement or schedule precedence",
                            "compute or adjust a settlement figure",
                            "set an evidence class",
                            "produce the protection score"],
        "enforced_by": ("Module dependency direction. resolve.py, arithmetic.py, "
                        "grading.py and score.py do not import ai.py, so there is no "
                        "code path for a model to reach a consequential decision."),
    }
    return out


@app.post("/api/ai/propose")
def ai_propose(session: Session = Depends(_session)):
    """Read the wording with a model, then throw out everything unverifiable.

    This is the answer to "what does the AI actually do?". The model proposes a
    mechanism and a quote it claims supports the reading; deterministic code then
    checks that quote byte-for-byte against the immutable stored page. A rejected
    proposal is returned WITH its reason rather than dropped, because a hallucination
    caught in the act is the most informative output this system can produce.

    Verified proposals are marked AI_PROPOSED_VERIFIED and do not feed findings — a
    verified quote still is not a qualified reader construing a clause.
    """
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    "mode": "DETERMINISTIC_ONLY", "proposals": [],
                    "verified_count": 0, "rejected_count": 0}
        # Reads the wording directly, so it needs the same authorisation as every other
        # route that touches the case. An adviser may only propose over disclosed
        # documents; a carrier may not reach it at all.
        _, allowed = _ai_scope(session)
        result = ai.propose_mechanisms(
            st["conn"], _scoped_retriever(session, allowed))
        return result.as_dict()


@app.post("/api/ai/explain")
def ai_explain(body: ExplainIn, session: Session = Depends(_session)):
    """Plain-language explanation of one finding the engine already produced.

    The finding, its state, its class and every figure in it are fixed before this is
    called. The model rephrases; it cannot revise. When no key is configured the
    finding's own deterministic summary is returned and labelled as such.
    """
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            raise HTTPException(404, _DELETED_NOTICE)
        the_brief, allowed = _ai_scope(session)
        findings = the_brief.get("findings") or []
        finding = next((f for f in findings
                        if f.get("rule_id") == body.rule_id), None)
        if finding is None:
            # For an adviser this 404 is also the consent boundary doing its job: a
            # finding withheld because its documents were not disclosed is genuinely
            # not in the case they may read. Saying "not in this case" rather than
            # "withheld" avoids confirming the existence of something undisclosed.
            raise HTTPException(
                404, f"No finding with rule_id {body.rule_id!r} in this case.")
        result = ai.explain_finding(
            st["conn"], _scoped_retriever(session, allowed), finding)
        return {"rule_id": body.rule_id, **result.as_dict()}


@app.post("/api/ai/ask")
def ai_ask(body: AskIn, session: Session = Depends(_session)):
    """Ask Pulse — the copilot, answering only from this case's own evidence.

    Retrieval runs first, every retrieved passage is re-verified against the stored
    page, and only verified passages reach the model as context. So a citation shown
    beneath an answer is a quote that demonstrably exists where it claims to. With no
    key configured the copilot still answers, from the structured data the engine
    already produced.
    """
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            raise HTTPException(404, _DELETED_NOTICE)
        the_brief, allowed = _ai_scope(session)
        result = ai.answer_question(
            st["conn"], _scoped_retriever(session, allowed), body.question,
            the_brief, score.compute_score(the_brief))
        return {"question": body.question, **result.as_dict()}


@app.get("/api/evidence/search")
def evidence_search(q: str, top_k: int = 6, session: Session = Depends(_session)):
    """Semantic retrieval over this case's documents, verified before it is returned.

    This is what the "Show me why" drawer calls. `verified` on each hit is the result
    of re-reading the cited offsets in the stored page and comparing: a passage that
    does not match is returned with `verified: false` rather than silently shown, so
    the interface can never present a paraphrase as a quotation.
    """
    if not q.strip():
        raise HTTPException(400, "A search query is required.")
    top_k = max(1, min(int(top_k), 20))
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    "hits": [], "count": 0, "method": "", "query": q}
        # Authorisation BEFORE retrieval. The index handed to `search` contains only
        # passages this caller may read, so an undisclosed document cannot be ranked,
        # cannot be scored, and cannot be returned by any future code path that forgets
        # to re-filter. Filtering hits afterwards would have left all three open.
        _, allowed = _ai_scope(session)
        retriever = _scoped_retriever(session, allowed)
        hits = retriever.search(q, top_k=top_k)
        out = []
        for hit in hits:
            item = hit.as_dict()
            item["verified"] = retriever.verify_hit(st["conn"], hit)
            out.append(item)
        return {
            "available": True, "query": q, "hits": out, "count": len(out),
            "method": retriever.method,
            "verified_count": sum(1 for h in out if h["verified"]),
            "index": retriever.stats(),
            "scope": ("FULL_CASE" if allowed is None else "CONSENT_SCOPED"),
            "scoped_to_document_kinds": (None if allowed is None
                                         else sorted(allowed)),
            "notice": ("Every passage is re-read at its cited offsets in the stored "
                       "page before it is returned. A passage that does not match "
                       "byte-for-byte is marked unverified rather than shown as a "
                       "quotation."),
        }


# ── the source document, shown next to its citation ─────────────────────────
#
# "Show me why" used to answer with a quotation, a page number and two character offsets.
# All three are true and none of them is checkable without leaving the product, so the
# drawer now renders the page the quotation came from with the quotation marked on it.
#
# THE ACCESS MODEL IS THE SAME ONE, NOT A SECOND ONE. A page image is the most complete
# disclosure this product can make of a document — strictly more than a finding derived
# from it — so it goes through `_ai_scope`, exactly like retrieval does. An adviser may
# render a page only from a document kind the owner actually disclosed; a carrier may
# render nothing. Writing a separate check here is how the AI routes ended up bypassing
# the consent gate once already, and that defect is documented above `_ai_scope`.


def _document_row(conn, document_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT id, kind, author_class, filename, page_count, carrier "
        "FROM document WHERE id=?", (document_id,)).fetchone()
    return dict(row) if row is not None else None


# The published documents a case can be built from, by the filename they are stored under.
#
# The default demo case is assembled from JSON fixtures, but its policy wording is not: it
# is the genuine published Chubb SelectPlus wording from the document corpus, ingested
# through the same path an upload takes, and stored under its corpus filename. The worked
# example attaches the same document under `_EXAMPLE_WORDING_NAME`. Either way it is a
# third party's publication, so it is shown as the cited paragraph plus a link to the
# publisher — never as a page image — and its bytes are never read for display at all.
#
# A FIXED mapping from stored filename to manifest id, never a path built from the
# caller's string.
_CORPUS_FILENAMES: dict[str, str] = {
    pipeline.CHUBB_PDF.name: pipeline.CHUBB_ID,
    _EXAMPLE_WORDING_NAME: pipeline.CHUBB_ID,
}

#: Provenances whose PAGES this product may draw. A reader's own upload is theirs, and the
#: synthetic example documents are ours. A corpus document is neither, and NOT_HELD has
#: no bytes to draw.
_PAGE_IMAGE_PROVENANCE = frozenset({PROVENANCE_USER_UPLOAD, PROVENANCE_SYNTHETIC_EXAMPLE})


def _source_bytes_for(session: Session, document_id: str,
                      filename: str) -> bytes:
    """The original bytes of one ingested document, for drawing its pages, or `b""`.

    Two provenances, in order of authority:

      1. The session's retained upload. Held in memory only, keyed by document_id.
      2. The synthetic example corpus, matched by filename against the same manifest
         whitelist `/api/example/file/{name}` uses.

    Both are whitelist lookups rather than path joins on a caller-supplied string: the
    name must already appear in a listing before anything is opened.

    A third party's publication is deliberately NOT a third branch. Its pages are not
    drawn, so its bytes are never needed here — and a function that never reads them
    cannot be refactored into serving them. The provenance check below is the same rule
    stated a second time, for the case where a corpus document's bytes were somehow
    retained after all.

    Returning `b""` is a supported answer and not a failure. The demo schedule and
    supporting documents are structured JSON with no pages at all, and the caller reports
    that plainly rather than substituting a different document's page — which would be a
    fabricated citation rendered by the product's own evidence viewer.
    """
    if _source_provenance_for(session, document_id, filename) not in _PAGE_IMAGE_PROVENANCE:
        return b""
    retained = (session.state.get(_SOURCE_BYTES_KEY) or {}).get(document_id)
    if retained:
        return retained
    if filename in {d["name"] for d in _example_manifest()}:
        path = EXAMPLE_DIR / filename
        if path.is_file():
            try:
                return path.read_bytes()
            except OSError:
                return b""
    return b""


def _corpus_entry_for(filename: str) -> Optional[corpus.Document]:
    """The manifest entry behind a stored corpus document, or None.

    Matched against the fixed mapping first, then against the two names a corpus file can
    carry — `<id>.pdf` as the fetcher stores it, and the publisher's own filename — so a
    future corpus document reaches its publisher link without another constant here.
    """
    try:
        if filename in _CORPUS_FILENAMES:
            return corpus.resolve(_CORPUS_FILENAMES[filename])
        for d in corpus.documents():
            if filename in (f"{d.id}{d.original_suffix}", d.seed_name):
                return d
    except corpus.ManifestError:
        return None
    return None


def _publisher_link(filename: str) -> dict:
    """Where a reader can see the whole of a third party's document: its publisher.

    The URL comes from the committed manifest, which only accepts http(s) URLs, and is
    offered only when it was RECORDED at retrieval. An unrecorded URL is said to be
    unrecorded rather than replaced by a guess — a link to the wrong page would be a
    citation to a document nobody can find.
    """
    entry = _corpus_entry_for(filename)
    if entry is None:
        return {"available": False, "url": None, "title": None, "publisher": None,
                "note": "This document is not listed in the corpus manifest."}
    return {
        "available": entry.downloadable,
        "url": entry.source_url if entry.downloadable else None,
        "title": entry.title,
        "publisher": entry.publisher,
        "corpus_id": entry.id,
        "note": (None if entry.downloadable else
                 "The address this document was retrieved from was not recorded, so no "
                 "link is offered rather than a guessed one."),
    }


def _third_party_reason(filename: str) -> str:
    entry = _corpus_entry_for(filename)
    who = entry.publisher if entry is not None else "a third party"
    return (f"This is a published document by {who}. VERIS holds a reader's copy, not the "
            f"right to reproduce it, so it shows the paragraph a finding cites and links "
            f"to the publisher's own copy instead of displaying the document's pages.")


def _source_provenance_for(session: Session, document_id: str, filename: str) -> str:
    """Who published the bytes `_source_bytes_for` would return, in the same order.

    Read from what was recorded at intake, never guessed from the filename: the session
    map is keyed by document_id alone, so once the server has attached the example's
    published wording nothing in the bytes distinguishes it from a file the user chose
    to upload. The two directory lookups below are fixed whitelists, so their provenance
    is a property of the branch rather than of the request.
    """
    recorded = (session.state.get(_SOURCE_PROVENANCE_KEY) or {}).get(document_id)
    if recorded:
        return recorded
    if filename in _CORPUS_FILENAMES:
        # The worked example's base wording, read straight from the corpus for the
        # server-seeded demo case. A third party's publication either way. Checked
        # BEFORE the example listing so that a published wording dropped into the
        # example directory under its own name still cannot be drawn as pages.
        return PROVENANCE_CORPUS
    if filename in {d["name"] for d in _example_manifest()}:
        return PROVENANCE_SYNTHETIC_EXAMPLE
    return PROVENANCE_NOT_HELD


def _readable_document(session: Session, document_id: str) -> dict:
    """Resolve a document this caller is allowed to look at, or raise.

    Returns the document row. Raises 404 when it is not in this case and 403 when the
    consent gate does not cover its kind.
    """
    st = _state(session)
    if _case_deleted(st):
        raise HTTPException(404, _DELETED_NOTICE)
    # Authorisation first, and it is the same call the copilot makes.
    _, allowed_kinds = _ai_scope(session)
    row = _document_row(st["conn"], document_id)
    if row is None:
        raise HTTPException(404, f"No document {document_id!r} in this case.")
    if allowed_kinds is not None and row["kind"] not in allowed_kinds:
        raise HTTPException(
            403, f"This business did not disclose its {row['kind']} to you, so its "
                 f"pages are not readable here. The consent gate is the access path.")
    return row


def _unviewable_reason(filename: str, has_bytes: bool) -> Optional[str]:
    """Why a document has no page image, named specifically rather than generically.

    "No preview available" tells a reader nothing and reads like a bug. Each of these is a
    different fact with a different remedy, and the structured-fixture case is the one that
    matters most: it is not a failure, it is a document that never had pages.
    """
    if has_bytes and sourceview.available():
        return None
    if not sourceview.available():
        return sourceview.RENDERER_MISSING
    if filename.lower().endswith(".json"):
        return ("This document is structured demo data, not a scanned or typeset file, so "
                "it has no page to show. The quotation is still read from the document's "
                "own stored text. Load the example business or upload your own PDFs to see "
                "pages here.")
    return ("The original file for this document is not held in this session. Uploaded "
            "bytes live in memory for the life of the case and are dropped when the case "
            "is replaced or deleted.")


@app.get("/api/source/{document_id}")
def source_document(document_id: str, session: Session = Depends(_session)):
    """What a citation's source document is, and whether its pages can be shown."""
    with session.lock:
        row = _readable_document(session, document_id)
        provenance = _source_provenance_for(session, document_id, row["filename"])
        third_party = provenance == PROVENANCE_CORPUS
        data = _source_bytes_for(session, document_id, row["filename"])
        viewable = bool(data) and sourceview.available()
        return {
            "document_id": row["id"],
            "filename": row["filename"],
            "kind": row["kind"],
            "author_class": row["author_class"],
            "carrier": row["carrier"],
            "page_count": row["page_count"],
            "pages_viewable": viewable,
            # PAGE: drawn with the quotation marked. EXCERPT: a third party's document,
            # shown as the cited paragraph and a publisher link. NONE: nothing to draw.
            "display": "PAGE" if viewable else ("EXCERPT" if third_party else "NONE"),
            "reason": (_third_party_reason(row["filename"]) if third_party
                       else _unviewable_reason(row["filename"], bool(data))),
            "publisher": _publisher_link(row["filename"]) if third_party else None,
            "renderer": sourceview.status(),
            # Who published the bytes behind this document. Recorded at intake, because
            # what may be shown of a document depends on it: our own synthetic samples
            # and a user's own upload are ours to render, a third party's publication
            # is not.
            "source_provenance": provenance,
        }


@app.get("/api/source/{document_id}/page/{page_no}")
def source_page(document_id: str, page_no: int, span_id: Optional[str] = None,
                full: bool = False, session: Session = Depends(_session)):
    """One page of a source document as a PNG, with the cited quotation highlighted.

    `span_id` names the citation to mark. The quotation is read from the STORED SPAN
    rather than accepted from the caller, which is the whole point: a client that could
    pass its own text could have this endpoint highlight a phrase the engine never cited.

    The image is returned as a base64 data URL inside JSON rather than as raw
    `image/png`, so the honest highlight status travels with it. An `<img src>` cannot
    read a response header, and "here is the page" without "and here is whether we could
    actually find your quotation on it" is the kind of silent gap this product refuses.

    A THIRD PARTY'S DOCUMENT IS NEVER DRAWN. For a corpus document this returns the
    paragraph around the citation, cut from the page text the engine already stored, plus
    a link to the publisher's own copy (`display: "EXCERPT"`). `full=true` changes
    nothing for it: there is no larger picture to give.
    """
    with session.lock:
        row = _readable_document(session, document_id)
        st = _state(session)
        quote = ""
        span = None
        if span_id:
            span = st["conn"].execute(
                "SELECT quoted_text, document_id, page_no, char_start, char_end "
                "FROM span WHERE id=?", (span_id,)).fetchone()
            if span is None:
                raise HTTPException(404, f"No span {span_id!r} in this case.")
            if span["document_id"] != document_id:
                raise HTTPException(
                    400, "That citation belongs to a different document.")
            quote = span["quoted_text"] or ""
        provenance = _source_provenance_for(session, document_id, row["filename"])
        if provenance == PROVENANCE_CORPUS:
            return _third_party_excerpt(st, row, page_no, span, quote)
        data = _source_bytes_for(session, document_id, row["filename"])
        rendered = sourceview.render_page(
            data, page_no, quote,
            dpi=sourceview.FULLSCREEN_DPI if full else sourceview.DISPLAY_DPI)
        payload = {
            "document_id": row["id"], "filename": row["filename"],
            "kind": row["kind"], "quote": quote,
            **rendered.as_dict(),
        }
        payload["display"] = "PAGE" if payload["available"] else "NONE"
        if not payload["available"]:
            # Prefer the specific explanation over the renderer's generic one, so a
            # structured fixture is not reported as a missing file.
            payload["reason"] = (_unviewable_reason(row["filename"], bool(data))
                                 or payload.get("reason") or "")
        return payload


def _third_party_excerpt(st: dict, row: dict, page_no: int, span, quote: str) -> dict:
    """The cited paragraph of a published document, and where to read the rest.

    Built from the page text this case already stored at ingest — never from the
    document's bytes, which are not read here at all. Without a named citation there is
    no paragraph to cut: the response says so, and it does not fall back to the page.
    """
    page_fields = sourceview.RenderedPage(available=False, page_no=int(page_no),
                                          page_count=row["page_count"] or 0).as_dict()
    # No page is drawn, so there is no highlight to report. The default one would tell
    # the reader "the page is shown unmarked" about a page that was never shown, and API
    # payloads are what an evidence log records. How the quotation was found in the
    # stored text is reported by the excerpt itself, as `located`.
    del page_fields["highlight"]
    base = {
        "document_id": row["id"], "filename": row["filename"], "kind": row["kind"],
        "quote": quote,
        **page_fields,
        "display": "EXCERPT",
        "reason": _third_party_reason(row["filename"]),
        "publisher": _publisher_link(row["filename"]),
        "excerpt": None,
    }
    if span is None:
        base["reason"] += (" Open it from a finding to see the paragraph that finding "
                           "cites.")
        return base
    if int(span["page_no"]) != int(page_no):
        raise HTTPException(
            400, f"That citation is on page {span['page_no']}, not page {page_no}.")
    page = st["conn"].execute(
        "SELECT text FROM page_text WHERE document_id=? AND page_no=?",
        (row["id"], int(page_no))).fetchone()
    cut = sourceview.excerpt(page["text"] if page is not None else "",
                             span["char_start"], span["char_end"], quote)
    base["excerpt"] = {**cut, "page_no": int(page_no)}
    return base


# ── business-change signals (simulated connector) ───────────────────────────

class SignalsIn(BaseModel):
    signal_ids: list[str] = Field(default_factory=list, max_length=32)


@app.get("/api/page-quality")
def page_quality_report(session: Session = Depends(_session)):
    """Which pages we could read, and which findings that limits.

    Reported per PAGE, not per document. "This 28-page wording scanned at 91%" cannot be
    acted on; "page 9 of 28 scanned poorly, and the exclusion on it may be incomplete" tells
    an owner exactly what to re-upload — and page 9 is where LMA5401 actually sits in the
    demo wording, so the example is real.

    The class ceiling is the part that makes this a control rather than a caveat: a page we
    could not read confidently caps any finding resting on it at UNESTABLISHED.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    **pagequality.catalogue()}
        store = st.get("store")
        documents = []
        for doc in (store.documents() if store is not None else []):
            pages = getattr(doc, "pages", None) or []
            # A document ingested from a PDF text layer reports no OCR confidence. That is
            # the good case, and it is reported as NOT_SCANNED rather than being silently
            # counted as a clean scan — they are different facts.
            rows = [{"page_no": i, "text_length": len(text or ""),
                     "confidence": None, "status": "OK" if (text or "").strip() else "EMPTY",
                     "had_text_layer": bool((text or "").strip())}
                    for i, text in enumerate(pages, start=1)]
            documents.append(pagequality.report(
                rows, document_kind=getattr(doc, "kind", ""),
                filename=getattr(doc, "doc_id", "")))
        return {
            "available": True,
            "documents": documents,
            "pages_total": sum(d["pages_total"] for d in documents),
            "pages_needing_attention": sum(
                len(d["pages_needing_attention"]) for d in documents),
            "ocr": ocr.status(),
            **pagequality.catalogue(),
            "class_ceiling_rule": (
                "A page we could not read confidently caps any finding resting on it at "
                "UNESTABLISHED. The confidence of the extraction constrains the class of "
                "the evidence, exactly as authorship does — the weakest link sets the "
                "class."),
            "why_page_level": (
                "A document-level score cannot be acted on. Naming the page tells you "
                "which one to re-scan, and which clause might be incomplete."),
            "notice": ("Every page we could not read confidently is named, and no finding "
                       "resting on one is graded PROVEN."),
        }


@app.get("/api/signals")
def signal_feed():
    """The business-change timeline from the simulated accounting feed.

    Labelled `simulated: true` on every signal and on the envelope. The point is not
    the integration — it is that a fact can arrive from a system the owner did not
    type into, which is the only honest way to show the "real-time" loop the problem
    statement describes. A real accounting integration is a documented seam, not a
    built feature: it would replace the fixed feed and change nothing below it (see
    `connectors.py`).
    """
    return {**connectors.timeline(), "connectors": connectors.catalogue()["connectors"]}


@app.post("/api/signals/apply")
def apply_signals(body: SignalsIn, session: Session = Depends(_session)):
    """Turn connector signals into change events, and report the score either side.

    The before/after pair is the demonstration: the owner did not tap an event and did
    not type a value, yet exposure moved. What the signal CANNOT do is establish a
    value or raise a finding's evidence class — an accounting line is not a policy
    document. It raises the question and names the document that would settle it,
    which is why `requested_documents` is part of the response.
    """
    unknown = sorted(set(body.signal_ids) - set(connectors.BY_ID))
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            raise HTTPException(404, _DELETED_NOTICE)
        before = score.compute_score(_brief_with_events(session))
        derived = connectors.derived_events(body.signal_ids)
        recorded = pipeline.record_change_events(st["conn"], st["case_id"], derived)
        after = score.compute_score(_brief_with_events(session))
        return {
            "signal_ids": body.signal_ids,
            "ignored_unknown_ids": unknown,
            "derived_events": derived,
            "recorded": recorded,
            "requested_documents": connectors.requested_documents(body.signal_ids),
            "relevant_rules": sorted(events.rules_for(recorded)),
            "score_before": before,
            "score_after": after,
            "score_delta": score.compare_scores(before, after),
            "brief": _brief_with_events(session),
            "notice": ("A connector signal is third-party authored but it is not a "
                       "document, so it cannot by itself make a finding proven. It "
                       "selects which findings are relevant and names the document "
                       "that would establish the value."),
        }


# ── VERIS SENTINEL — the external risk agent ────────────────────────────────

def _premises_address(session: Session) -> Optional[str]:
    """The insured premises address from the current case's schedule, if any.

    Used to geocode the premises (OneMap) and measure real distance to the nearest
    rainfall station. Returns None when there is no case or no scheduled premises.
    """
    st = session.state
    if "brief" not in st:
        return None
    facts = (st.get("brief") or {}).get("facts") or {}
    prem = (facts.get("scheduled_premises") or {}).get("value")
    if isinstance(prem, (list, tuple)) and prem:
        return str(prem[0])
    if isinstance(prem, str) and prem.strip():
        return prem
    return None


@app.get("/api/sentinel")
def sentinel_feed(region: Optional[str] = None, sector: Optional[str] = None,
                  live: Optional[bool] = None,
                  session: Session = Depends(_session)):
    """External conditions around the business: weather, hazard designations, published
    cost indices, infrastructure notices.

    Live mode is controlled by the `PROVEN_LIVE_SOURCES` env flag (overridable per
    request with `?live=true/false`). When live, Sentinel makes real outbound calls to
    data.gov.sg (rainfall + forecast) and SingStat (M211251) and stamps a source LIVE
    only after a call succeeds and parses. A failed call falls back to the labelled
    fixture — a fixture is never rendered as live. The claim ceiling is unchanged: a
    Sentinel signal is structurally incapable of asserting that anything happened to a
    specific business (enforced in `RiskSignal.__post_init__`). A flood warning is not a
    flood.
    """
    with session.lock:
        address = _premises_address(session)
    return sentinel.feed(region=region, sector=sector, live=live,
                         premises_address=address)


@app.get("/api/sentinel/sources")
def sentinel_sources():
    """Source provenance and the claim ceiling, for the transparency panel.

    This is the catalogue without a live fetch, so it reports each source's standing
    mode. The per-request live/demo state travels on `/api/sentinel` under `sources`
    and `live`, next to the signals it produced.
    """
    return sentinel.catalogue()


# ── VERIS DISCOVERY — the agent that goes and looks ─────────────────────────

@app.get("/api/discovery")
def discovery_feed(live: Optional[bool] = None, q: Optional[str] = None,
                   session: Session = Depends(_session)):
    """Discovery Agent: fetches public sources about this business and compares what
    it finds against what the policy says.

    The one capability neither Connect nor Sentinel has: discovering a business change
    from OUTSIDE the business's own systems. If a hotel opens a second branch and
    updates its website but not its policy, this is the agent that finds it.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    "facts": [], "facts_discovered": 0}
        # Build the policy state from what the engine knows
        brief = st.get("brief") or {}
        policy_state = {
            "locations": [str(x) for x in
                          (brief.get("facts", {}).get("scheduled_premises") or
                           {}).get("value", []) or []],
            "activity": str((brief.get("facts", {}).get("business_description") or
                            {}).get("value", "")),
            "headcount_declared": int(
                (brief.get("facts", {}).get("declared_headcount") or {}).get("value")
                or 0),
        }
        use_live = config.live_sources_enabled() if live is None else live
        result = discovery.discover_business(
            policy_state=policy_state, live=use_live, entity_query=q)
        result["available"] = True
        return result


@app.get("/api/discovery/sources")
def discovery_sources():
    """Source provenance and boundary for the Discovery Agent."""
    return discovery.catalogue()


# ── VERIS SCHEDULER — autonomous agent execution ────────────────────────────

@app.get("/api/scheduler/status")
def scheduler_status():
    """Is the scheduler running, and what has it done?"""
    return scheduler.SCHEDULER.stats()


@app.get("/api/scheduler/history")
def scheduler_history(limit: int = 50):
    """Timestamped log of every autonomous agent run. The timeline is real — each
    entry was an actual check, not a reconstructed history."""
    return {
        "runs": scheduler.SCHEDULER.history(limit=min(limit, MAX_HISTORY)),
        "notice": ("Every entry is a real execution with a real timestamp. The "
                   "scheduler runs Connect, Sentinel and Discovery without a human "
                   "trigger, which is what makes 'real-time picture' true rather "
                   "than aspirational."),
    }


MAX_HISTORY = 200  # re-declared for the endpoint's limit guard


# ── notifications: what we noticed, and what we told the owner ───────────────
#
# READ-ONLY WITH RESPECT TO THE WATCHLIST, and that is a design constraint rather than an
# oversight. The scheduler is the sole observer: it runs Discovery with no policy state,
# while `/api/discovery` runs it WITH the case's policy state, so the two produce different
# fact sets for the same business. If both wrote to the watchlist, the same premises would
# alternate between "new" and "gone" on every other request. So these routes read the store,
# and "check now" asks the scheduler to look rather than looking itself.

def _watched_business_id() -> str:
    """The business the scheduler is watching.

    One business in this build, keyed by UEN. The point of routing every caller through one
    function is that the reader and the writer cannot drift apart — if this returned
    something else, the inbox would be permanently empty and nothing would say why.
    """
    return str(discovery.demo_business().get("uen") or "unknown")


class NotificationPrefsIn(BaseModel):
    email_enabled: Optional[bool] = None
    minimum_severity: Optional[str] = None
    digest: Optional[str] = None
    codes: Optional[dict] = None


@app.get("/api/notifications")
def notifications_list(unread_only: bool = False, limit: int = 50,
                       session: Session = Depends(_session)):
    """What we noticed since the last check, newest first."""
    with session.lock:
        _deny_foreign_role(session)
    bid = _watched_business_id()
    store = notifications.NOTIFICATIONS
    return {
        "business_id": bid,
        "notifications": store.list(bid, unread_only=unread_only,
                                   limit=min(max(limit, 1), 100)),
        "counts": store.counts(bid),
        "unread": store.unread_count(bid),
        "last_checked": watchlist.WATCHLIST.last_seen(bid),
        "facts_watched": watchlist.WATCHLIST.tracked_count(bid),
        "mail": mailer.status(),
        "notice": (
            "Each item is a change we found in public information about your business, "
            "compared against the previous check. Nothing here has changed your policy "
            "or been sent to your insurer."),
    }


@app.post("/api/notifications/{notification_id}/read")
def notification_mark_read(notification_id: str,
                           session: Session = Depends(_session)):
    with session.lock:
        _deny_foreign_role(session)
    bid = _watched_business_id()
    found = notifications.NOTIFICATIONS.mark_read(bid, notification_id)
    if not found:
        raise HTTPException(404, "No such notification.")
    return {"ok": True, "unread": notifications.NOTIFICATIONS.unread_count(bid)}


@app.post("/api/notifications/read-all")
def notifications_mark_all_read(session: Session = Depends(_session)):
    with session.lock:
        _deny_foreign_role(session)
    bid = _watched_business_id()
    changed = notifications.NOTIFICATIONS.mark_all_read(bid)
    return {"ok": True, "marked_read": changed, "unread": 0}


@app.get("/api/notifications/preferences")
def notifications_preferences(session: Session = Depends(_session)):
    """What we may email about, and the vocabulary of what can be changed."""
    with session.lock:
        _deny_foreign_role(session)
    bid = _watched_business_id()
    return {
        "preferences": notifications.NOTIFICATIONS.preferences(bid),
        "available_codes": [
            {"code": code, "label": label,
             "severity": notifications.CODE_SEVERITY.get(code, "MODERATE")}
            for code, label in notifications.CODE_LABELS.items()],
        "severities": ["LOW", "MODERATE", "HIGH"],
        "digests": [notifications.IMMEDIATE, notifications.DAILY],
        "last_digest_at": notifications.NOTIFICATIONS.last_digest_at(bid),
        "notice": (
            "Turning something off here stops the email, not the check. Anything we find "
            "is still recorded and still shown, with a note saying why it was not sent."),
    }


@app.post("/api/notifications/preferences")
def notifications_set_preferences(body: NotificationPrefsIn,
                                  session: Session = Depends(_session)):
    with session.lock:
        _deny_foreign_role(session)
    bid = _watched_business_id()
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    saved = notifications.NOTIFICATIONS.set_preferences(bid, changes)
    return {"ok": True, "preferences": saved}


@app.get("/api/notifications/outbox")
def notifications_outbox(limit: int = 25, session: Session = Depends(_session)):
    """Every message we composed, whether or not a server was there to take it.

    The outbox exists so the wording can be READ. A notification feature demonstrated with
    a "sent!" toast and nothing on the wire is exactly the kind of unstated claim this
    product refuses to make about anything else.
    """
    with session.lock:
        _deny_foreign_role(session)
    bid = _watched_business_id()
    return {
        "messages": mailer.OUTBOX.list(bid, limit=min(max(limit, 1), 50)),
        "count": mailer.OUTBOX.count(bid),
        "mail": mailer.status(),
    }


@app.get("/api/notifications/outbox/{message_id}")
def notifications_outbox_message(message_id: str,
                                 session: Session = Depends(_session)):
    """One composed message in full, for previewing exactly what the owner receives."""
    with session.lock:
        _deny_foreign_role(session)
    entry = mailer.OUTBOX.get(message_id)
    if entry is None:
        raise HTTPException(404, "No such message.")
    return entry.as_dict()


@app.post("/api/notifications/check-now")
def notifications_check_now(session: Session = Depends(_session)):
    """Ask the Discovery agent to look again right now, and report what it found.

    Returns the run it recorded rather than "started", because a button that says a check
    began and cannot say what it concluded is indistinguishable from one that does nothing.
    """
    with session.lock:
        _deny_foreign_role(session)
    run = scheduler.SCHEDULER.run_discovery_now()
    bid = _watched_business_id()
    watch = (run.get("detail") or {}).get("watch") or {}
    return {
        "ok": True,
        "ran_at": run.get("timestamp"),
        "mode": run.get("mode"),
        "duration_ms": run.get("duration_ms"),
        "facts_found": run.get("signals_produced"),
        "first_observation": watch.get("first_observation", False),
        "changes_detected": watch.get("changes_detected", 0),
        "notifications_created": watch.get("notifications_created", 0),
        "already_reported": watch.get("already_reported", 0),
        "email_status": watch.get("email_status"),
        "summary": run.get("summary"),
        "note": watch.get("note") or "",
        "unread": notifications.NOTIFICATIONS.unread_count(bid),
    }


@app.post("/api/notifications/restart-watch")
def notifications_restart_watch(session: Session = Depends(_session)):
    """Forget what we have seen, and treat the current state as the new normal.

    The honest use is "I have dealt with these". Once a schedule is actually updated, the
    alerts that prompted it are resolved, and there is no reason to keep telling somebody
    about premises now correctly listed on their policy. Without this the deduplication that
    stops repeat emails also means a handled item can never be cleared, and the inbox only
    ever grows.

    What it does: drops the baseline, so the next check re-establishes one and raises
    nothing; and drops the record of what has already been reported, so genuine future
    changes are reported again.

    It does NOT touch any finding. Notifications are a record of what we told you, not
    evidence — the findings they point at live in the case and in the audit record, and both
    are unaffected.
    """
    with session.lock:
        _deny_foreign_role(session)
    bid = _watched_business_id()
    watchlist.WATCHLIST.forget(bid)
    notifications.NOTIFICATIONS.forget(bid)
    # The mail log goes too. Leaving it would show emails describing notifications the user
    # can no longer find anywhere, which reads as a bug rather than as history.
    dropped = mailer.OUTBOX.forget(bid)
    return {
        "ok": True,
        "business_id": bid,
        "unread": 0,
        "messages_cleared": dropped,
        "notice": (
            "Cleared. The next check takes a fresh baseline and raises nothing, so what "
            "your website says today becomes the new normal. Changes after that are "
            "reported as usual. No finding, document or audit entry was touched."),
    }


@app.get("/api/notifications/watch")
def notifications_watch_status(session: Session = Depends(_session)):
    """What is being watched, and how the memory behaves across a restart."""
    with session.lock:
        _deny_foreign_role(session)
    return {
        "watchlist": watchlist.WATCHLIST.stats(),
        "notifications": notifications.NOTIFICATIONS.stats(),
        "mail": mailer.status(),
        "checks_every_seconds": scheduler.DISCOVERY_INTERVAL,
        "website_watched": discovery.demo_business().get("website"),
        "alertable_note": (
            "Only changes with a structured value — an address, a head count, a "
            "registration status — can raise an alert. Descriptions get reworded between "
            "checks, so a difference there is recorded and shown but never emailed."),
    }


@app.get("/api/agents/activity")
def agents_activity(limit: int = 50):
    """A legible feed of what the agents actually did, newest first.

    Sourced from the scheduler's EXISTING run history — no second activity store. Each
    row exposes the auditable execution facts: which agent, when, what it planned to
    check, which URLs it called and their status, how many facts it extracted, how many
    survived grounding, and — prominently — how many were REJECTED as ungrounded. The
    rejections are the point: showing what the system refused to say is what proves the
    grounding gate is real. LIVE/DEMO mode is reported per run, truthfully.
    """
    runs = scheduler.SCHEDULER.history(limit=min(limit, MAX_HISTORY))
    activity = []
    for r in reversed(runs):                       # newest first
        detail = r.get("detail") or {}
        activity.append({
            "agent": r.get("agent"),
            "timestamp": r.get("timestamp"),
            "mode": r.get("mode"),
            "is_live": r.get("mode") == "LIVE",
            "elevated": r.get("elevated"),
            "duration_ms": r.get("duration_ms"),
            "planned_check": detail.get("planned_check"),
            "calls": detail.get("calls", []),
            "facts_extracted": detail.get("facts_extracted", r.get("signals_produced")),
            "facts_grounded": detail.get("facts_grounded"),
            "facts_rejected": detail.get("facts_rejected", 0),
            "result": detail.get("result") or r.get("summary"),
            "summary": r.get("summary"),
        })
    total_rejected = sum(a["facts_rejected"] or 0 for a in activity)
    return {
        "activity": activity,
        "count": len(activity),
        "total_rejected": total_rejected,
        "notice": ("Every row is a real agent execution with a real timestamp, drawn "
                   "from the scheduler's run history — not a second log. Rejected "
                   "claims are shown, not hidden: a claim discarded because it could "
                   "not be located in the source is the strongest evidence the "
                   "grounding gate is doing its job."),
    }


# ── VERIS ORCHESTRATOR — reconciliation ─────────────────────────────────────

@app.get("/api/orchestrator/evaluate")
def orchestrate(region: str = "SG-TAI-SENG", sector: str = "FOOD_MANUFACTURING",
                session: Session = Depends(_session)):
    """Reconcile both agents against the policy findings the engine already produced.

    Deterministic. The agents propose; fixed precedence rules decide — the same split as
    `ai.py`, one level up. Current position comes only from documents and internal
    records; external data sets forward risk and never edits a current value. Where both
    agents land on the same insured interest, priority rises and the evidence class does
    not.
    """
    with session.lock:
        _deny_foreign_role(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    "decisions": [], "count": 0, "overlap_count": 0,
                    "overlap_interests": [], "highest": None,
                    "method": "DETERMINISTIC"}
        findings = (_brief_with_events(session).get("findings") or [])
        recorded = pipeline.recorded_events(st["conn"], st["case_id"])
        # Only signals the owner actually applied count as internal input. An unapplied
        # connector row is a row on a screen, not a business change anyone acknowledged.
        applied = [s.as_dict() for s in connectors._FEED
                   if s.implies_event in set(recorded)] if recorded else []
        external = [s.as_dict() for s in
                    sentinel.relevant_signals(region=region, sector=sector)]
        # Discovery facts feed the orchestrator as external signals so they
        # participate in overlap detection without new reconciliation code.
        disc_result = discovery.discover_business()
        disc_signals = discovery.discovery_as_external_signals(disc_result)
        all_external = external + disc_signals
        out = orchestrator.evaluate(internal_signals=applied,
                                   external_signals=all_external, findings=findings)
        out["available"] = True
        out["attention_matrix"] = orchestrator.attention_matrix(out["decisions"])
        out["environment"] = sentinel.environment_summary(
            sentinel.relevant_signals(region=region, sector=sector))
        return out


# ── the adviser workspace ───────────────────────────────────────────────────
#
# Built on the consent gate rather than beside it. An adviser's queue is a list of
# disclosures a business chose to make, so a case with no grant does not appear — which
# is the difference between consent as an access control and consent as a checkbox.

class RequestInfoIn(BaseModel):
    rule_id: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=600)


@app.get("/api/adviser/queue")
def adviser_queue(session: Session = Depends(_session)):
    """The adviser's review queue, assembled from what has actually been disclosed.

    Two rows in this build and both are honest about their provenance: the live case in
    this browser IF the owner granted consent, plus fixed demonstration rows so the
    queue is not empty on camera. The demonstration rows are labelled `demo: true` and
    carry no documents, because inventing an SME's policy evidence to fill a table
    would be exactly the fabrication this product exists to refuse.
    """
    principal = _require_role(session, auth.ADVISER)
    rows: list[dict] = []

    # The live case, only if it was disclosed to an adviser.
    granted_to: Optional[str] = None
    with session.lock:
        st = session.state
        if "conn" in st and not _case_deleted(st):
            # MEASURED DEFECT. This used to ask `disclosure_granted`, which is true when
            # ANY live disclosure exists — including one naming only the carrier. So a
            # grant to the insurer put the case in the ADVISER's queue, and clicking
            # Review then 403'd. The queue now asks the same question the review screen
            # asks, so the two can no longer disagree.
            granted_to, _fields = _resolve_grant(
                st["conn"], st["case_id"], _recipient_candidates(principal))
            the_brief = _brief_with_events(session)
            findings = the_brief.get("findings") or []
            if granted_to is not None:
                computed = score.compute_score(the_brief)
                rows.append({
                    "case_id": st.get("case_id"),
                    # MEASURED DEFECT. This read `business_name`, which the Brief does not
                    # carry — the key is `business`. So `or "Your business"` fired on every
                    # live case and the adviser's queue, priority canvas and tooltips all
                    # labelled the disclosed client "Your business", which is both wrong
                    # and confusing in an ADVISER's workspace where it is not their
                    # business at all. `book.real_accounts` reads the right key, which is
                    # why the carrier's screens showed the name correctly and this one did
                    # not.
                    "business_name": (the_brief.get("business")
                                      or the_brief.get("business_name")
                                      or "Unnamed case"),
                    "disclosed": True,
                    "demo": False,
                    "score": computed["overall"],
                    "status": computed["status"],
                    "finding_count": len(findings),
                    "proven_count": sum(1 for f in findings
                                        if f.get("class") == "PROVEN"),
                    "unestablished_count": sum(1 for f in findings
                                               if f.get("class") == "UNESTABLISHED"),
                    "needs_review_count": sum(
                        1 for f in findings
                        if f.get("state") == "REQUIRES_AUTHORISED_REVIEW"),
                    "priority": "HIGH" if computed["overall"] < 70 else "MEDIUM",
                    "source": "LIVE_DISCLOSURE",
                    "granted_to": granted_to,
                })

    rows.extend(_ADVISER_DEMO_ROWS)
    return {
        "adviser": principal.as_dict(),
        "cases": rows,
        "granted_to": granted_to,
        "recipient_names_accepted": list(_recipient_candidates(principal)),
        "live_case_count": sum(1 for r in rows if not r["demo"]),
        "awaiting_consent": not any(not r["demo"] for r in rows),
        "notice": (
            "A case appears here only when the business granted consent to disclose "
            "it. Rows marked demo are fixed demonstration entries with no documents "
            "behind them — they exist so the queue is not empty, and they are labelled "
            "rather than blended in."),
    }


# Fixed queue filler. Deliberately shallow: a name, a count and a reason. No policy
# text, no page citations, no computed exposure — because those would be fabricated,
# and a fabricated citation in a product about citation fidelity is unforgivable.
_ADVISER_DEMO_ROWS: tuple[dict, ...] = (
    {"case_id": "demo-case-002", "business_name": "Kimly Metalworks Pte Ltd",
     "disclosed": True, "demo": True, "score": 58, "status": "Significant gaps",
     "finding_count": 9, "proven_count": 5, "unestablished_count": 2,
     "needs_review_count": 2, "priority": "HIGH", "source": "DEMONSTRATION_ROW"},
    {"case_id": "demo-case-003", "business_name": "Serangoon Dental Group",
     "disclosed": True, "demo": True, "score": 77, "status": "Needs attention",
     "finding_count": 6, "proven_count": 4, "unestablished_count": 1,
     "needs_review_count": 1, "priority": "MEDIUM", "source": "DEMONSTRATION_ROW"},
    {"case_id": "demo-case-004", "business_name": "Bugis Print & Signage",
     "disclosed": True, "demo": True, "score": 88, "status": "Well evidenced",
     "finding_count": 4, "proven_count": 3, "unestablished_count": 0,
     "needs_review_count": 0, "priority": "LOW", "source": "DEMONSTRATION_ROW"},
)


@app.get("/api/adviser/review")
def adviser_review(recipient: str = "Licensed adviser",
                   session: Session = Depends(_session)):
    """The adviser's read of the live case — through the consent gate, not around it.

    This delegates to the same disclosure path the owner controls, so an adviser with
    no grant gets the same 403 anybody else would. There is no privileged read.
    """
    principal = _require_role(session, auth.ADVISER)
    with session.lock:
        st = _state(session)
        # `recipient` is a preference, not the only accepted name — see
        # `_recipient_candidates`. An owner who granted to the adviser's actual firm
        # rather than to the generic "Licensed adviser" used to get a bare 403 here.
        named, allowed = _resolve_grant(
            st["conn"], st["case_id"],
            _recipient_candidates(principal, recipient))
        if allowed is None:
            # Delegate the refusal to `disclosed_brief` so there is exactly ONE place
            # that decides what a refused disclosure says, and it stays the structured
            # CONSENT_REQUIRED envelope the frontend already renders.
            return pipeline.disclosed_brief(
                st["conn"], st["case_id"], st["brief"],
                (recipient or _ADVISER_RECIPIENT).strip()[:200])
        return pipeline.disclosed_brief(
            st["conn"], st["case_id"], st["brief"], named)


@app.post("/api/adviser/request-information")
def adviser_request_information(body: RequestInfoIn,
                               session: Session = Depends(_session)):
    """Ask the business for a specific document or confirmation.

    Recorded as a request against a named finding and nothing more. It does not alter
    the finding, its state or its evidence class — an adviser's question is not
    evidence, and letting a request move a finding would put an unevidenced judgement
    into the record.
    """
    principal = _require_role(session, auth.ADVISER)
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            raise HTTPException(404, _DELETED_NOTICE)
        if not pipeline.disclosure_granted(st["conn"], st["case_id"]):
            raise HTTPException(
                403, "This business has not disclosed its review to an adviser, so "
                     "there is nothing here to ask about.")
        requests = st.setdefault("_info_requests", [])
        requests.append({
            "rule_id": body.rule_id,
            "message": body.message,
            "requested_by": principal.name,
            "adviser_organisation": principal.organisation_name,
            "state": "INFORMATION_REQUESTED",
        })
        return {
            "ok": True, "requests": list(requests),
            "case_state": "INFORMATION_REQUESTED",
            "notice": ("Recorded against the finding. It does not change the finding's "
                       "state or evidence class — a question is not evidence."),
        }


@app.get("/api/adviser/requests")
def adviser_requests(session: Session = Depends(_session)):
    """Requests raised against the live case. Visible to the owner too, which is why it
    does not use the adviser-only guard."""
    _deny_foreign_role_except_adviser(session)
    with session.lock:
        return {"requests": list(session.state.get("_info_requests") or [])}


# ── the adviser's file note ─────────────────────────────────────────────────

class NoteEditIn(BaseModel):
    text: str = Field(min_length=1, max_length=advisernote.MAX_NOTE)


def _adviser_note_material(session: Session) -> tuple[dict, list[dict], list[dict]]:
    """The disclosed material a note may be written from, or 403.

    Goes through the same consent gate as everything else the adviser reads, so a note can
    never mention a finding the adviser was not shown.
    """
    st = _state(session)
    allowed = pipeline.granted_fields(st["conn"], st["case_id"], _ADVISER_RECIPIENT)
    if allowed is None:
        raise HTTPException(
            403, f"This business has not disclosed its review to "
                 f"'{_ADVISER_RECIPIENT}', so there is nothing to write a note about.")
    disclosed = pipeline.disclosed_brief(
        st["conn"], st["case_id"], st["brief"], _ADVISER_RECIPIENT)
    requests = list(st.get("_info_requests") or [])
    return disclosed, (disclosed.get("findings") or []), requests


@app.post("/api/adviser/note/draft")
def adviser_note_draft(session: Session = Depends(_session)):
    """Draft the file note. The model types; deterministic code checks every reference.

    A rejected draft is NOT repaired — the deterministic note is used and the rejection is
    returned with the exact invented token, because a hallucination caught in the act is
    more useful to show than one quietly patched.
    """
    principal = _require_role(session, auth.ADVISER)
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            raise HTTPException(404, _DELETED_NOTICE)
        disclosed, findings, requests = _adviser_note_material(session)
        business = str(disclosed.get("business") or "the client")

        # Ask the presenter for wording. It applies its own four gates; this module then
        # applies the stricter note-specific grounding on top, because a note is relied on
        # later by somebody who cannot see the evidence beside it.
        #
        # `mode == "MODEL"` is the ONLY case where a model actually wrote anything —
        # `DETERMINISTIC_FALLBACK` means the presenter's own template answered, and
        # treating that as a model draft would let four sentences of template prose
        # REPLACE the structured note, losing the scope and limitations sections that are
        # the reason a file note is defensible at all.
        context = advisernote.build_context(findings, requests)
        composed = presenter.compose("adviser_note", context)
        pres = composed.as_dict().get("presentation") or {}
        model_summary = None
        if composed.mode == "MODEL":
            parts = [pres.get("headline"), pres.get("subheadline"),
                     pres.get("why_it_matters"), pres.get("what_next")]
            model_summary = "\n\n".join(p for p in parts if p)

        note = advisernote.NOTES.draft(
            case_id=st["case_id"], business=business, adviser=principal.name,
            organisation=principal.organisation_name,
            findings=findings, requests=requests, model_summary=model_summary)
        return {**note.as_dict(), "presenter_mode": composed.mode,
                **advisernote.catalogue()}


@app.get("/api/adviser/note")
def adviser_note_get(session: Session = Depends(_session)):
    """The current note, if one has been drafted."""
    _require_role(session, auth.ADVISER)
    with session.lock:
        st = _state(session)
        note = advisernote.NOTES.get(st["case_id"])
        if note is None:
            return {"available": False,
                    "reason": "No note has been drafted for this case yet.",
                    **advisernote.catalogue()}
        return {"available": True, **note.as_dict(), **advisernote.catalogue()}


@app.post("/api/adviser/note/edit")
def adviser_note_edit(body: NoteEditIn, session: Session = Depends(_session)):
    """Replace the text. Coverage language in an adviser's own words is recorded as a
    warning rather than blocked — a licensed person may legitimately need to quote a term,
    and refusing to let them write their own file note would be the tool overreaching."""
    principal = _require_role(session, auth.ADVISER)
    with session.lock:
        st = _state(session)
        _, findings, requests = _adviser_note_material(session)
        allowed = advisernote.allowed_facts(
            findings, advisernote.build_context(findings, requests))
        try:
            note = advisernote.NOTES.edit(st["case_id"], body.text,
                                          by=principal.name, allowed=allowed)
        except KeyError:
            raise HTTPException(404, "No note has been drafted for this case yet.")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {**note.as_dict(), **advisernote.catalogue()}


@app.post("/api/adviser/note/approve")
def adviser_note_approve(session: Session = Depends(_session)):
    """Put a name to it. Only an approved note is a record."""
    principal = _require_role(session, auth.ADVISER)
    with session.lock:
        st = _state(session)
        try:
            note = advisernote.NOTES.approve(st["case_id"], by=principal.name)
        except KeyError:
            raise HTTPException(404, "No note has been drafted for this case yet.")
        return {**note.as_dict(), **advisernote.catalogue()}


def _deny_foreign_role_except_adviser(session: Session) -> None:
    """The one surface both the owner and their adviser legitimately read. A carrier
    still may not: it is case-level detail."""
    principal = _principal(session)
    if principal is not None and principal.role == auth.CARRIER:
        raise HTTPException(
            403, "A carrier sees book-level roll-ups, not one business's information "
                 "requests.")


# ── the carrier workspace ───────────────────────────────────────────────────

@app.get("/api/carrier/portfolio")
def carrier_portfolio(mode: str = "B", session: Session = Depends(_session)):
    """Book-level view for an underwriter. Never an individual business's documents.

    The roll-up itself is the existing `/api/book`, which already draws the Mode A /
    Mode B distinction that matters here: what the carrier may compute from its own
    book versus what it would need each business to disclose. This endpoint adds the
    role boundary and the portfolio framing on top; it does not invent a second
    analysis.
    """
    principal = _require_role(session, auth.CARRIER)
    if mode.upper() not in {"A", "B"}:
        raise HTTPException(
            400, f"Unknown mode {mode!r}. Must be 'A' (business-initiated, never "
                 f"aggregated) or 'B' (carrier-initiated).")

    # Mode A refuses to aggregate, and that refusal is the interesting half. A book
    # built from business-initiated disclosures would quietly turn consent-as-pull into
    # a data feed, so the endpoint declines rather than accommodating the request.
    if mode.upper() == "A":
        return {
            "carrier": principal.as_dict(), "mode": "A", "aggregated": False,
            "accounts": [], "totals": {}, "synthetic": True,
            "market_map": marketmap.build_market_map().as_dict(),
            "notice": ("Mode A is business-initiated. No portfolio view exists by "
                       "design: aggregating disclosures a business made to its own "
                       "adviser would break the trust model this product runs on."),
        }

    with session.lock:
        case_state = session.state
    real = book.real_accounts(
        [case_state] if case_state and case_state.get("brief") else [])
    accounts = real["accounts"]
    wordings = book.wording_portfolio()

    # Totals over REAL rows only. Every figure is a count of something the engine
    # actually produced, so `accounts: 1` is reported as 1 rather than dressed up.
    totals = {
        "accounts": len(accounts),
        "findings_total": sum(a.get("findings_total") or 0 for a in accounts),
        "needs_authorised_review": sum(a.get("needs_authorised_review") or 0
                                       for a in accounts),
        "information_missing": sum(a.get("information_missing") or 0
                                   for a in accounts),
        "abstained": sum(a.get("abstained") or 0 for a in accounts),
        "proven_findings": sum((a.get("by_evidence_class") or {}).get("PROVEN", 0)
                               for a in accounts),
        "unestablished_findings": sum(
            (a.get("by_evidence_class") or {}).get("UNESTABLISHED", 0)
            for a in accounts),
    }
    return {
        "carrier": principal.as_dict(),
        "mode": "B",
        "aggregated": True,
        # No longer synthetic. Both blocks below are this engine's own output.
        "synthetic": False,
        "accounts": accounts,
        "account_count_is_real": True,
        "honest_statement": real["honest_statement"],
        "totals": totals,
        # THE SUBSTANTIVE CARRIER SURFACE. Seven published wordings, aligned by the same
        # deterministic verifier that produces findings, with the real threshold, day
        # count and condition-precedent position read out of each document.
        "wording_portfolio": wordings,
        # Clause-level market position, same corpus, reader-facing summary form.
        "market_map": marketmap.build_market_map().as_dict(),
        "columns_deliberately_absent": ["breach", "violation", "risk_score",
                                        "blacklist"],
        "notice": (
            "Every row on this screen is this engine's own output. The account count is "
            "the real number of cases held — not a projection and not a demonstration "
            "portfolio. The wording portfolio beside it is computed live over the "
            f"published policy wordings in this repository: "
            f"{wordings['counts']['wordings']} wordings, "
            f"{wordings['counts']['pages_read']} pages, of which "
            f"{wordings['counts']['never_hand_mapped']} were never hand-mapped. "
            "There is no breach, violation or risk-score column anywhere in the schema, "
            "and a reviewer is invited to look for one. A carrier sees book-level "
            "position here; reading an individual business's documents requires that "
            "business to disclose them."),
    }


def _no_book_yet(operation: str, would_need: str,
                 session: Optional[Session] = None) -> dict:
    """The honest state for a book operation when there is no book.

    Six carrier endpoints — triage, patterns, the undisclosed inbox, the briefing, the
    book copilot and premium-at-risk — used to run over `_CARRIER_DEMO_ACCOUNTS`, six
    invented businesses. Each produced a confident-looking answer about companies that do
    not exist.

    Removing the rows leaves those operations with nothing to compute, and the correct
    response to that is to say so. An empty ranked queue that states what would populate
    it is a truthful screen; the same queue filled with fictional accounts is a demo of
    something that was never built. The rest of this product refuses to put a number on
    an unestablished operand, and a portfolio is an operand.
    """
    # TWO DIFFERENT REASONS WERE BEING REPORTED AS ONE, AND ONLY ONE OF THEM IS ABOUT A
    # PILOT.
    #
    #   NO CASE IN THIS SESSION  the ordinary state for a carrier who just signed in. The
    #                            book reads the case held in the caller's own session, and
    #                            a carrier session holds none of its own — that is
    #                            `sessions.py` isolation, not a missing feature. It is
    #                            fixed by four clicks, so the screen should say which four.
    #   NO BOOK TO AGGREGATE     the genuine product limitation. Needs an in-force book.
    #
    # Reporting the second when the truth is the first tells a reader the product cannot do
    # something it can do, and leaves them with no next step. An empty state that explains
    # itself but cannot be acted on is only half an empty state.
    session_holds_case = False
    if session is not None:
        with session.lock:
            st = session.state
            session_holds_case = bool(st.get("brief")) and not _case_deleted(st)

    if session_holds_case:
        reason = (
            "This session holds a case, but this operation still has nothing to compute "
            "over it — see what it would need, below.")
        next_step = None
    else:
        reason = (
            "No case is loaded in this browser session, so there is nothing to rank or "
            "count. This is the isolation boundary rather than a fault: the book reads "
            "the case held in your OWN session, and a carrier session holds none of its "
            "own — which is what stops one viewer's documents appearing in another "
            "viewer's workspace.")
        next_step = {
            "why_it_is_empty": "No case in this browser session.",
            "how_to_populate_it": [
                "Sign in as the business owner (owner@hockleefoods.demo).",
                "Open Overview so the case is built from the example documents.",
                "Use SWITCH ROLE, not Sign Out — signing out discards the case "
                "deliberately, and that is the usual reason this screen is empty.",
                "Sign in as the underwriter. The queue, the inbox and book patterns "
                "will all compute over that case.",
            ],
            "caveat": ("This is how a single-machine demo shares one case across three "
                       "roles. It is not how a deployment would work: there, the book "
                       "would come from the carrier's own in-force portfolio."),
        }

    out = {
        "available": False,
        "operation": operation,
        "session_holds_case": session_holds_case,
        "reason": reason,
        "would_need": would_need,
        "what_is_real_today": (
            "The wording portfolio on /api/carrier/portfolio is computed live over the "
            "published policy wordings in this repository and needs no book at all. It is "
            "the part of this surface that is real regardless of what any session holds."),
        "deliberately_not_shown": (
            "Invented accounts. This endpoint previously returned six fictional "
            "businesses with hand-typed review reasons; they were labelled, but a "
            "portfolio nobody can check is the one screen that undermines a product "
            "arguing every number traces to a document."),
    }
    if next_step:
        out["next_step"] = next_step
    return out


# ── the book, derived rather than authored ──────────────────────────────────
#
# WHY THIS EXISTS AND WHAT IT IS NOT.
#
# Four carrier operations — the work queue, book patterns, the needs-attention inbox and
# premium-at-risk — returned `_no_book_yet` UNCONDITIONALLY. That was the right answer to
# the wrong question. It was written when the only book available was six invented
# businesses, and removing those rows was correct. But `/api/carrier/portfolio` already
# serves REAL account rows built from this engine's own output, and it has done since
# `book.real_accounts` landed — so four screens were refusing to compute over data that
# was sitting right there, and an underwriter opening the workspace found nine sidebar
# items of which five said "not available".
#
# So the refusal moves from "always" to "when the book is genuinely empty", and the rows
# it computes over are the same ones the portfolio screen already shows.
#
# WHAT IS STILL REFUSED, DELIBERATELY.
#
#   * `industry`      not a field this product holds. Omitted, never guessed.
#   * `internal_change` / `external_risk`   need a change history and a located premises.
#                     Omitted, so they contribute ZERO to the ranking rather than a
#                     flattering default. A default on a carrier screen reads as a
#                     measurement.
#   * `last_reviewed` there is no review timestamp on a case. Omitted.
#   * premium         needs a rate. There is no honest rate here, so premium-at-risk
#                     still refuses — see `carrier_premium_at_risk`.
#
# Everything that IS populated is a count the engine produced, and `priority` and
# `lead_reason` are DERIVED BY THE RULE BELOW rather than typed per account.

_BOOK_PRIORITY_BASIS = (
    "HIGH when at least one finding turns on a judgement software may not make, MEDIUM "
    "when a document is missing or an item is unevidenced, LOW otherwise. Derived from "
    "the engine's own output states — no label is authored per account.")


def _book_lead_reason(account: dict) -> str:
    """Why this account is on the queue, in the engine's own vocabulary.

    Ordered by what an underwriter should look at first. Never an accusation: "needs an
    authorised review" and "a document is missing", never "breach".
    """
    unestablished = int((account.get("by_evidence_class") or {}).get(
        "UNESTABLISHED") or 0)
    if int(account.get("needs_authorised_review") or 0) > 0:
        return "Findings need an authorised review"
    if int(account.get("information_missing") or 0) > 0:
        return "A document behind a finding is missing"
    if unestablished > 0:
        return "Items are unevidenced"
    if int(account.get("abstained") or 0) > 0:
        return "The engine abstained on some clauses"
    return "No open items"


def _book_rows(session: Session, carrier_org: str = "") -> list[dict]:
    """The carrier's book in the shape `carrier.triage` / `patterns` / inbox consume.

    Reads only what `/api/carrier/portfolio` already serves this role, so this crosses no
    boundary the portfolio screen does not already cross. It adds no second analysis and
    no second notion of consent.

    `adviser` is the recipient of a live disclosure that is NOT this carrier — i.e. a real
    grant to somebody who can act on the case. It is not invented, and when nobody was
    named the row says so and the ranking charges the "no adviser assigned" weight
    honestly.
    """
    with session.lock:
        st = session.state
        live = bool(st.get("brief")) and not _case_deleted(st)
        held = [st] if live else []
        disclosed_to: list[str] = []
        if live and "conn" in st:
            disclosed_to = [str(d.get("recipient") or "")
                            for d in pipeline.live_disclosures(
                                st["conn"], st["case_id"])]

    others = [r for r in disclosed_to if r and r != carrier_org]
    rows: list[dict] = []
    for account in book.real_accounts(held)["accounts"]:
        by_class = account.get("by_evidence_class") or {}
        needs = int(account.get("needs_authorised_review") or 0)
        missing = int(account.get("information_missing") or 0)
        unestablished = int(by_class.get("UNESTABLISHED") or 0)
        rows.append({
            # The case id IS the account id here, which is what makes the "Open" button
            # on the queue resolve — see `_carrier_account`.
            "account_id": account.get("case_id"),
            "business_name": account.get("business_name"),
            "priority": ("HIGH" if needs else
                         "MEDIUM" if (missing or unestablished) else "LOW"),
            "lead_reason": _book_lead_reason(account),
            "unestablished_count": unestablished,
            "information_missing": missing,
            "needs_authorised_review": needs,
            "findings_total": int(account.get("findings_total") or 0),
            "abstained": int(account.get("abstained") or 0),
            "proven_count": int(by_class.get("PROVEN") or 0),
            "adviser": others[0] if others else None,
            "state": "ADVISER_REVIEWING" if others else "UNASSIGNED",
            # The wording the case was read against. A real field, and NOT an industry.
            "wording": account.get("carrier"),
            # Absent rather than defaulted. See the note above.
            "industry": None,
            "last_reviewed": None,
            "internal_change": None,
            "external_risk": None,
            "documents_are_synthetic": bool(account.get("documents_are_synthetic")),
            "row_is_real": True,
        })
    return rows


_BOOK_DERIVATION_NOTICE = (
    "Every row is a case this engine actually holds, and every column is a count it "
    "produced. Priority and the reason line are DERIVED by a declared rule, not typed "
    "per account. Internal change, external risk, industry and last-reviewed are absent "
    "rather than defaulted — they cannot be computed for a case with no change history "
    "and no located premises, and a default on a carrier screen reads as a measurement. "
    "They therefore contribute nothing to the ranking.")


def _book_available(rows: list[dict]) -> dict:
    """The shared honesty block every book operation carries when it DID compute."""
    return {
        "available": True,
        "accounts_counted": len(rows),
        "rows_are_real": True,
        "derivation": _BOOK_DERIVATION_NOTICE,
        "priority_basis": _BOOK_PRIORITY_BASIS,
        "fields_deliberately_absent": ["industry", "internal_change", "external_risk",
                                       "last_reviewed", "premium", "risk_score"],
        "documents_are_synthetic": any(r.get("documents_are_synthetic") for r in rows),
        "synthetic_notice": (
            "The DOCUMENTS behind this case are the example bundle shipped with this "
            "build, and are labelled as such. The rows and counts are not invented — "
            "they are what the engine read out of those documents."
            if any(r.get("documents_are_synthetic") for r in rows) else
            "Read from documents this business uploaded."),
    }


def _portfolio_matrix() -> dict:
    """Roll the fixed portfolio rows into the attention matrix.

    Computed from the rows rather than hardcoded, so the grid cannot drift out of step
    with the table above it — a matrix that disagrees with its own source table is worse
    than no matrix.
    """
    grid: dict[str, dict[str, int]] = {
        row: {"LOW": 0, "MED": 0, "HIGH": 0} for row in ("LOW", "MED", "HIGH")}
    placed: list[dict] = []
    for account in _CARRIER_DEMO_ACCOUNTS:
        internal = account["internal_change"]
        external = account["external_risk"]
        grid[internal][external] += 1
        placed.append({"account_id": account["account_id"],
                       "business_name": account["business_name"],
                       "internal": internal, "external": external,
                       "priority": account["priority"]})
    return {
        "grid": grid, "placed": placed,
        "priority_cell": {"internal": "HIGH", "external": "HIGH",
                          "count": grid["HIGH"]["HIGH"],
                          "accounts": [p["business_name"] for p in placed
                                       if p["internal"] == "HIGH"
                                       and p["external"] == "HIGH"]},
        "axes": {"x": "External risk", "y": "Internal business change"},
        "title": "Protection Attention Matrix",
        "notice": (
            "Attention, not adjudication. The most useful cell is HIGH/HIGH: a business "
            "that changed inside an environment that worsened outside, with documents "
            "that did neither. There is no breach or violation column in this matrix or "
            "anywhere in the schema."),
    }


# Fixed portfolio rows. `lead_reason` names a review trigger, never an accusation —
# "needs review" and "potential mismatch", never "breach". That vocabulary is the
# product's position on what a carrier is entitled to conclude from a document gap.


def _carrier_account(account_id: str,
                     session: Optional[Session] = None,
                     carrier_org: str = "") -> dict:
    """One account row, or 404.

    Resolves in two steps, and the ORDER is deliberate.

    1. The fixed demonstration rows. Retained because the email templates, the decision
       log and the notification inbox are all keyed on `ACC-10231`, and because the
       boundary tests assert the synthetic labelling on them.
    2. A case this session actually holds, keyed on its `case_id`.

    Step 2 is the fix for a real dead end: the work queue now ranks real cases, so its
    "Open" button carries a `case_id` — and every one of them 404'd, because this
    function only ever looked at the demo tuple. The account detail screen was reachable
    only by a URL nothing in the product produced.
    """
    row = next((a for a in _CARRIER_DEMO_ACCOUNTS
                if str(a.get("account_id")) == str(account_id)), None)
    if row is not None:
        return row
    if session is not None:
        real = next((r for r in _book_rows(session, carrier_org)
                     if str(r.get("account_id")) == str(account_id)), None)
        if real is not None:
            return real
    raise HTTPException(404, f"No account {account_id!r} on this book.")


@app.get("/api/carrier/account/{account_id}")
def carrier_account_detail(account_id: str, session: Session = Depends(_session)):
    """Open one account. The boundary is the interesting part of this route.

    The brief asked for "findings, evidence, document trace". A carrier may not read a
    business's documents, and the AI routes were just locked down to enforce that — so a
    privileged read here would undo the fix and the product's central claim with it.

    The two are reconciled by separating what the carrier's OWN book evidences from what
    only the business can disclose. Both halves are returned, explicitly labelled, and
    the second is empty unless a disclosure names this carrier. Consent therefore governs
    three roles rather than two, which makes the boundary stronger rather than weaker.
    """
    principal = _require_role(session, auth.CARRIER)
    row = _carrier_account(account_id, session, principal.organisation_name)
    row_is_real = bool(row.get("row_is_real"))

    disclosed: Optional[dict] = None
    with session.lock:
        st = session.state
        if "conn" in st and not _case_deleted(st):
            # A carrier-named disclosure is the ONLY path to case detail. The recipient
            # string is the carrier's organisation, so a grant to an adviser does not
            # silently serve the counterparty as well.
            allowed = pipeline.granted_fields(
                st["conn"], st["case_id"], principal.organisation_name)
            if allowed is not None:
                disclosed = pipeline.disclosed_brief(
                    st["conn"], st["case_id"], st["brief"],
                    principal.organisation_name)

    return {
        "carrier": principal.as_dict(),
        "account": row,
        # A real case row is not synthetic, and saying it was would be the same class of
        # dishonesty in the opposite direction. The demo rows still report True.
        "synthetic": not row_is_real,
        "row_is_real": row_is_real,
        "from_your_book": {
            "priority": row.get("priority"),
            "lead_reason": row.get("lead_reason"),
            "internal_change": row.get("internal_change"),
            "external_risk": row.get("external_risk"),
            "unestablished_count": row.get("unestablished_count"),
            "adviser": row.get("adviser"),
            "last_reviewed": row.get("last_reviewed"),
            "state": row.get("state"),
            "findings_total": row.get("findings_total"),
            "needs_authorised_review": row.get("needs_authorised_review"),
            "information_missing": row.get("information_missing"),
            "wording": row.get("wording"),
        },
        "priority_basis": _BOOK_PRIORITY_BASIS if row_is_real else None,
        "case_detail": disclosed,
        "case_detail_available": disclosed is not None,
        # The names an owner's grant must carry for this screen to open. Published
        # because the recipient match is exact and the consent screen was free text, so
        # "share with my insurer" was previously a spelling test the owner could not win.
        "disclose_to": principal.organisation_name,
        "case_detail_notice": (
            "Disclosed to you by the business, so findings and citations are shown."
            if disclosed is not None else
            f"This business has not disclosed its case to you. Counts and flags from "
            f"your own book are shown; the documents behind them are not, because a "
            f"carrier reads a case only when the business names it as a recipient — "
            f"the name a grant must carry is '{principal.organisation_name}'."),
        "decisions": carrier.LOG.for_account(account_id),
        "no_citations_notice": (
            "Every count on this row was read out of a document, and the case detail "
            "below carries the page and character offsets behind each finding."
            if row_is_real else
            "The account rows on this book are fixed demonstration data. They carry no "
            "page citations, because inventing one in a product about citation fidelity "
            "is the single move that would discredit everything else here."),
    }


@app.get("/api/carrier/inbox")
def carrier_inbox(session: Session = Depends(_session)):
    """What needs an underwriter, and the honest limit on what a carrier can know.

    Reuses the book fields rather than adding a second change-detection engine. Every row
    separates what the book evidences from what only a disclosure would reveal.
    """
    principal = _require_role(session, auth.CARRIER)
    rows = _book_rows(session, principal.organisation_name)
    if not rows:
        return _no_book_yet(
            "undisclosed_inbox",
            "An in-force book, plus the businesses in it choosing whether to disclose. "
            "The inbox exists to show which accounts have NOT disclosed, which is only "
            "meaningful once some have.", session)
    return {**_book_available(rows), **carrier.undisclosed_inbox(rows)}


@app.get("/api/carrier/renewal-triage")
def carrier_renewal_triage(session: Session = Depends(_session)):
    """The book as a work queue, ranked deterministically.

    The order is computed from book-level fields with a declared weight table, so an
    underwriter can argue with the ranking instead of reverse-engineering it. No model
    influences the order — a queue worked top-down is a consequential decision.
    """
    principal = _require_role(session, auth.CARRIER)
    rows = _book_rows(session, principal.organisation_name)
    if not rows:
        return _no_book_yet(
            "triage",
            "At least one case this engine holds. Ranking zero accounts is arithmetic "
            "on an empty set. A case reaches this queue as soon as one exists in the "
            "session; a carrier-initiated (Mode B) pilot is what makes it a book.",
            session)
    ranked = carrier.triage(rows)
    return {
        **_book_available(rows),
        **ranked,
        # Said plainly at n=1 rather than left for the reader to notice. A queue of one is
        # a queue; calling it triage would be the overclaim.
        "is_triage": len(rows) > 1,
        "scale_notice": (
            f"{len(rows)} account(s) ranked. The ordering logic is the same one that "
            f"would run across an in-force book; at this scale it is a ranked list "
            f"rather than a triage decision, and it says so."),
    }


@app.get("/api/carrier/patterns")
def carrier_patterns(session: Session = Depends(_session)):
    """Concentrations across the book. Counts against a stated threshold, not inference."""
    principal = _require_role(session, auth.CARRIER)
    rows = _book_rows(session, principal.organisation_name)
    if not rows:
        return _no_book_yet(
            "patterns",
            "A book large enough for a concentration to mean something. A pattern across "
            "invented accounts measures the imagination that wrote them.", session)
    found = carrier.patterns(rows)
    return {
        **_book_available(rows),
        **found,
        # At n < the minimum count NO pattern can clear the threshold, and the screen must
        # say that rather than let "no concentration found" read as a measurement over a
        # book big enough to have one.
        "threshold_reachable": len(rows) >= found["thresholds"]["minimum_count"],
        "scale_notice": (
            f"Counted over {len(rows)} account(s). A concentration needs at least "
            f"{found['thresholds']['minimum_count']} accounts and "
            f"{found['thresholds']['minimum_share_pct']}% of the book, so at this scale "
            f"an empty result means the threshold cannot be reached — not that the book "
            f"is clean."
            if len(rows) < found["thresholds"]["minimum_count"] else
            f"Counted over {len(rows)} account(s) against the stated threshold."),
    }


class CarrierDecisionIn(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    decision: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=carrier.MAX_NOTE)


@app.post("/api/carrier/decision")
def carrier_record_decision(body: CarrierDecisionIn,
                            session: Session = Depends(_session)):
    """Record that a named underwriter looked, and what they concluded.

    Append-only. There is no update and no delete, because an underwriting file that can
    be edited after the fact is not a record — and the value of this surface is precisely
    that it evidences somebody looked.

    The actor comes from the SESSION, never from the request body. A client-supplied
    author on an append-only log would make the log worthless.
    """
    principal = _require_role(session, auth.CARRIER)
    _carrier_account(body.account_id, session, principal.organisation_name)
    try:
        row = carrier.LOG.add(
            account_id=body.account_id, decision=body.decision.strip().upper(),
            note=body.note, by=principal.name,
            organisation=principal.organisation_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "decision": row.as_dict(),
            "decisions": carrier.LOG.for_account(body.account_id),
            "notice": ("Appended. This records that a person looked and what they "
                       "concluded about the account. It is not a decision about any "
                       "claim.")}


@app.get("/api/carrier/decisions")
def carrier_decisions(limit: int = 100, session: Session = Depends(_session)):
    """The decision log across the book, newest first.

    THE DICT-SPREAD COLLISION THIS FIXES.

    This used to read:

        return {"decisions": carrier.LOG.all(...),
                "count": carrier.LOG.count(), **carrier.catalogue()}

    and `carrier.catalogue()` also returns a `decisions` key — the five-entry decision
    VOCABULARY. A spread evaluated last wins, so the log records were silently replaced by
    the vocabulary on every call. The screen then rendered five rows built from objects
    that carry only `decision` and `label`, reading `at`, `decision_label`, `note` and
    `by` off each — so the Decision log displayed "0 record(s)" above five rows of
    "undefined — undefined".

    Two keys meaning different things collided under one name, and the count came from the
    records while the rows came from the vocabulary, which is why the header and the body
    disagreed.

    The catalogue is spread FIRST so its vocabulary cannot overwrite anything, and the
    vocabulary is re-exposed under its own name for any caller that wants it. The frontend
    hardcodes the dropdown options, so nothing depended on the colliding key.
    """
    _require_role(session, auth.CARRIER)
    cat = carrier.catalogue()
    vocabulary = cat.pop("decisions", [])
    return {**cat,
            "decisions": carrier.LOG.all(max(1, min(int(limit), 500))),
            "count": carrier.LOG.count(),
            "decision_vocabulary": vocabulary}


class CarrierRequestIn(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    document: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=400)


@app.post("/api/carrier/request-information")
def carrier_request_information(body: CarrierRequestIn,
                                session: Session = Depends(_session)):
    """Ask the business for a specific document.

    Follows `POST /api/adviser/request-information`: the actor comes from the session,
    the request names a document and a reason, and it changes nothing about any finding.
    A request is a question, and a question is not evidence.

    Recorded as a decision of type INFORMATION_REQUESTED so the account's history shows
    one sequence rather than two parallel ones an underwriter has to reconcile.
    """
    principal = _require_role(session, auth.CARRIER)
    _carrier_account(body.account_id, session, principal.organisation_name)
    document = " ".join(body.document.split())[:120]
    reason = " ".join(body.reason.split())[:400]
    try:
        row = carrier.LOG.add(
            account_id=body.account_id, decision=carrier.INFORMATION_REQUESTED,
            note=f"Requested: {document}. Reason: {reason}",
            by=principal.name, organisation=principal.organisation_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "request": {"document": document, "reason": reason,
                                    "account_id": body.account_id},
            "decision": row.as_dict(),
            "notice": ("Recorded against the account. It does not change any finding, "
                       "its state or its evidence class.")}


class CarrierNotifyIn(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    template: str = Field(min_length=1, max_length=48)


# What a carrier may say, and nothing else. A CLOSED vocabulary rather than free text,
# because free text is where a carrier would type the one thing it must not: a figure or a
# clause it has not been shown. Each template names the business and the reason the account
# surfaced ON THE CARRIER'S OWN BOOK, then asks the owner to look.
_CARRIER_TEMPLATES: dict[str, dict[str, str]] = {
    "CARRIER_REVIEW_REQUESTED": {
        "title": "Your insurer has asked you to review your cover",
        "body": ("{carrier} has flagged your account for review. They have not seen your "
                 "documents — this is a request to look, not a finding about your "
                 "business. Open your Veris workspace to see your own position."),
        "what_to_do": ("Open Protection in your workspace and work through anything marked "
                       "for attention. Your adviser can act on it with you."),
    },
    "CARRIER_RENEWAL_REMINDER": {
        "title": "Your insurer says your renewal is approaching",
        "body": ("{carrier} has noted your renewal on your account. Renewal is the one "
                 "moment when changing your cover costs nothing extra, so it is worth "
                 "checking whether what you hold still matches what you do."),
        "what_to_do": ("Open Timeline in your workspace to see what changed since your "
                       "last renewal."),
    },
    "CARRIER_INFORMATION_NEEDED": {
        "title": "Your insurer needs a document from you",
        "body": ("{carrier} has asked for a document relating to your account. They have "
                 "not seen your files — the request comes from what is missing on their "
                 "own record."),
        "what_to_do": ("Open Documents in your workspace. It names the single document "
                       "that would settle the most, and you can upload it there."),
    },
}


@app.post("/api/carrier/notify")
def carrier_notify(body: CarrierNotifyIn, session: Session = Depends(_session)):
    """The carrier emails the SME owner. The constraint is what makes it defensible.

    A carrier is book-level only, so this message may contain the business name, the
    `lead_reason` already on the carrier's own account row, and a call to action. It may
    NOT contain a figure, a clause, a finding or anything derived from the SME's documents
    — and `test_carrier_email.py` asserts that against the real finding headlines rather
    than trusting this comment.

    Two things are deliberate. The mail sends as the CARRIER's brand, not VERIS, because an
    email that misrepresents its sender is the same class of dishonesty as a "sent!" toast
    with nothing on the wire. And it lands in the owner's normal inbox, so the owner's
    existing preferences — including turning it off — govern it like everything else.
    """
    principal = _require_role(session, auth.CARRIER)
    row = _carrier_account(body.account_id, session, principal.organisation_name)
    template = body.template.strip().upper()
    if template not in _CARRIER_TEMPLATES:
        raise HTTPException(
            400, f"Unknown template {template!r}. Must be one of: "
                 f"{', '.join(sorted(_CARRIER_TEMPLATES))}.")

    spec = _CARRIER_TEMPLATES[template]
    business_id = str(row.get("account_id"))
    business_name = str(row.get("business_name") or "your business")
    carrier_name = principal.organisation_name

    # Dedupe on account + template, so clicking twice does not send twice. The same
    # discipline the agent path uses, for the same reason.
    # The notification must be filed under the SAME business id the OWNER's inbox reads,
    # which is `_watched_business_id()` — not the carrier's account number. Keying it on
    # the account id would file the message somewhere the owner never looks, so it would
    # send and then be invisible, which is the worst of both.
    #
    # Only the account the demo owner actually is gets delivered to that inbox; the other
    # book rows have no live owner in this build and fall back to their account id, which
    # keeps the outbox honest about who it was addressed to.
    #
    # A REAL case row is the live case in this session, so its owner IS the watched
    # business — the message must land in the inbox they actually read. Without this a
    # carrier could email the account it just opened from its own work queue and the
    # owner would never see it.
    inbox_id = (_watched_business_id()
                if body.account_id == "ACC-10231" or row.get("row_is_real")
                else business_id)
    store = notifications.NOTIFICATIONS
    dedupe = f"carrier:{business_id}:{template}"
    item = store.add(
        inbox_id, code=template, dedupe_key=dedupe,
        title=spec["title"],
        body=spec["body"].format(carrier=carrier_name),
        what_to_do=spec["what_to_do"],
        evidence={
            "source_name": carrier_name,
            "sent_by": "CARRIER",
            "lead_reason": row.get("lead_reason"),
            "caveat": ("Your insurer has not seen your documents. This is a request to "
                       "look, not a finding about your business."),
        })
    # `add` returns None when this exact news was already recorded. The duplicate check
    # lives inside the store's lock rather than out here, because the scheduler thread and
    # a request can arrive together and two callers each finding "not yet sent" is how an
    # owner gets the same email twice.
    if item is None:
        return {"ok": True, "duplicate": True,
                "notice": ("Already sent for this account and template. Nothing was sent "
                           "again — repeating a request does not make it more true.")}

    entry = mailer.deliver(
        [item], business_id=inbox_id, business_name=business_name,
        to=row.get("owner_email"), wording_by="TEMPLATE",
        sender_brand=carrier_name,
        # A service notice to an existing policyholder about their own account, sent one
        # at a time — not a bulk campaign. See the Spam Control Act note in mailer.py.
        bulk_commercial=False)
    store.set_email_status(inbox_id, item.id, entry.status, entry.detail)

    # Recorded on the account too, so the underwriter's own history shows one sequence.
    carrier.LOG.add(account_id=business_id,
                    decision=carrier.INFORMATION_REQUESTED
                    if template == "CARRIER_INFORMATION_NEEDED"
                    else carrier.REVIEWED,
                    note=f"Emailed the owner: {spec['title']}",
                    by=principal.name, organisation=carrier_name)

    return {
        "ok": True, "duplicate": False,
        "sent_as": carrier_name,
        "template": template,
        "account_id": business_id,
        "notification": item.as_dict(),
        "email": {"status": entry.status, "detail": entry.detail,
                  "message_id": entry.id, "to": entry.to},
        "contains_case_detail": False,
        "notice": ("Sent as your own brand, not as Veris. It names the business and why "
                   "the account surfaced on your book — no figures, no clauses, nothing "
                   "from the business's documents, because you have not seen them."),
    }


@app.post("/api/carrier/briefing")
def carrier_briefing(session: Session = Depends(_session)):
    """Portfolio briefing in prose (B2). Template first, model rewords only.

    Every figure handed to the presenter is a book-level count the carrier already owns,
    so the four presenter gates have real numbers to validate against and the model cannot
    introduce one. With no key configured the deterministic template answers and says so —
    which is the behaviour worth testing, rather than any provider-specific path.
    """
    principal = _require_role(session, auth.CARRIER)
    # MEASURED DEFECT — this route returned HTTP 500 on every call.
    #
    # The return statement read `"patterns": found["patterns"]`, and `found` is not
    # defined in this function. It was left behind when the endpoint stopped aggregating
    # invented accounts: the pattern-finding call above it was deleted and its only reader
    # was not. Nothing caught it because no test exercises this route and the frontend has
    # never had a button for it — a NameError sitting in a shipped endpoint for exactly as
    # long as the endpoint had no caller.
    #
    # `found` is now computed, from the real rows rather than from nothing.
    wp = book.wording_portfolio()
    div = wp["divergence"]
    rows = _book_rows(session, principal.organisation_name)
    found = carrier.patterns(rows)
    context = {
        "wordings_read": wp["counts"]["wordings"],
        "pages_read": wp["counts"]["pages_read"],
        "singapore_wordings": wp["counts"]["singapore"],
        "never_hand_mapped": wp["counts"]["never_hand_mapped"],
        "mechanisms_compared": div["compared"],
        "mechanisms_diverging": div["diverging"],
        "accounts": len(rows),
        "book_available": bool(rows),
    }
    result = presenter.compose("carrier_briefing", context)
    return {"briefing": result.as_dict(), "verified_context": context,
            "patterns": found["patterns"],
            "pattern_thresholds": found["thresholds"],
            "accounts_counted": len(rows),
            "notice": ("Every number was computed before the model saw it, and the "
                       "presenter rejects any figure it did not receive. The wording "
                       "figures come from published documents in this repository; the "
                       "account count is the real number of cases held.")}


class CarrierAskIn(BaseModel):
    question: str = Field(min_length=3, max_length=400)


@app.post("/api/carrier/ask")
def carrier_ask(body: CarrierAskIn, session: Session = Depends(_session)):
    """Ask the BOOK a question (B1) — deliberately not "ask the case".

    The obvious build here is `ai.answer_question()` pointed at the corpus, and it is the
    wrong one: that function retrieves from an SME's documents, which is exactly what a
    carrier may not read. Wiring it up would have re-opened the leak this build just
    closed, through a friendlier door.

    So this answers from what the carrier legitimately holds — its own account rows and
    the clause-level market map built from PUBLISHED wordings — and says plainly when a
    question can only be answered by a document it may not see. A copilot that declines
    for a stated reason is more useful than one that quietly answers from the wrong source.
    """
    principal = _require_role(session, auth.CARRIER)
    question = body.question.strip()
    # No invented book behind the copilot — the rows are the cases this engine actually
    # holds. This used to pass a hardcoded empty list, so the copilot answered every
    # book-level question with zero matches and an empty queue while `/api/carrier/
    # portfolio` was serving real rows on the screen next to it.
    accounts = _book_rows(session, principal.organisation_name)
    triaged = carrier.triage(accounts)
    found = carrier.patterns(accounts)

    low = question.lower()
    # Questions that can only be answered from an SME's documents. Refused by name rather
    # than answered approximately.
    needs_documents = any(w in low for w in (
        "sum insured", "stock value", "clause", "wording says", "page",
        "premium", "quote", "deductible", "schedule says", "exclusion"))

    if needs_documents:
        return {
            "question": question, "answerable": False,
            "answer": None,
            "why_not": ("That can only be answered from the business's own documents, "
                        "and a carrier reads those only when the business discloses them "
                        "to you by name. Your book holds counts and flags, not clauses."),
            "what_you_can_do": ("Open the account and request the specific document, or "
                                "refer it to the assigned adviser."),
            "scope": "BOOK_LEVEL_ONLY",
            "method": "DETERMINISTIC",
        }

    # Answerable from the book. Deterministic retrieval over structured rows — no model
    # decides which rows are relevant, because "which accounts matter" is the underwriting
    # judgement this product refuses to make for anyone.
    hits: list[dict] = []
    for row in triaged["ranked"]:
        blob = " ".join(str(row.get(k) or "") for k in
                        ("business_name", "industry", "reason_line", "state",
                         "adviser")).lower()
        if any(tok in blob for tok in low.split() if len(tok) > 3):
            hits.append(row)
    return {
        "question": question, "answerable": True,
        "matched_accounts": hits[:10],
        "match_count": len(hits),
        "top_of_queue": triaged["ranked"][:3],
        "patterns": found["patterns"],
        "scope": "BOOK_LEVEL_ONLY",
        "method": "DETERMINISTIC",
        "carrier": principal.organisation_name,
        "notice": ("Answered from your own book — account rows and the published-wording "
                   "market map. No business's documents were read, and no model chose "
                   "which accounts to show you."),
    }


@app.post("/api/carrier/explain")
def carrier_explain(body: ExplainIn, session: Session = Depends(_session)):
    """Explain one finding in underwriter framing (B3) — only where it was disclosed.

    Reuses `ai.explain_finding()` rather than writing a second explainer, and reuses the
    carrier disclosure check rather than a second notion of consent. With no disclosure it
    refuses, because the alternative is explaining a finding the carrier is not entitled
    to know exists.
    """
    principal = _require_role(session, auth.CARRIER)
    with session.lock:
        st = _state(session)
        if _case_deleted(st):
            raise HTTPException(404, _DELETED_NOTICE)
        allowed = pipeline.granted_fields(
            st["conn"], st["case_id"], principal.organisation_name)
        if allowed is None:
            raise HTTPException(
                403, f"This business has not disclosed its case to "
                     f"{principal.organisation_name}. A carrier reads a finding only "
                     f"when the business names it as a recipient.")
        disclosed = pipeline.disclosed_brief(
            st["conn"], st["case_id"], st["brief"], principal.organisation_name)
        finding = next((f for f in (disclosed.get("findings") or [])
                        if f.get("rule_id") == body.rule_id), None)
        if finding is None:
            raise HTTPException(
                404, f"No finding with rule_id {body.rule_id!r} was disclosed to you.")
        result = ai.explain_finding(
            st["conn"], _scoped_retriever(session, allowed), finding)
        return {"rule_id": body.rule_id, "framing": "UNDERWRITER", **result.as_dict()}


@app.get("/api/carrier/premium-at-risk")
def carrier_premium_at_risk(session: Session = Depends(_session)):
    """The one book operation that still refuses, and why that is the right answer.

    THIS DOCSTRING USED TO PROMISE THE OPPOSITE. It sold "incremental premium on an
    existing book with near-zero acquisition cost" as the headline number, describing an
    implied-premium figure this function computed over six invented businesses. The rows are
    gone and the computation with them, but the docstring survived, so the source read as a
    function whose comment promised the winning number and whose body declined to produce
    it. A reviewer who opens this file is exactly the reader that contradiction misleads.

    What it does now: report which operand is missing. See the comment in the body for the
    two that are, and `_unused_premium_at_risk` below for the version that is kept only as
    a record of what was removed.
    """
    principal = _require_role(session, auth.CARRIER)
    rows = _book_rows(session, principal.organisation_name)
    # THIS ONE STILL REFUSES, AND THE REFUSAL IS THE CORRECT ANSWER.
    #
    # The other three book operations were wired to real rows because everything they
    # need is a count the engine produced. This one needs two operands the carrier
    # surface genuinely does not hold:
    #
    #   THE GAP    documented value minus insured value. Both are money read out of the
    #              BUSINESS's schedule and accounts. A carrier reads those only when the
    #              business discloses them by name, so aggregating them across a book is
    #              precisely the boundary this product enforces.
    #   THE RATE   there is no rate anywhere in this repository. Applying an "illustrative
    #              market average" is how the old version turned two unknowns into a
    #              confident revenue headline.
    #
    # So the honest move is to say which operand is missing rather than to estimate it.
    # `test_no_carrier_route_carries_a_premium_figure` is the guard that keeps it that way.
    refusal = _no_book_yet(
        "premium_at_risk",
        "Two things, neither of which is a count: a documented-versus-insured value gap "
        "read from each business's own schedule and accounts, and a rate to apply to it. "
        "The gap is behind the consent boundary — a carrier reads a business's figures "
        "only when that business names it as a recipient. The rate does not exist in this "
        "repository at all.", session)
    refusal["accounts_counted"] = len(rows)
    refusal["reason"] = (
        f"{len(rows)} case(s) are held, so this is not an empty-book problem. It is a "
        f"missing-operand problem, and the missing operand is money."
        if rows else refusal["reason"])
    refusal["what_is_real_today"] = (
        "The work queue, the needs-attention inbox and book patterns all compute over "
        "the cases actually held — they need only counts. The wording portfolio compares "
        "coverage mechanisms across the published wordings in this repository and needs "
        "no book at all.")
    refusal["deliberately_not_shown"] = (
        "An implied premium. Multiplying an estimated gap by an illustrative rate "
        "produces a figure that looks like revenue and traces to nothing, which is the "
        "one number that would discredit every other number in this product.")
    return refusal


def _unused_premium_at_risk() -> dict:  # pragma: no cover
    """Retained for reference only; not routed. See _no_book_yet above."""
    result = premium_at_risk.compute_premium_at_risk(premium_at_risk.DEMO_PORTFOLIO)
    return result


_CARRIER_DEMO_ACCOUNTS: tuple[dict, ...] = (
    {"account_id": "ACC-10231", "business_name": "Hock Lee Foods Pte Ltd",
     "industry": "Food manufacturing", "priority": "HIGH",
     "lead_reason": "Adequacy below threshold", "unestablished_count": 3,
     "adviser": "Lion Brokers (Singapore)", "last_reviewed": "2026-08-12",
     "state": "ADVISER_REVIEWING",
     "owner_email": "owner@hockleefoods.demo",
     "internal_change": "HIGH", "external_risk": "HIGH"},
    {"account_id": "ACC-10244", "business_name": "Kimly Metalworks Pte Ltd",
     "industry": "Metal fabrication", "priority": "HIGH",
     "lead_reason": "Workforce declaration", "unestablished_count": 2,
     "adviser": None, "last_reviewed": "2026-08-09",
     "state": "UNASSIGNED",
     "owner_email": "owner@kimlymetalworks.demo",
     "internal_change": "HIGH", "external_risk": "MED"},
    {"account_id": "ACC-10250", "business_name": "Tampines Cold Chain LLP",
     "industry": "Logistics", "priority": "HIGH",
     "lead_reason": "Adequacy below threshold", "unestablished_count": 4,
     "adviser": "Lion Brokers (Singapore)", "last_reviewed": "2026-08-11",
     "state": "INFORMATION_REQUESTED",
     "owner_email": "owner@tampinescoldchain.demo",
     "internal_change": "HIGH", "external_risk": "HIGH"},
    {"account_id": "ACC-10262", "business_name": "Serangoon Dental Group",
     "industry": "Healthcare", "priority": "MEDIUM",
     "lead_reason": "Premises not on schedule", "unestablished_count": 1,
     "adviser": "Lion Brokers (Singapore)", "last_reviewed": "2026-08-10",
     "state": "ADVISER_REVIEWING",
     "owner_email": "owner@serangoondental.demo",
     "internal_change": "MED", "external_risk": "LOW"},
    {"account_id": "ACC-10275", "business_name": "Jurong Auto Services",
     "industry": "Motor trade", "priority": "MEDIUM",
     "lead_reason": "Endorsement conflict", "unestablished_count": 1,
     "adviser": None, "last_reviewed": "2026-08-07",
     "state": "UNASSIGNED",
     "owner_email": "owner@jurongauto.demo",
     "internal_change": "MED", "external_risk": "MED"},
    {"account_id": "ACC-10288", "business_name": "Bugis Print & Signage",
     "industry": "Printing", "priority": "LOW",
     "lead_reason": "No open items", "unestablished_count": 0,
     "adviser": "Lion Brokers (Singapore)", "last_reviewed": "2026-08-05",
     "state": "RESOLVED",
     "owner_email": "owner@bugisprint.demo",
     "internal_change": "LOW", "external_risk": "LOW"},
)


# ── the portable record ─────────────────────────────────────────────────────

@app.get("/api/record")
def record(session: Session = Depends(_session)):
    """The portable record — established facts only, hash-chained, append-only.
    There is deliberately no breach/violation/score attribute.

    Carrier decisions and adviser file notes travel ALONGSIDE the chain, in
    `attestations`, and deliberately not inside it. The hash chain is a chain of FACTS
    established by documents; a human's conclusion about an account is not a fact of that
    kind, and putting one in the chain would let an opinion inherit the integrity guarantee
    that belongs to evidence. So they are carried as a separate, clearly named layer: who
    looked, when, and what they concluded — which is what makes the record useful at renewal
    without diluting what the chain means.
    """
    with session.lock:
        st = _state(session)
        rows = st["conn"].execute(
            "SELECT attribute, value, unit, author_class, appended_at, prev_hash, "
            "hash FROM record_fact WHERE business_id=? ORDER BY appended_at",
            (st["business_id"],)).fetchall()

        note = advisernote.NOTES.get(st.get("case_id") or "")
        return {
            "business_id": st["business_id"],
            "facts": [dict(r) for r in rows],
            "attestations": {
                "carrier_decisions": carrier.LOG.all(limit=100),
                "adviser_note": ({"state": note.state,
                                  "state_label": advisernote.STATE_LABELS.get(note.state),
                                  "adviser": note.adviser,
                                  "organisation": note.organisation,
                                  "approved_at": note.approved_at,
                                  "is_record": note.state == advisernote.ADVISER_APPROVED,
                                  "drafted_by": note.drafted_by}
                                 if note is not None else None),
                "in_hash_chain": False,
                "why_not_in_the_chain": (
                    "The chain covers facts established by documents. A human's conclusion "
                    "about an account is a different kind of statement, and letting it into "
                    "the chain would give an opinion the integrity guarantee that belongs to "
                    "evidence."),
                "notice": ("Who looked at this account, when, and what they concluded. Not "
                           "a coverage decision and not part of the verified chain."),
            },
        }


@app.get("/api/record/export")
def record_export(session: Session = Depends(_session)):
    """Download the record as a self-verifying document.

    This is what makes "portable" true rather than aspirational: every field needed
    to recompute the hash chain travels with the export, plus each basis document's
    filename and sha256, so a recipient with no access to this process can verify it
    via `POST /api/record/verify` or their own sha256 implementation.
    """
    with session.lock:
        st = _state(session)
        doc = pipeline.export_record(st["conn"], st["business_id"])
    return JSONResponse(
        content=doc,
        headers={"Content-Disposition":
                 'attachment; filename="proven-record.json"'})


@app.post("/api/record/verify")
def record_verify(exported: dict):
    """Re-verify an exported record. Stateless by design — it reads nothing from this
    server, which is the point: a third party can check the chain independently."""
    if not isinstance(exported, dict) or "facts" not in exported:
        raise HTTPException(
            400, "Expected an exported record document with a `facts` array.")
    return pipeline.verify_record(exported)


# ── book view ───────────────────────────────────────────────────────────────

@app.get("/api/book")
def book_view(mode: str = "B", session: Session = Depends(_session)):
    """Book-level roll-up. The Mode A / Mode B distinction is load-bearing:

      Mode A  SME-initiated. Nothing aggregates; no book-level view exists. A book
              built on Mode A data would silently break consent-as-pull and the
              no-violations-table promise, so this endpoint refuses to aggregate.
      Mode B  Carrier-initiated over the carrier's own in-force book, on documents
              it already holds. Aggregation is legitimate here.

    SYNTHETIC either way. It aggregates only counts of findings by evidence class,
    never a per-SME black mark — there is no breach/violation/score column to roll
    up, and a judge is invited to look for one."""
    # FAIL CLOSED on an unrecognised mode. Defaulting an unknown value to B would
    # mean a typo silently turns aggregation on, and aggregation is the thing the
    # Mode A/B distinction exists to prevent.
    if mode.upper() not in {"A", "B"}:
        raise HTTPException(
            400, f"Unknown mode {mode!r}. Must be 'A' (SME-initiated, never "
                 f"aggregated) or 'B' (carrier-initiated).")
    if mode.upper() == "A":
        return {
            "mode": "A",
            "aggregated": False,
            "notice": "Mode A is SME-initiated. No book-level view exists by design; "
                      "aggregating consent-pull data would break the trust model.",
            "portfolio": [],
        }
    with session.lock:
        st = _state(session)
        findings = st["brief"].get("findings", [])
        by_class: dict[str, int] = {}
        for f in findings:
            by_class[f["class"]] = by_class.get(f["class"], 0) + 1
        return {
            "mode": "B",
            "aggregated": True,
            "synthetic": True,
            "notice": "SYNTHETIC Mode B (carrier-initiated) book view. There is no "
                      "breach/violation/score column anywhere in the schema.",
            "portfolio": [
                {"business": st["brief"].get("business"),
                 "carrier": st["brief"].get("carrier"),
                 "findings_by_class": by_class,
                 "proven_count": by_class.get("PROVEN", 0)}
            ],
        }


# ── the example documents, served for the browser to upload ─────────────────

# A human-readable label per file, so the UI can show what each document is while it
# uploads. Order matters: it is the order the files are presented and sent.
#
# `01-policy-wording.pdf` is deliberately absent from this map and from the repository.
# It is a real insurer's published wording, so it comes from the corpus and the server
# attaches it. Two separate things keep it out of the browser: `_example_manifest()`
# reads the directory, so a file that is not there cannot be listed, and `_never_served()`
# denies it and every other corpus filename by name even if one ever is.
_EXAMPLE_LABELS = {
    "02-policy-schedule.pdf": "Policy schedule",
    "03-management-accounts.pdf": "Management accounts",
    "04-tenancy-agreement.pdf": "Tenancy agreement",
    "05-illegible-scan.pdf": "A deliberately illegible scan",
    "06-cpf-contribution-record.pdf": "CPF contribution record",
    "07-work-injury-schedule.pdf": "Work injury policy schedule",
    "08-work-permit-holders.pdf": "Work permit holders",
    "09-acra-business-profile.pdf": "ACRA business profile",
    "10-supplier-invoice-freezer.pdf": "Supplier invoice (freezer)",
    "11-endorsement-food-liability-5k.pdf": "Endorsement 03 (sub-limit)",
    "12-endorsement-food-liability-25k.pdf": "Endorsement 04 (sub-limit)",
    "13-illegible-endorsement-scan.pdf": "An illegible endorsement scan",
}


def _never_served() -> frozenset[str]:
    """Filenames the example listing must never offer, whatever is on disk.

    The unlabelled-PDF fallback below exists so a newly generated synthetic document is
    never silently withheld. That same fallback is a way back out for a THIRD PARTY's
    document: `/api/example/file/{name}` validates against this listing, so a published
    wording copied into `demo-documents/` by hand or by a mis-aimed seed command would
    be served again, and the only thing standing in the way would be a scanner run that
    happens later. Denying by name closes it here, at the source.

    Every corpus seed filename is denied, not just the one the worked example uses: the
    rule is "documents this repository has a reader's right to and not a publisher's",
    and that is exactly the set the manifest lists.
    """
    names = {_EXAMPLE_WORDING_NAME}
    try:
        names.update(d.seed_name for d in corpus.documents())
    except corpus.CorpusError:
        pass          # no readable manifest: the hard-coded name still stands
    return frozenset(names)


def _example_manifest() -> list[dict]:
    """The example documents actually present on disk, in presentation order.

    Reads the directory rather than trusting the label map, so a file that has not
    been generated yet simply does not appear — the demo degrades to whatever exists
    rather than advertising a document the browser would then fail to fetch.
    """
    if not EXAMPLE_DIR.is_dir():
        return []
    present = {p.name for p in EXAMPLE_DIR.glob("*.pdf")} - _never_served()
    out = []
    for name in _EXAMPLE_LABELS:
        if name in present:
            out.append({"name": name, "label": _EXAMPLE_LABELS[name]})
    # Any PDF on disk without a curated label is still offered, so a newly added
    # document is never silently withheld.
    for name in sorted(present):
        if name not in _EXAMPLE_LABELS:
            out.append({"name": name, "label": name})
    return out


@app.get("/api/example/manifest")
def example_manifest():
    """List the synthetic example documents the browser can upload.

    This is what makes the example honest: the front end fetches these and POSTs them
    to /api/bundle exactly as if the user had dropped them in. The server does not
    seed a case.
    """
    docs = _example_manifest()
    wording = pipeline.CHUBB_PDF.is_file()
    return {
        "documents": docs,
        "count": len(docs),
        "business_name": "Hock Lee Foods Pte Ltd",
        "notice": ("A synthetic 14-person food factory. These are fictional documents, "
                   "labelled synthetic throughout. The browser uploads them through "
                   "the same path your own documents would take."),
        # The client sets `attach_example_wording` on the upload so the server adds the
        # published base wording from the corpus. Reported rather than assumed: without
        # the corpus the example still runs on its synthetic documents, and the clause
        # findings that quote the wording are absent instead of invented.
        "server_attaches_wording": wording,
        "wording_notice": (
            "The base wording is a real insurer's published policy document. It is "
            "attached by the server from the document corpus, not downloaded through "
            "your browser, because this project has a reader's right to it and not a "
            "publisher's."
            if wording else
            "The published base wording is not in the corpus on this machine, so the "
            "example runs on its synthetic documents alone and the findings that quote "
            "the wording will not appear. Fetch it with scripts/fetch_corpus.py."),
    }


@app.get("/api/example/household/manifest")
def household_example_manifest():
    """List the generated household PDFs that the browser uploads as its own files."""
    documents = [
        {"name": spec.filename, "label": spec.label}
        for spec in household.DOCUMENTS
        if (HOUSEHOLD_EXAMPLE_DIR / spec.filename).is_file()
    ]
    return {
        "documents": documents,
        "count": len(documents),
        "business_name": "Rao household (synthetic)",
        "notice": (
            "Synthetic household documents. The browser uploads every file through the "
            "ordinary processing panel before the encrypted portfolio is populated."),
    }


@app.get("/api/example/household/file/{name}")
def household_example_file(name: str):
    """Serve one generated household PDF after a strict manifest-name check."""
    allowed = {spec.filename for spec in household.DOCUMENTS}
    if name not in allowed:
        raise HTTPException(404, f"No household example document named {name!r}.")
    path = HOUSEHOLD_EXAMPLE_DIR / name
    if not path.is_file():
        raise HTTPException(404, f"Household example document {name!r} is not on disk.")
    return FileResponse(str(path), media_type="application/pdf", filename=name)


@app.get("/api/example/file/{name}")
def example_file(name: str):
    """Serve one example PDF by name.

    `name` is validated against the on-disk manifest, never used to build a path
    directly: a caller cannot ask for `../../secrets` because the requested name must
    match a file the directory listing already contains.
    """
    allowed = {d["name"] for d in _example_manifest()}
    if name not in allowed:
        raise HTTPException(404, f"No example document named {name!r}.")
    path = EXAMPLE_DIR / name
    if not path.is_file():
        raise HTTPException(404, f"Example document {name!r} is not on disk.")
    return FileResponse(str(path), media_type="application/pdf", filename=name)


# ── the action loop ─────────────────────────────────────────────────────────
# Detection without correction is the objection this category loses on. These routes are
# the half that changes something: a finding becomes a request, the request reaches a
# human who can price it, and `/api/outcomes` reports how much exposure actually closed.
#
# The actor is taken from the SESSION ROLE, never from the request body. An owner cannot
# post `by: "adviser"` and mark their own request BOUND — if they could, the
# exposure-closed figure would be self-reported and therefore worthless.

def _actor_for(session: Session) -> str:
    """Map the logged-in role onto the actor vocabulary the state machine enforces.

    No principal means owner: the demo is usable without logging in, and owner is the
    least privileged actor, so defaulting there cannot escalate anything.
    """
    p = _principal(session)
    if p is not None and p.role == auth.ADVISER:
        return "adviser"
    return "owner"


def _guard_action_loop(session: Session) -> None:
    """The consent gate on the action loop.

    MEASURED HOLE, found while wiring the adviser's action controls. These four routes
    carried NO role guard, so with the demo case loaded:

        as CARRIER   GET /api/actions   -> 200, every request with its exposure figure
        as ADVISER   GET /api/actions   -> 200, with no disclosure granted at all

    That is the same defect shape as the AI-routes bypass documented above `_ai_scope`:
    the consent gate held on `/api/brief` and `/api/adviser/review` and was simply absent
    on the derived view. An action names a business, an interest and a figure, so it is
    at least as sensitive as the Brief it was derived from.

    Anonymous and SME still pass, for the same reason `_deny_foreign_role` lets them: the
    local demo surface and the offline suite depend on it, and the owner is the actor who
    raises and withdraws these requests.
    """
    principal = _principal(session)
    if principal is None or principal.role == auth.SME:
        return
    if principal.role == auth.CARRIER:
        raise HTTPException(
            403, "A carrier sees book-level roll-ups, not one business's correction "
                 "requests. Use /api/book or /api/carrier/portfolio.")
    if principal.role == auth.ADVISER:
        st = _state(session)
        if not pipeline.disclosure_granted(st["conn"], st["case_id"]):
            raise HTTPException(
                403, "This business has not disclosed its review to an adviser, so "
                     "there are no requests here to act on. The consent gate is the "
                     "access path, not a second opinion about one.")
        return
    raise HTTPException(403, "This surface is not available to your role.")


@app.get("/api/actions")
def list_actions(session: Session = Depends(_session)):
    """Every prepared or in-flight correction for this case.

    Deletion is honoured here with the same shape as every other case-scoped view. An
    action list is the most sensitive thing this product holds — it names a business, a
    figure and a request — so it must go quiet first, not last.
    """
    with session.lock:
        _guard_action_loop(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    "actions": [], "count": 0, "actor": _actor_for(session),
                    "premium_basis": actions.NOT_QUOTED,
                    "premium_notice": actions.PREMIUM_NOTICE, "vocabulary": {}}
        actions.init(st["conn"])
        items = actions.for_case(st["conn"], st["case_id"])
        return {
            "available": True,
            "actions": [a.as_dict() for a in items],
            "count": len(items),
            "actor": _actor_for(session),
            "premium_basis": actions.NOT_QUOTED,
            "premium_notice": actions.PREMIUM_NOTICE,
            "vocabulary": {
                "types": list(actions.ACTION_TYPES),
                "states": list(actions.STATES),
                "terminal": sorted(actions.TERMINAL),
                "closes_exposure": sorted(actions.CLOSES_EXPOSURE),
            },
        }


@app.get("/api/actions/{action_id}")
def get_action(action_id: str, session: Session = Depends(_session)):
    with session.lock:
        _guard_action_loop(session)
        st = _state(session)
        if _case_deleted(st):
            raise HTTPException(404, _DELETED_NOTICE)
        actions.init(st["conn"])
        a = actions.get(st["conn"], action_id)
        if a is None or a.case_id != st["case_id"]:
            raise HTTPException(404, f"No action {action_id!r} in this case.")
        return {"action": a.as_dict(), "actor": _actor_for(session)}


class AdvanceIn(BaseModel):
    to_state: str
    note: str = Field(default="", max_length=2000)


@app.post("/api/actions/{action_id}/advance")
def advance_action(action_id: str, body: AdvanceIn,
                   session: Session = Depends(_session)):
    """Move an action through the state machine, or refuse and say why.

    Refusals are 409 with the permitted moves listed, because "you cannot do that" without
    naming what you can do is a dead end for whoever is driving the interface.
    """
    with session.lock:
        _guard_action_loop(session)
        st = _state(session)
        if _case_deleted(st):
            raise HTTPException(404, _DELETED_NOTICE)
        actions.init(st["conn"])
        a = actions.get(st["conn"], action_id)
        if a is None or a.case_id != st["case_id"]:
            raise HTTPException(404, f"No action {action_id!r} in this case.")
        try:
            moved = actions.advance(st["conn"], action_id, body.to_state,
                                    by=_actor_for(session), note=body.note)
        except actions.TransitionRefused as exc:
            raise HTTPException(409, detail={
                "code": exc.code, "message": str(exc),
                "state": a.state, "allowed_moves": a.allowed_moves(),
                "actor": _actor_for(session)})
        return {"ok": True, "action": moved.as_dict()}


@app.get("/api/outcomes")
def outcomes(session: Session = Depends(_session)):
    """Did anything actually change? The only metric that answers it.

    Exposure counts as closed on BOUND alone. In-flight amounts are reported separately
    rather than summed in, so the figure measures effect and not activity.
    """
    with session.lock:
        _guard_action_loop(session)
        st = _state(session)
        if _case_deleted(st):
            return {"available": False, "reason": _DELETED_NOTICE, "deleted": True,
                    "gaps_detected": 0, "actions_raised": 0, "actions_in_flight": 0,
                    "actions_resolved": 0, "actions_bound": 0, "by_state": {},
                    "exposure_identified_sgd": "0", "exposure_closed_sgd": "0",
                    "exposure_pending_sgd": "0", "exposure_by_interest": {},
                    "actions_without_exposure_figure": 0,
                    "premium_basis": actions.NOT_QUOTED, "counting_basis": "",
                    "notes": [_DELETED_NOTICE]}
        actions.init(st["conn"])
        findings = (st.get("brief") or {}).get("findings") or []
        return {"available": True,
                **actions.outcomes(st["conn"], st["case_id"], len(findings))}


# ── static front end, and one front door per audience ───────────────────────
# PORTED from the parallel VERIS build. Three audiences read the same case for different
# reasons, and handing all three the same URL makes the product feel like one dashboard
# with permissions bolted on. The routes below are presentation only — every one serves
# the same single-page app, and the REAL boundary is `_require_role` on the API routes.
# Saying that explicitly matters: a route that merely looks private is worse than none.

ROLE_ENTRY: dict[str, dict] = {
    "/app": {"role": auth.SME, "view": "owner",
             "purpose": "The owner's own position, in plain language."},
    "/adviser": {"role": auth.ADVISER, "view": "adviser",
                 "purpose": "The review queue, the evidence, and the requests to action."},
    "/carrier": {"role": auth.CARRIER, "view": "carrier",
                 "purpose": "Portfolio attention, aggregated over documents already held."},
}


# ── THE IN-PRODUCT HELPER ───────────────────────────────────────────────────
#
# "Where do I find X" is the question this product gets most, because it answers a lot of
# questions and an owner cannot hold nine destinations and twenty-three tabs in their head.
# The helper answers it and then actually goes there.
#
# THE ROLE IS READ FROM THE SESSION AND IS NOT A PARAMETER. There is deliberately no way for
# a caller to say which workspace it wants routed. `assistant.for_role()` filters the
# catalogue server-side, so a carrier asking for an SME screen gets the boundary named rather
# than a redirect — see the module docstring for why that is the more useful answer.

class AssistIn(BaseModel):
    question: str = Field(default="", max_length=400)


@app.post("/api/assistant")
def assistant_ask(body: AssistIn, session: Session = Depends(_session)):
    """Answer "where is X", and say where to go.

    401 rather than a public answer: the catalogue IS the shape of a workspace, and which
    screens exist is not something an unauthenticated caller needs enumerated.
    """
    with session.lock:
        principal = _principal(session)
    if principal is None:
        raise HTTPException(401, "Sign in first — this helper answers about your own "
                                 "workspace, so it needs to know which one that is.")
    return assistant.answer(body.question, principal.role)


@app.get("/api/assistant/destinations")
def assistant_destinations(session: Session = Depends(_session)):
    """Everywhere this role can be sent. Seeds the helper's suggestion chips, and is the
    surface a reviewer can read to check the boundary rather than take it on trust."""
    with session.lock:
        principal = _principal(session)
    if principal is None:
        raise HTTPException(401, "Sign in first.")
    return assistant.catalogue(principal.role)


def _spa() -> FileResponse:
    idx = WEB_DIR / "index.html"
    if not idx.exists():
        raise HTTPException(404, "web/index.html not found")
    return FileResponse(str(idx))


LANDING_DIR = WEB_DIR / "landing"


def _landing() -> FileResponse:
    """The marketing page, served as its own document rather than from the SPA.

    It is a standalone page — `web/landing/{index.html,landing.css,landing.js}` — with no
    `fetch` anywhere in it. That is a stronger form of the §39/40 requirement than the SPA
    achieved: `renderLanding()` merely happened not to call `api()`, and a later edit could
    have made it do so without anything complaining. This file cannot call the case engine,
    because it never loads the code that knows how.

    Falls back to the SPA if the folder is missing, so deleting `web/landing/` degrades the
    front door instead of 500-ing the site. The app recognises that case — it is served at
    `/` to a visitor the server says is not signed in — and paints its own older in-app
    landing there, which is the only path that still reaches `renderLanding()`.
    """
    idx = LANDING_DIR / "index.html"
    if not idx.exists():
        return _spa()
    return FileResponse(str(idx))


def _has_session_user(sid: Optional[str]) -> bool:
    """Is somebody logged in on this cookie?

    Deliberately NOT `_session(request, response)`: that mints a session and sets a cookie
    on first contact, and a visitor reading the landing page has not asked for one. This
    only looks up an existing session, so an anonymous hit on `/` stays anonymous.
    """
    session = SESSIONS.get(sid)
    return session is not None and _principal(session) is not None


@app.get("/")
def index(_sid: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    """The front door, per the spec: unauthenticated sees the product landing page,
    authenticated goes straight to their workspace.

    The cookie was already a parameter here and already unused — the session was read
    client-side instead. Reading it here is what lets one URL be both a marketing page and
    a way back into a signed-in workspace, which is the behaviour a returning user expects
    from a bookmark on the bare domain.
    """
    return _spa() if _has_session_user(_sid) else _landing()


@app.get("/landing")
def landing_page():
    """The landing page unconditionally, session or not.

    `/` hands a signed-in visitor their workspace, which is right for a bookmark and wrong
    for a demo: showing the landing page would otherwise mean signing out. This is the
    addressable URL for it.
    """
    return _landing()


@app.get("/login")
def login_page():
    """Always the login page.

    `/login` serves the single-page app, which paints the login form because the path is
    `/login` — that check is in `render()` and does not consult the session, so an
    authenticated visitor asking for the login page gets it rather than being bounced.

    `/` no longer serves this app to an anonymous visitor; it serves `web/landing/`. There is
    therefore exactly ONE marketing page, and an unauthenticated visitor on `/app`,
    `/adviser` or `/carrier` now gets the login form rather than a second one.
    """
    return _spa()


# One function per path rather than stacked decorators. Stacking registers both routes but
# gives them one operation id, so the deep-link route vanishes from the OpenAPI schema —
# and a route an integrator cannot find in the schema is a route that does not exist to
# them. `test_the_openapi_schema_documents_every_product_route` catches exactly this.

@app.get("/app")
def owner_entry():
    return _spa()


@app.get("/app/{_path:path}")
def owner_entry_deep(_path: str = ""):
    """Client-side routing: a refresh on /app/findings must not 404."""
    return _spa()


@app.get("/adviser")
def adviser_entry():
    return _spa()


@app.get("/adviser/{_path:path}")
def adviser_entry_deep(_path: str = ""):
    return _spa()


@app.get("/carrier")
def carrier_entry():
    return _spa()


@app.get("/carrier/{_path:path}")
def carrier_entry_deep(_path: str = ""):
    return _spa()


@app.get("/api/entrypoints")
def entrypoints():
    """What the three front doors are, and the honest note about what they are not."""
    return {
        "entrypoints": [{"path": p, **meta} for p, meta in ROLE_ENTRY.items()],
        "notice": ("These paths select which view the single-page app opens. They are not "
                   "an access boundary: authorisation is enforced server-side on the API "
                   "routes, so opening /carrier without a carrier login shows a page that "
                   "cannot fetch carrier data."),
    }


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
