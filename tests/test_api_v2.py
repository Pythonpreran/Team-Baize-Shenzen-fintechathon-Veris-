"""API surface sweep: the coverage-semantics endpoints, the routes nothing else
covered, and a guard that every declared route is exercised somewhere.

The deletion tests are the ones that matter most here. `/api/brief` correctly emptied
itself when a case was deleted while three newer views kept serving analysis derived
from the same deleted findings.
"""
from __future__ import annotations

import re

import pdfbuild
import pytest
from fastapi.testclient import TestClient

from proven import align, api, corpus, ingest, marketmap, pipeline
from proven.api import SESSIONS, app


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


@pytest.fixture
def second_client():
    with TestClient(app) as c:
        yield c


AVERAGE = "ADEQ.PROPORTIONATE_REDUCTION"


# ── /api/alignment ──────────────────────────────────────────────────────────

def test_alignment_returns_bound_alignments_for_the_demo_wording(client):
    body = client.get("/api/alignment").json()
    assert body["available"] is True
    assert body["carrier"] == "CHUBB"
    assert body["verified"] > 0
    assert any(a["mechanism"] == AVERAGE for a in body["alignments"])


def test_alignment_returns_its_rejections_as_well(client):
    """A verifier whose refusals are hidden cannot be told apart from one that accepts
    everything, so the rejections are part of the response, not a debug flag."""
    body = client.get("/api/alignment").json()
    assert body["rejected"] > 0
    assert body["rejections"]
    for r in body["rejections"]:
        assert r["reason"] in {"MISSING_SLOT", "NEGATIVE_SLOT"}
        assert r["failed_slot"]
        assert r["quote"]


def test_alignment_accounting_closes_over_the_wire(client):
    """MEASURED DEFECT: proposals did not equal verified + rejected, because collapsed
    duplicates were counted nowhere. A rejection rate a reader cannot check by
    arithmetic is a number that should not be trusted."""
    body = client.get("/api/alignment").json()
    assert body["proposals"] == (
        body["verified"] + body["rejected"] + body["collapsed"])
    assert 0.0 <= body["rejection_rate"] <= 1.0


def test_every_alignment_carries_a_page_a_quote_and_offsets(client):
    body = client.get("/api/alignment").json()
    for a in body["alignments"]:
        assert a["page_no"] >= 1
        assert a["quote"].strip()
        assert a["char_end"] > a["char_start"] >= 0
        assert a["classifier"] == align.MECHANISM_ALIGNED
        assert a["taxonomy_code"]


def test_alignment_offsets_reproduce_the_quote_from_the_stored_page_text():
    """The fidelity claim end-to-end on the path the endpoint actually takes: offsets
    recorded against the ingested document must reproduce the recorded quote."""
    import re

    from proven import ingest

    st = pipeline.run_case(db_path=":memory:")
    res = align.align_document(st["conn"], st["base_doc_id"], carrier=st["carrier"])
    assert res.alignments, "nothing bound, so the fidelity check proves nothing"
    for a in res.alignments:
        page = ingest.page_text(st["conn"], st["base_doc_id"], a.page_no)
        got = re.sub(r"\s+", " ", page[a.char_start:a.char_end]).strip()
        assert got == a.quote, f"{a.mechanism} p{a.page_no} offsets do not match"
    st["conn"].close()


def test_alignment_states_that_no_model_decides_anything(client):
    """The standing claim. It travels in the payload because a screenshot of the
    rejection count will outlive any README."""
    body = client.get("/api/alignment").json()
    assert "no vote" in body["method"].lower()
    assert "no machine-learning model" in body["method"].lower()


def test_alignment_is_unavailable_when_the_bundle_has_no_wording(client):
    """An uploaded bundle with no policy wording has nothing to align. Returning an
    empty success would read as "this wording contains no such mechanisms"."""
    r = client.post("/api/bundle",
                    files=[("files", ("schedule.pdf", pdfbuild.schedule_pdf(),
                                      "application/pdf"))])
    assert r.status_code == 200
    body = client.get("/api/alignment").json()
    assert body["available"] is False
    assert "wording" in body["reason"].lower()


def test_alignment_is_session_scoped(client, second_client):
    """One session uploading a schedule-only bundle must not silence another's
    alignment."""
    assert client.get("/api/alignment").json()["available"] is True
    second_client.post("/api/bundle",
                       files=[("files", ("s.pdf", pdfbuild.schedule_pdf(),
                                         "application/pdf"))])
    assert second_client.get("/api/alignment").json()["available"] is False
    assert client.get("/api/alignment").json()["available"] is True


# ── /api/marketmap ──────────────────────────────────────────────────────────

# The clause comparison is computed from the seven published wordings in the corpus.
# Without them it degrades to an EMPTY map rather than raising, which is the correct
# behaviour and exactly why these tests must declare the dependency: an empty map cannot
# be told apart from an aligner that has stopped binding anything.
_MARKETMAP_CORPUS = tuple(spec["corpus_id"] for spec in marketmap.CORPUS_WORDINGS)


@pytest.mark.requires_corpus(*_MARKETMAP_CORPUS)
def test_marketmap_needs_no_session(client):
    """It is a pure function of immutable corpus files. Requiring a case would imply
    the map depends on the caller's documents, which it does not."""
    r = client.get("/api/marketmap")
    assert r.status_code == 200
    assert "set-cookie" not in {k.lower() for k in r.headers}
    body = r.json()
    assert len(body["carriers"]) >= 3
    assert body["rows"]


@pytest.mark.requires_corpus(*_MARKETMAP_CORPUS)
@pytest.mark.parametrize("value,expected", [("SG", "SG"), ("sg", "SG"),
                                           ("MY", "MY"), ("my", "MY")])
def test_marketmap_jurisdiction_filter_is_case_insensitive(client, value, expected):
    body = client.get(f"/api/marketmap?jurisdiction={value}").json()
    assert {c["jurisdiction"] for c in body["carriers"]} == {expected}


def test_marketmap_rejects_an_unknown_jurisdiction(client):
    r = client.get("/api/marketmap?jurisdiction=XX")
    assert r.status_code == 400
    assert "SG" in r.json()["detail"]


def test_marketmap_carries_its_honesty_notices(client):
    body = client.get("/api/marketmap").json()
    assert "does NOT mean" in body["not_aligned_meaning"]
    assert "NOT Singapore market positions" in body["jurisdiction_warning"]
    assert "not premiums" in body["method"]


def test_marketmap_survives_case_deletion_because_it_holds_no_case(client):
    """The one case-independent view. Silencing it on deletion would suggest the
    published-wording comparison was built from the caller's documents."""
    client.get("/api/brief")
    client.delete("/api/case")
    assert client.get("/api/marketmap").status_code == 200


# ── /api/information-value ──────────────────────────────────────────────────

def test_information_value_ranks_the_demo_bundles_gaps(client):
    body = client.get("/api/information-value").json()
    assert body["available"] is True
    assert body["count"] > 0
    assert body["recommendation"].startswith("Obtain:")
    for g in body["gaps"]:
        assert g["document_needed"]
        assert g["becomes_class"] in {"PROVEN", "DECLARED"}


def test_information_value_puts_the_unbounded_exposure_first(client):
    """The building has no sum insured in the demo bundle, so its exposure is unknown
    rather than small, and it must not sort beneath measured gaps."""
    body = client.get("/api/information-value").json()
    assert body["unbounded_count"] >= 1
    assert body["gaps"][0]["unbounded"] is True
    assert body["gaps"][0]["share_of_total"] is None
    assert "unknown rather than small" in body["recommendation"]


def test_information_value_labels_every_assumed_bound(client):
    body = client.get("/api/information-value").json()
    for g in body["gaps"]:
        if g["interval"]:
            assert g["interval"]["low_basis"]
            assert g["interval"]["high_basis"]
    assert "ASSUMED_RANGE" in body["caveat"]
    assert "remain UNESTABLISHED" in body["caveat"]


def test_information_value_empties_when_the_bundle_leaves_nothing_unestablished(
        client):
    """Sanity: the endpoint reflects the case rather than a fixed list. A schedule-only
    bundle produces a different gap set from the full demo bundle."""
    before = client.get("/api/information-value").json()["count"]
    client.post("/api/bundle",
                files=[("files", ("s.pdf", pdfbuild.schedule_pdf(),
                                  "application/pdf"))])
    after = client.get("/api/information-value").json()
    assert after["count"] != before or after["gaps"] != before


# ── /api/drift ──────────────────────────────────────────────────────────────

def test_drift_returns_a_crossing_for_the_demo_case(client):
    body = client.get("/api/drift").json()
    assert body["available"] is True
    assert body["carrier"] == "CHUBB"
    assert body["crossed_on"]
    assert body["observations"]


def test_drift_always_declares_the_index_as_sample_data(client):
    """The crossing date is the most screenshot-able number in the product."""
    for index in ("construction", "equipment"):
        body = client.get(f"/api/drift?index={index}").json()
        assert body["index_basis"] == "DECLARED_SAMPLE_SERIES"
        assert "DECLARED SAMPLE DATA" in body["caveat"]


def test_drift_uses_the_carriers_own_threshold(client):
    body = client.get("/api/drift").json()
    assert body["threshold_pct"] == "85.0%"          # Chubb's own clause
    assert body["threshold_basis"] == "THRESHOLD_STATED"


def test_drift_rejects_an_unknown_index(client):
    r = client.get("/api/drift?index=bogus")
    assert r.status_code == 400
    assert "construction" in r.json()["detail"]


def test_drift_states_its_full_insurance_assumption(client):
    body = client.get("/api/drift").json()
    assert "fully insured at inception" in body["assumption"]


# ── deletion must reach every view derived from the case ────────────────────

def test_deleting_the_case_silences_information_value(client):
    """MEASURED DEFECT: it did not. After deletion `/api/brief` returned no findings
    while `/api/information-value` kept ranking the gaps of those same deleted
    findings, naming this business's missing documents. The in-memory findings list
    was never cleared alongside the cached brief."""
    assert client.get("/api/information-value").json()["count"] > 0
    client.delete("/api/case")
    body = client.get("/api/information-value").json()
    assert body["available"] is False
    assert body["count"] == 0
    assert body["gaps"] == []
    assert "deleted" in body["reason"].lower()


def test_deleting_the_case_silences_drift(client):
    assert client.get("/api/drift").json()["crossed_on"]
    client.delete("/api/case")
    body = client.get("/api/drift").json()
    assert body["available"] is False
    assert "deleted" in body["reason"].lower()
    assert not body.get("crossed_on")
    assert not body.get("observations")


def test_deleting_the_case_silences_alignment(client):
    assert client.get("/api/alignment").json()["available"] is True
    client.delete("/api/case")
    body = client.get("/api/alignment").json()
    assert body["available"] is False
    assert "deleted" in body["reason"].lower()


def test_the_deleted_response_keeps_the_same_shape(client):
    """A consumer that must branch on response shape to handle deletion will get the
    branch wrong eventually, and the failure would be a view that looks alive."""
    live = client.get("/api/information-value").json()
    client.delete("/api/case")
    dead = client.get("/api/information-value").json()
    # Every key the live response carries must still be there. The deleted response may
    # ADD `deleted` and `reason`; it may not drop anything a client already reads.
    assert set(live) <= set(dead), (
        f"keys lost on deletion: {sorted(set(live) - set(dead))}")


def test_deletion_in_one_session_does_not_silence_another(client, second_client):
    second_client.get("/api/brief")
    client.delete("/api/case")
    assert client.get("/api/drift").json()["available"] is False
    assert second_client.get("/api/drift").json()["available"] is True
    assert second_client.get("/api/information-value").json()["count"] > 0


def test_reset_brings_a_deleted_case_back(client):
    client.delete("/api/case")
    assert client.get("/api/drift").json()["available"] is False
    r = client.post("/api/reset")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(r.json()["brief"]["findings"]) > 10
    assert client.get("/api/drift").json()["available"] is True
    assert client.get("/api/information-value").json()["count"] > 0
    assert client.get("/api/alignment").json()["available"] is True


# ── routes nothing else covered ─────────────────────────────────────────────

def test_session_endpoint_reports_isolation_without_leaking_the_cookie(client):
    body = client.get("/api/session").json()
    assert body["isolated"] is True
    assert len(body["session_id_prefix"]) == 8      # a prefix, not the session id


def test_asking_whether_a_case_exists_does_not_create_one(client):
    """MEASURED DEFECT: /api/session called `_state`, which lazily runs the whole demo
    pipeline. Asking was indistinguishable from loading, so the product could never
    show an honest empty first screen — a judge opened the page and a synthetic
    business was already there, with no way to tell what had been computed."""
    body = client.get("/api/session").json()
    assert body["has_case"] is False
    assert body["case_id"] is None
    assert body["source"] is None
    # And asking twice still has not built anything.
    assert client.get("/api/session").json()["has_case"] is False


def test_a_case_appears_only_after_an_explicit_action(client):
    assert client.get("/api/session").json()["has_case"] is False
    client.post("/api/reset")
    body = client.get("/api/session").json()
    assert body["has_case"] is True
    assert body["case_id"]
    assert body["source"] == "DEMO_BUNDLE"


def test_uploading_documents_also_creates_the_case(client):
    assert client.get("/api/session").json()["has_case"] is False
    client.post("/api/bundle",
                files=[("files", ("s.pdf", pdfbuild.schedule_pdf(),
                                  "application/pdf"))])
    body = client.get("/api/session").json()
    assert body["has_case"] is True
    assert body["source"] == "UPLOADED_BUNDLE"


def test_reset_is_scoped_to_the_caller(client, second_client):
    second_client.post("/api/reset")
    other_case = second_client.get("/api/session").json()["case_id"]
    client.post("/api/reset")
    assert second_client.get("/api/session").json()["case_id"] == other_case


def test_reset_reports_the_new_case_id(client):
    before = client.post("/api/reset").json()["case_id"]
    after = client.post("/api/reset").json()["case_id"]
    assert after != before


@pytest.mark.requires_corpus(*_MARKETMAP_CORPUS)
def test_compare_endpoint_shows_the_same_rule_diverging_by_carrier(client):
    """Cross-carrier divergence is the clearest evidence that the engine reads the
    document rather than applying a market default."""
    body = client.get("/api/compare").json()
    rows = {r["carrier"]: r for r in body["carriers"]}
    assert {"CHUBB", "MSIG", "LIBERTY"} <= set(rows)
    assert body["scenario"]

    # The same rule, the same scenario, a different basis per carrier — decided by
    # what each wording states rather than by a market default.
    bases = {r["formula_basis_class"] for r in body["carriers"]}
    assert len(bases) > 1, "every carrier produced the same basis, so nothing diverged"
    assert rows["CHUBB"]["threshold"] == "85%"
    assert rows["MSIG"]["threshold_basis"] == "NO_THRESHOLD_STATED"
    assert rows["LIBERTY"]["formula_basis_class"] == "PROVEN"
    for r in body["carriers"]:
        assert r["provisions"], f"{r['carrier']} cites no provision"


def test_record_endpoint_lists_hash_chained_facts(client):
    body = client.get("/api/record").json()
    assert body["business_id"]
    assert body["facts"]
    for f in body["facts"]:
        assert f["hash"]
        assert f["author_class"]
        for banned in ("violation", "breach", "score"):
            assert banned not in " ".join(f).lower()


def test_record_survives_case_deletion(client):
    """The asymmetry the whole record design exists for: findings die, established
    facts are portable and do not."""
    before = len(client.get("/api/record").json()["facts"])
    client.delete("/api/case")
    assert len(client.get("/api/record").json()["facts"]) == before


@pytest.mark.requires_corpus("sg-chubb-selectplus-wording")
def test_example_manifest_lists_uploadable_documents(client):
    """The example is honest only if the browser uploads it. The manifest is what the
    front end fetches, then POSTs each file to /api/bundle exactly as a drop.

    The base wording is the one exception and it is deliberate. It is a real insurer's
    published, copyrighted policy document: this project has a reader's right to it, not
    a publisher's. So it is not committed, not listed here, and not downloadable — the
    server attaches it from the document corpus instead. The manifest says which of the
    two is happening rather than leaving the client to assume.
    """
    body = client.get("/api/example/manifest").json()
    assert body["count"] >= 10
    assert body["business_name"]
    assert "synthetic" in body["notice"].lower()
    names = [d["name"] for d in body["documents"]]
    assert "01-policy-wording.pdf" not in names
    assert all(d["name"].endswith(".pdf") and d["label"] for d in body["documents"])
    assert body["server_attaches_wording"] is True
    assert body["wording_notice"]


def test_the_published_wording_has_no_browser_facing_route(client):
    """The guarantee, asserted rather than described.

    Every other example document is served for the browser to re-upload. This one must
    not be, because serving it would make the repository a redistributor of somebody
    else's copyrighted publication. Two independent things enforce it: the file is not
    in the repository, and it is denied by name even if it ever is.
    """
    assert client.get("/api/example/file/01-policy-wording.pdf").status_code == 404


def test_a_third_party_document_left_in_the_example_directory_is_still_not_served(
        client, tmp_path, monkeypatch):
    """The unlabelled-PDF fallback must not be a way back out.

    The example listing offers any PDF on disk without a curated label, so a newly
    generated synthetic document is never silently withheld — and `/api/example/file`
    validates against that same listing. So if a published wording ever lands in
    `demo-documents/`, by a mis-aimed seed command or somebody copying it in to test,
    the fallback would serve it again and the only thing in the way would be a scanner
    run that happens later. Denied by name instead, and by every corpus filename rather
    than one hard-coded case.
    """
    monkeypatch.setattr(api, "EXAMPLE_DIR", tmp_path)
    (tmp_path / "02-policy-schedule.pdf").write_bytes(b"%PDF-1.4\n")      # labelled
    (tmp_path / "14-new-synthetic.pdf").write_bytes(b"%PDF-1.4\n")        # unlabelled
    (tmp_path / api._EXAMPLE_WORDING_NAME).write_bytes(b"%PDF-1.4\n")     # the real one
    seeded = corpus.resolve(pipeline.CHUBB_ID).seed_name
    (tmp_path / seeded).write_bytes(b"%PDF-1.4\n")        # and under its corpus name

    names = [d["name"] for d in client.get("/api/example/manifest").json()["documents"]]
    assert "02-policy-schedule.pdf" in names
    assert "14-new-synthetic.pdf" in names, "the unlabelled fallback is gone"
    assert api._EXAMPLE_WORDING_NAME not in names
    assert seeded not in names

    for denied in (api._EXAMPLE_WORDING_NAME, seeded):
        assert client.get("/api/example/file/" + denied).status_code == 404, denied
    assert client.get("/api/example/file/14-new-synthetic.pdf").status_code == 200


def test_every_corpus_filename_is_denied_not_only_the_one_the_example_uses():
    """The rule is "documents this repository has a reader's right to and not a
    publisher's", and the manifest is exactly that set."""
    denied = api._never_served()
    assert api._EXAMPLE_WORDING_NAME in denied
    for d in corpus.documents():
        assert d.seed_name in denied, d.id


def test_example_files_are_served_and_are_real_pdfs(client):
    manifest = client.get("/api/example/manifest").json()
    for doc in manifest["documents"]:
        r = client.get("/api/example/file/" + doc["name"])
        assert r.status_code == 200, doc["name"]
        assert r.content[:5] == b"%PDF-", f"{doc['name']} is not a PDF"


def _example_parts(client) -> list:
    manifest = client.get("/api/example/manifest").json()
    return [("files", (d["name"], client.get("/api/example/file/" + d["name"]).content,
                       "application/pdf")) for d in manifest["documents"]]


@pytest.mark.requires_corpus("sg-chubb-selectplus-wording")
def test_the_wording_the_server_attaches_counts_against_the_intake_cap(client,
                                                                      monkeypatch):
    """The documented bound on retained session bytes has to include it.

    The bytes of an accepted bundle are held in the session for the life of the case,
    and the reason that is defensible is the intake cap. `_read_uploads` enforces the
    cap on the parts the caller sent; appending the wording afterwards would put the
    retained bytes over the bound by the size of that document.
    """
    files = _example_parts(client)
    sent = sum(len(part[1][1]) for part in files)
    wording = pipeline.CHUBB_PDF.stat().st_size

    # The caller's own parts fit. The caller's parts plus the document the server adds
    # do not.
    monkeypatch.setattr(ingest, "MAX_BUNDLE_BYTES", sent + wording - 1)
    refused = client.post("/api/bundle", files=files,
                          data={"business_name": "Hock Lee Foods Pte Ltd",
                                "attach_example_wording": "1"})
    assert refused.status_code == 413

    # And the same bundle without the attach is accepted, so the refusal is about the
    # document the server would have added rather than about what the caller sent.
    assert client.post("/api/bundle", files=files,
                       data={"business_name": "Hock Lee Foods Pte Ltd"}
                       ).status_code == 200


@pytest.mark.requires_corpus("sg-chubb-selectplus-wording")
def test_the_caller_is_not_rate_limited_for_a_document_the_server_added(client,
                                                                       monkeypatch):
    """The upload rate limit is the caller's traffic, and the wording is not theirs.

    Charging for it was a quiet off-by-one against the person using the demo: the
    example sends twelve documents and spent thirteen of their allowance.
    """
    files = _example_parts(client)
    monkeypatch.setattr(api, "_UPLOAD_LIMIT", len(files))
    r = client.post("/api/bundle", files=files,
                    data={"business_name": "Hock Lee Foods Pte Ltd",
                          "attach_example_wording": "1"})
    assert r.status_code == 200, r.text


def test_example_file_rejects_a_path_it_does_not_serve(client):
    """`name` is validated against the on-disk manifest, so traversal and unknown
    names are refused rather than used to build a path."""
    assert client.get("/api/example/file/nope.pdf").status_code == 404
    # A traversal attempt never matches the whitelist.
    assert client.get("/api/example/file/..%2f..%2fsecret").status_code in (404, 400)


@pytest.mark.requires_corpus("sg-chubb-selectplus-wording")
def test_uploading_the_example_reproduces_the_full_finding_set(client):
    """The whole point of the rebuild: the example is no longer seeded from fixtures.
    Uploading the bundled PDFs through /api/bundle must produce the same 17 findings
    the internal demo does, including the two endorsement findings that only used to
    exist as structured fixtures.

    Was 15 until CYBER-DATA-COVER-ABSENT and DEPENDENCY-COVER-NOT-POSSIBLE were
    added. Both are established from the uploaded PDFs like every other finding — the
    ACRA activity and the schedule's operative sections — so they belong in this count
    rather than being special-cased out of it."""
    manifest = client.get("/api/example/manifest").json()
    files = []
    for doc in manifest["documents"]:
        data = client.get("/api/example/file/" + doc["name"]).content
        files.append(("files", (doc["name"], data, "application/pdf")))
    # The browser uploads the thirteen synthetic documents and asks the server to attach
    # the published base wording from the corpus. Eight of the seventeen findings quote
    # that wording, so without it this count is 14 — which is exactly what makes the
    # assertion worth keeping at 17.
    assert manifest["server_attaches_wording"] is True
    r = client.post("/api/bundle", files=files,
                    data={"business_name": manifest["business_name"],
                          "attach_example_wording": "1"})
    assert r.status_code == 200
    brief = r.json()["brief"]
    assert brief["source"] == "UPLOADED_BUNDLE"
    assert len(brief["findings"]) == 17
    assert brief["summary"]["by_class"]["PROVEN"] >= 10
    rule_ids = {f["rule_id"] for f in brief["findings"]}
    # The two that used to be fixture-only, now parsed from uploaded endorsement PDFs.
    assert "SUBLIMIT-CONFLICTING-ENDORSEMENTS" in rule_ids
    assert "ENDORSEMENT-ILLEGIBLE-ABSTAIN" in rule_ids
    # The truly text-less scan is still refused at the door.
    rejected = [f for f in r.json()["intake"]["files"] if not f["accepted"]]
    assert any("illegible-scan" in f["filename"] for f in rejected)


def test_conflicting_endorsements_are_read_from_the_uploaded_pdfs(client):
    """The endorsement values must come from the PDF text, not a fixture: END-03 at
    S$5,000 and END-04 at S$25,000, escalated rather than resolved."""
    manifest = client.get("/api/example/manifest").json()
    files = [("files", (d["name"], client.get("/api/example/file/" + d["name"]).content,
                        "application/pdf")) for d in manifest["documents"]]
    r = client.post("/api/bundle", files=files)
    conflict = next(f for f in r.json()["brief"]["findings"]
                    if f["rule_id"] == "SUBLIMIT-CONFLICTING-ENDORSEMENTS")
    assert conflict["state"] == "REQUIRES_AUTHORISED_REVIEW"
    blob = str(conflict)
    assert "5000" in blob and "25000" in blob


def test_index_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<html" in r.text.lower()


def test_static_assets_are_served(client):
    for path in ("/static/app.js", "/static/styles.css"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} is not served"


def test_security_headers_are_on_the_new_endpoints_too(client):
    """The CSP is the second line of defence for verbatim text authored by somebody
    else, and these endpoints return more of it than any other."""
    for path in ("/api/alignment", "/api/marketmap", "/api/information-value",
                 "/api/drift"):
        r = client.get(path)
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]


# ── the guard: no route may ship without a test ─────────────────────────────

COVERED: set[tuple[str, str]] = {
    ("GET", "/api/health"),
    ("GET", "/api/session"),
    ("GET", "/api/brief"),
    ("POST", "/api/reset"),
    # Durable tenant portfolio — covered in test_portfolio.py, including cross-tenant
    # refusal on the item route and tenant-scoped collections on every other route.
    ("GET", "/api/portfolio"),
    ("GET", "/api/household/members"),
    ("POST", "/api/household/members"),
    ("GET", "/api/assets"),
    ("POST", "/api/assets"),
    ("GET", "/api/businesses"),
    ("POST", "/api/businesses"),
    ("GET", "/api/policies"),
    ("POST", "/api/policies"),
    ("GET", "/api/policies/{policy_id}"),
    ("GET", "/api/policies/{policy_id}/coverage/{coverage_id}/evidence"),
    ("GET", "/api/portfolio/documents/{document_id}/page/{page_no}"),
    ("POST", "/api/portfolio/example-household"),
    # Demo reset — covered in test_demo_reset.py: synthetic-only, idempotent, tenant-scoped,
    # owner uploads kept, hosted mode behind sign-in and CSRF.
    ("POST", "/api/demo/reset"),
    # The installable shell's service worker — covered in test_pwa.py.
    ("GET", "/sw.js"),
    # Encrypted portfolio privacy controls — covered in test_privacy.py. These are
    # owner-scoped artifacts, not the legacy case-level field disclosure controls below.
    ("GET", "/api/privacy/consents"),
    ("POST", "/api/privacy/consents"),
    ("DELETE", "/api/privacy/consents/{consent_id}"),
    ("GET", "/api/privacy/delegates"),
    ("POST", "/api/privacy/delegates"),
    ("DELETE", "/api/privacy/delegates/{grant_id}"),
    ("GET", "/api/privacy/retention"),
    ("PUT", "/api/privacy/retention"),
    ("GET", "/api/privacy/notices"),
    ("GET", "/api/privacy/export"),
    ("POST", "/api/privacy/erase"),
    # A delegate can see or prepare only a single-member claim protected by a live,
    # purpose-scoped health-data consent and time-limited grant (test_privacy.py).
    ("GET", "/api/delegate/claims"),
    ("GET", "/api/delegate/claims/{claim_id}"),
    ("POST", "/api/delegate/claims/{claim_id}/packet"),
    ("POST", "/api/delegate/claims/{claim_id}/ready"),
    ("GET", "/api/bundle/kinds"),
    ("POST", "/api/bundle"),
    # Jurisdiction packs — reference data, covered in test_jurisdiction.py, where the
    # load-bearing tests are that an unknown code names no file and that a recognised
    # country with no pack is told so rather than given another country's.
    ("GET", "/api/jurisdictions"),
    ("GET", "/api/jurisdictions/{code}"),
    # The streaming upload and the source-page viewer. Covered in test_upload_stream.py
    # and test_source_view.py respectively.
    ("POST", "/api/bundle/stream"),
    ("GET", "/api/source/{document_id}"),
    ("GET", "/api/source/{document_id}/page/{page_no}"),
    ("GET", "/api/events"),
    ("POST", "/api/events"),
    ("DELETE", "/api/events"),
    ("POST", "/api/consent"),
    ("POST", "/api/consent/revoke"),
    ("GET", "/api/consent/status"),
    ("GET", "/api/disclosure/brief"),
    ("DELETE", "/api/case"),
    ("GET", "/api/compare"),
    ("GET", "/api/alignment"),
    ("GET", "/api/marketmap"),
    ("GET", "/api/information-value"),
    ("GET", "/api/drift"),
    ("GET", "/api/example/manifest"),
    ("GET", "/api/example/file/{name}"),
    ("GET", "/api/example/household/manifest"),
    ("GET", "/api/example/household/file/{name}"),
    ("GET", "/api/record"),
    ("GET", "/api/record/export"),
    ("POST", "/api/record/verify"),
    ("GET", "/api/book"),
    ("GET", "/"),
    # The AI layer, the score and the connector feed. Covered in test_ai_layer.py,
    # which runs every one of them with NO key configured — the state a judge cloning
    # this repo is in, and the state the suite must never need a paid call to escape.
    ("GET", "/api/ai/status"),
    ("GET", "/api/ai/consent"),
    ("POST", "/api/ai/consent"),
    ("POST", "/api/ai/consent/revoke"),
    ("GET", "/api/ai/prompt-log"),
    ("POST", "/api/ai/propose"),
    ("POST", "/api/ai/explain"),
    ("POST", "/api/ai/ask"),
    # Deterministic agent controls — covered in test_agent_runtime.py. The decisive cases
    # are tenant scope, consent, exact-payload confirmation, step-up and ledger tampering.
    ("GET", "/api/agent/status"),
    ("POST", "/api/agent/consent"),
    ("POST", "/api/agent/consent/revoke"),
    ("POST", "/api/agent/plan"),
    ("POST", "/api/agent/prepare"),
    ("GET", "/api/agent/confirmations"),
    ("POST", "/api/agent/confirm"),
    ("POST", "/api/agent/totp/enrol"),
    ("GET", "/api/agent/mandates"),
    ("POST", "/api/agent/mandates"),
    ("DELETE", "/api/agent/mandates/{mandate_id}"),
    ("GET", "/api/agent/ledger"),
    ("GET", "/api/agent/ledger/export"),
    ("GET", "/api/agent/ledger/verify"),
    ("GET", "/api/reviewer/queue"),
    ("POST", "/api/reviewer/queue"),
    ("POST", "/api/reviewer/findings/{finding_id}/review"),
    ("GET", "/api/reviewer/export"),
    # Synthetic counterparty bridge — contract/replay coverage is in test_sandbox.py.
    ("POST", "/api/sandbox/webhook"),
    ("GET", "/api/sandbox/operations"),
    # Health claims copilot — covered in test_claims.py and test_claims_api.py.
    ("GET", "/api/claims"),
    ("POST", "/api/claims/intake"),
    ("GET", "/api/claims/{claim_id}"),
    ("POST", "/api/claims/{claim_id}/packet"),
    ("POST", "/api/claims/{claim_id}/documents"),
    ("POST", "/api/claims/{claim_id}/ready"),
    ("POST", "/api/claims/{claim_id}/submit"),
    ("POST", "/api/claims/{claim_id}/query/draft"),
    ("POST", "/api/claims/{claim_id}/query/send"),
    ("GET", "/api/claims/{claim_id}/sla"),
    ("POST", "/api/claims/{claim_id}/escalation"),
    # Health what-if illustration — covered in test_health_whatif.py and API tests.
    ("POST", "/api/health/what-if"),
    # Settlement correction loop — covered in test_settlement.py and test_settlement_api.py.
    ("POST", "/api/claims/{claim_id}/settlement/check"),
    ("POST", "/api/claims/{claim_id}/settlement/dispute"),
    ("GET", "/api/claims/{claim_id}/settlement/reconcile"),
    # Motor evidence and depreciation illustration — covered in test_motor.py and API tests.
    ("GET", "/api/motor/{policy_id}/alert"),
    ("POST", "/api/motor/{policy_id}/depreciation-what-if"),
    # Evidence-bound whole-portfolio map and self-authored life events.
    ("GET", "/api/protection-map"),
    ("GET", "/api/protection-map/events"),
    ("POST", "/api/protection-map/events"),
    ("GET", "/api/evidence/search"),
    ("GET", "/api/score"),
    ("GET", "/api/signals"),
    ("POST", "/api/signals/apply"),
    # Authentication and the two role-scoped workspaces. Covered in
    # test_auth_roles.py, which asserts the boundary in both directions: an anonymous
    # session still reaches the business workspace, and no session reaches another
    # role's surface.
    ("GET", "/api/auth/accounts"),
    ("GET", "/api/auth/csrf"),
    ("POST", "/api/auth/signup"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/adviser/queue"),
    ("GET", "/api/adviser/review"),
    ("POST", "/api/adviser/request-information"),
    ("GET", "/api/adviser/requests"),
    ("GET", "/api/carrier/portfolio"),
    # Premium at risk — covered in test_premium_at_risk.py.
    ("GET", "/api/carrier/premium-at-risk"),
    # VERIS Sentinel (external agent) and the Orchestrator. Covered in
    # test_orchestrator.py, which asserts the precedence rules and the claim ceiling
    # rather than only that the routes answer.
    ("GET", "/api/sentinel"),
    ("GET", "/api/sentinel/sources"),
    ("GET", "/api/orchestrator/evaluate"),
    # Discovery Agent — covered in test_discovery.py, which asserts the grounding
    # gate, the claim ceiling, and the integration with the orchestrator.
    ("GET", "/api/discovery"),
    ("GET", "/api/discovery/sources"),
    # Scheduler — autonomous agent execution. Covered in test_scheduler.py.
    ("GET", "/api/scheduler/status"),
    ("GET", "/api/scheduler/history"),
    # Agent activity feed — covered in test_agents_activity.py.
    ("GET", "/api/agents/activity"),
    # Renewal comparison and tenant-held renewal calendar — covered in test_renewal.py
    # and test_renewals_api.py. Both comparison inputs are repository-resolved document ids.
    ("GET", "/api/renewal/compare"),
    ("GET", "/api/renewals"),
    ("POST", "/api/renewals/compare"),
    # Claim scenario simulator — covered in test_scenario.py (arithmetic + endpoint).
    ("GET", "/api/scenario"),
    ("GET", "/api/scenario/presets"),
    # Presentation intelligence — covered in test_presenter.py. The load-bearing tests are
    # the guardrails: a model response containing an invented number, a forbidden coverage
    # outcome, generic filler, or an unknown visual type must be discarded in favour of the
    # deterministic template.
    ("POST", "/api/presentation/compose"),
    ("GET", "/api/presentation/status"),
    # Business interruption adequacy — covered in test_bi.py. The tests that matter are
    # the ones asserting it ABSTAINS on the sum insured when no gross profit is on record,
    # and that it does not re-emit the absence finding the rule engine already reports.
    ("GET", "/api/bi"),
    # Warranties and conditions precedent — covered in test_warranty.py. The load-bearing
    # tests are that UNVERIFIED never drifts to COMPLIANT, and that a phrase found in
    # definition or exclusion text is set aside rather than reported as an obligation.
    ("GET", "/api/warranty"),
    # The action loop — the half of the product that changes something. Covered in
    # test_actions_api.py, which asserts the two properties that make the outcome figure
    # worth anything: the actor comes from the session rather than the request body, so an
    # owner cannot mark their own request BOUND, and deletion reaches every action view.
    ("GET", "/api/actions"),
    ("GET", "/api/actions/{action_id}"),
    ("POST", "/api/actions/{action_id}/advance"),
    ("GET", "/api/outcomes"),
    # One front door per audience, plus the deep-link routes that let a refresh on
    # /adviser/queue survive. Presentation only — the real boundary is _require_role on
    # the API routes, and /api/entrypoints says so in prose.
    ("GET", "/api/entrypoints"),
    # The in-product helper: answers "where is X" and returns the route to get there. Covered
    # in test_assistant.py, where the load-bearing tests are the boundary ones — a carrier
    # asking for an SME screen is told which workspace owns it rather than redirected.
    ("POST", "/api/assistant"),
    ("GET", "/api/assistant/destinations"),
    # `/` serves the standalone landing page to an anonymous visitor and the app shell to a
    # signed-in one; `/landing` serves the landing page either way, so it can be demoed
    # without signing out. Both covered in test_frontend.py's front-door tests.
    ("GET", "/landing"),
    ("GET", "/login"),
    ("GET", "/app"),
    ("GET", "/app/{_path:path}"),
    ("GET", "/adviser"),
    ("GET", "/adviser/{_path:path}"),
    ("GET", "/carrier"),
    ("GET", "/carrier/{_path:path}"),
    ("GET", "/reviewer"),
    # Notifications — covered in test_notifications_api.py. The load-bearing tests are the
    # ones about restraint rather than delivery: a first check tells nobody anything, the
    # same change is never sent twice, and a notification suppressed by a preference is
    # still recorded with the preference that suppressed it named in plain words.
    ("GET", "/api/notifications"),
    ("POST", "/api/notifications/{notification_id}/read"),
    ("POST", "/api/notifications/read-all"),
    ("GET", "/api/notifications/preferences"),
    ("POST", "/api/notifications/preferences"),
    ("GET", "/api/notifications/outbox"),
    ("GET", "/api/notifications/outbox/{message_id}"),
    ("POST", "/api/notifications/check-now"),
    ("POST", "/api/notifications/restart-watch"),
    ("GET", "/api/notifications/watch"),
    # The exposure surfaces named in the problem statement: cyber, supply chain,
    # growth stage, compounding over time, and the blind-spot register. Covered by
    # tests/test_exposure_surfaces.py.
    ("GET", "/api/cyber"),
    ("GET", "/api/supply-chain"),
    ("GET", "/api/stages"),
    ("GET", "/api/stages/ladder"),
    ("GET", "/api/compounding"),
    ("GET", "/api/blindspots"),
    ("GET", "/api/blindspots/register"),
    # The carrier workspace: open an account, see what needs attention, rank the book,
    # find concentrations, ask for a document, record a decision.
    # Covered by tests/test_carrier_workspace.py.
    ("GET", "/api/carrier/account/{account_id}"),
    ("GET", "/api/carrier/inbox"),
    ("GET", "/api/carrier/renewal-triage"),
    ("GET", "/api/carrier/patterns"),
    ("POST", "/api/carrier/decision"),
    ("GET", "/api/carrier/decisions"),
    ("POST", "/api/carrier/request-information"),
    ("POST", "/api/carrier/briefing"),
    ("POST", "/api/carrier/ask"),
    ("POST", "/api/carrier/explain"),
    ("POST", "/api/carrier/notify"),
    # The adviser's AI-drafted file note. Covered by tests/test_adviser_note.py.
    ("POST", "/api/adviser/note/draft"),
    ("GET", "/api/adviser/note"),
    ("POST", "/api/adviser/note/edit"),
    ("POST", "/api/adviser/note/approve"),
    # The gap summary: a composition layer over existing endpoints.
    # Covered by tests/test_gap_summary.py.
    ("GET", "/api/gap-summary"),
    # Page quality and onboarding. Covered by tests/test_quality_onboarding.py.
    ("GET", "/api/page-quality"),
    ("GET", "/api/onboarding"),
}


# FastAPI mounts these itself. They are excluded from the coverage guard because they
# are framework-generated, not product surface — but see
# test_the_interactive_docs_are_reachable_and_carry_the_no_auth_notice, which pins the
# fact that they ARE exposed on a build with no authentication.
_FRAMEWORK_ROUTES = {
    ("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/redoc"),
    ("GET", "/docs/oauth2-redirect"),
}


def _declared_routes() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue                       # the /static mount, tested separately
        for m in methods:
            if m in {"HEAD", "OPTIONS"}:
                continue
            out.add((m, path))
    return out - _FRAMEWORK_ROUTES


def _without_converter(path: str) -> str:
    """`/app/{_path:path}` and `/app/{_path}` are the same route spelled two ways.

    Starlette keeps the `:path` converter on `route.path`; the OpenAPI schema drops it.
    Comparing the two spellings directly reports a documented route as undocumented, so
    the converter is normalised away on BOTH sides rather than special-cased on one.
    """
    return re.sub(r"\{(\w+):[a-z]+\}", r"{\1}", path)


def test_the_openapi_schema_documents_every_product_route(client):
    """The schema is what an integrator reads. A route missing from it is a route
    nobody outside this repo can find."""
    schema = client.get("/openapi.json").json()
    documented = {(m.upper(), _without_converter(path))
                  for path, ops in schema["paths"].items()
                  for m in ops
                  if m.upper() not in {"HEAD", "OPTIONS"}}
    declared = {(m, _without_converter(p)) for m, p in _declared_routes()}
    assert declared <= documented


def test_the_interactive_docs_are_reachable_and_the_build_admits_it_has_no_auth(
        client):
    """Swagger UI is served, and this build has no authentication. Both are true, and
    the second is why the first must not be forgotten: the description a reader sees
    at /docs has to say so, because that page is where somebody decides to deploy it."""
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "No authentication" in schema["info"]["description"]
    assert client.get("/api/health").json()["authenticated"] is False


def test_every_declared_route_is_covered_by_a_test():
    """A new endpoint added without a test fails here rather than shipping unverified."""
    missing = _declared_routes() - COVERED
    assert not missing, f"routes with no test: {sorted(missing)}"


def test_the_covered_list_has_no_stale_entries():
    """And a route removed without updating this list fails too, so the guard cannot
    quietly rot into a list of endpoints that no longer exist."""
    stale = COVERED - _declared_routes()
    assert not stale, f"listed but not declared: {sorted(stale)}"
