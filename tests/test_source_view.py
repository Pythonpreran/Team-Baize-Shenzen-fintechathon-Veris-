"""GET /api/source/... — the cited page, rendered with the quotation highlighted.

The drawer used to answer "show me why" with a quotation, a page number and two character
offsets: all true, none checkable without leaving the product. These tests pin the three
properties that make showing the page an improvement rather than a decoration:

  1. the page shown is the page cited, and the highlight is FOUND rather than assumed
  2. the quotation highlighted is the STORED span, never a string the caller passed in
  3. the consent gate and case deletion both reach the page image, which is the most
     complete disclosure of a document this product can make
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

import demodocs

from proven import corpus, sourceview
from proven.api import app

WORDING = "01-policy-wording.pdf"
SCHEDULE = "02-policy-schedule.pdf"
ADVISER = {"email": "adviser@lionbrokers.demo", "password": "Demo@123"}
CARRIER = {"email": "underwriter@carrier.demo", "password": "Demo@123"}
RENDERER_ABSENT = "the PDFium renderer (pypdfium2) is not installed"

# Every test here renders a cited page out of the 28-page published wording, which is
# not committed: it comes from the corpus.
pytestmark = pytest.mark.requires_corpus(demodocs.WORDING_CORPUS_ID)


def _part(name: str):
    return demodocs.part(name)


@pytest.fixture()
def client() -> TestClient:
    c = TestClient(app)
    r = c.post("/api/bundle", files=[_part(WORDING), _part(SCHEDULE)],
               data={"business_name": "Hock Lee Foods Pte Ltd"})
    assert r.status_code == 200, r.text
    # The intake report is the only place document_id is published per file, and the
    # per-kind consent test needs the id of a document no finding happens to cite.
    c.intake = r.json()["intake"]["files"]
    return c


def _document_id(client: TestClient, kind: str) -> str:
    for row in client.intake:
        if row.get("accepted") and row.get("kind") == kind:
            return row["document_id"]
    pytest.skip(f"no {kind} in the uploaded bundle")


def _first_span(client: TestClient) -> dict:
    for f in client.get("/api/brief").json()["findings"]:
        for s in f.get("spans") or []:
            if s.get("document_id"):
                return s
    pytest.skip("no finding in the demo bundle carries a document-backed span")


# ── the citation resolves to a real page ─────────────────────────────────────

def test_the_brief_span_carries_enough_to_locate_the_page():
    """`quote` is the key the Brief actually publishes.

    Pinned because the drawer read `quoted_text` — the DB column name — and therefore
    rendered an empty quotation for every finding while looking entirely correct.
    """
    c = TestClient(app)
    c.post("/api/bundle", files=[_part(WORDING), _part(SCHEDULE)])
    span = _first_span(c)
    assert span["quote"], "the Brief must publish the quoted text under `quote`"
    assert "quoted_text" not in span
    for key in ("span_id", "document_id", "page_no", "char_start", "char_end"):
        assert key in span


def test_the_source_document_metadata_names_the_file(client):
    span = _first_span(client)
    d = client.get(f"/api/source/{span['document_id']}").json()
    assert d["filename"] == WORDING
    assert d["kind"] == "BASE_WORDING"
    assert d["page_count"] > 1
    assert d["pages_viewable"] is sourceview.available()


@pytest.mark.skipif(not sourceview.available(), reason=RENDERER_ABSENT)
def test_the_page_is_rendered_and_the_quotation_is_actually_found(client):
    span = _first_span(client)
    r = client.get(f"/api/source/{span['document_id']}/page/{span['page_no']}",
                   params={"span_id": span["span_id"]})
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["page_no"] == span["page_no"], "a citation must show the page it cites"
    assert d["width"] > 400 and d["height"] > 400
    assert d["image"].startswith("data:image/png;base64,")
    png = base64.b64decode(d["image"].split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # The point of the whole feature: the quotation was located on the page, so the
    # highlight marks the real text rather than a plausible position.
    assert d["highlight"]["found"] is True
    assert d["highlight"]["method"] == sourceview.EXACT
    assert d["highlight"]["rect_count"] >= 1
    assert d["highlight"]["note"]


@pytest.mark.skipif(not sourceview.available(), reason=RENDERER_ABSENT)
def test_a_page_with_no_span_named_is_rendered_unhighlighted_and_says_so(client):
    span = _first_span(client)
    d = client.get(
        f"/api/source/{span['document_id']}/page/{span['page_no']}").json()
    assert d["available"] is True
    assert d["highlight"]["found"] is False
    assert d["highlight"]["method"] == sourceview.NOT_FOUND
    assert "nothing is highlighted" in d["highlight"]["note"].lower()


def test_the_highlight_quotation_comes_from_the_stored_span_not_the_caller(client):
    """There is no parameter for the text to highlight, and that is the design.

    A client that could pass its own string could have this endpoint draw a box around a
    phrase the engine never cited — a fabricated citation rendered by the product's own
    evidence viewer.
    """
    span = _first_span(client)
    d = client.get(f"/api/source/{span['document_id']}/page/{span['page_no']}",
                   params={"span_id": span["span_id"],
                           "quote": "THIS TEXT WAS NEVER CITED"}).json()
    assert d["quote"] == span["quote"]
    assert "NEVER CITED" not in d["quote"]


def test_an_unknown_document_is_refused(client):
    span = _first_span(client)
    r = client.get("/api/source/not-a-real-document-id/page/1",
                   params={"span_id": span["span_id"]})
    assert r.status_code == 404


def test_a_span_belonging_to_a_different_document_is_refused(client):
    """The span and the document must agree, or the page shown is not the page cited."""
    span = _first_span(client)                       # cites the wording
    other = _document_id(client, "SCHEDULE")
    r = client.get(f"/api/source/{other}/page/1",
                   params={"span_id": span["span_id"]})
    assert r.status_code == 400
    assert "different document" in r.json()["detail"]


def test_a_page_outside_the_document_is_refused_by_name(client):
    span = _first_span(client)
    d = client.get(f"/api/source/{span['document_id']}/page/9999").json()
    assert d["available"] is False
    assert "outside this document" in d["reason"]


# ── the access model is the same one ─────────────────────────────────────────

def test_a_carrier_cannot_render_a_page_of_one_business_document(client):
    span = _first_span(client)
    client.post("/api/auth/login", json=CARRIER)
    for path in (f"/api/source/{span['document_id']}",
                 f"/api/source/{span['document_id']}/page/{span['page_no']}"):
        assert client.get(path).status_code == 403, path


def test_an_adviser_with_no_disclosure_cannot_render_a_page(client):
    span = _first_span(client)
    client.post("/api/auth/login", json=ADVISER)
    r = client.get(f"/api/source/{span['document_id']}/page/{span['page_no']}")
    assert r.status_code == 403
    assert "disclose" in r.json()["detail"].lower()


def test_an_adviser_may_render_only_a_disclosed_document_kind(client):
    """A page image is a fuller disclosure than a finding derived from it, so it goes
    through the same gate — per document kind, not per case."""
    wording = _document_id(client, "BASE_WORDING")
    schedule = _document_id(client, "SCHEDULE")
    client.post("/api/consent", json={"recipient": "Licensed adviser",
                                      "fields": ["BASE_WORDING"]})
    client.post("/api/auth/login", json=ADVISER)
    granted = client.get(f"/api/source/{wording}")
    assert granted.status_code == 200, granted.text
    withheld = client.get(f"/api/source/{schedule}")
    assert withheld.status_code == 403
    assert "SCHEDULE" in withheld.json()["detail"]


def test_deleting_the_case_makes_the_source_page_unreadable(client):
    """The strongest promise in the product has to reach the retained bytes too.

    Uploaded bytes are held in memory so the page can be shown. If deletion left them
    renderable, "delete the case" would be cosmetic in the one place it matters most —
    the original document.
    """
    span = _first_span(client)
    assert client.get(f"/api/source/{span['document_id']}").status_code == 200
    client.delete("/api/case")
    for path in (f"/api/source/{span['document_id']}",
                 f"/api/source/{span['document_id']}/page/{span['page_no']}"):
        assert client.get(path).status_code == 404, path


def test_a_new_upload_drops_the_previous_bundle_bytes(client):
    """`reset_case` must reach the byte store, or a session accumulates every PDF it has
    ever been shown."""
    span = _first_span(client)
    client.post("/api/bundle", files=[_part(SCHEDULE)])
    assert client.get(f"/api/source/{span['document_id']}").status_code == 404


# ── the module's own contract ────────────────────────────────────────────────

# ── the DEFAULT case, which is what a first-time visitor sees ────────────────

def _assert_cited_excerpt(page: dict, span: dict, rule_id: str) -> None:
    """The shape every citation into a published document must take.

    The cited paragraph, with the quotation inside it at the offsets the server reports,
    a publisher link taken from the manifest — and no picture of the page, at any size.
    """
    where = f"{rule_id} p{span['page_no']}"
    assert page["display"] == "EXCERPT", where
    assert page["available"] is False, f"{where}: a published page was drawn"
    assert "image" not in page, f"{where}: a page image was served for a corpus document"
    x = page["excerpt"]
    assert x and x["text"], f"{where}: no paragraph"
    assert x["text"][x["quote_start"]:x["quote_end"]] == span["quote"], (
        f"{where}: the quotation is not at the offsets the excerpt claims")
    assert x["located"] == "AT_STORED_OFFSETS", (
        f"{where}: the stored offsets did not hold the quotation ({x['located']})")
    pub = page["publisher"]
    doc = corpus.resolve(demodocs.WORDING_CORPUS_ID)
    assert pub["available"] is True and pub["url"] == doc.source_url, where
    assert pub["publisher"] == doc.publisher, where


def test_the_demo_case_wording_is_shown_as_the_cited_paragraph_and_a_publisher_link():
    """The screen a visitor lands on shows every citation into the published wording.

    REPLACES `test_the_demo_case_wording_pdf_is_viewable`, which asserted the opposite on
    purpose: that the default case DREW pages of the genuine Chubb SelectPlus wording.
    That behaviour was removed deliberately. The wording is a third party's copyrighted
    publication that this project holds a reader's copy of, so it is shown as the cited
    paragraph with a link to the publisher's own copy. What the old test protected — that
    the first screen a visitor lands on can show the evidence behind each citation, for at
    least four citations into a real 28-page PDF — is asserted here in the new form.
    """
    c = TestClient(app)
    c.post("/api/reset")
    brief = c.get("/api/brief").json()
    assert brief["source"] == "DEMO_BUNDLE"

    shown = 0
    for f in brief["findings"]:
        for s in f.get("spans") or []:
            if not s.get("document_id"):
                continue
            meta = c.get(f"/api/source/{s['document_id']}").json()
            if not meta["filename"].lower().endswith(".pdf"):
                continue
            assert meta["display"] == "EXCERPT" and meta["pages_viewable"] is False
            for full in (False, True):
                page = c.get(
                    f"/api/source/{s['document_id']}/page/{s['page_no']}",
                    params={"span_id": s["span_id"], "full": full}).json()
                _assert_cited_excerpt(page, s, f["rule_id"])
            shown += 1
    assert shown >= 4, f"only {shown} demo-case citations reached their paragraph"


def test_a_structured_fixture_document_says_it_has_no_pages(client):
    """Not a failure, and it must not read as one.

    The demo schedule and supporting documents are JSON. There IS no page, and the honest
    answer is to say that and point at the path that has one — never to substitute a
    different document's page under the citation.
    """
    c = TestClient(app)
    c.post("/api/reset")
    brief = c.get("/api/brief").json()
    json_span = None
    for f in brief["findings"]:
        for s in f.get("spans") or []:
            if not s.get("document_id"):
                continue
            meta = c.get(f"/api/source/{s['document_id']}").json()
            if meta["filename"].lower().endswith(".json"):
                json_span = (s, meta)
                break
        if json_span:
            break
    if json_span is None:
        pytest.skip("no JSON-backed fixture document in the demo case")
    span, meta = json_span
    assert meta["pages_viewable"] is False
    assert "structured demo data" in meta["reason"]
    page = c.get(f"/api/source/{span['document_id']}/page/{span['page_no']}",
                 params={"span_id": span["span_id"]}).json()
    assert page["available"] is False
    assert "structured demo data" in page["reason"]
    # And the quotation is still served, because it is read from the stored text.
    assert page["quote"]


def test_the_example_business_shows_the_evidence_for_every_single_citation():
    """The example path uploads real PDFs, so EVERY span must resolve to its evidence.

    Deliberately checks every span of every finding rather than the first of each. Doing
    only the first passed while one citation silently could not be highlighted at all —
    see `test_a_citation_drawn_outside_its_page_is_found_and_labelled`.

    REPLACES `test_the_example_business_renders_a_page_for_every_single_citation`. The
    synthetic documents the browser uploads are still drawn as pages with the quotation
    found and marked, exactly as before. The published base wording the SERVER attaches
    is no longer drawn — that was removed deliberately, because it is a third party's
    publication — so its citations must each resolve to the cited paragraph and a
    publisher link instead. Same strength: every citation, none unaccounted for.
    """
    if not sourceview.available():
        pytest.skip(RENDERER_ABSENT)
    c = TestClient(app)
    manifest = c.get("/api/example/manifest").json()
    # Exactly what the browser does: upload the synthetic documents it was offered, and
    # ask the server to attach the published base wording from the corpus. The wording
    # is not in the manifest listing and there is no route that serves it, so without
    # this flag two thirds of the example's citations would not exist — which is how
    # this assertion caught the change in the first place.
    assert manifest["server_attaches_wording"] is True
    assert demodocs.WORDING not in {d["name"] for d in manifest["documents"]}
    files = [_part(d["name"]) for d in manifest["documents"]]
    r = c.post("/api/bundle", files=files,
               data={"business_name": manifest["business_name"],
                     "attach_example_wording": "1"})
    assert r.status_code == 200
    attached = {row["document_id"] for row in r.json()["intake"]["files"]
                if row.get("accepted") and row.get("filename") == WORDING}
    assert attached, "the server did not attach the published wording"
    unviewable = []
    checked = pages = excerpts = 0
    for f in c.get("/api/brief").json()["findings"]:
        for s in f.get("spans") or []:
            if not s.get("document_id"):
                continue
            checked += 1
            page = c.get(f"/api/source/{s['document_id']}/page/{s['page_no']}",
                         params={"span_id": s["span_id"]}).json()
            if s["document_id"] in attached:
                _assert_cited_excerpt(page, s, f["rule_id"])
                excerpts += 1
                continue
            pages += 1
            if (page["display"] != "PAGE" or not page["available"]
                    or not page["highlight"]["found"]):
                unviewable.append((f["rule_id"], page.get("reason"),
                                   page.get("highlight", {}).get("method")))
    assert checked > 20, f"only {checked} citations checked"
    assert pages and excerpts, (pages, excerpts)
    assert not unviewable, f"citations with no highlighted page: {unviewable}"


def test_a_corpus_document_is_recorded_as_one_and_not_as_a_user_upload():
    """What this product may show of a document depends on who published it.

    The example's base wording is a real insurer's publication and the SERVER attaches
    it from the corpus. It is appended to the uploads and retained in the session keyed
    by document_id, exactly like a file the user chose to upload, and nothing in the
    bytes tells the two apart. So the provenance is recorded at intake and reported —
    a distinction reconstructed later from a bytes-keyed map is a distinction that gets
    reconstructed wrongly.
    """
    c = TestClient(app)
    manifest = c.get("/api/example/manifest").json()
    assert manifest["server_attaches_wording"] is True
    files = [_part(d["name"]) for d in manifest["documents"]]
    r = c.post("/api/bundle", files=files,
               data={"business_name": manifest["business_name"],
                     "attach_example_wording": "1"})
    assert r.status_code == 200, r.text
    rows = {row["filename"]: row for row in r.json()["intake"]["files"]
            if row.get("accepted")}

    attached = c.get(f"/api/source/{rows[WORDING]['document_id']}").json()
    assert attached["source_provenance"] == "CORPUS"

    own = c.get(f"/api/source/{rows[SCHEDULE]['document_id']}").json()
    assert own["source_provenance"] == "USER_UPLOAD"


def test_the_demo_cases_base_wording_is_recorded_as_a_corpus_document_too():
    """The server-seeded demo case reads the same published wording straight off the
    corpus path, so it must report the same provenance as the uploaded route. One rule,
    whichever way the document arrived."""
    c = TestClient(app)
    brief = c.get("/api/brief").json()
    assert brief["source"] == "DEMO_BUNDLE"

    seen: dict[str, str] = {}
    for f in brief["findings"]:
        for s in f.get("spans") or []:
            if not s.get("document_id"):
                continue
            meta = c.get(f"/api/source/{s['document_id']}").json()
            seen[meta["filename"]] = meta["source_provenance"]
            if meta["kind"] == "BASE_WORDING":
                assert meta["source_provenance"] == "CORPUS", meta["filename"]

    assert any(p == "CORPUS" for p in seen.values()), \
        f"no citation into the published wording: {seen}"
    # A structured demo fixture has no original bytes at all, and says so rather than
    # borrowing a provenance from the document next to it.
    for filename, prov in seen.items():
        if filename.lower().endswith(".json"):
            assert prov == "NOT_HELD", filename


def test_a_citation_drawn_outside_its_page_is_found_and_labelled():
    """MEASURED, in this project's own demo schedule.

    `02-policy-schedule.pdf` draws part of its content at y=832 on a 792pt page — below
    the page edge. pypdf reads the content stream and returns it, so the engine stored
    that line and cited it correctly. A render of the page box crops it out of the
    picture, because the text is genuinely outside the printable area. So the citation
    was real and the evidence invisible, and the drawer said "Not highlighted" about a
    perfectly good quote.

    The renderer extends the rendered area to include the text, reporting
    OFF_PAGE_CONTENT — which says the view was extended and that the source document is
    at fault. The alternative, silently cropping the evidence out of the picture, is the
    worse failure.

    The premise is now checked through PDFium (`pdfrender`), which replaced the previous
    renderer: PDFium's text layer FINDS the line, at coordinates outside the page box,
    where the old renderer clipped and found nothing. Same premise, measured with the
    engine that does the drawing.
    """
    if not sourceview.available():
        pytest.skip(RENDERER_ABSENT)
    from proven import pdfrender

    data = demodocs.read("02-policy-schedule.pdf")
    quote = "2.17 Theft Endorsement (Full Theft): NOT APPLICABLE"

    # The premise: in the file? Yes. Inside the visible page? No.
    with pdfrender.document(data) as doc:
        page = doc.page(0)
        rects = page.find(quote)
        assert rects, "premise gone: the text is no longer in the file at all"
        assert page.outside(rects), "premise gone: the text is now inside the page"
        left, bottom, right, top = page.box()
        page_height = top - bottom

    out = sourceview.render_page(data, 1, quote)
    assert out.available is True
    assert out.highlight.found is True
    assert out.highlight.method == sourceview.OFF_PAGE
    note = out.highlight.as_dict()["note"]
    assert "beyond its own page boundary" in note.lower()
    assert "extended" in note.lower(), "the note must say the view was extended"
    # The render is TALLER than the page, because the page was grown to include the line.
    # This assertion is what distinguishes a working fix from a silently clamped one.
    unextended = page_height * (sourceview.DISPLAY_DPI / 72.0)
    assert out.height > unextended, (
        f"render is {out.height}px, the same as the unextended page ({unextended}px) — "
        f"the off-page line was cropped out of the picture")


def _example_bundle(c: TestClient) -> dict:
    """Upload the worked example the way the browser does; return intake rows by name."""
    manifest = c.get("/api/example/manifest").json()
    files = [_part(d["name"]) for d in manifest["documents"]]
    r = c.post("/api/bundle", files=files,
               data={"business_name": manifest["business_name"],
                     "attach_example_wording": "1"})
    assert r.status_code == 200, r.text
    return {row["filename"]: row for row in r.json()["intake"]["files"]
            if row.get("accepted")}


def test_a_published_document_without_a_named_citation_shows_nothing_of_itself():
    """No citation, no paragraph — and never the page as a fallback."""
    c = TestClient(app)
    rows = _example_bundle(c)
    doc_id = rows[WORDING]["document_id"]
    for full in (False, True):
        d = c.get(f"/api/source/{doc_id}/page/1", params={"full": full}).json()
        assert d["display"] == "EXCERPT"
        assert d["available"] is False and "image" not in d
        assert d["excerpt"] is None
        assert "Open it from a finding" in d["reason"]
        assert d["publisher"]["url"] == corpus.resolve(demodocs.WORDING_CORPUS_ID).source_url


def test_an_excerpt_is_refused_for_a_page_the_citation_is_not_on():
    c = TestClient(app)
    rows = _example_bundle(c)
    doc_id = rows[WORDING]["document_id"]
    span = next(s for f in c.get("/api/brief").json()["findings"]
                for s in f.get("spans") or [] if s.get("document_id") == doc_id)
    wrong = 1 if span["page_no"] != 1 else 2
    r = c.get(f"/api/source/{doc_id}/page/{wrong}", params={"span_id": span["span_id"]})
    assert r.status_code == 400
    assert f"page {span['page_no']}" in r.json()["detail"]


def test_the_server_attached_wording_is_never_held_for_display():
    """Its provenance is recorded; its bytes are not retained. The bytes exist in the
    session only so a page can be drawn, and this document's pages are never drawn."""
    from proven.api import SESSIONS, _SOURCE_BYTES_KEY, _SOURCE_PROVENANCE_KEY

    c = TestClient(app)
    rows = _example_bundle(c)
    wording_id = rows[WORDING]["document_id"]
    schedule_id = rows[SCHEDULE]["document_id"]
    session = SESSIONS.get(c.cookies.get("proven_sid"))
    held = session.state[_SOURCE_BYTES_KEY]
    assert wording_id not in held, "a published wording's bytes were retained"
    assert schedule_id in held, "the user's own upload must still be retained"
    assert session.state[_SOURCE_PROVENANCE_KEY][wording_id] == "CORPUS"
    meta = c.get(f"/api/source/{wording_id}").json()
    assert meta["display"] == "EXCERPT" and meta["pages_viewable"] is False
    assert meta["publisher"]["available"] is True


def test_a_users_own_copy_of_a_published_wording_is_drawn_as_pages(client):
    """The rule is about who supplied the copy, not what the document is. A user who
    uploads the wording they were sent is shown their own document's pages."""
    if not sourceview.available():
        pytest.skip(RENDERER_ABSENT)
    span = _first_span(client)
    meta = client.get(f"/api/source/{span['document_id']}").json()
    assert meta["source_provenance"] == "USER_UPLOAD"
    assert meta["display"] == "PAGE" and meta["publisher"] is None
    page = client.get(f"/api/source/{span['document_id']}/page/{span['page_no']}",
                      params={"span_id": span["span_id"]}).json()
    assert page["display"] == "PAGE"
    assert page["image"].startswith("data:image/png;base64,")
    assert page["highlight"]["found"] is True


def test_an_exact_match_is_not_reported_as_off_page(client):
    """The extended search only runs when the ordinary one fails, so a normal citation
    must still come back EXACT and be rendered at the page's own size."""
    if not sourceview.available():
        pytest.skip(RENDERER_ABSENT)
    span = _first_span(client)
    d = client.get(f"/api/source/{span['document_id']}/page/{span['page_no']}",
                   params={"span_id": span["span_id"]}).json()
    assert d["highlight"]["method"] == sourceview.EXACT


# ── the module's own contract ────────────────────────────────────────────────

def test_the_renderer_reports_availability_by_detection():
    st = sourceview.status()
    assert st["available"] is sourceview.available()
    # PDFium replaced the previous renderer; the contract — named only when detected —
    # is unchanged.
    assert st["renderer"] == ("pdfium" if sourceview.available() else None)
    methods = {m["method"] for m in st["highlight_methods"]}
    assert methods == {sourceview.EXACT, sourceview.NORMALISED, sourceview.LEADING,
                       sourceview.OFF_PAGE, sourceview.NOT_FOUND}
    assert all(m["note"] for m in st["highlight_methods"])
    assert "offsets" in st["notice"]


def test_render_page_refuses_missing_bytes_rather_than_raising():
    out = sourceview.render_page(b"", 1, "anything")
    assert out.available is False
    assert "not held in this session" in out.reason
    assert out.as_dict()["highlight"]["found"] is False


def test_page_numbering_is_one_based_at_the_boundary():
    """`span.page_no` is 1-based and PDFium is 0-based. An off-by-one here shows the
    wrong page under a quotation, which reads as a fabricated citation."""
    if not sourceview.available():
        pytest.skip(RENDERER_ABSENT)
    data = demodocs.read(WORDING)
    assert sourceview.render_page(data, 0).available is False
    first = sourceview.render_page(data, 1)
    assert first.available is True and first.page_no == 1
    last = sourceview.render_page(data, first.page_count)
    assert last.available is True
    assert sourceview.render_page(data, first.page_count + 1).available is False
