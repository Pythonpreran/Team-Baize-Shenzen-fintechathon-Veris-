"""PRIORITY 1 — real external data, tested without touching the network.

Every test here MOCKS `urllib.request.urlopen` so the suite never makes an outbound
call, while still exercising the real parsing, grounding, mode-reporting and fallback
code paths. The contract under test is the one the whole build turns on:

  * a source is LIVE only after a real call succeeds AND parses;
  * a failed/timed-out/malformed call falls back to the labelled fixture;
  * a fixture is never rendered as live.
"""
import io
import json
import urllib.error

import pytest

from proven import sentinel, discovery, drift


# ── fake HTTP plumbing ───────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body: bytes, content_type: str = "application/json"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, n: int = -1):
        return self._body

    # urllib returns a context manager
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # discovery.fetch_url uses these
    def getheader(self, k, default=None):
        return self.headers.get(k, default)


def _rainfall_payload(peak_mm: float = 38.0):
    return {
        "code": 0,
        "data": {
            "stations": [
                {"id": "S43", "name": "Kim Chuan Road",
                 "location": {"latitude": 1.3337, "longitude": 103.8912}},
                {"id": "S07", "name": "Somewhere Else",
                 "location": {"latitude": 1.30, "longitude": 103.70}},
            ],
            "readings": [{"timestamp": "2026-08-15T13:00:00+08:00",
                          "data": [{"stationId": "S43", "value": peak_mm},
                                   {"stationId": "S07", "value": 0}]}],
            "readingType": "TB1", "readingUnit": "mm",
        },
    }


def _forecast_payload():
    return {"data": {"records": [{
        "timestamp": "2026-08-15T12:00:00+08:00",
        "general": {"forecast": {"text": "Thundery Showers", "code": "TS"}},
        "periods": [{"regions": {"east": {"text": "Thundery Showers"},
                                 "west": {"text": "Cloudy"}}}],
    }]}}


def _singstat_payload():
    # Two years of monthly points; steel bars rising 700 -> 770 (+10%).
    cols = []
    val = 700.0
    for y in (2025, 2026):
        for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                  "Sep", "Oct", "Nov", "Dec"):
            cols.append({"key": f"{y} {m}", "value": f"{val:.1f}"})
            val += 2.8
    return {"Data": {"row": [
        {"rowText": "Steel Reinforcement Bars (16-32mm High Tensile)",
         "columns": cols}]}, "StatusCode": 200}


def _acra_payload():
    return {"result": {"records": [
        {"uen": "202138702C", "entity_name": "STRAITS FOODS (S) PTE. LTD.",
         "uen_status_desc": "Registered", "entity_type_desc": "Local Company",
         "reg_street_name": "SOUTH BRIDGE ROAD"}]}}


def _onemap_payload():
    return {"found": 1, "results": [
        {"SEARCHVAL": "TAI SENG AVENUE", "LATITUDE": "1.3369", "LONGITUDE": "103.8912",
         "ADDRESS": "TAI SENG AVENUE"}]}


def _router(mapping: dict, error_for: set = frozenset()):
    """Return a fake urlopen that dispatches on substrings of the request URL."""
    def _fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for key in error_for:
            if key in url:
                raise urllib.error.URLError("simulated network failure")
        for key, payload in mapping.items():
            if key in url:
                body = payload if isinstance(payload, bytes) \
                    else json.dumps(payload).encode()
                return _FakeResp(body)
        raise urllib.error.HTTPError(url, 404, "not mapped", {}, None)
    return _fake


# ── Sentinel weather: LIVE success ───────────────────────────────────────────

def test_weather_live_success(monkeypatch):
    monkeypatch.setattr(sentinel.urllib.request, "urlopen", _router({
        "rainfall": _rainfall_payload(38.0),
        "twenty-four-hr": _forecast_payload(),
        "onemap": _onemap_payload(),
    }))
    sig, log = sentinel._fetch_weather("Tai Seng Avenue")
    assert sig is not None
    assert sig.source.mode == "LIVE"
    assert sig.as_dict()["simulated"] is False
    assert sig.severity == "HIGH"           # 38mm >= 30
    assert "38" in sig.detail
    # real distance to nearest station computed from coordinates
    assert "S43" in sig.detail and "km" in sig.detail


def test_weather_timeout_falls_back(monkeypatch):
    monkeypatch.setattr(sentinel.urllib.request, "urlopen",
                        _router({}, error_for={"rainfall"}))
    sig, log = sentinel._fetch_weather()
    assert sig is None                       # no live signal
    assert any(not c["ok"] for c in log)


def test_weather_malformed_response(monkeypatch):
    monkeypatch.setattr(sentinel.urllib.request, "urlopen",
                        _router({"rainfall": b"not json at all"}))
    sig, log = sentinel._fetch_weather()
    assert sig is None


# ── Sentinel cost index: LIVE parse + magnitude ──────────────────────────────

def test_cost_index_movement_live(monkeypatch):
    monkeypatch.setattr(sentinel.urllib.request, "urlopen",
                        _router({"tabledata": _singstat_payload()}))
    movement, err = sentinel.cost_index_movement()
    assert err == ""
    assert movement["table_id"] == "M211251"
    assert "Steel" in movement["material"]
    assert movement["newest_month"] == "2026 Dec"
    assert movement["yoy_pct"] > 0


def test_cost_index_malformed(monkeypatch):
    monkeypatch.setattr(sentinel.urllib.request, "urlopen",
                        _router({"tabledata": {"Data": {"row": []}}}))
    movement, err = sentinel.cost_index_movement()
    assert movement is None and err


# ── Sentinel feed: LIVE vs DEMO truthful reporting ───────────────────────────

def test_feed_live_marks_sources_live(monkeypatch):
    monkeypatch.setattr(sentinel.urllib.request, "urlopen", _router({
        "rainfall": _rainfall_payload(5.0), "twenty-four-hr": _forecast_payload(),
        "tabledata": _singstat_payload(), "onemap": _onemap_payload(),
    }))
    out = sentinel.feed(region="SG-TAI-SENG", sector="FOOD_MANUFACTURING", live=True)
    assert out["live"]["any_live"] is True
    live_ids = set(out["live"]["live_source_ids"])
    assert "src-met" in live_ids and "src-index" in live_ids
    # the live signals carry LIVE source mode and simulated=False
    live_sigs = [s for s in out["signals"] if s["source"]["mode"] == "LIVE"]
    assert live_sigs and all(s["simulated"] is False for s in live_sigs)
    assert out["sources"]["live_count"] >= 2


def test_feed_demo_default_is_all_fixture():
    """With live off (the test default), every source stays DEMO_FIXTURE — the
    invariant the offline suite depends on."""
    out = sentinel.feed(live=False)
    assert out["live"]["any_live"] is False
    for s in out["signals"]:
        assert s["source"]["mode"] == "DEMO_FIXTURE"
        assert s["simulated"] is True
    assert out["sources"]["live_count"] == 0


def test_feed_live_but_all_calls_fail_falls_back(monkeypatch):
    monkeypatch.setattr(sentinel.urllib.request, "urlopen",
                        _router({}, error_for={"rainfall", "tabledata",
                                               "twenty-four-hr"}))
    out = sentinel.feed(live=True)
    assert out["live"]["enabled"] is True
    assert out["live"]["any_live"] is False   # nothing succeeded
    assert out["sources"]["live_count"] == 0  # fixtures, honestly labelled


# ── Discovery: real ACRA lookup, grounded ────────────────────────────────────

def test_acra_live_lookup_grounds_facts(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, "urlopen",
                        _router({"datastore_search": _acra_payload()}))
    records, raw, err = discovery.fetch_acra_by_name("FOODS")
    assert err == "" and records
    facts = discovery._acra_live_facts(records, raw, {"locations": []}, "2026-08-15")
    assert facts
    entity = facts[0]
    assert entity.claim_scope == discovery.DISCOVERY_VERIFIED
    assert entity.grounded is True
    assert "202138702C" in entity.value


def test_acra_ungrounded_value_drops_to_hint():
    """A value that is not present verbatim in the fetched bytes cannot be VERIFIED."""
    raw = json.dumps({"result": {"records": [{"uen": "999", "entity_name": "X"}]}})
    facts = discovery._acra_live_facts(
        [{"uen": "NOT_IN_RAW", "entity_name": "GHOST", "reg_street_name": ""}],
        raw, {"locations": []}, "2026-08-15")
    assert facts
    assert facts[0].claim_scope == discovery.DISCOVERY_HINT
    assert facts[0].grounded is False


def test_acra_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, "urlopen",
                        _router({}, error_for={"datastore_search"}))
    records, raw, err = discovery.fetch_acra_by_name("FOODS")
    assert records == [] and err


def test_discovery_live_reports_live_mode(monkeypatch):
    monkeypatch.setattr(discovery.urllib.request, "urlopen",
                        _router({"datastore_search": _acra_payload()},
                                error_for={"hockleefoods"}))
    out = discovery.discover_business(live=True, entity_query="FOODS")
    assert out["demo_mode"] is False
    acra_log = [c for c in out["fetch_log"] if c["source"] == "ACRA"][0]
    assert acra_log["records_returned"] == 1


# ── Drift: real published index, arithmetic unchanged ────────────────────────

def test_drift_build_live_index_rebases_to_100(monkeypatch):
    monkeypatch.setattr(sentinel.urllib.request, "urlopen",
                        _router({"tabledata": _singstat_payload()}))
    movement, _ = sentinel.cost_index_movement()
    idx = drift.build_live_index(movement, window_months=24)
    assert idx is not None
    assert idx.basis == drift.INDEX_PUBLISHED
    assert idx.first.value == 100  # rebased
    assert idx.last.value > idx.first.value  # rising series


def test_drift_live_uses_published_series(monkeypatch):
    from decimal import Decimal
    monkeypatch.setattr(sentinel.urllib.request, "urlopen",
                        _router({"tabledata": _singstat_payload()}))

    class _F:
        established = True
        value = Decimal("100000")
        author_class = "INSURER"

    class _Facts:
        def get(self, name, unit=None):
            return _F() if name == "plant_sum_insured" else \
                type("N", (), {"established": False})()

    out = drift.drift_for_case(facts=_Facts(), carrier="CHUBB", live=True)
    assert out["available"] is True
    assert out["index_basis"] == drift.INDEX_PUBLISHED
    assert out["live"]["is_live"] is True
    assert "M211251" in out["source_presentation"]


def test_drift_demo_default_is_sample():
    from decimal import Decimal

    class _F:
        established = True
        value = Decimal("100000")
        author_class = "INSURER"

    class _Facts:
        def get(self, name, unit=None):
            return _F() if name == "plant_sum_insured" else \
                type("N", (), {"established": False})()

    out = drift.drift_for_case(facts=_Facts(), carrier="CHUBB", live=False)
    assert out["index_basis"] == drift.INDEX_SAMPLE
    assert out["live"]["is_live"] is False
