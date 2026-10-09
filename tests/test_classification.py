"""Layer 2 classification — the exact map, the heading-keyword fallback, and the
boundary between them.

The headline claim this protects: a keyword match shrinks the unaccounted bucket but
may NEVER feed a finding, because nobody read the clause. If that separation leaks,
the product is asserting things on the strength of a word in a heading.
"""
from proven import account, pipeline
from proven.classify import EXACT_MAP, HEADING_KEYWORD, HeadingClassifier


def test_classifier_loads_rules_from_data():
    c = HeadingClassifier.load()
    assert c.meta["loaded"] is True
    assert c.meta["rule_count"] > 20
    assert c.meta["feeds_findings"] is False


def test_heading_keyword_classifies_and_labels_itself():
    c = HeadingClassifier.load()
    result = c.classify("Average", "4.2 Average If at the time of Damage...")
    assert result.code == "ADEQ.AVERAGE_CONDITION"
    assert result.classifier == HEADING_KEYWORD
    assert result.matched_on


def test_definition_form_is_classified_by_pattern_not_by_word():
    c = HeadingClassifier.load()
    hit = c.classify("Vacant means", "2.35 Vacant means any building or portion")
    assert hit.code in {"DEFN.DEFINED_TERM", "COND.UNOCCUPANCY"} or \
        hit.ambiguous_candidates
    # A clause that merely uses the word "means" in prose is not a definition.
    miss = c.classify("Claims Procedure",
                      "3.7 The Insured must notify Chubb by any means available")
    assert miss.code != "DEFN.DEFINED_TERM"


def test_ambiguity_abstains_rather_than_picking():
    """The engine that refuses to break a precedence tie does not get to break a
    classification tie either."""
    c = HeadingClassifier.load()
    result = c.classify("Theft and Fraudulent Claims",
                       "Theft is covered only where there is forcible entry, and any "
                       "fraudulent claim is excluded")
    assert result.code is None
    assert len(result.ambiguous_candidates) >= 2
    assert result.classifier is None


def test_no_condition_matches_nothing():
    from proven.classify import KeywordRule
    assert KeywordRule(code="X").matches("anything", "anything") is False


def test_accounting_reports_the_two_classifiers_separately():
    acc = pipeline.run_case()["brief"]["accounting"]
    assert acc["verified_classified"] > 0
    assert acc["keyword_classified"] > 0
    assert (acc["verified_classified"] + acc["keyword_classified"]
            == acc["classified"])
    assert acc["classified"] + acc["unaccounted"] == acc["detected"]
    note = acc["detection_note"]
    assert "hand-verified" in note
    assert "feeds no finding" in note


def test_the_accounting_note_states_the_model_claim_precisely():
    """This test previously asserted the note said step 3 "is NOT implemented" and
    that there was "no model anywhere in this pipeline". Both were true when written
    and stopped being true when align.py gained an optional TF-IDF proposer.

    A stale claim in the product's own output is the specific failure this product
    exists to prevent, so the test now pins the precise claim and fails if the
    overstated one comes back."""
    note = acc_note()
    assert "No machine-learning model makes a consequential decision" in note
    assert "structural mechanism alignment" in note
    # The overstated version must not reappear.
    for stale in ("no model anywhere", "There is no model",
                  "step 3 is NOT implemented"):
        assert stale not in note, f"the note has regressed to {stale!r}"


def acc_note() -> str:
    return pipeline.run_case()["brief"]["accounting"]["detection_note"]


def test_detection_schemes_are_named_with_their_yield():
    acc = pipeline.run_case()["brief"]["accounting"]
    assert acc["schemes_used"]
    names = {s["name"] for s in acc["schemes_used"]}
    assert "NUMERIC_DOTTED" in names
    for s in acc["schemes_used"]:
        assert s["hits"] > 0
        assert s["description"]


def test_alpha_prefixed_scheme_finds_general_conditions():
    """MSIG GC12 and Liberty GC4 are invisible to a dotted-decimal detector, which is
    why detection is scheme-plural."""
    hits = account.scheme_matches(
        "GC12 UNDERINSURANCE\nIf any property insured shall be of greater value\n"
        "GC17 CONDITION PRECEDENT\nThe validity of this Policy is subject to")
    refs = [h.ref for h in hits]
    assert "GC12" in refs and "GC17" in refs


def test_cl_prefix_is_not_treated_as_a_provision():
    """'cl 3.20 provides...' at the start of a line is a citation, not a provision.
    Treating it as one invents a clause out of a cross-reference."""
    hits = account.scheme_matches("cl 3.20 provides that cover is terminated")
    assert all(not h.ref.upper().startswith("CL") for h in hits)


def test_narrative_document_falls_back_to_headings():
    """A wording that numbers nothing must still be segmented. Reporting zero
    detected provisions would read like a clean bill of health on a document we could
    not read."""
    text = ("GENERAL CONDITIONS\nThe insured must take care.\n"
            "PREMIUM PAYMENT WARRANTY\nPremium is due in sixty days.\n"
            "CLAIMS PROCEDURE\nNotify us promptly.\n")
    hits = account.scheme_matches(
        text, account.PRIMARY_SCHEMES + account.FALLBACK_SCHEMES)
    assert any(h.scheme == "UPPER_HEADING" for h in hits)


def test_mapped_codes_is_the_basis_for_citing_a_clause():
    """Chubb's exclusions render as `5.1` with `(b)(v)` on later lines, so the literal
    ref never appears for a detector to find — yet the clause was read in full and
    page-cited. Gating findings on detection would silence the strongest evidence."""
    codes = account.mapped_codes("CHUBB")
    assert "EXCL.THEFT_FORCIBLE_ENTRY" in codes
    assert "EXCL.VACANCY_WATER" in codes
    assert account.mapped_codes("NOT_A_CARRIER") == set()
    assert account.carrier_is_mapped("CHUBB") is True
    assert account.carrier_is_mapped("UNKNOWN") is False


def test_keyword_classification_never_feeds_a_finding():
    """The separation, asserted end to end: every clause a finding cites must trace
    to the hand-verified map, never to a heading keyword."""
    r = pipeline.run_case()
    doc_id = r["conn"].execute(
        "SELECT id FROM document WHERE kind='BASE_WORDING'").fetchone()["id"]
    keyword_only = {
        row["taxonomy_code"] for row in r["conn"].execute(
            "SELECT taxonomy_code FROM provision WHERE document_id=? "
            "AND classifier=?", (doc_id, HEADING_KEYWORD)).fetchall()
        if row["taxonomy_code"]
    }
    verified = account.mapped_codes("CHUBB")
    rule_defs = r["brief"] and None
    _ = rule_defs
    from proven import rules as rules_mod
    for rule in rules_mod.load_rules().values():
        needed = rules_mod.required_base_codes(rule)
        for code in needed:
            # Every code a rule requires is either verified for this carrier (and so
            # may be cited) or the rule abstained. It is never satisfied by a keyword.
            if code in keyword_only and code not in verified:
                raise AssertionError(
                    f"rule {rule['id']} would rest on a keyword classification "
                    f"of {code}")


def test_empty_accounting_is_an_honest_zero():
    acc = account.empty_accounting("UNKNOWN")
    assert acc.detected == 0 and acc.classified == 0
    assert "no base policy wording" in acc.detection_note.lower()
    acc.assert_invariant()


def test_provision_rows_record_their_classifier_and_scheme():
    r = pipeline.run_case()
    rows = r["conn"].execute(
        "SELECT classifier, detection_scheme, status FROM provision "
        "WHERE status='CLASSIFIED'").fetchall()
    assert rows
    for row in rows:
        assert row["classifier"] in {EXACT_MAP, HEADING_KEYWORD}
        assert row["detection_scheme"]
