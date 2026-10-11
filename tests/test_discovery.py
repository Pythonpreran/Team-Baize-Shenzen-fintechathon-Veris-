"""Tests for the Discovery Agent.

The agent that goes and looks — fetching public information about a business and
comparing it against the policy. This is the one capability nobody else in the system
has: discovering a business change from outside the business's own software.
"""
import json

import pytest

from proven import discovery


# ── the structural ceiling ──────────────────────────────────────────────────

def test_discovery_may_not_emit_confirmed_evidence():
    """The same boundary as Sentinel: only a document can establish a fact."""
    with pytest.raises(ValueError, match="CONFIRMED_EVIDENCE"):
        discovery.DiscoveredFact(
            id="test", source=discovery.ACRA_SOURCE,
            fact_type="LOCATION", label="test", detail="test",
            claim_scope=discovery.CONFIRMED_EVIDENCE)


def test_discovery_verified_requires_authority_source():
    """Only an authority-published source can reach VERIFIED status."""
    # ACRA is authority — can be VERIFIED
    f = discovery.DiscoveredFact(
        id="test", source=discovery.ACRA_SOURCE,
        fact_type="LOCATION", label="test", detail="test",
        claim_scope=discovery.DISCOVERY_VERIFIED, grounded=True)
    assert f.claim_scope == discovery.DISCOVERY_VERIFIED

    # Website is SELF — should only ever be HINT
    f2 = discovery.DiscoveredFact(
        id="test2", source=discovery.COMPANY_WEBSITE,
        fact_type="LOCATION", label="test", detail="test",
        claim_scope=discovery.DISCOVERY_HINT)
    assert f2.claim_scope == discovery.DISCOVERY_HINT


# ── the grounding gate ──────────────────────────────────────────────────────

def test_grounding_finds_exact_text():
    text = "The company operates from Unit 03-12, 21 Tai Seng Avenue"
    ok, quote = discovery.ground_fact("Unit 03-12", text)
    assert ok is True
    assert "Unit 03-12" in quote


def test_grounding_rejects_absent_text():
    text = "The company operates from Unit 03-11, 21 Tai Seng Avenue"
    ok, quote = discovery.ground_fact("Unit 03-99", text)
    assert ok is False
    assert quote == ""


def test_grounding_handles_whitespace_differences():
    text = "Branch 1:  21 Tai Seng\n  Avenue #03-12"
    ok, quote = discovery.ground_fact("21 Tai Seng Avenue #03-12", text)
    assert ok is True


def test_grounding_rejects_empty_inputs():
    assert discovery.ground_fact("", "some text") == (False, "")
    assert discovery.ground_fact("something", "") == (False, "")


# ── the demo discovery ──────────────────────────────────────────────────────

def test_demo_discovery_finds_the_branch():
    """The headline capability: a branch registered with ACRA that is not on the
    policy schedule."""
    result = discovery.discover_business()
    assert result["facts_discovered"] > 0
    branch_facts = [f for f in result["facts"] if f["fact_type"] == "LOCATION"]
    assert branch_facts, "no location fact discovered"
    # At least one should be from ACRA and VERIFIED (grounded in the text)
    verified = [f for f in branch_facts
                if f["claim_scope"] == discovery.DISCOVERY_VERIFIED]
    assert verified, "no ACRA-verified location fact"
    assert "03-12" in verified[0]["value"]


def test_demo_discovery_finds_the_activity_change():
    result = discovery.discover_business()
    activity_facts = [f for f in result["facts"] if f["fact_type"] == "ACTIVITY"]
    assert activity_facts
    assert "online" in activity_facts[0]["value"].lower()


def test_demo_discovery_finds_headcount_on_website():
    result = discovery.discover_business()
    head_facts = [f for f in result["facts"]
                  if f["fact_type"] == "HEADCOUNT" and f["source"]["id"] == "disc-web"]
    assert head_facts
    assert head_facts[0]["claim_scope"] == discovery.DISCOVERY_HINT  # SELF source


def test_demo_discovery_labels_all_as_demo_fixture():
    result = discovery.discover_business()
    assert result["demo_mode"] is True
    for fact in result["facts"]:
        assert fact["simulated"] is True


def test_demo_discovery_carries_method_statement():
    result = discovery.discover_business()
    assert "ground" in result["method"].lower()
    assert "model reads" in result["method"].lower()
    assert "deterministic" in result["method"].lower()


# ── integration with orchestrator ───────────────────────────────────────────

def test_discovery_converts_to_external_signals():
    result = discovery.discover_business()
    signals = discovery.discovery_as_external_signals(result)
    assert len(signals) == result["facts_discovered"]
    for s in signals:
        assert s["agent"] == "discovery"
        assert "insured_interests" in s
        assert "claim_scope" in s


def test_discovery_signals_feed_orchestrator():
    """Discovery facts must participate in the orchestrator's overlap detection."""
    from proven import orchestrator
    result = discovery.discover_business()
    disc_signals = discovery.discovery_as_external_signals(result)
    # Feed only discovery signals (no internal, no sentinel)
    out = orchestrator.evaluate(external_signals=disc_signals, findings=[])
    # Should produce decisions for the interests discovery touches
    assert out["count"] > 0
    touched = out["inputs"]["interests_touched"]
    assert "premises" in touched  # the branch fact touches premises


# ── the catalogue ───────────────────────────────────────────────────────────

def test_catalogue_declares_the_boundary():
    cat = discovery.catalogue()
    assert discovery.DISCOVERY_VERIFIED in cat["boundary"]["may_assert"]
    assert discovery.CONFIRMED_EVIDENCE in cat["boundary"]["may_not_assert"]
    assert cat["demo_count"] == len(discovery.SOURCES)


# ── the fetch layer ─────────────────────────────────────────────────────────

def test_fetch_url_handles_bad_urls_gracefully():
    """The agent must never crash on a bad URL — it reports the failure and moves on."""
    ok, text, err = discovery.fetch_url("http://this-domain-does-not-exist-xyz.invalid/")
    assert ok is False
    assert err  # has an error message
    assert text == ""


# ── LLM extraction: the model reads, deterministic code decides ─────────────
#
# Every case below is a failure actually observed when the extractor was first run against
# the ACRA fixture with gpt-4o-mini, not a hypothetical. The model's proposals were all
# genuinely present on the page — grounding passed — and three of them were still wrong,
# because being printed on a page does not make a share-capital figure a headcount.


def _text(*lines: str) -> str:
    return "\n".join(lines)


# gate 1: grounding

def test_ungrounded_proposal_is_discarded_not_softened():
    """A value the model cannot point to in the fetched bytes does not become a hint."""
    page = _text("Registered address: 21 Tai Seng Avenue #03-12, Singapore 534054")
    proposals = [{"fact_type": "LOCATION",
                  "value": "9 Jurong Industrial Way #05-01, Singapore 619000",
                  "label": "Second site"}]
    kept, rejected = discovery.facts_from_proposals(
        proposals, page, discovery.ACRA_SOURCE, today="2026-08-11")

    assert kept == []
    assert len(rejected) == 1
    assert rejected[0]["gate"] == "GROUNDING"


def test_grounded_proposal_from_authority_may_be_verified():
    page = _text("Registered address: 21 Tai Seng Avenue #03-12, Singapore 534054")
    proposals = [{"fact_type": "LOCATION",
                  "value": "21 Tai Seng Avenue #03-12, Singapore 534054",
                  "label": "Registered address"}]
    kept, rejected = discovery.facts_from_proposals(
        proposals, page, discovery.ACRA_SOURCE, today="2026-08-11")

    assert rejected == []
    assert len(kept) == 1
    assert kept[0].claim_scope == discovery.DISCOVERY_VERIFIED
    assert kept[0].grounded is True
    assert kept[0].extraction == "MODEL"
    assert kept[0].ground_quote


def test_website_ceiling_survives_a_grounded_quote():
    """Grounding proves the words are on the page, not that the business is telling the
    truth about itself. A self-authored source can never reach VERIFIED."""
    page = _text("Visit our second kitchen at 9 Kaki Bukit Road #05-01, Singapore 417818")
    proposals = [{"fact_type": "LOCATION",
                  "value": "9 Kaki Bukit Road #05-01, Singapore 417818",
                  "label": "Second kitchen"}]
    kept, _ = discovery.facts_from_proposals(
        proposals, page, discovery.COMPANY_WEBSITE, today="2026-08-11")

    assert len(kept) == 1
    assert kept[0].grounded is True
    assert kept[0].claim_scope == discovery.DISCOVERY_HINT


# gate 2: is the value the kind of thing the model called it?

@pytest.mark.parametrize("fact_type,value,reason", [
    # all three were returned by the model and are all really on the ACRA page
    ("HEADCOUNT", "100,000 ordinary shares", "share capital is not people"),
    ("LOCATION", "HOCK LEE FOODS PTE. LTD.", "a company name is not an address"),
    ("STATUS", "Active Work Permits: 4", "a count is not a registration status"),
])
def test_type_gate_rejects_measured_mislabellings(fact_type, value, reason):
    ok, why = discovery.type_is_plausible(fact_type, value)
    assert ok is False, f"{reason}: {value!r} was accepted as {fact_type}"
    assert why


@pytest.mark.parametrize("fact_type,value", [
    ("LOCATION", "21 Tai Seng Avenue #03-11, Singapore 534054"),
    ("ACTIVITY", "Manufacture of prepared meals and dishes"),
    ("STATUS", "Live"),
    ("HEADCOUNT", "24"),
])
def test_type_gate_accepts_correctly_labelled_values(fact_type, value):
    ok, why = discovery.type_is_plausible(fact_type, value)
    assert ok is True, f"{value!r} should be a valid {fact_type}, got: {why}"


def test_type_gate_runs_after_grounding_so_a_mislabel_is_reported_as_such():
    page = _text("Issued share capital: 100,000 ordinary shares")
    proposals = [{"fact_type": "HEADCOUNT", "value": "100,000 ordinary shares",
                  "label": "Staff"}]
    kept, rejected = discovery.facts_from_proposals(
        proposals, page, discovery.ACRA_SOURCE, today="2026-08-11")

    assert kept == []
    assert rejected[0]["gate"] == "TYPE"


def test_mislabelled_headcount_never_reaches_workforce_interests():
    """Why the type gate is not cosmetic: fact_type selects the insured interests the
    orchestrator reasons over."""
    page = _text("Issued share capital: 100,000 ordinary shares")
    kept, _ = discovery.facts_from_proposals(
        [{"fact_type": "HEADCOUNT", "value": "100,000 ordinary shares", "label": "Staff"}],
        page, discovery.ACRA_SOURCE, today="2026-08-11")
    assert all("workforce" not in f.insured_interests for f in kept)


# dedupe: the model restates what a pattern already found

def test_containment_counts_as_duplication():
    """Measured: the branch pattern captured the whole line, the model returned just the
    address inside it. Same fact, two rows."""
    existing = [discovery.DiscoveredFact(
        id="P1", source=discovery.ACRA_SOURCE, fact_type="LOCATION",
        label="Branch", detail="",
        value="21 Tai Seng Avenue #03-12, Singapore 534054 (Registered: 1 June 2026)")]
    assert discovery.is_duplicate_fact(
        "LOCATION", "21 Tai Seng Avenue #03-12, Singapore 534054", existing) is True


def test_a_genuinely_different_unit_is_not_a_duplicate():
    existing = [discovery.DiscoveredFact(
        id="P1", source=discovery.ACRA_SOURCE, fact_type="LOCATION",
        label="Branch", detail="",
        value="21 Tai Seng Avenue #03-12, Singapore 534054 (Registered: 1 June 2026)")]
    assert discovery.is_duplicate_fact(
        "LOCATION", "21 Tai Seng Avenue #03-11, Singapore 534054", existing) is False


def test_headcount_duplicates_compare_as_numbers():
    existing = [discovery.DiscoveredFact(
        id="P1", source=discovery.MOM_SOURCE, fact_type="HEADCOUNT",
        label="Permits", detail="", value="4")]
    assert discovery.is_duplicate_fact(
        "HEADCOUNT", "Total Foreign Workers: 4", existing) is True


def test_short_values_do_not_swallow_unrelated_facts():
    """The headcount pattern yields the bare string "4", which is a substring of half the
    addresses in Singapore. Containment must not apply to it."""
    existing = [discovery.DiscoveredFact(
        id="P1", source=discovery.MOM_SOURCE, fact_type="HEADCOUNT",
        label="Permits", detail="", value="4")]
    assert discovery.is_duplicate_fact(
        "LOCATION", "24 Kaki Bukit Road #05-01, Singapore 417818", existing) is False


# absence of a model must never break discovery

def test_no_model_key_means_no_proposals_and_no_crash(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert discovery.extraction_enabled() is False

    proposals, meta = discovery.propose_facts_from_text("some page text", "ACRA")
    assert proposals == []
    assert meta["attempted"] is False


def test_patterns_still_work_with_the_model_switched_off(monkeypatch):
    """The model is an addition, not a dependency. With no key, discovery must behave
    exactly as it did before this feature existed."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = discovery.discover_business()

    assert result["facts_discovered"] >= 3
    assert result["extraction"]["model_used"] is False
    assert result["extraction"]["model_facts"] == 0
    assert result["extraction"]["pattern_facts"] == result["facts_discovered"]
    # the pre-existing pattern findings are untouched
    assert any(f["fact_type"] == "LOCATION" for f in result["facts"])
    assert all(f["extraction"] == "PATTERN" for f in result["facts"])


def test_pattern_facts_are_labelled_pattern_not_model():
    result = discovery.discover_business()
    for f in result["facts"]:
        assert f["extraction"] in ("PATTERN", "MODEL")


# the response must show what was thrown away

def test_discarded_proposals_are_reported_not_hidden(monkeypatch):
    """"We read six things and threw two away" is the most informative sentence this agent
    can produce, so it has to survive into the response."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = discovery.discover_business()

    assert "discarded" in result
    assert "discarded_by_gate" in result["extraction"]
    assert set(result["extraction"]["discarded_by_gate"]) == {
        "not_on_page", "wrong_kind_of_fact"}


# the whitelist: an unknown fact_type has no interest mapping, so it cannot be used

def test_unknown_fact_type_is_dropped_before_it_reaches_grounding(monkeypatch):
    """A type outside _FACT_TYPES has no insured-interest mapping, so it is unusable
    regardless of whether it is true."""
    import json as _json

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps({"choices": [{"message": {"content": _json.dumps({
                "facts": [
                    {"fact_type": "DIRECTOR", "value": "Tan Hock Lee", "label": "Director"},
                    {"fact_type": "LOCATION",
                     "value": "21 Tai Seng Avenue #03-12, Singapore 534054",
                     "label": "Address"},
                ]})}}]}).encode()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(discovery.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())

    proposals, meta = discovery.propose_facts_from_text("page", "ACRA", model_allowed=True)
    assert meta["proposed"] == 1
    assert [p["fact_type"] for p in proposals] == ["LOCATION"]


def test_model_transport_failure_degrades_to_patterns(monkeypatch):
    """A model outage must cost us the extra facts, not the agent."""
    def _boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(discovery.urllib.request, "urlopen", _boom)

    proposals, meta = discovery.propose_facts_from_text("page text", "ACRA", model_allowed=True)
    assert proposals == []
    assert meta["error"] == "Model transport failed"

    # and the whole agent still runs
    result = discovery.discover_business()
    assert result["facts_discovered"] >= 3


# ── HTML → text, so the grounding gate and the model read the same string ────
#
# The fixtures were plain text; a real website is HTML. If the model is handed clean prose
# and its proposal is then checked against raw markup, grounding rejects the parser rather
# than the model, and true facts get discarded as hallucinations.

def test_html_entities_are_decoded_or_grounding_would_reject_true_facts():
    """The measured trap. The demo site emits `Cold Storage &amp; Distribution` because
    that is correct HTML. Unescaped, the model reads the `&` version, proposes it, and the
    gate cannot find it in the raw bytes."""
    raw = "<div><b>Cold Storage &amp; Distribution</b></div>"
    text = discovery.html_to_text(raw)

    assert "&amp;" not in text
    assert "Cold Storage & Distribution" in text
    grounded, _ = discovery.ground_fact("Cold Storage & Distribution", text)
    assert grounded is True


def test_html_to_text_leaves_json_untouched():
    """This function is also on the ACRA path, where the response is JSON that facts are
    grounded against verbatim. Tag-stripping JSON would corrupt the evidence anchor."""
    payload = '{"result":{"records":[{"entity_name":"HOCK LEE FOODS PTE. LTD."}]}}'
    assert discovery.html_to_text(payload) == payload


def test_html_to_text_leaves_plain_text_untouched():
    plain = "Entity Name: HOCK LEE FOODS\nUEN: T09HL1234A"
    assert discovery.html_to_text(plain) == plain


def test_html_to_text_drops_script_and_style_content():
    raw = ("<html><head><style>.a{color:red}</style></head><body>"
           "<script>var x = 'not content';</script><p>Real content</p></body></html>")
    text = discovery.html_to_text(raw)
    assert "Real content" in text
    assert "color:red" not in text
    assert "not content" not in text


def test_address_split_across_lines_still_grounds():
    """Markup wraps addresses. ground_fact normalises whitespace on both sides, and this
    locks that in — it is what lets the site be formatted for humans."""
    raw = "<div><b>#03-11, 21 Tai Seng Avenue, Singapore\n534054</b></div>"
    text = discovery.html_to_text(raw)
    grounded, _ = discovery.ground_fact(
        "#03-11, 21 Tai Seng Avenue, Singapore 534054", text)
    assert grounded is True


def test_empty_and_none_html_are_safe():
    assert discovery.html_to_text("") == ""
    assert discovery.html_to_text(None) == ""


# ── the entity identity gate ─────────────────────────────────────────────────
#
# The worst failure this agent has produced. Searching the live register for the fictional
# "Hock Lee Foods Pte Ltd" returned MILLENNIUM AUTOMATION & SYSTEMS PTE. LTD. of Bencoolen
# Street, and it was published as DISCOVERY_VERIFIED facts about this business — because
# `q=` is a fuzzy search, something always ranks first, and grounding confirmed the name
# really was in the fetched bytes. Grounding proves a string is on the page. It says nothing
# about whose page.

def test_wrong_uen_is_not_this_business():
    ok, why = discovery.record_identifies_business(
        {"uen": "201201936C", "entity_name": "MILLENNIUM AUTOMATION & SYSTEMS PTE. LTD."},
        expected_name="Hock Lee Foods Pte Ltd", expected_uen="T09HL1234A")
    assert ok is False
    assert "201201936C" in why


def test_matching_uen_identifies_the_business():
    ok, why = discovery.record_identifies_business(
        {"uen": "T09HL1234A", "entity_name": "HOCK LEE FOODS PTE. LTD."},
        expected_name="Hock Lee Foods Pte Ltd", expected_uen="T09HL1234A")
    assert ok is True
    assert why == "UEN_MATCH"


def test_uen_beats_a_convincing_name():
    """A right-looking name with the wrong UEN is a different company. UEN is the
    authoritative identifier and no amount of name similarity overrides it."""
    ok, _ = discovery.record_identifies_business(
        {"uen": "999X", "entity_name": "HOCK LEE FOODS PTE. LTD."},
        expected_name="Hock Lee Foods Pte Ltd", expected_uen="T09HL1234A")
    assert ok is False


def test_name_match_ignores_legal_form_suffixes():
    ok, why = discovery.record_identifies_business(
        {"entity_name": "HOCK LEE FOODS PTE. LTD."},
        expected_name="Hock Lee Foods Pte Ltd", expected_uen="T09HL1234A")
    assert ok is True
    assert why == "NAME_MATCH"


@pytest.mark.parametrize("a,b", [
    ("HOCK LEE FOODS PTE. LTD.", "Hock Lee Foods Pte Ltd"),
    ("STRAITS FOODS (S) PTE. LTD.", "Straits Foods (S) Pte Ltd"),
    ("Acme Trading Private Limited", "ACME TRADING PTE LTD"),
])
def test_normalise_entity_name_equates_the_same_company(a, b):
    assert discovery.normalise_entity_name(a) == discovery.normalise_entity_name(b)


def test_normalise_entity_name_keeps_meaningful_words():
    """Stripping "Singapore" as boilerplate would make "Singapore Airlines" match
    "Airlines" — the exact class of error the gate exists to stop."""
    assert (discovery.normalise_entity_name("Singapore Airlines Limited")
            != discovery.normalise_entity_name("Airlines"))


def test_identify_subject_records_reports_who_was_rejected_and_why():
    records = [
        {"uen": "201201936C", "entity_name": "MILLENNIUM AUTOMATION & SYSTEMS PTE. LTD."},
        {"uen": "199203392Z", "entity_name": "DE COMFORT TRADING PTE LTD"},
    ]
    subject, meta = discovery.identify_subject_records(
        records, expected_name="Hock Lee Foods Pte Ltd", expected_uen="T09HL1234A")

    assert subject == []
    assert meta["records_returned"] == 2
    assert meta["records_identified"] == 0
    assert len(meta["not_this_business"]) == 2
    assert all(r["reason"] for r in meta["not_this_business"])


def test_no_expectation_means_no_identity_claim():
    """Direct unit tests of extraction pass records with no expected identity. That must
    not silently assert the record IS the business."""
    ok, why = discovery.record_identifies_business({"uen": "1", "entity_name": "X"})
    assert ok is True
    assert why == "NOT_CHECKED"


def test_a_register_full_of_other_companies_produces_no_facts(monkeypatch):
    """The integration lock. However many records come back, if none is this business then
    Discovery must publish nothing from the register and say so."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    others = [
        {"uen": "201201936C", "entity_name": "MILLENNIUM AUTOMATION & SYSTEMS PTE. LTD.",
         "reg_street_name": "BENCOOLEN STREET", "uen_status_desc": "Registered"},
        {"uen": "199203392Z", "entity_name": "DE COMFORT TRADING PTE LTD",
         "reg_street_name": "SOUTH BRIDGE ROAD", "uen_status_desc": "Registered"},
    ]
    monkeypatch.setattr(discovery, "fetch_acra_by_name",
                        lambda *a, **k: (others, json.dumps(others), ""))
    # No website either, so the register is the only source under test.
    monkeypatch.setattr(discovery, "fetch_url",
                        lambda *a, **k: (False, "", "refused"))

    out = discovery.discover_business(live=True)

    assert not any(f["source"]["id"] == "disc-acra" for f in out["facts"]), \
        "a record for another company reached the findings"
    acra = [c for c in out["fetch_log"] if c["source"] == "ACRA"][0]
    assert acra["records_returned"] == 2
    assert acra["records_identified"] == 0
    assert acra["ok"] is False
    assert "none of which is this business" in acra["error"]
    assert "BENCOOLEN" not in json.dumps(out["facts"]).upper()


def test_the_right_company_still_gets_through(monkeypatch):
    """The gate must not be a wall. A record that IS this business still produces facts."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    mine = [{"uen": "T09HL1234A", "entity_name": "HOCK LEE FOODS PTE. LTD.",
             "reg_street_name": "TAI SENG AVENUE", "uen_status_desc": "Registered",
             "entity_type_desc": "Local Company"}]
    monkeypatch.setattr(discovery, "fetch_acra_by_name",
                        lambda *a, **k: (mine, json.dumps(mine), ""))
    monkeypatch.setattr(discovery, "fetch_url",
                        lambda *a, **k: (False, "", "refused"))

    out = discovery.discover_business(live=True)

    acra = [c for c in out["fetch_log"] if c["source"] == "ACRA"][0]
    assert acra["records_identified"] == 1
    assert acra["identity"]["identified_by"] == "UEN_MATCH"
    assert any(f["source"]["id"] == "disc-acra" for f in out["facts"])


# ── the demo website address is resolved at call time ────────────────────────

def test_demo_business_website_comes_from_config_at_call_time(monkeypatch):
    """A module-level literal would freeze whatever the environment held at import, so
    monkeypatch.setenv would appear to work and change nothing."""
    monkeypatch.setenv("PROVEN_DEMO_SITE_URL", "http://127.0.0.1:9999")
    assert discovery.demo_business()["website"] == "http://127.0.0.1:9999"


def test_website_fetch_failure_on_localhost_says_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(discovery, "fetch_acra_by_name", lambda *a, **k: ([], "", ""))
    monkeypatch.setattr(discovery, "fetch_url",
                        lambda *a, **k: (False, "", "URL error: refused"))

    out = discovery.discover_business(live=True)
    web = [c for c in out["fetch_log"] if c["source"] == "Website"][0]
    assert web["ok"] is False
    assert "demo-site/site.py" in web["error"]


# ── one address mentioned twice on a page is one address ─────────────────────

def test_an_address_repeated_on_the_page_yields_one_fact(monkeypatch):
    """MEASURED on the real demo site. Opening a branch adds it to the locations list AND
    publishes a news post announcing it, so the address appears twice, `re.findall` returned
    it twice, and one new branch was reported as two. The old static fixture hid this by
    mentioning every address exactly once."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(discovery, "fetch_acra_by_name", lambda *a, **k: ([], "", ""))

    page = (
        "Our locations\n"
        "Kaki Bukit Central Kitchen\n"
        "#05-01, 9 Kaki Bukit Road 1, Singapore 417818\n"
        "News\n"
        "We have opened a new site at #05-01, 9 Kaki Bukit Road 1, Singapore 417818.\n")
    monkeypatch.setattr(discovery, "fetch_url", lambda *a, **k: (True, page, ""))

    out = discovery.discover_business(live=True)
    locations = [f for f in out["facts"] if f["fact_type"] == "LOCATION"]

    assert len(locations) == 1, f"expected one address, got {[f['value'] for f in locations]}"
