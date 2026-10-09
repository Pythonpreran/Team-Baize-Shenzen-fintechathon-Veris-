"""What a reader is shown of a third party's published document: one paragraph.

A published wording in the corpus is somebody else's copyrighted publication. The evidence
viewer therefore never draws its pages; it shows the paragraph a finding cites, cut from
the page text the engine already stored, plus a link to the publisher's own copy. These
tests pin the cut — that it contains the quotation exactly where it claims to, that it is
a paragraph and not a page, and that when the quotation cannot be anchored nothing else
of the page is disclosed — and the publisher link.

None of this needs the corpus, so none of it skips without one.
"""
from __future__ import annotations

from proven import api, corpus, sourceview

PAGE = (
    "GENERAL CONDITIONS\n\n"
    "4.1 Reasonable precautions. You must take all reasonable precautions to prevent "
    "loss or damage.\n\n"
    "4.2 Average. If the property insured shall at the time of any loss be of greater "
    "value than the sum insured, the amount payable shall be proportionately reduced.\n\n"
    "4.3 Other insurance. If there is any other insurance covering the same loss we will "
    "pay only our rateable proportion.\n")
QUOTE = "the amount payable shall be proportionately reduced"


def _cut(text=PAGE, quote=QUOTE, start=None, end=None):
    if start is None and quote in text:
        start = text.index(quote)
        end = start + len(quote)
    return sourceview.excerpt(text, start, end, quote)


def test_the_quotation_sits_at_the_offsets_the_excerpt_reports():
    x = _cut()
    assert x["text"][x["quote_start"]:x["quote_end"]] == QUOTE
    assert x["located"] == "AT_STORED_OFFSETS"


def test_the_excerpt_is_the_enclosing_paragraph_not_the_page():
    x = _cut()
    assert x["text"].startswith("4.2 Average.")
    assert x["text"].rstrip().endswith("proportionately reduced.")
    assert "4.1" not in x["text"] and "4.3" not in x["text"]
    assert x["cut_before"] is False and x["cut_after"] is False


def test_offsets_that_do_not_hold_the_quotation_fall_back_to_a_search_and_say_so():
    x = _cut(start=0, end=len(QUOTE))          # wrong offsets: they point at the heading
    assert x["located"] == "SEARCHED"
    assert x["text"][x["quote_start"]:x["quote_end"]] == QUOTE


def test_an_unanchored_quotation_discloses_nothing_else_of_the_page():
    x = sourceview.excerpt(PAGE, None, None, "a sentence this page does not contain")
    assert x["located"] == "NOT_FOUND"
    assert x["text"] == "a sentence this page does not contain"
    assert "GENERAL CONDITIONS" not in x["text"]


def test_a_page_with_no_paragraph_breaks_is_capped_either_side_and_says_where():
    filler = " ".join(f"word{i}" for i in range(400))
    text = f"{filler} {QUOTE} {filler}"
    x = _cut(text=text)
    assert x["text"][x["quote_start"]:x["quote_end"]] == QUOTE
    cap = sourceview.EXCERPT_CONTEXT_CHARS
    assert len(x["text"]) <= len(QUOTE) + 2 * cap
    assert x["cut_before"] is True and x["cut_after"] is True
    # Cut on a word boundary, never inside a word.
    assert x["text"].split(" ")[0].startswith("word")
    assert not x["text"].startswith(" ") and not x["text"].endswith(" ")


def test_windows_line_endings_are_paragraph_breaks_too():
    text = PAGE.replace("\n", "\r\n")
    x = _cut(text=text)
    assert x["text"].startswith("4.2 Average.")
    assert "4.3" not in x["text"]


# ── the publisher link ───────────────────────────────────────────────────────

def test_the_publisher_link_comes_from_the_manifest():
    doc = corpus.resolve("sg-chubb-selectplus-wording")
    for stored_as in (f"{doc.id}.pdf", api._EXAMPLE_WORDING_NAME):
        link = api._publisher_link(stored_as)
        assert link["available"] is True
        assert link["url"] == doc.source_url
        assert link["publisher"] == doc.publisher and link["title"] == doc.title


def test_no_link_is_offered_when_the_address_was_never_recorded():
    """An unrecorded URL is said to be unrecorded, never replaced by a guess."""
    doc = corpus.resolve("sg-aig-homes-advantage-wording")
    assert doc.source_url_status == "UNRECORDED"
    link = api._publisher_link(f"{doc.id}.pdf")
    assert link["available"] is False and link["url"] is None
    assert "not recorded" in link["note"]


def test_a_document_the_manifest_does_not_list_gets_no_link():
    link = api._publisher_link("someone-elses-upload.pdf")
    assert link["available"] is False and link["url"] is None


def test_the_reason_names_the_publisher_and_the_rule():
    reason = api._third_party_reason(api._EXAMPLE_WORDING_NAME)
    doc = corpus.resolve("sg-chubb-selectplus-wording")
    assert doc.publisher in reason
    assert "reader's copy" in reason and "pages" in reason


def test_a_published_document_dropped_in_the_example_folder_is_still_not_drawn():
    """The corpus name wins over the example listing, so a published wording placed in
    the synthetic example directory cannot be drawn as pages by way of the listing."""

    class _Session:
        state: dict = {}

    assert api._source_provenance_for(
        _Session(), "doc-x", api._EXAMPLE_WORDING_NAME) == api.PROVENANCE_CORPUS
    assert api._source_bytes_for(_Session(), "doc-x", api._EXAMPLE_WORDING_NAME) == b""


def test_bytes_retained_for_a_corpus_document_are_still_not_served_for_display():
    """Defence in depth. Intake does not retain a corpus document's bytes; if something
    ever did, the provenance rule must still stop them reaching the renderer."""

    class _Session:
        state = {api._SOURCE_BYTES_KEY: {"doc-c": b"%PDF-1.4 published bytes"},
                 api._SOURCE_PROVENANCE_KEY: {"doc-c": api.PROVENANCE_CORPUS}}

    assert api._source_bytes_for(_Session(), "doc-c", "anything.pdf") == b""


def test_a_users_own_upload_is_served_for_display():
    class _Session:
        state = {api._SOURCE_BYTES_KEY: {"doc-u": b"%PDF-1.4 my own file"},
                 api._SOURCE_PROVENANCE_KEY: {"doc-u": api.PROVENANCE_USER_UPLOAD}}

    assert api._source_bytes_for(_Session(), "doc-u", "mine.pdf") == b"%PDF-1.4 my own file"
