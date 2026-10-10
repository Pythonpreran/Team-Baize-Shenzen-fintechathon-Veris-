"""The Singapore SME output is unchanged by the jurisdiction packs — byte for byte.

PLAN T03: "Migrate the Singapore constants out of code into SG.yaml ... Behaviour must not
change — pin it with a golden snapshot of the existing SME brief taken before the change."
The snapshots in tests/golden/ were captured from the tree before any pack existed
(`python tests/sme_golden.py --write`). See tests/sme_golden.py for exactly what the
normalisation removes — per-run ids, timestamps and the session-derived business id — and
for the one key added since, `jurisdiction`, which these tests check is really present
rather than letting the normalisation hide its absence.
"""
from __future__ import annotations

import pytest

import sme_golden
from proven import pipeline
from proven.api import SESSIONS


@pytest.fixture(autouse=True)
def _fresh_sessions():
    SESSIONS.clear()
    yield
    SESSIONS.clear()


_NEEDS_WORDING = pytest.mark.requires_corpus(pipeline.CHUBB_ID)


@pytest.mark.parametrize("name", [
    pytest.param("demo", marks=_NEEDS_WORDING),
    pytest.param("example-upload", marks=_NEEDS_WORDING),
    "example-synthetic",
])
def test_the_singapore_sme_output_is_unchanged(name):
    capture, _needs_corpus = sme_golden.SCENARIOS[name]
    payload = capture()
    # The added key is dropped by the comparison, so prove it is there and says Singapore.
    assert payload["brief"]["jurisdiction"]["pack"] == "SG"
    assert payload["brief"]["jurisdiction"]["rules_not_applied"] == []
    actual = sme_golden.comparable(name, payload)
    expected = sme_golden.load(name)
    diff = sme_golden.first_difference(expected, actual)
    assert diff is None, f"{name} changed at {diff}"


@pytest.mark.requires_corpus(pipeline.CHUBB_ID)
def test_no_committed_golden_file_quotes_the_wording():
    """The snapshots are committed; the published wording is not, and must not be smuggled
    in through them. Every quotation of it is stored as a length and a hash."""
    windows = sme_golden.wording_windows()
    assert windows

    def strings(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from strings(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from strings(v)
        elif isinstance(obj, str):
            yield obj

    for path in sorted(sme_golden.GOLDEN_DIR.glob("sme-*.json")):
        stored = sme_golden.json.loads(path.read_text(encoding="utf-8"))
        quoting = [s[:80] for s in strings(stored) if sme_golden.quotes_wording(s, windows)]
        assert not quoting, f"{path.name} quotes the published wording: {quoting[:3]}"


def test_protecting_the_wording_replaces_quotations_and_nothing_else():
    clause = "cover under the Policy is automatically terminated immediately after the expiry"
    windows = frozenset(clause[i:i + sme_golden.WORDING_WINDOW]
                        for i in range(len(clause) - sme_golden.WORDING_WINDOW + 1))
    out = sme_golden.protect_wording(
        {"quote": "... the cover under the Policy is automatically terminated immediately "
                  "after the expiry of that 60-day period",
         "ours": "Premium appears to have been received on day 71 of the policy period.",
         "n": 71}, windows)
    assert out["quote"].startswith("<quotes the published wording: ")
    assert out["ours"] == "Premium appears to have been received on day 71 of the policy period."
    assert out["n"] == 71
    # Exact: one changed character is a different hash.
    again = sme_golden.protect_wording(
        {"quote": "... the cover under the Policy is automatically terminated immediately "
                  "after the expiry of that 61-day period"}, windows)
    assert again["quote"] != out["quote"]


def test_the_normalisation_removes_only_what_it_names():
    raw = {"id": "0123456789abcdef0123456789abcdef", "at": "2026-10-10T12:00:00+00:00",
           "business_id": "uploaded-AbCd1234", "headline": "S$60,000 on day 71",
           "jurisdiction": {"pack": "SG"}, "orchestration": {"x": 1},
           "nested": [{"jurisdiction": 1, "keep": "this"}]}
    assert sme_golden.normalise(raw) == {
        "id": "<id>", "at": "<ts>", "business_id": "uploaded-<sid>",
        "headline": "S$60,000 on day 71", "nested": [{"keep": "this"}]}
