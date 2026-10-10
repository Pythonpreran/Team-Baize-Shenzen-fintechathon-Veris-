"""Document intake — the difference between a demo and a product.

Every one of these was previously untestable because there was no way to put a
document into the system.
"""
import pdfbuild
import pytest

from proven import detect, extract, ingest, pipeline
from proven.errors import (
    BundleError, DocumentTooLarge, EmptyDocument, NoTextLayer, UnsupportedDocument,
)


# ── validation: the six ways a real bundle breaks ───────────────────────────

def test_empty_file_rejected():
    with pytest.raises(EmptyDocument):
        ingest.extract_pdf_pages(b"", "empty.pdf")


def test_non_pdf_rejected():
    with pytest.raises(UnsupportedDocument):
        ingest.extract_pdf_pages(b"just some text, not a pdf at all", "notes.txt")


def test_corrupt_pdf_rejected_with_actionable_message():
    with pytest.raises(UnsupportedDocument) as exc:
        ingest.extract_pdf_pages(b"%PDF-1.4 truncated garbage", "broken.pdf")
    assert "broken.pdf" in str(exc.value)


def test_oversized_file_rejected_before_parsing():
    payload = b"%PDF-" + b"0" * (ingest.MAX_FILE_BYTES + 1)
    with pytest.raises(DocumentTooLarge):
        ingest.extract_pdf_pages(payload, "huge.pdf")


def test_scan_without_text_layer_is_reported_not_guessed():
    """A photographed document has no text layer. OCR is deliberately out of this
    path, so the only honest outcome is a clear refusal."""
    with pytest.raises(NoTextLayer) as exc:
        ingest.extract_pdf_pages(pdfbuild.scan_pdf(), "photo.pdf")
    assert "scan" in str(exc.value).lower()


def test_over_page_cap_rejected():
    big = pdfbuild.build_pdf([["page"] for _ in range(ingest.MAX_PAGES + 1)])
    with pytest.raises(DocumentTooLarge):
        ingest.extract_pdf_pages(big, "long.pdf")


def test_empty_bundle_rejected():
    with pytest.raises(BundleError):
        pipeline.run_bundle([])


def test_bundle_of_only_unreadable_files_rejected():
    with pytest.raises(BundleError):
        pipeline.run_bundle([
            pipeline.UploadedFile("a.pdf", b"nonsense"),
            pipeline.UploadedFile("b.pdf", b"%PDF-1.4 broken"),
        ])


def test_filename_path_traversal_is_stripped():
    assert ingest.safe_filename("../../../etc/passwd") == "passwd"
    assert "/" not in ingest.safe_filename("a/b/c.pdf")
    assert "\\" not in ingest.safe_filename(r"..\..\windows\system32\x.pdf")
    assert ingest.safe_filename("") == "document.pdf"


# ── detection ───────────────────────────────────────────────────────────────

def test_carrier_detected_from_real_wording():
    pages = ingest.extract_pdf_pages_cached(
        pipeline.CHUBB_PDF.read_bytes(), "chubb.pdf")
    det = detect.detect_carrier(pages)
    assert det.value == "CHUBB"
    assert det.confidence == "HIGH"
    assert det.matched                      # evidence for the decision travels


def test_unknown_carrier_is_never_guessed():
    det = detect.detect_carrier(["A policy wording with no carrier name at all."])
    assert det.value == detect.UNKNOWN_CARRIER
    assert det.confidence == "NONE"


def test_document_kinds_detected():
    cases = {
        "SCHEDULE": pdfbuild.schedule_pdf(),
        "TENANCY": pdfbuild.tenancy_pdf(),
        "ACCOUNTS": pdfbuild.accounts_pdf(),
    }
    for expected, data in cases.items():
        pages = ingest.extract_pdf_pages_cached(data, f"{expected}.pdf")
        assert detect.detect_kind(pages, f"{expected}.pdf").value == expected


def test_author_class_is_derived_and_unknown_falls_to_the_safe_default():
    # Before a bundle establishes a jurisdiction, a CPF record is not upgraded from
    # GENERIC's SELF class to the class a country-specific pack might later assign.
    assert detect.author_class_for("CPF_RECORD") == "SELF"
    assert detect.author_class_for("BASE_WORDING") == "INSURER"
    assert detect.author_class_for("TENANCY") == "THIRD_PARTY"
    # An unidentified document can only ever WEAKEN a finding.
    assert detect.author_class_for("UNKNOWN") == "SELF"
    assert detect.author_class_for("something-invented") == "SELF"


def test_uploader_cannot_choose_author_class():
    """The upload contract accepts a document KIND and derives author_class from it.
    A caller who could assert AUTHORITY could manufacture a PROVEN finding out of a
    spreadsheet they wrote themselves."""
    import inspect
    sig = inspect.signature(pipeline.UploadedFile.__init__)
    assert "author_class" not in sig.parameters
    assert "declared_kind" in sig.parameters


# ── end to end on uploaded documents ────────────────────────────────────────

def _full_bundle() -> list[pipeline.UploadedFile]:
    return [
        pipeline.UploadedFile("wording.pdf", pipeline.CHUBB_PDF.read_bytes()),
        pipeline.UploadedFile("schedule.pdf", pdfbuild.schedule_pdf()),
        pipeline.UploadedFile("tenancy.pdf", pdfbuild.tenancy_pdf()),
        pipeline.UploadedFile("accounts.pdf", pdfbuild.accounts_pdf()),
    ]


def test_uploaded_bundle_produces_real_findings():
    b = pipeline.run_bundle(_full_bundle(), business_name="Acme Foods")["brief"]
    by_rule = {f["rule_id"]: f for f in b["findings"]}
    assert b["business"] == "Acme Foods"
    assert b["synthetic"] is False
    assert b["carrier"] == "CHUBB"
    # Clause accounting ran on the uploaded wording.
    assert b["accounting"]["detected"] > 50
    assert (b["accounting"]["classified"] + b["accounting"]["unaccounted"]
            == b["accounting"]["detected"])
    # Values were extracted from uploaded PDFs and the arithmetic ran on them.
    avg = by_rule["AVERAGE-ADEQUACY-BELOW-THRESHOLD"]
    assert avg["state"] == "DOCUMENTED"
    assert avg["class"] == "MARKET_CONVENTION"
    assert avg["detail"]["adequacy_ratio"] == "66.7%"
    # Premises on the tenancy but not the schedule, from two uploaded documents.
    assert (by_rule["ALTERATION-PREMISES-NOT-ON-SCHEDULE"]["state"]
            == "REQUIRES_AUTHORISED_REVIEW")
    # 71 days from inception, computed from dates read out of the schedule PDF.
    assert by_rule["PREMIUM-PAYMENT-WARRANTY-TIMING"]["detail"][
        "days_from_inception"] == 71


def test_uploaded_bundle_reports_per_file_intake():
    files = _full_bundle() + [pipeline.UploadedFile("broken.pdf", b"%PDF-1.4 bad")]
    b = pipeline.run_bundle(files)["brief"]
    intake = b["intake"]
    assert intake["accepted_count"] == 4
    assert intake["rejected_count"] == 1
    rejected = [f for f in intake["files"] if not f["accepted"]]
    assert rejected[0]["filename"] == "broken.pdf"
    assert rejected[0]["code"] == "UNSUPPORTED_DOCUMENT"
    # One bad file must not lose the review of the other four.
    assert len(b["findings"]) > 5


def test_incomplete_bundle_abstains_instead_of_crashing():
    """The old rules indexed fixture JSON directly and raised KeyError on anything
    else. A wording with no schedule must now produce named, honest abstentions."""
    b = pipeline.run_bundle([
        pipeline.UploadedFile("wording.pdf", pipeline.CHUBB_PDF.read_bytes()),
    ])["brief"]
    by_rule = {f["rule_id"]: f for f in b["findings"]}
    avg = by_rule["AVERAGE-ADEQUACY-BELOW-THRESHOLD"]
    assert avg["state"] == "INFORMATION_MISSING"
    assert avg["class"] == "UNESTABLISHED"
    assert "SCHEDULE" in avg["detail"]["missing_documents"]
    assert "stock_sum_insured" in avg["detail"]["missing_facts"]
    # And it says so in the coverage statement rather than staying silent.
    assert any("schedule" in n.lower() for n in b["not_searched"])


def test_supporting_documents_only_claims_nothing_about_the_policy():
    """No wording uploaded means no statement about policy terms — and the
    accounting must be an honest zero, not a blank that reads like a clean bill."""
    b = pipeline.run_bundle([
        pipeline.UploadedFile("tenancy.pdf", pdfbuild.tenancy_pdf()),
    ])["brief"]
    assert b["accounting"]["detected"] == 0
    assert b["accounting"]["classified"] == 0
    assert any("no policy wording" in n.lower() for n in b["not_searched"])


def test_unknown_carrier_produces_no_clause_cited_finding():
    """A wording nobody has read must not have Chubb's clauses attributed to it."""
    b = pipeline.run_bundle([
        pipeline.UploadedFile("mystery.pdf", pdfbuild.wording_pdf()),
        pipeline.UploadedFile("schedule.pdf", pdfbuild.schedule_pdf()),
    ])["brief"]
    assert b["carrier"] == detect.UNKNOWN_CARRIER
    for f in b["findings"]:
        cites = (f.get("detail") or {}).get("cite") or []
        # Only BASE_WORDING citations are constrained. A citation to the Schedule's
        # own operative-sections list is legitimate whoever wrote the wording.
        wording_cites = [c for c in cites
                         if isinstance(c, dict) and c.get("clause")
                         and c.get("document_kind") == "BASE_WORDING"]
        if wording_cites and f["state"] != "INFORMATION_MISSING":
            raise AssertionError(
                f"{f['rule_id']} cites wording clause {wording_cites} on a wording "
                f"nobody has read")
    assert any("not been read by a human" in n for n in b["not_searched"])


def test_extraction_cache_is_content_addressed():
    ingest.clear_page_cache()
    data = pdfbuild.schedule_pdf()
    first = ingest.extract_pdf_pages_cached(data, "a.pdf")
    second = ingest.extract_pdf_pages_cached(data, "different-name.pdf")
    assert first == second
    # Different bytes must never collide onto a cached entry.
    other = ingest.extract_pdf_pages_cached(pdfbuild.tenancy_pdf(), "b.pdf")
    assert other != first


def test_extracted_needles_anchor_verbatim():
    """A needle is a byte range into the stored page, so it must occur in the page
    verbatim. Whitespace-normalising it silently turned good extractions into
    ABSENT facts."""
    pages = ingest.extract_pdf_pages_cached(pdfbuild.schedule_pdf(), "s.pdf")
    hit = extract.find_money(pages, ("stock in trade",))
    assert hit is not None
    amount, needle, page_no = hit
    assert str(amount) == "100000"
    assert needle in pages[page_no - 1]
