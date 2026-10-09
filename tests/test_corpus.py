"""The document corpus: the manifest, the lookup, and the fetcher.

WHY THIS FILE EXISTS. The engine reads real insurer wordings. Those documents cannot be
committed — they are other people's copyrighted publications — so the repository holds a
manifest and the bytes live outside git. That arrangement has two failure modes worth
defending against, and they fail in opposite directions:

  1. The corpus is absent and the suite passes anyway because tests that need a wording
     quietly assert nothing. The run looks identical whether a 28-page wording was read
     or not, which makes the measurement worthless.

  2. The corpus is present but is not the document the manifest describes. Then a page
     citation points at real-looking text that is not the text the citation was taken
     from, which is a fabricated citation rendered by the product's own evidence viewer.

So: absence must be a NAMED skip, and a hash mismatch must be an error rather than a
warning anyone can scroll past.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from proven import corpus

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script(name: str):
    """Import a file from `scripts/`, which is deliberately not a package.

    Those files are entry points run as `python scripts/x.py`; making them a package
    would invite production code to import them, and a one-shot maintenance script is
    not something the engine should be able to call.
    """
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetch_corpus = _load_script("fetch_corpus")


# ── the manifest is valid, and validation is not decorative ─────────────────

def test_the_committed_manifest_loads_and_validates():
    manifest = corpus.load_manifest(refresh=True)
    assert manifest.version >= 1
    assert len(manifest.documents) >= 20, "the manifest lost documents"


def test_every_document_records_what_it_is_and_what_it_is_not_for():
    for d in corpus.documents():
        assert d.title.strip(), f"{d.id} has no title"
        assert d.publisher.strip(), f"{d.id} has no publisher"
        assert d.usage_note.strip(), f"{d.id} has no usage note"
        assert d.bytes > 0, f"{d.id} records no size"


def test_no_document_may_be_redistributed():
    """Not a style check. A `true` here would mean somebody had decided a third party's
    wording could be committed, and the whole point of the corpus is that no such entry
    exists."""
    assert all(d.redistribute is False for d in corpus.documents())
    assert corpus.status()["redistributable"] == []


def test_a_source_url_is_either_recorded_or_openly_declared_missing():
    """We do not invent a URL to fill the field.

    Three documents were retrieved before the team started capturing URLs. Marking them
    UNRECORDED keeps them usable from a local copy while making it impossible to claim a
    provenance nobody has. A guessed URL that happens to 404 is a worse outcome than an
    honest blank.
    """
    for d in corpus.documents():
        if d.source_url_status == "RECORDED":
            assert d.source_url and d.source_url.startswith("https://")
            assert d.downloadable
        else:
            assert d.source_url is None
            assert not d.downloadable


def test_the_validator_rejects_a_redistributable_entry(tmp_path):
    bad = _one_entry_manifest(redistribute=True)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(corpus.ManifestError) as exc:
        corpus.load_manifest(path, refresh=True)
    assert "redistribute" in str(exc.value)


def test_the_validator_rejects_a_missing_usage_note(tmp_path):
    bad = _one_entry_manifest(usage_note="   ")
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(corpus.ManifestError) as exc:
        corpus.load_manifest(path, refresh=True)
    assert "usage_note" in str(exc.value)


def test_the_validator_rejects_a_malformed_hash(tmp_path):
    bad = _one_entry_manifest(sha256="not-a-hash")
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(corpus.ManifestError) as exc:
        corpus.load_manifest(path, refresh=True)
    assert "sha256" in str(exc.value)


def test_the_validator_rejects_a_duplicate_id(tmp_path):
    doc = _one_entry_manifest()
    doc["documents"].append(dict(doc["documents"][0]))
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(corpus.ManifestError) as exc:
        corpus.load_manifest(path, refresh=True)
    assert "duplicate" in str(exc.value)


def test_the_validator_rejects_an_unknown_field(tmp_path):
    """`extra="forbid"`, so a typo in a field name fails loudly instead of being
    silently ignored — a misspelled `redistribut:` would otherwise default to a value
    nobody chose."""
    bad = _one_entry_manifest()
    bad["documents"][0]["redistribut"] = False
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(corpus.ManifestError):
        corpus.load_manifest(path, refresh=True)


def _one_entry_manifest(**overrides) -> dict:
    entry = {
        "id": "test-doc",
        "title": "A test document",
        "publisher": "Nobody",
        "source_url": "https://example.invalid/x.pdf",
        "source_url_status": "RECORDED",
        "country": "SG",
        "line_of_business": "SME_PACKAGE",
        "document_class": "POLICY_WORDING",
        "media_type": "application/pdf",
        "seed_path": "x.pdf",
        "sha256": "0" * 64,
        "bytes": 10,
        "pages": 1,
        "redistribute": False,
        "usage_note": "testing",
    }
    entry.update(overrides)
    return {"version": 1, "documents": [entry]}


# ── lookup is by id ─────────────────────────────────────────────────────────

def test_documents_resolve_by_manifest_id():
    d = corpus.resolve("sg-msig-sumo-wording")
    assert d.country == "SG"
    assert d.document_class == "POLICY_WORDING"


def test_documents_also_resolve_by_published_filename_as_an_alias():
    """The evaluation labels record the file a human actually read. Keeping the filename
    resolvable preserves the link between a label and its source document."""
    d = corpus.resolve("sg-msig-sumo-sme-policy-wording.txt")
    assert d.id == "sg-msig-sumo-wording"


def test_an_unknown_reference_names_what_is_known():
    with pytest.raises(corpus.ManifestError) as exc:
        corpus.resolve("no-such-document")
    assert "sg-msig-sumo-wording" in str(exc.value)


def test_the_corpus_directory_is_read_from_the_environment_at_call_time(monkeypatch,
                                                                       tmp_path):
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path))
    assert corpus.corpus_dir() == tmp_path
    assert corpus.text_path("sg-msig-sumo-wording").parent == tmp_path / "text"


def test_a_path_inside_a_caller_named_directory_still_resolves_the_id_first(tmp_path):
    """The clause comparison takes its corpus directory as an argument, so it cannot
    use `text_path()`. It must still go through the manifest."""
    assert (corpus.text_path_in(tmp_path, "sg-msig-sumo-wording")
            == tmp_path / "sg-msig-sumo-wording.txt")
    # The filename alias resolves to the id, so one document has one path whichever
    # name the caller held.
    assert (corpus.text_path_in(tmp_path, "sg-msig-sumo-sme-policy-wording.txt")
            == tmp_path / "sg-msig-sumo-wording.txt")


def test_an_unknown_id_raises_instead_of_producing_a_path_that_does_not_exist(tmp_path):
    """This is the whole reason the helper exists. `directory / f"{id}.txt"` built by
    hand turns a mistyped id into a file that merely is not there, and every caller
    skips a document whose file is absent — so the typo would quietly shrink the clause
    comparison, which is indistinguishable from an aligner that stopped binding."""
    with pytest.raises(corpus.ManifestError):
        corpus.text_path_in(tmp_path, "sg-msig-sumo-wordign")      # transposed


def test_the_clause_comparison_refuses_a_wording_id_the_manifest_does_not_list(
        tmp_path, monkeypatch):
    """Measured through the real consumer rather than asserted about the helper."""
    from proven import book, marketmap

    bad = dict(marketmap.CORPUS_WORDINGS[0])
    bad["corpus_id"] = "no-such-wording"
    monkeypatch.setattr(marketmap, "CORPUS_WORDINGS", (bad,))
    with pytest.raises(corpus.ManifestError):
        marketmap.build_market_map(corpus_dir=tmp_path)
    with pytest.raises(corpus.ManifestError):
        book.wording_portfolio(corpus_dir=tmp_path)


def test_a_text_retrieval_has_no_separate_original():
    """For a text retrieval the retrieval IS the text. Two names for one file is how a
    caller ends up verifying one and reading the other."""
    text_doc = corpus.resolve("sg-msig-sumo-wording")
    assert text_doc.media_type == corpus.TEXT_MEDIA
    assert corpus.original_path(text_doc) == corpus.text_path(text_doc)

    pdf_doc = corpus.resolve("sg-chubb-selectplus-wording")
    assert pdf_doc.media_type == corpus.PDF_MEDIA
    assert corpus.original_path(pdf_doc) != corpus.text_path(pdf_doc)


# ── absence is a supported state, and it says what is missing ───────────────

def test_an_absent_corpus_reports_every_document_as_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "nothing-here"))
    absent = corpus.missing()
    assert len(absent) == len(corpus.documents())
    assert corpus.is_seeded() is False
    assert corpus.status()["complete"] is False


def test_requiring_an_absent_document_says_how_to_fetch_it(monkeypatch, tmp_path):
    """The error a developer hits must carry the command, not just the path. An
    exception that only names a missing file sends the reader hunting for a script."""
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "nothing-here"))
    with pytest.raises(corpus.CorpusMissing) as exc:
        corpus.require_original("sg-chubb-selectplus-wording")
    message = str(exc.value)
    assert "fetch_corpus.py" in message
    assert "sg-chubb-selectplus-wording" in message


def test_the_requires_corpus_marker_is_registered():
    """`--strict-markers` is on, so an unregistered marker is an error rather than a
    typo that silently never skips anything."""
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml")
    assert "requires_corpus" in pyproject.read_text(encoding="utf-8")


# ── page splitting matches every other reader in the codebase ───────────────

def test_page_markers_are_counted_not_guessed():
    raw = ("SOURCE_URL: https://example.invalid/x.pdf\nCONTENT_TYPE: application/pdf\n"
           "BYTES: 10\n\n\n=== PAGE 1 ===\nfirst\n\n\n=== PAGE 2 ===\nsecond\n")
    assert corpus.count_pages(raw) == 2
    # split_pages keeps the header block as its leading element, because the clause
    # comparison and the alignment harness have always read these files that way and
    # their page numbering is calibrated to it.
    assert len(corpus.split_pages(raw)) == 3


def test_the_longer_legacy_marker_is_still_recognised():
    """An earlier extraction used `===== PAGE n =====`. Both forms contain
    `=== PAGE `, so both split correctly and neither corpus file needs rewriting."""
    raw = "===== PAGE 1 =====\nfirst\n===== PAGE 2 =====\nsecond\n"
    assert corpus.count_pages(raw) == 2


def test_text_with_no_markers_counts_zero_pages_and_still_yields_content():
    """An HTML retrieval has no pagination. Zero pages is the right answer, and it must
    still contribute text rather than scoring nothing."""
    raw = "a product web page with no page markers at all"
    assert corpus.count_pages(raw) == 0
    assert corpus.split_pages(raw) == [raw]


# ── the fetcher ─────────────────────────────────────────────────────────────

@pytest.mark.requires_corpus("sg-msig-sumo-wording")
def test_seeding_writes_the_original_and_the_page_marked_text(monkeypatch, tmp_path):
    doc = corpus.resolve("sg-msig-sumo-wording")
    # Read the real bytes BEFORE repointing the corpus directory, so the seed copy is
    # the document the manifest describes and the hash check is exercised for real.
    payload = corpus.text_path(doc).read_bytes()

    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    seed = tmp_path / "seed"
    target = seed / doc.seed_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    record = fetch_corpus.seed_one(doc, seed)
    assert record["ok"] is True, record
    assert record["pages"] == doc.pages
    assert corpus.text_path(doc).is_file()


def test_a_seeded_file_with_the_wrong_bytes_is_an_error_not_a_warning(tmp_path,
                                                                     monkeypatch):
    """The committed page citations were taken against the bytes in the manifest. A
    different file with the same name is the one case where carrying on quietly would
    put a wrong page under a correct-looking quotation."""
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    seed = tmp_path / "seed"
    doc = corpus.resolve("sg-msig-sumo-wording")
    target = seed / doc.seed_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"this is not the document the manifest describes")

    record = fetch_corpus.seed_one(doc, seed)
    assert record["ok"] is False
    assert "sha256 mismatch" in record["error"]
    assert record["expected_sha256"] == doc.sha256
    assert record["actual_sha256"] != doc.sha256
    # And nothing was written, so a mismatch cannot half-populate the corpus.
    assert not corpus.text_path(doc).exists()


def test_a_missing_seed_file_is_reported_with_the_path_it_looked_in(tmp_path,
                                                                   monkeypatch):
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    doc = corpus.resolve("sg-msig-sumo-wording")
    record = fetch_corpus.seed_one(doc, tmp_path / "empty")
    assert record["ok"] is False
    assert doc.seed_path in record["error"].replace("\\", "/")


def test_a_document_with_no_recorded_url_cannot_be_downloaded():
    """It can still be seeded from a local copy. What it cannot do is reach the network
    on a URL nobody recorded — there is no URL to guess at, and guessing is the failure
    this status exists to prevent."""
    doc = corpus.resolve("sg-aig-homes-advantage-wording")
    assert doc.source_url_status == "UNRECORDED"
    record = fetch_corpus.download_one(doc)
    assert record["ok"] is False
    assert "no source_url" in record["error"]


def test_extracted_text_reproduces_the_existing_corpus_structure():
    """The format is not cosmetic. Every committed page citation was measured against
    text in this shape: three header lines, two blank lines, then the markers."""
    doc = corpus.resolve("sg-chubb-selectplus-wording")
    rendered = fetch_corpus._render_text(["page one text", "page two text"], doc, 1234)
    lines = rendered.split("\n")
    assert lines[0].startswith("SOURCE_URL: ")
    assert lines[1] == "CONTENT_TYPE: application/pdf"
    assert lines[2] == "BYTES: 1234"
    assert lines[3] == ""
    assert lines[4] == ""
    assert lines[5] == "=== PAGE 1 ==="
    assert corpus.count_pages(rendered) == 2


def test_rendered_text_is_written_with_crlf_endings(tmp_path):
    """The existing corpus is CRLF. A citation offset computed against CRLF text does
    not survive the file being rewritten as LF, and the difference is invisible on
    screen."""
    doc = corpus.resolve("sg-chubb-selectplus-wording")
    out = tmp_path / "x.txt"
    fetch_corpus._write_text(out, fetch_corpus._render_text(["a"], doc, 1))
    raw = out.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "a bare LF got through"


def test_an_unrecorded_url_is_labelled_in_the_rendered_header():
    """The header has to say the URL is missing rather than leave an empty field that
    reads like a retrieval from nowhere."""
    doc = corpus.resolve("sg-aig-homes-advantage-wording")
    rendered = fetch_corpus._render_text(["a"], doc, 1)
    assert "NOT RECORDED" in rendered.split("\n")[0]


def test_a_re_extraction_on_a_path_with_no_verified_seed_warns_about_the_offsets(
        tmp_path, monkeypatch):
    """`extracted` on its own is too quiet a label for this.

    The manifest records a verified extraction for the PDF entries, because the
    committed page citations were measured against that text. A download has no local
    seed directory to read it from and re-extracts with whatever pypdf is installed,
    which the fetcher's own docstring says can shift page offsets — and a shifted offset
    turns a correct citation into a wrong one pointing at real-looking text.
    """
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    doc = corpus.resolve("sg-chubb-selectplus-wording")
    assert doc.text is not None, "this document is supposed to carry a verified seed"

    pdf = _one_page_pdf()
    record = fetch_corpus._store(doc, pdf, seed_dir=None, action="download")
    assert record["text"] == "extracted"
    warnings = " ".join(record.get("warnings", ()))
    assert "re-extracted" in warnings
    assert "page offsets" in warnings
    assert "--seed-from" in warnings


def test_a_publisher_change_is_quarantined_rather_than_written_over_the_verified_copy(
        tmp_path, monkeypatch):
    """A publisher may republish. Overwriting the verified copy is what must not happen.

    The committed page citations were measured against the manifest's bytes. A reader
    quoting page 12 has no way to tell that the file underneath it changed, so the new
    bytes are kept outside the directories anything reads from, both hashes are
    recorded, and the document counts as not fetched — which the suite already reports
    as a named skip and the demo already handles as a degraded mode.
    """
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    doc = corpus.resolve("sg-chubb-selectplus-wording")

    # A verified copy is already in place.
    verified = corpus.original_path(doc)
    verified.parent.mkdir(parents=True, exist_ok=True)
    verified.write_bytes(b"the bytes the manifest describes")

    drifted = b"%PDF-1.4 the publisher changed this file\n"
    record = fetch_corpus._quarantine(doc, drifted, corpus.sha256_bytes(drifted))

    assert record["ok"] is False
    assert record["hash_changed"] is True
    assert record["expected_sha256"] == doc.sha256
    assert record["actual_sha256"] == corpus.sha256_bytes(drifted)
    assert verified.read_bytes() == b"the bytes the manifest describes"
    quarantined = Path(record["quarantined"])
    assert quarantined.read_bytes() == drifted
    assert corpus.quarantine_dir() in quarantined.parents


def test_a_download_whose_hash_drifted_never_reaches_the_corpus(tmp_path, monkeypatch):
    """The same property through `download_one`, with the network stubbed out."""
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    doc = corpus.resolve("sg-chubb-selectplus-wording")
    drifted = b"%PDF-1.4 not the manifest's bytes\n"

    class _Resp:
        def read(self):
            return drifted

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(fetch_corpus.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())
    record = fetch_corpus.download_one(doc)
    assert record["ok"] is False
    assert record["quarantined"]
    assert not corpus.original_path(doc).exists()
    assert not corpus.text_path(doc).exists()
    assert corpus.missing([doc.id]) == [doc.id]


def test_stored_bytes_are_verified_when_they_are_read_not_only_when_fetched(
        tmp_path, monkeypatch):
    """Fetching verified the hash; until now nothing verified it again.

    A corpus file replaced by hand, restored from a stale backup or left behind by an
    interrupted write would be read as authoritative, and every citation taken against
    the manifest's bytes would point at text it was not taken from. That is a fabricated
    citation rendered by the product's own evidence viewer.
    """
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    doc = corpus.resolve("sg-chubb-selectplus-wording")
    planted = corpus.original_path(doc)
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(b"%PDF-1.4 a different document entirely\n")

    with pytest.raises(corpus.CorpusDrifted) as exc:
        corpus.require_original(doc)
    message = str(exc.value)
    assert doc.sha256 in message
    assert "manifest" in message
    # And it is NOT reported as absence: absence is a supported, skippable state, and
    # a file that is present but wrong is a different fact with a different remedy.
    assert not isinstance(exc.value, corpus.CorpusMissing)


@pytest.mark.requires_corpus("sg-chubb-selectplus-wording")
def test_the_real_corpus_copy_matches_the_manifest():
    """The verification above, pointed at the documents this build actually reads."""
    doc = corpus.resolve("sg-chubb-selectplus-wording")
    assert corpus.verify_stored(doc) == corpus.original_path(doc)


def test_a_single_document_run_does_not_erase_the_log_for_the_others(tmp_path,
                                                                    monkeypatch):
    """`--only <id>` fetches one document; the log is the only account of when each of
    the others was retrieved and whether its hash drifted."""
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    first = {"id": "sg-msig-sumo-wording", "ok": True, "action": "seed",
             "pages": 1, "text": "verbatim", "when": "2026-10-01T00:00:00+00:00"}
    fetch_corpus._write_log([first], seed_from=tmp_path)

    second = {"id": "sg-chubb-selectplus-wording", "ok": True, "action": "seed",
              "pages": 28, "text": "seeded", "when": "2026-10-09T00:00:00+00:00"}
    fetch_corpus._write_log([second], seed_from=tmp_path)

    log = json.loads(corpus.fetch_log_path().read_text(encoding="utf-8"))
    recorded = {rec["id"]: rec for rec in log["documents"]}
    assert set(recorded) == {"sg-msig-sumo-wording", "sg-chubb-selectplus-wording"}
    assert recorded["sg-msig-sumo-wording"]["when"] == first["when"]
    # And the merged log still says what the most recent run touched.
    assert log["last_run"] == ["sg-chubb-selectplus-wording"]


def test_a_second_run_of_the_same_document_replaces_its_record(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    fetch_corpus._write_log(
        [{"id": "sg-msig-sumo-wording", "ok": False, "error": "not found"}],
        seed_from=tmp_path)
    fetch_corpus._write_log(
        [{"id": "sg-msig-sumo-wording", "ok": True, "action": "seed", "pages": 1,
          "text": "verbatim"}], seed_from=tmp_path)
    log = json.loads(corpus.fetch_log_path().read_text(encoding="utf-8"))
    assert len(log["documents"]) == 1
    assert log["documents"][0]["ok"] is True


def test_an_unreadable_log_is_replaced_rather_than_half_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "corpus"))
    path = corpus.fetch_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all", encoding="utf-8")
    fetch_corpus._write_log([{"id": "sg-msig-sumo-wording", "ok": True,
                             "action": "seed", "pages": 1, "text": "verbatim"}],
                            seed_from=tmp_path)
    log = json.loads(path.read_text(encoding="utf-8"))
    assert [r["id"] for r in log["documents"]] == ["sg-msig-sumo-wording"]


def _one_page_pdf() -> bytes:
    """A real one-page PDF, so the extractor runs rather than being mocked."""
    import pdfbuild

    return pdfbuild.build_pdf([["a line of text on the only page"]])


# ── the skip conversion fires on absence, and only on absence ───────────────
# `tests/conftest.py` turns an uncaught `corpus.CorpusMissing` into a skip naming the
# document. That is right when the corpus has not been fetched and wrong in every other
# case: on a seeded machine the exception means a mis-resolved id or a path regression,
# and converting it would turn roughly two hundred tests green-by-absence while the run
# still exited 0.

def test_an_absent_document_is_converted_into_a_named_skip(monkeypatch, tmp_path):
    import conftest

    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path / "nothing-here"))
    exc = corpus.CorpusMissing("the wording is not here",
                               document_id="sg-chubb-selectplus-wording")
    reason = conftest._corpus_absence(exc)
    assert reason and "not here" in reason


@pytest.mark.requires_corpus("sg-chubb-selectplus-wording")
def test_the_same_exception_on_a_seeded_machine_stays_a_failure():
    """The document is present, so `CorpusMissing` about it is a bug in the code that
    resolved it — and a bug must fail."""
    import conftest

    exc = corpus.CorpusMissing("mis-resolved somewhere",
                               document_id="sg-chubb-selectplus-wording")
    assert conftest._corpus_absence(exc) is None


def test_an_os_error_on_a_corpus_file_that_exists_is_not_treated_as_absence(
        monkeypatch, tmp_path):
    """The second, narrower conversion: an OSError whose filename sits inside the corpus
    directory. A permission error on a file that IS there is not a missing document."""
    import conftest

    monkeypatch.setenv("VERIS_CORPUS_DIR", str(tmp_path))
    present = tmp_path / "text" / "something.txt"
    present.parent.mkdir(parents=True, exist_ok=True)
    present.write_text("x", encoding="utf-8")

    absent_err = OSError(2, "No such file", str(tmp_path / "text" / "gone.txt"))
    absent_err.filename = str(tmp_path / "text" / "gone.txt")
    assert conftest._corpus_absence(absent_err)

    present_err = PermissionError(13, "Permission denied", str(present))
    present_err.filename = str(present)
    assert conftest._corpus_absence(present_err) is None


def test_an_unrelated_failure_is_never_converted():
    import conftest

    assert conftest._corpus_absence(ValueError("a real bug")) is None
    assert conftest._corpus_absence(AssertionError("a real failure")) is None
