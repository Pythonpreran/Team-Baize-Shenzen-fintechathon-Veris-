"""Every read of a corpus document is held to the manifest, not only the first one.

The first review of the corpus work found that the hash check at READ time was wired into
one of four places the published wording is read. The other three — the worked example's
server-side attach, the source viewer's fixture branch, and the cross-carrier comparison —
opened the file straight off the path, so a copy replaced by hand, restored from a stale
backup or half-written would still have been ingested and cited as the published text.

The same review found the page-marked TEXT was never verified at all, although it is the
artefact the committed page offsets were measured against and the manifest records its
hash wherever the team seeded a verified extraction.

These tests plant a wrong file where the right one should be and assert each read path
refuses it — with `CorpusDrifted`, which is deliberately not a `CorpusMissing`, because a
present-but-wrong document must fail loudly where an absent one degrades to a named skip.
"""
from __future__ import annotations

import json

import pytest

from proven import api, corpus, marketmap, pipeline

CHUBB = "sg-chubb-selectplus-wording"         # a PDF with a verified, seeded extraction
MSIG = "sg-msig-sumo-wording"                 # a text/plain retrieval


@pytest.fixture()
def scratch_corpus(tmp_path, monkeypatch):
    """An empty corpus directory the tests can plant files in."""
    root = tmp_path / "corpus"
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(root))
    return root


def _plant(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _log(text_state: str, doc_id: str = CHUBB) -> None:
    path = corpus.fetch_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"documents": [
        {"id": doc_id, "ok": True, "action": "seed", "text": text_state}]}),
        encoding="utf-8")


# ── the page-marked text ─────────────────────────────────────────────────────

def test_seeded_text_that_is_not_the_seed_is_refused_at_read_time(scratch_corpus):
    doc = corpus.resolve(CHUBB)
    assert doc.text is not None, "premise: this document carries a verified seed"
    _log("seeded")
    _plant(corpus.text_path(doc), b"=== PAGE 1 ===\nnot the extraction the offsets fit\n")

    with pytest.raises(corpus.CorpusDrifted) as exc:
        corpus.require_text(doc)
    assert "page-marked text" in str(exc.value)
    assert doc.text.sha256 in str(exc.value)
    assert not isinstance(exc.value, corpus.CorpusMissing)


def test_a_re_extracted_text_is_not_held_to_a_seed_it_was_not_written_from(scratch_corpus):
    """A download re-extracts with the installed pypdf, and the fetcher warns about the
    offsets when it does. That text has no single expected hash, so it is not refused —
    refusing it would make the download path unusable rather than safer."""
    doc = corpus.resolve(CHUBB)
    _log("extracted")
    path = corpus.text_path(doc)
    _plant(path, b"=== PAGE 1 ===\na fresh extraction\n")
    assert corpus.require_text(doc) == path


def test_a_text_retrieval_is_verified_against_the_publishers_own_hash(scratch_corpus):
    doc = corpus.resolve(MSIG)
    assert doc.media_type == corpus.TEXT_MEDIA
    _plant(corpus.text_path(doc), b"=== PAGE 1 ===\nedited by hand\n")
    with pytest.raises(corpus.CorpusDrifted):
        corpus.require_text(doc)


def test_present_text_and_original_are_none_when_absent_and_raise_when_wrong(
        scratch_corpus):
    assert corpus.present_text(MSIG) is None
    assert corpus.present_original(CHUBB) is None

    _plant(corpus.text_path(MSIG), b"wrong\n")
    with pytest.raises(corpus.CorpusDrifted):
        corpus.present_text(MSIG)
    _plant(corpus.original_path(CHUBB), b"%PDF-1.4 wrong\n")
    with pytest.raises(corpus.CorpusDrifted):
        corpus.present_original(CHUBB)


@pytest.mark.requires_corpus(CHUBB, MSIG)
def test_the_real_corpus_text_verifies():
    """The checks above, pointed at the corpus this build actually reads."""
    assert corpus.require_text(CHUBB) == corpus.text_path(CHUBB)
    assert corpus.require_text(MSIG) == corpus.text_path(MSIG)


# ── the three read paths that used to skip the check ────────────────────────

def test_the_worked_example_attach_refuses_a_drifted_wording(scratch_corpus):
    """`/api/bundle` with `attach_example_wording` feeds the browser-driven example.

    Absent is a supported answer there (the example runs on its synthetic documents and
    says so). Drifted is not: it must not be ingested as the published wording.
    """
    assert api._corpus_wording_upload() is None
    _plant(corpus.original_path(CHUBB), b"%PDF-1.4 a hand-replaced copy\n")
    with pytest.raises(corpus.CorpusDrifted):
        api._corpus_wording_upload()


def test_the_cross_carrier_comparison_refuses_a_drifted_wording(scratch_corpus):
    _plant(corpus.text_path(MSIG), b"=== PAGE 1 ===\nThe average condition is gone.\n")
    with pytest.raises(corpus.CorpusDrifted):
        pipeline._compare_carriers_uncached("verification-test")


def test_the_cross_carrier_comparison_still_skips_an_absent_wording(scratch_corpus):
    """Absence keeps its old meaning: that carrier is simply not in the comparison."""
    result = pipeline._compare_carriers_uncached("verification-test")
    assert isinstance(result, dict)


def test_the_clause_comparison_verifies_the_text_it_aligns(scratch_corpus):
    _plant(corpus.text_path(MSIG), b"=== PAGE 1 ===\nedited\n")
    with pytest.raises(corpus.CorpusDrifted):
        marketmap.build_market_map(corpus_dir=corpus.text_dir())


def test_the_source_viewer_never_reads_corpus_bytes_for_display(scratch_corpus):
    """The viewer's old fixture branch read the published wording straight off the path.

    It is gone rather than verified: a published document's pages are never drawn, so its
    bytes are never needed for display. Planting a wrong file there must therefore change
    nothing — the viewer does not open it.
    """
    _plant(corpus.original_path(CHUBB), b"%PDF-1.4 a hand-replaced copy\n")

    class _Session:
        state: dict = {}

    data = api._source_bytes_for(_Session(), "doc-x", pipeline.CHUBB_PDF.name)
    assert data == b""
    assert api._source_provenance_for(_Session(), "doc-x",
                                      pipeline.CHUBB_PDF.name) == api.PROVENANCE_CORPUS
