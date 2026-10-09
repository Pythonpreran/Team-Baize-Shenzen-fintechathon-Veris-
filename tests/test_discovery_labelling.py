"""The label must follow the fetch — in both directions.

THE MEASURED DEFECT.

Every fact in `discovery.py` was built with a module-level source constant, and those
constants hardcode `mode="DEMO_FIXTURE"`. Nothing ever flipped them. Since
`DiscoveredFact.as_dict()` derives `simulated` from `source.mode != "LIVE"`, facts that had
genuinely been fetched from data.gov.sg and from a live website reported `simulated: true`.

Verified against the running demo site before the fix: the demo site's published team size
was changed from 22 to 99, discovery reported 99 — so it was unquestionably reading live —
and the fact still came back `simulated: true, mode: DEMO_FIXTURE`.

WHY BOTH DIRECTIONS ARE TESTED.

Rendering a fixture as live overstates the product, and this codebase treats that as
unforgivable. Rendering live data as a fixture understates it, and on a demo screen it makes
the one genuinely live capability look simulated. Both are the same failure — the label not
following the fetch — so both get a test.

`sentinel.py` already had this right, using `dataclasses.replace(SOURCE, mode="LIVE")` after
a successful call. The fix applies that same pattern rather than inventing a new one.
"""
from __future__ import annotations

import pytest

from proven import discovery


def test_as_live_marks_a_source_without_mutating_the_constant():
    """The module constants are shared. Mutating one would mark every later fact live."""
    live = discovery.as_live(discovery.COMPANY_WEBSITE)
    assert live.mode == "LIVE"
    assert live.as_dict()["is_live"] is True
    # The original must be untouched, or one live run poisons every subsequent fixture run.
    assert discovery.COMPANY_WEBSITE.mode == "DEMO_FIXTURE"
    assert discovery.COMPANY_WEBSITE.as_dict()["is_live"] is False


def test_fixture_mode_labels_every_fact_simulated():
    """The direction that must never break: nothing fetched, nothing claimed as live."""
    result = discovery.discover_business(live=False)
    assert result["facts"], "the fixture run produced no facts"
    for fact in result["facts"]:
        assert fact["simulated"] is True, (
            f"{fact['id']} was labelled live in a fixture run")
        assert fact["source"]["is_live"] is False


def test_a_live_website_fetch_labels_its_facts_live(monkeypatch):
    """The direction that WAS broken. A successful fetch must not say fixture."""
    page = ("Hock Lee Foods. We are a team of 42 people. "
            "Our sites: #05-01, 9 Kaki Bukit Road 1, Singapore 417818.")
    monkeypatch.setattr(discovery, "fetch_url",
                        lambda url: (True, page, ""))
    monkeypatch.setattr(discovery, "html_to_text", lambda raw: raw)
    monkeypatch.setattr(discovery, "fetch_acra_by_name",
                        lambda *a, **k: ([], "", "skipped"))
    monkeypatch.setattr(discovery, "demo_business", lambda: {
        **discovery._DEMO_BUSINESS, "website": "http://127.0.0.1:8100"})

    result = discovery.discover_business(live=True)
    website_facts = [f for f in result["facts"]
                     if f["source"]["id"] == discovery.COMPANY_WEBSITE.id]
    assert website_facts, "the live website fetch produced no facts"
    for fact in website_facts:
        assert fact["simulated"] is False, (
            f"{fact['id']} came from a live fetch and was labelled simulated")
        assert fact["source"]["mode"] == "LIVE"


def test_a_failed_live_fetch_still_says_fixture(monkeypatch):
    """`live=True` is a request, not a result. A website that does not resolve must not
    upgrade anything — deriving the label from the mode would produce exactly that error."""
    monkeypatch.setattr(discovery, "fetch_url",
                        lambda url: (False, "", "getaddrinfo failed"))
    monkeypatch.setattr(discovery, "fetch_acra_by_name",
                        lambda *a, **k: ([], "", "network down"))
    monkeypatch.setattr(discovery, "demo_business", lambda: {
        **discovery._DEMO_BUSINESS, "website": "http://does-not-resolve.invalid"})

    result = discovery.discover_business(live=True)
    for fact in result["facts"]:
        assert fact["simulated"] is True, (
            f"{fact['id']} was labelled live after every fetch failed")


def test_live_acra_records_are_labelled_live(monkeypatch):
    """The registry half of the same bug."""
    record = {
        "uen": "T09HL1234A",
        "entity_name": "HOCK LEE FOODS PTE. LTD.",
        "uen_status_desc": "Live",
        "entity_type_desc": "Private Company Limited by Shares",
        "reg_street_name": "TAI SENG AVENUE",
    }
    raw = (
        '{"uen":"T09HL1234A","entity_name":"HOCK LEE FOODS PTE. LTD.",'
        '"uen_status_desc":"Live","reg_street_name":"TAI SENG AVENUE"}')
    monkeypatch.setattr(discovery, "fetch_acra_by_name",
                        lambda *a, **k: ([record], raw, ""))
    monkeypatch.setattr(discovery, "fetch_url", lambda url: (False, "", "no website"))
    monkeypatch.setattr(discovery, "demo_business", lambda: {
        **discovery._DEMO_BUSINESS, "name": "HOCK LEE FOODS PTE. LTD.",
        "uen": "T09HL1234A", "website": None})

    result = discovery.discover_business(live=True)
    acra_facts = [f for f in result["facts"]
                  if f["source"]["id"] == discovery.ACRA_SOURCE.id]
    assert acra_facts, "the live ACRA lookup produced no facts"
    for fact in acra_facts:
        assert fact["simulated"] is False, (
            f"{fact['id']} came from the live register and was labelled simulated")
        assert fact["source"]["mode"] == "LIVE"


def test_the_two_provenances_can_coexist_in_one_run(monkeypatch):
    """A live website and an unreachable register in the same run must produce facts with
    DIFFERENT labels. One blanket flag for the whole run is what caused the original bug."""
    page = "We are a team of 42 people."
    monkeypatch.setattr(discovery, "fetch_url", lambda url: (True, page, ""))
    monkeypatch.setattr(discovery, "html_to_text", lambda raw: raw)
    monkeypatch.setattr(discovery, "fetch_acra_by_name",
                        lambda *a, **k: ([], "", "register unreachable"))
    monkeypatch.setattr(discovery, "demo_business", lambda: {
        **discovery._DEMO_BUSINESS, "website": "http://127.0.0.1:8100"})

    result = discovery.discover_business(live=True)
    labels = {f["simulated"] for f in result["facts"]}
    # At minimum the website facts are live; nothing asserts the register produced any.
    assert False in labels, "a successful live fetch produced no live-labelled fact"
