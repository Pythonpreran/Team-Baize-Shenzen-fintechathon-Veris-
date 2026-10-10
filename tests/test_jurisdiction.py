"""Jurisdiction packs, detection and the GENERIC fallback.

The failure this guards against is specific and was real: the engine knew one country's
law, so it read every document against it. A Malaysian wording produced Singapore work
injury findings, a Singapore penalty and a Singapore data-protection deadline, with nothing
on screen to say a country had been assumed. These tests hold four lines:

  1. A pack is validated, and an entry that states something about the law cannot omit
     its source. A CANDIDATE entry never becomes a sentence an owner reads.
  2. The country is DETECTED from the documents' own words — the governing-law clause
     first — and when the documents do not settle it, the answer is GENERIC, with the
     reasons both ways, never a guess.
  3. Under GENERIC, no rule written against a country's law runs, no screen states one,
     and the owner is told which pack is missing.
  4. Under Singapore, nothing changes (pinned byte for byte in test_sme_golden.py).
"""
from __future__ import annotations

import copy
import importlib.util
import json
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfbuild
import pytest
import yaml
from fastapi.testclient import TestClient

from proven import (corpus, cyber, detect, extract, ingest, jurisdiction, pipeline, rules,
                    stages, warranty)
from proven.api import EXAMPLE_DIR, SESSIONS, app

REPO = Path(__file__).resolve().parents[1]
PACK_DIR = REPO / "src" / "proven" / "jurisdictions"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"_script_{name}",
                                                  REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scan_repo = _load_script("scan_repo")
verify_pack_sources = _load_script("verify_pack_sources")

#: The rules written against Singapore law. Read from the rule files, not typed here, so a
#: new Singapore rule is covered by every test below the moment it is added.
SG_RULES = {rid for rid, r in rules.load_rules().items() if r.get("jurisdiction") == "SG"}


@pytest.fixture
def client():
    SESSIONS.clear()
    with TestClient(app) as c:
        yield c
    SESSIONS.clear()


def _sg_raw() -> dict:
    return yaml.safe_load((PACK_DIR / "SG.yaml").read_text(encoding="utf-8"))


def _generic_raw() -> dict:
    return yaml.safe_load((PACK_DIR / "GENERIC.yaml").read_text(encoding="utf-8"))


def _invalid(data: dict) -> str:
    with pytest.raises(jurisdiction.PackError) as exc:
        jurisdiction.validate_pack(data, source="test")
    return str(exc.value)


# ── synthetic Malaysian documents (no corpus needed) ────────────────────────
#
# Fictional, and marked as such. They carry the signals a real Malaysian wording carries — a
# governing-law clause, ringgit amounts, a company number in the Malaysian form, a Kuala
# Lumpur postcode — and a premium warranty clause, so the warranty screen has something to
# locate.
MY_WORDING_LINES = [
    "SYNTHETIC SAMPLE - NOT ISSUED BY ANY INSURER",
    "POLICY WORDING",
    "GENERAL CONDITIONS",
    "1. Premium Payment Warranty",
    "It is a condition that the premium shall be paid and received by the Company within",
    "the period stated in the Schedule, failing which cover is automatically terminated.",
    "2. Applicable Law",
    "This Policy is governed by Malaysian Law.",
    "Excess: RM 1,000 each and every loss.",
    "Example Underwriting Berhad (Company No. 123456-K), 50450 Kuala Lumpur.",
]
MY_SCHEDULE_LINES = [
    "SYNTHETIC SAMPLE - NOT ISSUED BY ANY INSURER",
    "POLICY SCHEDULE",
    "Policy Number: MY-TEST-0001",
    "Insured Name: Contoh Makanan Sdn Bhd",
    "Period of Insurance: from 2026-03-01 to 2027-02-28",
    "Premium Received: 2026-05-11",
    "Insured Premises: Lot 5, Jalan Contoh, 50450 Kuala Lumpur",
    "Operative Sections: PROPERTY, PUBLIC LIABILITY",
]


def _my_files() -> list[pipeline.UploadedFile]:
    return [
        pipeline.UploadedFile("wording.pdf", pdfbuild.build_pdf([MY_WORDING_LINES]),
                              declared_kind="BASE_WORDING"),
        pipeline.UploadedFile("schedule.pdf", pdfbuild.build_pdf([MY_SCHEDULE_LINES]),
                              declared_kind="SCHEDULE"),
    ]


def _example_files(names: list[str]) -> list[pipeline.UploadedFile]:
    return [pipeline.UploadedFile(n, (EXAMPLE_DIR / n).read_bytes()) for n in names]


#: The synthetic Singapore example documents that carry every Singapore rule's inputs.
SG_EXAMPLE = ["02-policy-schedule.pdf", "03-management-accounts.pdf",
              "04-tenancy-agreement.pdf", "06-cpf-contribution-record.pdf",
              "07-work-injury-schedule.pdf", "08-work-permit-holders.pdf",
              "09-acra-business-profile.pdf"]


# ── 1. the packs validate, and the validator bites ──────────────────────────

def test_the_packs_on_disk_validate_and_generic_is_always_there():
    assert jurisdiction.pack_codes() == ("GENERIC", "SG")
    assert jurisdiction.generic().is_generic
    sg = jurisdiction.pack("SG")
    assert sg.currency.code == "SGD"
    assert sg.timezone == "Asia/Singapore"
    assert sg.entries(), "the Singapore pack states nothing"
    # Recognisable without a pack, so a document from there gets an honest notice.
    assert {c["code"] for c in jurisdiction.recognised_without_pack()} == {"MY", "IN", "HK"}


def test_an_entry_without_a_verification_block_is_rejected():
    data = _sg_raw()
    del data["statutes"][0]["verification"]
    assert "verification" in _invalid(data)


@pytest.mark.parametrize("field", ["status", "source_url", "retrieved_on", "quote",
                                   "verified_by"])
def test_a_verification_block_may_not_omit_a_field(field):
    """Every field must be PRESENT, even when null, so an entry cannot silently skip the
    part of its sourcing it could not fill in."""
    data = _sg_raw()
    del data["statutes"][0]["verification"][field]
    assert field in _invalid(data)


@pytest.mark.parametrize("field", ["quote", "source_url", "retrieved_on"])
def test_source_quoted_needs_the_quote_its_address_and_the_date(field):
    data = _sg_raw()
    entry = next(e for e in data["statutes"]
                 if e["verification"]["status"] == "SOURCE_QUOTED")
    entry["verification"][field] = None
    assert "SOURCE_QUOTED" in _invalid(data)


def test_human_verified_needs_the_name_of_who_verified_it():
    data = _sg_raw()
    entry = next(e for e in data["compulsory_covers"]
                 if e["verification"]["status"] == "HUMAN_VERIFIED")
    entry["verification"]["verified_by"] = None
    assert "verified_by" in _invalid(data)


def test_a_source_must_be_an_https_address():
    data = _sg_raw()
    data["statutes"][0]["verification"]["source_url"] = "http://example.org/act"
    assert "https" in _invalid(data)


def test_a_time_limit_cannot_be_recorded_on_an_unverified_step():
    """The complaint ladder records a step's time limit ONLY with a sourced quote."""
    data = _sg_raw()
    step = next(s for s in data["complaint_ladder"]
                if s["verification"]["status"] == "CANDIDATE")
    step["values"]["time_limit_days"] = 30
    assert "time limit" in _invalid(data)


def test_generic_may_make_no_country_specific_statement():
    data = _generic_raw()
    data["statutes"] = [copy.deepcopy(_sg_raw()["statutes"][0])]
    data["statutes"][0]["id"] = "GENERIC-STAT-X"
    assert "GENERIC makes no country-specific statement" in _invalid(data)


def test_entry_ids_carry_their_own_pack_code():
    data = _sg_raw()
    data["statutes"][0]["id"] = "MY-STAT-BORROWED"
    assert "must start with 'SG-'" in _invalid(data)


def test_a_document_nobody_identified_may_only_ever_be_self_authored():
    data = _sg_raw()
    unknown = next(k for k in data["document_kinds"] if k["kind"] == "UNKNOWN")
    unknown["author_class"] = "AUTHORITY"
    assert "UNKNOWN" in _invalid(data)


def test_a_country_pack_may_not_regrade_a_kind_every_pack_shares():
    """Raising ACCOUNTS to AUTHORITY in one country would let a self-written spreadsheet
    grade PROVEN there — the manufacture of evidence author_class exists to stop."""
    data = _sg_raw()
    next(k for k in data["document_kinds"] if k["kind"] == "ACCOUNTS")["author_class"] = \
        "AUTHORITY"
    regraded = jurisdiction.validate_pack(data)
    with pytest.raises(jurisdiction.PackError, match="re-grades the shared document kind"):
        jurisdiction.check_against_generic(regraded, jurisdiction.generic())


def test_complaint_ladder_steps_are_numbered_in_order():
    data = _sg_raw()
    data["complaint_ladder"][0]["values"]["order"] = 2
    assert "complaint_ladder" in _invalid(data)


# ── the CANDIDATE boundary ──────────────────────────────────────────────────

def test_only_quoted_or_verified_entries_are_owner_facing():
    sg = jurisdiction.pack("SG")
    statuses = {e.verification.status for e in sg.entries()}
    assert jurisdiction.CANDIDATE in statuses, "nothing exercises the CANDIDATE boundary"
    for e in sg.entries():
        assert e.owner_facing == (e.verification.status in
                                  {jurisdiction.SOURCE_QUOTED, jurisdiction.HUMAN_VERIFIED})
        local = e.id.split("-", 1)[1]
        if e.verification.status == jurisdiction.CANDIDATE:
            assert sg.statement(local) is None, f"{e.id} would become a sentence"
        else:
            assert sg.statement(local) is e


def test_no_candidate_entry_reaches_a_brief_screen_or_rule(client):
    """End to end: the sentence of every CANDIDATE entry in the Singapore pack is absent
    from everything the owner can read about a Singapore case."""
    candidates = [e for e in jurisdiction.pack("SG").entries()
                  if e.verification.status == jurisdiction.CANDIDATE]
    files = [("files", (n, (EXAMPLE_DIR / n).read_bytes(), "application/pdf"))
             for n in SG_EXAMPLE]
    assert client.post("/api/bundle", files=files).status_code == 200
    blob = json.dumps([client.get(r).json() for r in
                       ("/api/brief", "/api/stages", "/api/cyber", "/api/warranty")])
    for e in candidates:
        assert e.statement not in blob, f"{e.id} is CANDIDATE and reached the owner"


def _sg_with(entry_id: str, status: str) -> jurisdiction.Pack:
    data = _sg_raw()
    entries = [
        entry for section in ("regulators", "statutes", "compulsory_covers",
                              "regulatory_timelines", "market_conventions")
        for entry in data[section]
    ]
    if data.get("data_protection"):
        entries.append(data["data_protection"])
    for entry in entries:
        if entry["id"] == entry_id:
            entry["verification"].update(
                status=status,
                verified_by=("test reviewer" if status == jurisdiction.HUMAN_VERIFIED
                             else None),
            )
            return jurisdiction.validate_pack(data)
    raise AssertionError(f"no entry {entry_id}")


def test_a_finding_whose_own_figure_is_a_candidate_is_not_run():
    """The work-pass minimum IS the finding's threshold. Unverified, there is no finding
    to state — so the rule is reported as not applied, with the reason."""
    pack = _sg_with("SG-COMP-WORKPASS-MEDICAL", jurisdiction.CANDIDATE)
    bound, not_applied, _ = rules.bind(rules.load_rules(), pack)
    assert "WORKPASS-MEDICAL-BELOW-MINIMUM" not in bound
    reason = next(r for r in not_applied
                  if r["rule_id"] == "WORKPASS-MEDICAL-BELOW-MINIMUM")["reason"]
    assert "CANDIDATE" in reason


def test_a_candidate_figure_drops_its_sentence_and_never_the_finding(monkeypatch):
    """PLAN rule 18. The headcount discrepancy is two documents disagreeing; the penalty
    sentence beside it is a statement about the law. Unverify the penalty and the sentence
    goes, the finding stays, and the record says what was dropped."""
    pack = _sg_with("SG-COMP-WORK-INJURY-PENALTY", jurisdiction.CANDIDATE)
    bound, not_applied, dropped = rules.bind(rules.load_rules(), pack)
    assert "WICA-HEADCOUNT-UNDERDECLARED" in bound
    assert "consequence_note" not in bound["WICA-HEADCOUNT-UNDERDECLARED"]
    assert {"rule_id": "WICA-HEADCOUNT-UNDERDECLARED", "sentence": "consequence_note"} \
        .items() <= dropped[0].items()

    packs = {"GENERIC": jurisdiction.generic(), "SG": pack}
    monkeypatch.setattr(jurisdiction, "_packs", lambda: packs)
    result = pipeline.run_bundle(_example_files(SG_EXAMPLE))
    wica = next(f for f in result["brief"]["findings"]
                if f["rule_id"] == "WICA-HEADCOUNT-UNDERDECLARED")
    assert wica["state"] == "DOCUMENTED"
    assert "consequence_note" not in wica["detail"]
    assert "10,000" not in json.dumps(wica)
    assert result["brief"]["jurisdiction"]["sentences_dropped"]


# ── 2. detection ────────────────────────────────────────────────────────────

def _pages(*pages: str, label: str = "doc.pdf") -> list[tuple[str, list[str]]]:
    return [(label, list(pages))]


def test_detection_unpacks_as_code_confidence_and_reasons():
    code, confidence, reasons = jurisdiction.detect_jurisdiction(
        _pages("This Policy is governed by the laws of Singapore."))
    assert (code, confidence) == ("SG", "HIGH")
    assert reasons[0]["signal"] == "governing_law"
    assert reasons[0]["page"] == 1


def test_nothing_recognisable_is_generic_and_says_why():
    d = jurisdiction.detect_jurisdiction(_pages("A page with no country on it at all."))
    assert (d.code, d.confidence) == ("GENERIC", "NONE")
    assert d.why and d.reasons[-1]["direction"] == "against"


def test_one_weak_signal_is_not_enough_to_apply_a_countrys_law():
    """A currency code alone does not establish which law governs a contract."""
    d = jurisdiction.detect_jurisdiction(_pages("Excess SGD 500."))
    assert (d.code, d.confidence) == ("GENERIC", "LOW")
    assert d.leading == "SG"
    assert any(r["direction"] == "for" and r["signal"] == "currency" for r in d.reasons)
    assert "too weak" in d.why


def test_name_only_signals_cannot_select_a_country_pack():
    """A single regulator or statute name is not enough to apply a country's law."""
    names = jurisdiction.pack("SG").detection.names
    examples = (next(n for n in names if not (n.isupper() and len(n) <= 6)),
                next(n for n in names if n.isupper() and len(n) <= 6))
    for name in examples:
        d = jurisdiction.detect_jurisdiction(_pages(f"Reference: {name}."))
        assert (d.code, d.confidence, d.leading) == ("GENERIC", "LOW", "SG")
        assert "governing-law clause or two independent signal kinds" in d.why


def test_two_independent_non_law_signals_may_establish_a_country():
    name = next(n for n in jurisdiction.pack("SG").detection.names
                if not (n.isupper() and len(n) <= 6))
    d = jurisdiction.detect_jurisdiction(_pages(f"Reference: {name}. Excess SGD 500."))
    assert (d.code, d.confidence) == ("SG", "MEDIUM")


def test_an_unrecognised_governing_law_clause_overrides_a_local_name():
    name = next(n for n in jurisdiction.pack("SG").detection.names
                if not (n.isupper() and len(n) <= 6))
    d = jurisdiction.detect_jurisdiction(_pages(
        f"Reference: {name}. This Policy is governed by the laws of Thailand."))
    assert (d.code, d.confidence, d.leading) == ("GENERIC", "LOW", "SG")
    assert "does not recognise" in d.why
    assert any(r.get("signal") == "unrecognised_governing_law" for r in d.reasons)


def test_conflicting_governing_law_clauses_force_generic_even_with_other_signals():
    name = next(n for n in jurisdiction.pack("SG").detection.names
                if not (n.isupper() and len(n) <= 6))
    d = jurisdiction.detect_jurisdiction(_pages(
        f"This Policy is governed by the laws of Singapore. {name}. Excess SGD 500.",
        "Section B is governed by Malaysian Law."))
    assert (d.code, d.confidence) == ("GENERIC", "LOW")
    assert "conflicting governing-law clauses" in d.why


def test_two_countries_too_evenly_is_generic_with_the_reasons_both_ways():
    d = jurisdiction.detect_jurisdiction(_pages(
        "Section A is governed by the laws of Singapore.",
        "Section B is governed by Malaysian Law."))
    assert d.code == "GENERIC"
    assert "conflicting governing-law clauses" in d.why
    countries = {(r.get("country"), r["direction"]) for r in d.reasons if r.get("signal")}
    assert ("MY", "against") in countries or ("SG", "against") in countries
    assert len({c for c, _ in countries}) == 2


def test_volume_cannot_outvote_a_governing_law_clause():
    """Forty ringgit-free lines of SGD are one currency signal, not forty."""
    d = jurisdiction.detect_jurisdiction(_pages(
        *["Item SGD 1,000" for _ in range(40)],
        "This Policy is governed by Malaysian Law."))
    assert d.code == "MY"
    assert d.scores["SG"] == 3


def test_a_phone_number_is_evidence_by_its_country_code_only():
    d = jurisdiction.detect_jurisdiction(_pages(
        "This Policy is governed by the laws of Singapore. Call +65 6123 4567."))
    phone = next(r for r in d.reasons if r.get("signal") == "phone_numbers")
    assert phone["matched"] == "+65 …"
    assert "6123" not in json.dumps(d.reasons)


def test_detection_is_logged_both_ways_without_the_documents_text(caplog):
    caplog.set_level(logging.INFO, logger="proven.jurisdiction")
    filename = "customer-private-policy.pdf"
    jurisdiction.detect_jurisdiction(_pages(
        "This Policy is governed by the laws of Singapore. UEN 201912345K. "
        "+65 6123 4567. 21 Tai Seng Avenue, Singapore 534054.", label=filename))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "jurisdiction detected: SG" in logged
    assert "governing_law" in logged
    assert filename not in logged
    for private in ("201912345K", "6123", "534054"):
        assert private not in logged, f"{private!r} was written to the log"


def test_a_synthetic_malaysian_wording_is_malaysia_and_read_under_generic():
    pages = ingest.extract_pdf_pages_cached(pdfbuild.build_pdf([MY_WORDING_LINES]),
                                            "my.pdf")
    applied = jurisdiction.assess([("my.pdf", pages)])
    assert (applied.detection.code, applied.detection.confidence) == ("MY", "HIGH")
    assert applied.pack.code == "GENERIC" and not applied.pack_available
    assert "no Malaysia jurisdiction pack yet" in applied.notice
    assert "governed by Malaysian Law" in applied.notice


@pytest.mark.requires_corpus(pipeline.CHUBB_ID)
def test_the_published_wording_is_singapore_from_its_governing_law_clause():
    path = corpus.require_original(pipeline.CHUBB_ID)
    pages = ingest.extract_pdf_pages_cached(path.read_bytes(), path.name)
    d = jurisdiction.detect_jurisdiction([(path.name, pages)])
    assert (d.code, d.confidence) == ("SG", "HIGH")
    best = d.strongest_reason
    assert best["signal"] == "governing_law"
    assert "law of Singapore" in best["matched"]
    # The page a reason names is the page the clause is really on (cl 3.3, Applicable Law).
    assert best["matched"] in " ".join(pages[best["page"] - 1].split())


@pytest.mark.parametrize("doc_id", ["sg-msig-sumo-wording", "sg-liberty-smecare-wording",
                                    "sg-aig-homes-advantage-wording"])
def test_other_singapore_wordings_are_singapore(doc_id):
    if corpus.missing([doc_id]):
        pytest.skip(f"corpus document not fetched: {doc_id}")
    pages = corpus.split_pages(corpus.require_text(doc_id).read_text(encoding="utf-8"))
    d = jurisdiction.detect_jurisdiction([(doc_id, pages)])
    assert (d.code, d.confidence) == ("SG", "HIGH")


@pytest.mark.parametrize("doc_id", ["my-aig-sme-fire-and-perils-wording",
                                    "my-aig-all-risks-and-bi-wording",
                                    "my-qbe-retail-package-wording",
                                    "my-qbe-industrial-all-risk-wording"])
def test_real_malaysian_wordings_are_malaysia_and_get_no_singapore_pack(doc_id):
    if corpus.missing([doc_id]):
        pytest.skip(f"corpus document not fetched: {doc_id}")
    pages = corpus.split_pages(corpus.require_text(doc_id).read_text(encoding="utf-8"))
    applied = jurisdiction.assess([(doc_id, pages)])
    assert applied.detection.code == "MY"
    assert applied.pack.code == "GENERIC"
    assert "no Malaysia jurisdiction pack yet" in applied.notice


@pytest.mark.parametrize(
    ("pages", "confidence"),
    [
        pytest.param([["POLICY WORDING", "No location is stated here."]], "NONE",
                     id="no-signal"),
        pytest.param([["POLICY WORDING", "Excess SGD 500."]], "LOW",
                     id="sub-threshold"),
        pytest.param([["This Policy is governed by the laws of Singapore."],
                      ["Section B is governed by Malaysian Law."]], "LOW",
                     id="tied-governing-law"),
    ],
)
def test_uncertain_uploads_keep_the_generic_notice_and_skipped_rules(client, pages, confidence):
    response = client.post(
        "/api/bundle",
        files=[("files", ("policy.pdf", pdfbuild.build_pdf(pages), "application/pdf"))],
        data={"kinds": '["BASE_WORDING"]'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    view = body["brief"]["jurisdiction"]
    assert (view["pack"], view["pack_available"], view["confidence"]) == \
        ("GENERIC", False, confidence)
    assert view["notice"] and view["summary"] == view["notice"]
    assert any("Country-specific rules were not run" in line
               for line in body["brief"]["not_searched"])


# ── 3. under GENERIC nothing Singaporean runs or is said ────────────────────

def test_no_singapore_rule_fires_under_generic_even_with_its_inputs_present():
    """The CPF record and the work injury schedule are both here, 14 against 11 — the
    exact inputs of the headcount finding. The Malaysian wording outweighs them, so the
    bundle is not read as Singaporean and the Singapore rules are named, not run."""
    files = _my_files() + _example_files(["06-cpf-contribution-record.pdf",
                                          "07-work-injury-schedule.pdf"])
    brief = pipeline.run_bundle(files)["brief"]
    assert brief["jurisdiction"]["pack"] == "GENERIC"
    fired = {f["rule_id"] for f in brief["findings"]}
    assert not fired & SG_RULES, f"Singapore rules ran under GENERIC: {fired & SG_RULES}"
    assert {r["rule_id"] for r in brief["jurisdiction"]["rules_not_applied"]} == SG_RULES
    # And the documents only Singapore defines are not treated as authority documents.
    cpf = next(f for f in brief["intake"]["files"] if f["kind"] == "CPF_RECORD")
    assert cpf["author_class"] == "SELF"


def _owner_facing_sources(sources) -> list[dict]:
    assert sources, "a statement about the law was made with no source beside it"
    for s in sources:
        assert s["owner_facing"] is True and s["status"] != "CANDIDATE", s
        assert s["source_url"] or s["verified_by"], s
        assert s["status"] != "SOURCE_QUOTED" or (s["quote"] and s["source_url"]
                                                  and s["retrieved_on"]), s
    return sources


def test_every_statement_about_the_law_carries_its_quote_link_and_status(client):
    """PLAN rule 17: an owner-facing regulatory statement shows the quote, the source link
    and the status badge. So each surface that states a pack figure carries the provenance
    of exactly the entries it states."""
    files = [("files", (n, (EXAMPLE_DIR / n).read_bytes(), "application/pdf"))
             for n in SG_EXAMPLE]
    brief = client.post("/api/bundle", files=files).json()["brief"]
    wica = next(f for f in brief["findings"] if f["rule_id"] == "WICA-HEADCOUNT-UNDERDECLARED")
    ids = {s["entry_id"] for s in _owner_facing_sources(wica["detail"]["pack_sources"])}
    assert ids == {"SG-COMP-WORK-INJURY-PENALTY", "SG-COMP-WORK-INJURY",
                   "SG-TL-WICA-RENEWAL-INFORMATION"}
    workpass = next(f for f in brief["findings"]
                    if f["rule_id"] == "WORKPASS-MEDICAL-BELOW-MINIMUM")
    assert [s["entry_id"] for s in workpass["detail"]["pack_sources"]] == \
        ["SG-COMP-WORKPASS-MEDICAL"]
    # A rule that states no pack figure carries no provenance block at all.
    assert all("pack_sources" not in f["detail"] for f in brief["findings"]
               if f["rule_id"] not in SG_RULES)

    work = next(e for e in client.get("/api/stages").json()["expectations"]
                if e["protection"] == "WORK_INJURY")
    assert {s["entry_id"] for s in _owner_facing_sources(work["pack_sources"])} == \
        {"SG-COMP-WORK-INJURY", "SG-STAT-WICA"}
    # The regulator dependency is still CANDIDATE, so the policy-based analysis remains
    # available while the statutory block is withheld.
    cyber_result = client.get("/api/cyber").json()
    assert cyber_result["available"] is True
    assert "statutory" not in cyber_result


def _cyber_pack_with(candidate: str | None = None) -> jurisdiction.Pack:
    """A synthetic fully verified cyber regime, with one dependency optionally demoted."""
    data = _sg_raw()
    dependency_ids = {
        "SG-DP-PDPA", "SG-REG-PDPC", "SG-STAT-PDPA-PENALTY",
        "SG-TL-PDPA-BREACH-NOTIFICATION", "SG-STAT-PDPA-SIGNIFICANT-SCALE",
    }
    entries = [
        entry for section in ("regulators", "statutes", "regulatory_timelines")
        for entry in data[section]
    ] + [data["data_protection"]]
    for entry in entries:
        if entry["id"] not in dependency_ids:
            continue
        entry["verification"].update(
            status=(jurisdiction.CANDIDATE if entry["id"] == candidate
                    else jurisdiction.HUMAN_VERIFIED),
            verified_by=(None if entry["id"] == candidate else "test reviewer"),
        )
    return jurisdiction.validate_pack(data)


def test_cyber_regime_cites_every_verified_dependency():
    law = cyber.regime(_cyber_pack_with())
    assert law is not None
    assert {source["entry_id"] for source in law.sources} == {
        "SG-DP-PDPA", "SG-REG-PDPC", "SG-STAT-PDPA-PENALTY",
        "SG-TL-PDPA-BREACH-NOTIFICATION", "SG-STAT-PDPA-SIGNIFICANT-SCALE",
    }
    assert all(source["owner_facing"] for source in law.sources)


@pytest.mark.parametrize("candidate", [
    "SG-DP-PDPA", "SG-REG-PDPC", "SG-STAT-PDPA-PENALTY",
    "SG-TL-PDPA-BREACH-NOTIFICATION", "SG-STAT-PDPA-SIGNIFICANT-SCALE",
])
def test_cyber_regime_withholds_the_statutory_block_when_any_dependency_is_candidate(candidate):
    assert cyber.regime(_cyber_pack_with(candidate)) is None


def test_a_dropped_sentence_takes_its_source_with_it():
    pack = _sg_with("SG-COMP-WORK-INJURY-PENALTY", jurisdiction.CANDIDATE)
    bound, _, _ = rules.bind(rules.load_rules(), pack)
    ids = {s["entry_id"] for s in bound["WICA-HEADCOUNT-UNDERDECLARED"]["_sources"]}
    assert "SG-COMP-WORK-INJURY-PENALTY" not in ids
    assert ids == {"SG-TL-WICA-RENEWAL-INFORMATION"}


def test_the_warranty_period_shows_where_it_comes_from():
    reg = warranty.register(pipeline.run_bundle(_example_files(["02-policy-schedule.pdf"])
                                                + _my_files()[:1]))
    premium = next(e for e in reg["register"] if e["id"] == "PREMIUM_PAYMENT")
    # Under GENERIC no period is compared, so no source is claimed for one.
    assert premium["state"] == warranty.UNVERIFIED and "pack_sources" not in premium


@pytest.mark.requires_corpus(pipeline.CHUBB_ID)
def test_the_warranty_period_carries_its_source_under_singapore():
    reg = warranty.register(pipeline.run_case())
    premium = next(e for e in reg["register"] if e["id"] == "PREMIUM_PAYMENT")
    assert premium["state"] == warranty.BREACHED
    assert [s["entry_id"] for s in _owner_facing_sources(premium["pack_sources"])] == \
        ["SG-MC-PREMIUM-PAYMENT-WARRANTY"]


def test_every_singapore_rule_runs_under_the_singapore_pack():
    brief = pipeline.run_bundle(_example_files(SG_EXAMPLE))["brief"]
    assert brief["jurisdiction"]["pack"] == "SG"
    assert brief["jurisdiction"]["rules_not_applied"] == []
    assert brief["jurisdiction"]["sentences_dropped"] == []
    assert SG_RULES <= {f["rule_id"] for f in brief["findings"]}
    cpf = next(f for f in brief["intake"]["files"] if f["kind"] == "CPF_RECORD")
    assert cpf["author_class"] == "AUTHORITY"


def test_a_malaysian_upload_gets_an_honest_notice_not_a_singapore_answer(client):
    files = [("files", (f.filename, f.data, "application/pdf")) for f in _my_files()]
    r = client.post("/api/bundle", files=files,
                    data={"kinds": '["BASE_WORDING", "SCHEDULE"]'})
    assert r.status_code == 200, r.text
    body = r.json()
    j = body["brief"]["jurisdiction"]
    assert (j["detected"], j["pack"], j["pack_available"]) == ("MY", "GENERIC", False)
    assert "no Malaysia jurisdiction pack yet" in j["notice"]
    assert body["intake"]["jurisdiction"]["notice"] == j["notice"]
    assert any(r["signal"] == "governing_law" and r["direction"] == "for"
               for r in j["reasons"])
    assert any("Country-specific rules were not run" in n
               for n in body["brief"]["not_searched"])
    assert not any("CPF contribution record" in n for n in body["brief"]["not_searched"])

    # Nothing the owner reads states Singapore law or a Singapore figure.
    brief_text = json.dumps(body["brief"]["findings"])
    for word in ("Singapore", "S$", "WICA", "PDPA", "60-day"):
        assert word not in brief_text, f"{word!r} reached a Malaysian brief"
    reg = client.get("/api/warranty").json()
    premium = next(e for e in reg["register"] if e["id"] == "PREMIUM_PAYMENT")
    assert premium["clause_located"] is True
    assert premium["state"] == warranty.UNVERIFIED
    assert "no period is assumed" in premium["detail"]
    assert "60" not in premium["detail"]
    cy = client.get("/api/cyber").json()
    assert cy["available"] is False
    assert "ACRA" not in cy["reason"] and "CPF" not in cy["reason"]
    st = client.get("/api/stages").json()
    assert not [e for e in st.get("expectations", []) if e["basis"] == "STATUTORY"]

    # Nor does any derived screen ask for Singapore's records, cite its law, or look the
    # business up in its register. (The Brief's jurisdiction record names the Singapore
    # rules it did NOT apply; that list is the one place they may appear.)
    singaporean = ("Singapore", "CPF", "ACRA", "WICA", "Work Injury Compensation Act",
                   "PDPA", "PDPC", "S$")
    for route in ("/api/stages", "/api/cyber", "/api/warranty", "/api/score",
                  "/api/blindspots", "/api/gap-summary", "/api/information-value",
                  "/api/supply-chain", "/api/compounding"):
        text = json.dumps(client.get(route).json(), ensure_ascii=False)
        leaked = [w for w in singaporean if w in text]
        assert not leaked, f"{route} carries {leaked} for a Malaysian business"
    prefill = client.get("/api/onboarding").json()["identity_prefill"]
    assert prefill["jurisdiction_pack"] == "GENERIC" and prefill["fields"] == []
    assert "no lookup was attempted" in prefill["status"]


def test_consent_status_uses_the_case_document_class_under_generic(client):
    files = _my_files() + _example_files(["06-cpf-contribution-record.pdf"])
    response = client.post(
        "/api/bundle",
        files=[("files", (f.filename, f.data, "application/pdf")) for f in files],
        data={"kinds": '["BASE_WORDING", "SCHEDULE", "CPF_RECORD"]'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["brief"]["jurisdiction"]["pack"] == "GENERIC"
    shareable = {entry["kind"]: entry for entry in
                 client.get("/api/consent/status").json()["shareable"]}
    assert shareable["CPF_RECORD"]["author_class"] == "SELF"


def _sg_facts():
    return pipeline.run_bundle(_example_files(SG_EXAMPLE))["facts"]


def test_stages_state_a_statute_only_under_a_pack_that_verifies_it():
    facts = _sg_facts()
    sg = stages.assess({"facts": facts, "jurisdiction": {"pack": "SG"}})
    work = next(e for e in sg["expectations"] if e["protection"] == "WORK_INJURY")
    assert work["basis"] == "STATUTORY"
    assert work["authority"] == "Work Injury Compensation Act 2019 (Singapore)"
    assert "$2,600" in work["because"]
    generic = stages.assess({"facts": facts, "jurisdiction": {"pack": "GENERIC"}})
    assert generic["available"] is True
    assert not [e for e in generic["expectations"] if e["basis"] == "STATUTORY"]
    assert any("not listed as one" in u for u in generic["unestablished"])
    assert generic["basis_notice"].startswith("No jurisdiction pack applies")
    assert "Singapore" not in json.dumps(generic)


def test_cyber_states_no_law_without_a_data_protection_regime():
    """The business does hold personal data here (an online channel, employees), so the
    screen says so — and says it will not state a law it has not been given."""
    result = cyber.assess({"facts": _sg_facts(), "store": None,
                           "jurisdiction": {"pack": "GENERIC"}})
    assert result["available"] is False
    assert "no statement about the law" in result["reason"]
    blob = json.dumps(result)
    for word in ("PDPA", "PDPC", "S$", "Singapore"):
        assert word not in blob


def test_the_premium_period_is_never_assumed_without_a_pack():
    class _Doc:
        id, kind = "d", "SCHEDULE"

    class _Fact:
        def __init__(self, value):
            self.value, self.established, self.page_no = value, True, 1
            self.needle, self.author_class, self.doc = "n", "INSURER", _Doc()

    class _Facts(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    facts = _Facts({"inception": _Fact("2026-03-01"),
                    "premium_received": _Fact("2026-05-11")})
    state, detail, evidence = warranty._check_premium_payment(facts, None)
    assert state == warranty.UNVERIFIED
    assert "no period is assumed" in detail and "60" not in detail
    assert len(evidence) == 2
    state, _, _ = warranty._check_premium_payment(facts, None, jurisdiction.pack("SG"))
    assert state == warranty.BREACHED


def test_extraction_compares_work_pass_cover_only_against_a_verified_minimum():
    store = ingest.DocStore()
    store.add(ingest.LoadedDoc("d1", "WORK_PASS_LIST", "AUTHORITY",
                               pages=["Worker 1 medical insurance: 45000"],
                               filename="passes.pdf"))
    under_sg = extract.extract_facts(store, pack=jurisdiction.pack("SG"))
    assert under_sg.get("workpass_below_minimum_count").value == 1
    under_generic = extract.extract_facts(store, pack=jurisdiction.generic())
    assert not under_generic.has("workpass_below_minimum_count")
    assert any("no medical insurance minimum" in n for n in under_generic.notes)


def test_author_class_comes_from_the_pack_that_applies():
    sg, generic = jurisdiction.pack("SG"), jurisdiction.generic()
    assert detect.author_class_for("CPF_RECORD", sg) == "AUTHORITY"
    assert detect.author_class_for("CPF_RECORD", generic) == "SELF"
    assert detect.author_class_for("TENANCY", generic) == "THIRD_PARTY"
    # With no selected pack, a kind not listed by GENERIC stays SELF until a bundle
    # establishes a country; the upload picker must not pre-upgrade it.
    assert detect.author_class_for("CPF_RECORD") == "SELF"
    assert detect.author_class_for("never-heard-of-it") == "SELF"


# ── the rules and the packs agree ───────────────────────────────────────────

def test_every_yaml_rule_has_exactly_one_evaluator():
    implemented = list(rules.EVALUATOR_RULE.values())
    assert len(implemented) == len(set(implemented))
    assert set(implemented) == set(rules.load_rules())


def test_every_rule_jurisdiction_is_a_real_pack_and_its_figures_resolve_there():
    for rid, rule in rules.load_rules().items():
        home = rule.get("jurisdiction")
        if home is None:
            assert not rule.get("pack_values"), f"{rid} quotes a pack but names none"
            continue
        assert jurisdiction.has_pack(home), f"{rid} names unknown pack {home}"
        bound, not_applied, dropped = rules.bind({rid: rule}, jurisdiction.pack(home))
        assert rid in bound and not not_applied and not dropped, (rid, not_applied, dropped)


def test_the_singapore_figures_are_the_ones_the_prior_build_used():
    """Migrated UNCHANGED (PLAN rule 19). Each of these was a literal in code."""
    sg = jurisdiction.pack("SG")
    assert sg.value("COMP-WORKPASS-MEDICAL:minimum_annual_cover") == 60000
    assert sg.value("COMP-WORK-INJURY:non_manual_monthly_salary_threshold") == 2600
    assert sg.value("COMP-WORK-INJURY-PENALTY:max_fine") == 10000
    assert sg.value("COMP-WORK-INJURY-PENALTY:max_jail_months") == 12
    assert sg.value("TL-WICA-RENEWAL-INFORMATION:days_before_commencement") == 21
    assert sg.value("MC-PREMIUM-PAYMENT-WARRANTY:days") == 60
    assert sg.value("STAT-PDPA-PENALTY:penalty_ceiling") == 1000000
    assert sg.value("STAT-PDPA-PENALTY:turnover_trigger") == 10000000
    assert sg.value("STAT-PDPA-PENALTY:effective_from") == date(2022, 10, 1)
    assert sg.value("STAT-PDPA-SIGNIFICANT-SCALE:affected_individuals") == 500
    assert sg.value("TL-PDPA-BREACH-NOTIFICATION:days") == 3
    limits = sg.entry("SG-STAT-WICA-COMPENSATION-LIMITS").values
    assert (limits["effective_from"], limits["medical_expenses_max"],
            limits["death_max"], limits["permanent_incapacity_max"]) == \
        (date(2025, 11, 1), 53000, 269000, 346000)
    with pytest.raises(jurisdiction.PackError):
        sg.value("COMP-WORKPASS-MEDICAL:no_such_key")


def test_money_is_written_the_way_the_pack_writes_it_and_never_through_a_float():
    sgd = jurisdiction.pack("SG").currency
    assert sgd.format(60000) == "S$60,000"
    assert sgd.format(Decimal("2600"), local=True) == "$2,600"
    assert sgd.format(Decimal("1234.5"), places=2) == "S$1,234.50"
    assert sgd.millions(1000000) == "S$1 million"
    assert sgd.millions(Decimal("10000000")) == "S$10 million"
    assert sgd.millions(1500000) == "S$1.5 million"
    assert jurisdiction.symbol_for("SGD") == "S$"
    assert jurisdiction.format_money(1000, "XYZ") == "XYZ 1,000"
    assert jurisdiction.long_date(date(2022, 10, 1)) == "1 October 2022"


def test_the_regulators_name_appears_in_no_pack_but_singapores():
    names = scan_repo.REGULATOR_ALLOWLIST
    for path in (PACK_DIR / "GENERIC.yaml", PACK_DIR / "signals.yaml",
                 REPO / "src" / "proven" / "jurisdiction.py"):
        text = path.read_text(encoding="utf-8")
        for name in names:
            assert not scan_repo._word_regex(name).search(text), (path.name, name)
    sg_text = (PACK_DIR / "SG.yaml").read_text(encoding="utf-8")
    assert all(scan_repo._word_regex(n).search(sg_text) for n in names), \
        "the Singapore pack must be able to name its own regulator"


# ── the routes ──────────────────────────────────────────────────────────────

def test_the_index_lists_packs_recognised_countries_and_the_detection_rule(client):
    body = client.get("/api/jurisdictions").json()
    assert {p["code"] for p in body["packs"]} == {"GENERIC", "SG"}
    assert {c["code"] for c in body["recognised_without_pack"]} == {"MY", "IN", "HK"}
    assert body["fallback"] == "GENERIC"
    weights = [s["weight"] for s in body["detection"]["signals"]]
    assert weights == sorted(weights, reverse=True)
    assert body["detection"]["signals"][0]["signal"] == "governing_law"
    assert body["verification_ladder"] == ["CANDIDATE", "SOURCE_QUOTED", "HUMAN_VERIFIED"]


def test_a_pack_is_served_with_every_entrys_verification_and_status(client):
    body = client.get("/api/jurisdictions/sg").json()
    assert body["code"] == "SG"
    entries = (body["regulators"] + body["statutes"] + body["compulsory_covers"]
               + body["regulatory_timelines"] + body["complaint_ladder"]
               + body["market_conventions"])
    assert entries
    for e in entries:
        v = e["verification"]
        assert set(v) >= {"status", "source_url", "retrieved_on", "quote", "verified_by"}
        assert e["owner_facing"] is (v["status"] != "CANDIDATE")
    assert client.get("/api/jurisdictions/GENERIC").json()["statutes"] == []


def test_a_recognised_country_without_a_pack_is_told_so(client):
    r = client.get("/api/jurisdictions/MY")
    assert r.status_code == 404
    assert "no Malaysia jurisdiction pack yet" in r.json()["detail"]


@pytest.mark.parametrize("code", ["XX", "signals", "..", "SG.yaml", "sg%2F..%2FGENERIC"])
def test_an_unknown_code_names_no_file(client, code):
    assert client.get(f"/api/jurisdictions/{code}").status_code == 404


def test_the_bundle_kinds_say_who_authors_each_kind_in_each_pack(client):
    kinds = {k["kind"]: k for k in client.get("/api/bundle/kinds").json()["kinds"]}
    assert kinds["CPF_RECORD"]["author_class"] == "SELF"
    assert "No jurisdiction is selected" in kinds["CPF_RECORD"]["author_class_context"]
    assert kinds["CPF_RECORD"]["author_class_by_jurisdiction"] == \
        {"GENERIC": "SELF", "SG": "AUTHORITY"}
    assert kinds["TENANCY"]["author_class_by_jurisdiction"] == \
        {"GENERIC": "THIRD_PARTY", "SG": "THIRD_PARTY"}


# ── the source checker ──────────────────────────────────────────────────────

def test_the_source_checker_removes_page_spacing_and_nothing_else():
    norm = verify_pack_sources.normalise
    assert norm("( b ) in any other  case — $1 million .") == "(b) in any other case — $1 million."
    assert norm("Shall") != norm("shall"), "case must not be normalised away"


def test_source_checker_help_uses_its_summary(capsys):
    with pytest.raises(SystemExit, match="0"):
        verify_pack_sources.main(["--help"])
    assert "Check every quote in the jurisdiction packs" in capsys.readouterr().out


def _quote_entry_pack() -> jurisdiction.Pack:
    return jurisdiction.pack("SG")


def test_a_source_quoted_entry_fails_when_its_quote_is_not_in_the_source():
    checks = verify_pack_sources.check_pack(_quote_entry_pack(),
                                            fetcher=lambda url: "unrelated page text")
    failed = [c for c in checks if c.failed]
    assert failed and all(c.status == jurisdiction.SOURCE_QUOTED for c in failed)
    # A HUMAN_VERIFIED miss is reported, not failed: it rests on the person named.
    assert any(c.result == verify_pack_sources.MISSING and not c.failed for c in checks)


def test_a_source_that_cannot_be_fetched_is_not_a_pass():
    def offline(url):
        raise OSError("network disabled")

    checks = verify_pack_sources.check_pack(_quote_entry_pack(), fetcher=offline)
    assert checks and all(c.result == verify_pack_sources.UNREACHABLE for c in checks)
    assert any(c.failed for c in checks)


def test_every_quote_is_found_when_the_source_says_it():
    """Feed each check its own quote back, spaced the way an HTML page spaces it."""
    sg = _quote_entry_pack()
    quotes = {}
    for e in sg.entries():
        if e.verification.quote:
            quotes.setdefault(e.verification.source_url, []).append(e.verification.quote)
    for k in sg.document_kinds:
        if k.verification.quote and k.verification.source_url:
            quotes.setdefault(k.verification.source_url, []).append(k.verification.quote)

    def page(url):
        return " <p> ".join(q.replace("(b)", "( b )") for q in quotes[url])

    checks = verify_pack_sources.check_pack(sg, fetcher=page)
    assert len(checks) == sum(len(v) for v in quotes.values())
    assert all(c.result == verify_pack_sources.FOUND for c in checks)
