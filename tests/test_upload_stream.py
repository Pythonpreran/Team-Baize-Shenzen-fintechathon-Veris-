"""POST /api/bundle/stream — the upload, reported file by file as it happens.

The reason this endpoint exists is that the upload screen ticks each document off as it is
read, and there are only two ways to drive that: stream the real progress, or animate a
guess on the client. These tests pin the first, which means they assert the ORDER and the
CORRESPONDENCE of the events, not merely that some events arrive. A tick that appears
before the file was read would be exactly the unstated claim the product refuses.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import demodocs

from proven.api import app

WORDING = "01-policy-wording.pdf"
SCHEDULE = "02-policy-schedule.pdf"
UNREADABLE = "05-illegible-scan.pdf"

# The published wording is not committed; it comes from the corpus. These tests assert
# the ORDER and CORRESPONDENCE of per-file events across a real 28-page parse, so they
# need it rather than a stand-in.
pytestmark = pytest.mark.requires_corpus(demodocs.WORDING_CORPUS_ID)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _part(name: str):
    return demodocs.part(name)


def _stream(client: TestClient, names: list[str], **data) -> list[dict]:
    files = [_part(n) for n in names]
    with client.stream("POST", "/api/bundle/stream", files=files,
                       data=data or {"business_name": "Hock Lee Foods Pte Ltd"}) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["content-type"].startswith("application/x-ndjson")
        events = []
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_the_stream_is_newline_delimited_json_and_ends_with_the_bundle_payload(client):
    events = _stream(client, [WORDING, SCHEDULE])
    assert events[0] == {"event": "accepted", "total": 2}
    done = events[-1]
    assert done["event"] == "done"
    assert done["ok"] is True
    # The terminal event carries exactly what POST /api/bundle returns, so a caller needs
    # no follow-up request to render the result.
    assert "brief" in done and "intake" in done
    assert done["intake"]["accepted_count"] == 2


def test_every_file_is_reported_reading_then_read_in_upload_order(client):
    """The tick has to follow the work, not lead it.

    `reading` is emitted before the parse and `read` after it, so for each file the two
    must appear in that order and interleaved with no other file's pair — which is what
    makes the spinner-then-tick on the upload screen a report rather than an animation.
    """
    events = _stream(client, [WORDING, SCHEDULE])
    phases = [(e["event"], e["filename"]) for e in events
              if e["event"] in {"reading", "read"}]
    assert phases == [("reading", WORDING), ("read", WORDING),
                      ("reading", SCHEDULE), ("read", SCHEDULE)]


def test_a_read_event_carries_what_the_tick_claims(client):
    events = _stream(client, [WORDING])
    read = next(e for e in events if e["event"] == "read")
    assert read["accepted"] is True
    assert read["kind"] == "BASE_WORDING"
    assert read["pages"] > 1
    assert read["index"] == 0 and read["total"] == 1


def test_an_unreadable_file_is_reported_against_itself_and_does_not_fail_the_bundle(
        client):
    """One bad PDF in a bundle marks that row and leaves the others accepted.

    The scan has no text layer, which is a per-file failure by design — the product
    reports it rather than guessing at the page with OCR.
    """
    events = _stream(client, [WORDING, UNREADABLE, SCHEDULE])
    reads = {e["filename"]: e for e in events if e["event"] == "read"}
    assert reads[UNREADABLE]["accepted"] is False
    assert reads[UNREADABLE]["code"] == "NO_TEXT_LAYER"
    assert reads[UNREADABLE]["message"]
    assert reads[WORDING]["accepted"] is True
    assert reads[SCHEDULE]["accepted"] is True
    done = events[-1]
    assert done["event"] == "done"
    assert done["intake"]["accepted_count"] == 2
    assert done["intake"]["rejected_count"] == 1


def test_stored_events_carry_the_document_id_and_reading_precedes_analysing(client):
    events = _stream(client, [WORDING, SCHEDULE])
    order = [e["event"] for e in events]
    # Every file is read before anything is analysed: the two phases take noticeably
    # different amounts of time and the interface distinguishes them.
    assert order.index("analysing") > max(
        i for i, e in enumerate(order) if e == "read")
    stored = [e for e in events if e["event"] == "stored"]
    assert len(stored) == 2
    assert all(e["document_id"] for e in stored)


def test_the_stream_and_the_blocking_route_agree(client):
    """Two routes, one pipeline. If they could disagree, one of them is lying."""
    streamed = _stream(client, [WORDING, SCHEDULE])[-1]
    blocking = client.post(
        "/api/bundle", files=[_part(WORDING), _part(SCHEDULE)],
        data={"business_name": "Hock Lee Foods Pte Ltd"}).json()
    assert streamed["intake"]["accepted_count"] == \
        blocking["intake"]["accepted_count"]
    assert {f["kind"] for f in streamed["intake"]["files"]} == \
        {f["kind"] for f in blocking["intake"]["files"]}
    assert len(streamed["brief"]["findings"]) == len(blocking["brief"]["findings"])


def test_the_role_boundary_is_an_http_status_not_a_stream_event(client):
    """A guard that fails must fail BEFORE the 200.

    Once a status line and the first byte are on the wire, a 403 can no longer be
    expressed. So the role check runs ahead of the response rather than arriving as a
    terminal error event, which a client would have to parse to discover it was refused.
    """
    client.post("/api/auth/login", json={"email": "underwriter@carrier.demo",
                                         "password": "Demo@123"})
    r = client.post("/api/bundle/stream", files=[_part(WORDING)])
    assert r.status_code == 403
    assert "carrier" in r.json()["detail"].lower()


def test_a_bundle_nothing_could_be_read_from_ends_in_a_terminal_error_event(client):
    """The failure arrives as an `error` event, and the client is written for it."""
    events = _stream(client, [UNREADABLE])
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "BUNDLE_INVALID"
    assert not any(e["event"] == "done" for e in events)


def test_the_stream_hands_back_the_session_cookie(client):
    """MEASURED BUG. The upload worked and then vanished.

    `_session` sets the cookie on the INJECTED `Response`, and FastAPI merges that
    object's headers only when the handler returns a dict or a model. A handler returning
    its own `StreamingResponse` drops the Set-Cookie, so a first-time caller uploaded into
    a session whose id it was never told — and its next request created a fresh session
    that fell back to the demo fixture. Every event still looked correct.

    Caught by driving the live server over real HTTP rather than by reading the code, which
    is the second time that has been true of a bug in this file.
    """
    fresh = TestClient(app)
    with fresh.stream("POST", "/api/bundle/stream", files=[_part(WORDING)]) as r:
        assert r.status_code == 200
        assert any(c.lower() == "proven_sid" for c in r.cookies), \
            "the streaming upload did not set the session cookie"
        events = [json.loads(x) for x in r.iter_lines() if x.strip()]
    assert events[-1]["event"] == "done"
    # And the SAME case is what the next request reads, rather than a new session's
    # fixture fallback.
    uploaded = {f["filename"] for f in events[-1]["intake"]["files"]}
    assert uploaded == {WORDING}
    brief = fresh.get("/api/brief").json()
    assert brief["source"] == "UPLOADED_BUNDLE"
