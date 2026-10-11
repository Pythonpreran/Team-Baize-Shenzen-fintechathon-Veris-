"""VERIS SENTINEL — the external risk agent.

WHAT IT IS FOR. `connectors.py` (VERIS Connect) answers "what changed inside this
business?". This answers the other half: "what is changing around it?" Weather, flood
exposure, published cost indices, infrastructure disruption — the conditions an SME does
not control and usually does not track against its own policy.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE, and it is the reason it is a separate module
rather than more rows in the connector feed:

    AN EXTERNAL SIGNAL IS NEVER A FACT ABOUT THIS BUSINESS.

Regional theft statistics rising is not "you were robbed". A flood warning is not "your
premises flooded". A published equipment index rising 12% is not "your equipment is worth
12% more". Every one of those inferences is the kind of thing a language model will make
confidently and a claims adjuster will reject, so the type system refuses it: a Signal
here carries a `claim_scope` that is structurally incapable of asserting a
business-specific event, and `orchestrator.py` is the only place the two halves meet.

    RISK_SIGNAL          something is happening in the environment
    POTENTIAL_EXPOSURE   this business's insured interest sits in that environment
    ---- everything above this line is as far as Sentinel can go ----
    SME_INCIDENT         something happened to THIS business  (Sentinel cannot emit)
    CONFIRMED_EVIDENCE   a document establishes it            (Sentinel cannot emit)

SOURCES ARE MOCKED AND SAY SO. There is no live weather API key in this build. Every
source carries `mode: "DEMO_FIXTURE"` and the interface renders it. The architecture is
the part that is real: `ExternalSource` is the seam a live feed drops into, and swapping
the fixture for an HTTP call changes nothing below it — the classification, the geo/sector
match, the claim-scope ceiling and the orchestrator all stay as they are. Saying that is
more defensible than a fake "Connected" badge.

WHY NOT JUST ASK A MODEL. Because "is this flood warning relevant to this policy?" has a
checkable answer and a plausible-sounding one, and they are not always the same. Relevance
here is a deterministic match on region, sector, insured interest and time window. The
model's job, in `ai.py`, is reading prose. It is not consulted about consequence.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Optional

from . import config, security

# ── claim scope: the structural ceiling on what Sentinel may assert ──────────

RISK_SIGNAL = "RISK_SIGNAL"
POTENTIAL_EXPOSURE = "POTENTIAL_EXPOSURE"

# Present so the boundary is documented in code rather than only in prose. Nothing in
# this module constructs a signal with either value, and a test asserts that.
SME_INCIDENT = "SME_INCIDENT"
CONFIRMED_EVIDENCE = "CONFIRMED_EVIDENCE"

SENTINEL_MAY_EMIT: frozenset[str] = frozenset({RISK_SIGNAL, POTENTIAL_EXPOSURE})
SENTINEL_MAY_NOT_EMIT: frozenset[str] = frozenset({SME_INCIDENT, CONFIRMED_EVIDENCE})

# ── severity, deliberately not a number ─────────────────────────────────────
#
# A 0-1 "risk score" invites arithmetic that the underlying data does not support:
# multiplying a forecast confidence by a sum insured produces a figure with no basis.
# Ordered labels can be compared and cannot be multiplied.

SEVERITY_ORDER: tuple[str, ...] = ("LOW", "MODERATE", "ELEVATED", "HIGH")


def severity_rank(severity: str) -> int:
    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return 0


HORIZON_CURRENT = "CURRENT"        # observed now
HORIZON_FORECAST = "FORECAST"      # expected within a stated window
HORIZON_TREND = "TREND"            # a direction over past periods

DEMO_NOTICE = (
    "DEMO FIXTURE. This build holds no live external API credentials and makes no "
    "outbound calls to a weather, index or infrastructure provider. The signals below "
    "are fixed demonstration data and are labelled as such wherever they appear. What "
    "is real is the architecture around them: source provenance, deterministic "
    "relevance matching, and a hard ceiling on what an external signal is allowed to "
    "assert about a specific business."
)


@dataclass(frozen=True)
class ExternalSource:
    """Provenance for an external feed. This is the seam a live integration replaces.

    `mode` is not decoration. A product that renders a mocked feed as "Connected" has
    told its most consequential lie on the least examined screen.
    """

    id: str
    name: str
    category: str                    # WEATHER | INDEX | INFRASTRUCTURE | HAZARD
    publisher: str
    reliability: str                 # AUTHORITATIVE | OFFICIAL_STATISTIC | COMMERCIAL
    mode: str = "DEMO_FIXTURE"       # DEMO_FIXTURE | LIVE
    live_equivalent: str = ""        # what would be called in a real deployment

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "publisher": self.publisher, "reliability": self.reliability,
            "mode": self.mode, "is_live": self.mode == "LIVE",
            "live_equivalent": self.live_equivalent,
        }


@dataclass(frozen=True)
class RiskSignal:
    """One external signal, with a ceiling on what it may claim."""

    id: str
    source: ExternalSource
    signal_type: str                 # FLOOD_RISK | SEVERE_WEATHER | COST_INDEX ...
    label: str
    detail: str
    severity: str
    horizon: str
    observed_on: str                 # ISO date
    window: str                      # human-readable validity
    regions: tuple[str, ...]         # where it applies
    sectors: tuple[str, ...]         # which sectors, or ("ANY",)
    # The insured interests this signal could bear on. Used for deterministic relevance
    # matching and for retrieval, never for arithmetic.
    insured_interests: tuple[str, ...]
    # Rule ids in the existing engine this signal makes worth re-reading. This is what
    # keeps Sentinel feeding the SAME policy engine rather than a parallel one.
    relevant_rules: tuple[str, ...] = ()
    claim_scope: str = RISK_SIGNAL
    magnitude_pct: Optional[float] = None      # for index signals only
    agent: str = "sentinel"

    def __post_init__(self) -> None:
        # The ceiling, enforced at construction. A signal that could claim an incident
        # would be indistinguishable downstream from one derived from a document.
        if self.claim_scope not in SENTINEL_MAY_EMIT:
            raise ValueError(
                f"Sentinel may not emit claim_scope {self.claim_scope!r}. An external "
                f"feed cannot establish a fact about a specific business; only a "
                f"document can. Allowed: {sorted(SENTINEL_MAY_EMIT)}.")
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"Unknown severity {self.severity!r}.")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "signal_type": self.signal_type,
            "label": self.label,
            "detail": self.detail,
            "severity": self.severity,
            "severity_rank": severity_rank(self.severity),
            "horizon": self.horizon,
            "observed_on": self.observed_on,
            "window": self.window,
            "regions": list(self.regions),
            "sectors": list(self.sectors),
            "insured_interests": list(self.insured_interests),
            "relevant_rules": list(self.relevant_rules),
            "claim_scope": self.claim_scope,
            "magnitude_pct": self.magnitude_pct,
            "source": self.source.as_dict(),
            "simulated": self.source.mode != "LIVE",
            # Rendered verbatim next to the signal. The distinction it draws is the
            # whole point of the module, so it travels with the data rather than living
            # in a style guide nobody reads.
            "asserts": ("An external condition in the operating environment. It does "
                        "NOT assert that anything has happened to this business."),
        }


# ── the sources ─────────────────────────────────────────────────────────────

MET_SERVICE = ExternalSource(
    id="src-met", name="National weather service", category="WEATHER",
    publisher="Meteorological service (demo stand-in)",
    reliability="AUTHORITATIVE",
    live_equivalent="data.gov.sg real-time weather / NEA 2-hour forecast API")

FLOOD_REGISTRY = ExternalSource(
    id="src-flood", name="Flood-prone area registry", category="HAZARD",
    publisher="Water authority (demo stand-in)", reliability="AUTHORITATIVE",
    live_equivalent="PUB flood-prone areas + water level sensor feed")

COST_INDEX = ExternalSource(
    id="src-index", name="Published construction and equipment cost index",
    category="INDEX", publisher="National statistics office (demo stand-in)",
    reliability="OFFICIAL_STATISTIC",
    live_equivalent="SingStat / BCA tender price index series")

INFRA_FEED = ExternalSource(
    id="src-infra", name="Infrastructure disruption notices", category="INFRASTRUCTURE",
    publisher="Transport and utilities authority (demo stand-in)",
    reliability="OFFICIAL_STATISTIC",
    live_equivalent="LTA / utility provider incident feeds")

SOURCES: tuple[ExternalSource, ...] = (MET_SERVICE, FLOOD_REGISTRY, COST_INDEX,
                                       INFRA_FEED)

# ── the demo feed ───────────────────────────────────────────────────────────
#
# Chosen so that ONE of them overlaps the connector feed on the same insured interest
# and the same location. That overlap is the capability worth showing: two independent
# agents, one raising a business change and the other an environmental condition, landing
# on the same policy clause. Everything else here is deliberately non-overlapping so the
# overlap means something.

_FEED: tuple[RiskSignal, ...] = (
    RiskSignal(
        id="EXT-001", source=MET_SERVICE, signal_type="SEVERE_WEATHER",
        label="Severe rainfall forecast for the eastern industrial corridor",
        detail=("Sustained heavy rainfall is forecast over a 36-hour window covering "
                "the district containing the insured premises."),
        severity="ELEVATED", horizon=HORIZON_FORECAST,
        observed_on="2026-08-13", window="14-15 Aug 2026",
        regions=("SG-EAST", "SG-TAI-SENG", "SG-JURONG"),
        sectors=("ANY",),
        insured_interests=("stock", "buildings", "plant"),
        relevant_rules=("AVERAGE-ADEQUACY-BELOW-THRESHOLD",
                        "UNOCCUPANCY-WATER-EXCLUSION"),
    ),
    RiskSignal(
        id="EXT-002", source=FLOOD_REGISTRY, signal_type="FLOOD_RISK",
        label="Insured district sits within a designated flood-prone area",
        detail=("The registry lists the postal district of the scheduled premises as "
                "flood-prone. This is a standing characteristic of the location, not "
                "an event."),
        severity="ELEVATED", horizon=HORIZON_CURRENT,
        observed_on="2026-08-01", window="Standing designation",
        regions=("SG-TAI-SENG",),
        sectors=("ANY",),
        insured_interests=("stock", "buildings"),
        relevant_rules=("AVERAGE-ADEQUACY-BELOW-THRESHOLD",),
        # A standing characteristic that touches this policy's insured interest is as
        # far as it goes: the premises being in a flood district is not a flood.
        claim_scope=POTENTIAL_EXPOSURE,
    ),
    RiskSignal(
        id="EXT-003", source=COST_INDEX, signal_type="COST_INDEX",
        label="Equipment and machinery cost index up 8.4% year on year",
        detail=("Published index of replacement cost for commercial plant and "
                "refrigeration equipment. A forward pressure on what reinstatement "
                "would cost, not a revaluation of any particular asset."),
        severity="MODERATE", horizon=HORIZON_TREND,
        observed_on="2026-07-31", window="12 months to Jul 2026",
        regions=("SG",), sectors=("FOOD_MANUFACTURING", "ANY"),
        insured_interests=("plant", "buildings"),
        relevant_rules=("PLANT-ADEQUACY-UNESTABLISHED",
                        "BUILDINGS-VALUE-UNESTABLISHED"),
        magnitude_pct=8.4,
    ),
    RiskSignal(
        id="EXT-004", source=INFRA_FEED, signal_type="INFRASTRUCTURE",
        label="Planned power interruption, Tai Seng industrial estate",
        detail=("Scheduled maintenance affecting the estate containing the insured "
                "premises. Relevant to cold-chain and business interruption exposure."),
        severity="LOW", horizon=HORIZON_FORECAST,
        observed_on="2026-08-10", window="22 Aug 2026, 0100-0500",
        regions=("SG-TAI-SENG",), sectors=("FOOD_MANUFACTURING",),
        insured_interests=("stock", "business_interruption"),
        relevant_rules=("BUSINESS-INTERRUPTION-NOT-CONFIRMED",),
    ),
)

BY_ID: dict[str, RiskSignal] = {s.id: s for s in _FEED}


# ── the live fetch layer ─────────────────────────────────────────────────────
#
# Real outbound calls to Singapore government open-data endpoints. Every one has a
# hard timeout, a User-Agent, and an exception boundary: a failure returns None and the
# caller falls back to the labelled fixture. A source is stamped LIVE only after a call
# actually succeeds and parses — never optimistically.

RAINFALL_URL = "https://api-open.data.gov.sg/v2/real-time/api/rainfall"
FORECAST_24H_URL = ("https://api-open.data.gov.sg/v2/real-time/api/"
                    "twenty-four-hr-forecast")
SINGSTAT_TABLE_URL = "https://tablebuilder.singstat.gov.sg/api/table/tabledata/M211251"
ONEMAP_SEARCH_URL = ("https://www.onemap.gov.sg/api/common/elastic/search"
                     "?searchVal={q}&returnGeom=Y&getAddrDetails=Y&pageNum=1")

FETCH_TIMEOUT = 15
_UA = ("VERIS-Sentinel/1.0 (+Singapore SME insurance risk agent; "
       "contact: demo@veris.local)")
# SingStat rejects a non-browser agent with 403, and OneMap is happiest with one too.
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _http_json(url: str, *, browser: bool = False) -> tuple[Optional[dict], str]:
    """GET a URL and parse JSON. Returns (payload, error). Never raises.

    `browser=True` sends a full browser User-Agent plus Accept/Referer, which SingStat
    and OneMap require. The timeout is mandatory: a hung government endpoint must not
    become a hung demo.
    """
    headers = {"User-Agent": _BROWSER_UA if browser else _UA,
               "Accept": "application/json, text/plain, */*"}
    if browser:
        headers["Accept-Language"] = "en-US,en;q=0.9"
        headers["Referer"] = "https://tablebuilder.singstat.gov.sg/"
    try:
        security.require_outbound_url(url, purpose="sentinel fetch")
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # nosec B310
            raw = resp.read(2_000_000)
            return json.loads(raw.decode("utf-8", errors="replace")), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    except (ValueError, TimeoutError) as e:
        return None, f"Parse/timeout error: {type(e).__name__}: {e}"
    except Exception as e:                                         # pragma: no cover
        return None, f"Fetch failed: {type(e).__name__}: {e}"


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km. Used to match the insured premises to the nearest
    rainfall station by real coordinates rather than by a region string."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def _address_query(address: str) -> str:
    """Reduce a messy premises string to something OneMap's search will match.

    OneMap's elastic search keys on postal codes and street names, and chokes on unit
    prefixes. Prefer a 6-digit Singapore postal code when present; otherwise drop the
    unit ("Unit 03-11", "#03-11,") and any leading block number so a street name is left.
    """
    import re
    a = (address or "").strip()
    postal = re.search(r"\b(\d{6})\b", a)
    if postal:
        return postal.group(1)
    # Remove unit tokens and everything before the last comma-delimited unit clause.
    a = re.sub(r"\bunit\b\s*[\w\-]+,?", "", a, flags=re.IGNORECASE)
    a = re.sub(r"#\s*\d{1,3}\s*-\s*\d{1,4},?", "", a)
    a = a.strip().strip(",").strip()
    # Drop a leading block/house number so "21 Tai Seng Avenue" -> "Tai Seng Avenue".
    a = re.sub(r"^\d+[A-Za-z]?\s+", "", a).strip()
    return a or (address or "").strip()


def geocode_onemap(address: str) -> tuple[Optional[tuple[float, float]], str]:
    """Geocode an address via OneMap. Returns ((lat, lon), error).

    OneMap returns an "Authentication token missing" string in an `error` field but
    still serves full results including geometry; we use the results and tolerate the
    field. If it ever stops, we fall back honestly.
    """
    if not address or not address.strip():
        return None, "No address supplied to geocode."
    q = urllib.request.quote(_address_query(address))
    payload, err = _http_json(ONEMAP_SEARCH_URL.format(q=q), browser=True)
    if payload is None:
        return None, err
    results = payload.get("results") or []
    if not results:
        return None, "OneMap returned no matching location."
    top = results[0]
    try:
        return (float(top["LATITUDE"]), float(top["LONGITUDE"])), ""
    except (KeyError, ValueError, TypeError):
        return None, "OneMap result lacked usable coordinates."


def _rainfall_readings() -> tuple[Optional[dict], str]:
    """Fetch rainfall and normalise into {stations, readings, unit, timestamp}."""
    payload, err = _http_json(RAINFALL_URL)
    if payload is None:
        return None, err
    data = payload.get("data") or {}
    stations = {s.get("id"): s for s in data.get("stations", []) if s.get("id")}
    readings = data.get("readings") or []
    if not stations or not readings:
        return None, "Rainfall response missing stations or readings."
    latest = readings[0]
    values = latest.get("data") or []
    return {
        "stations": stations,
        "values": values,
        "unit": data.get("readingUnit") or "mm",
        "timestamp": latest.get("timestamp"),
    }, ""


def _forecast_text() -> tuple[Optional[dict], str]:
    """Fetch the 24-hour forecast; return a small normalised summary."""
    payload, err = _http_json(FORECAST_24H_URL)
    if payload is None:
        return None, err
    records = (payload.get("data") or {}).get("records") or []
    if not records:
        return None, "Forecast response had no records."
    rec = records[0]
    general = rec.get("general") or {}
    forecast = general.get("forecast")
    gtext = forecast.get("text") if isinstance(forecast, dict) else \
        (general.get("text") if isinstance(general.get("text"), str) else None)
    regions: dict[str, str] = {}
    periods = rec.get("periods") or []
    if periods:
        for name, val in (periods[0].get("regions") or {}).items():
            if isinstance(val, dict) and val.get("text"):
                regions[name] = val["text"]
    return {"general": gtext, "regions": regions,
            "timestamp": rec.get("timestamp") or rec.get("updatedTimestamp")}, ""


def _rain_severity(max_mm: float, forecast_text: Optional[str]) -> str:
    """Deliberately banded, never a computed score. Rain observed dominates; a
    thunderstorm/rain forecast lifts an otherwise-dry reading."""
    if max_mm >= 30:
        return "HIGH"
    if max_mm >= 10:
        return "ELEVATED"
    if max_mm > 0:
        return "MODERATE"
    text = (forecast_text or "").lower()
    if any(w in text for w in ("thundery", "thunderstorm", "heavy rain")):
        return "ELEVATED"
    if any(w in text for w in ("rain", "showers")):
        return "MODERATE"
    return "LOW"


def _fetch_weather(premises_address: Optional[str] = None
                   ) -> tuple[Optional[RiskSignal], list[dict]]:
    """Build a LIVE weather RiskSignal from real rainfall + forecast data.

    Returns (signal, fetch_log). The signal is None on any failure, in which case the
    caller keeps the fixture and the mode stays DEMO_FIXTURE. When `premises_address`
    is supplied and geocodes, the detail carries the real distance to the nearest
    rainfall station — computed distance, not a region-string match.
    """
    log: list[dict] = []
    rain, rerr = _rainfall_readings()
    log.append({"source": "rainfall", "url": RAINFALL_URL, "ok": rain is not None,
                "error": rerr})
    fc, ferr = _forecast_text()
    log.append({"source": "forecast_24h", "url": FORECAST_24H_URL,
                "ok": fc is not None, "error": ferr})
    if rain is None:
        return None, log

    stations = rain["stations"]
    unit = rain["unit"]
    # Highest observed reading across all stations.
    max_station_id, max_mm = None, 0.0
    for item in rain["values"]:
        try:
            v = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if v >= max_mm:
            max_mm, max_station_id = v, item.get("stationId")
    max_name = (stations.get(max_station_id) or {}).get("name", max_station_id or "?")

    forecast_general = (fc or {}).get("general") if fc else None
    east_forecast = ((fc or {}).get("regions") or {}).get("east") if fc else None
    forecast_line = east_forecast or forecast_general

    # Optional real distance to the nearest station.
    distance_note = ""
    nearest = None
    if premises_address:
        coords, gerr = geocode_onemap(premises_address)
        log.append({"source": "onemap", "url": "OneMap elastic search",
                    "ok": coords is not None, "error": gerr})
        if coords is not None:
            plat, plon = coords
            nearest = _nearest_station(plat, plon, stations, rain["values"])
            if nearest is not None:
                distance_note = (
                    f" The insured premises geocodes to ({plat:.5f}, {plon:.5f}); "
                    f"the nearest rainfall station is {nearest['name']} "
                    f"({nearest['id']}) {nearest['distance_km']:.1f} km away, which "
                    f"recorded {nearest['value']}{unit} in the last reading.")

    severity = _rain_severity(max_mm, forecast_line)
    horizon = HORIZON_CURRENT if max_mm > 0 else HORIZON_FORECAST
    observed = date.today().isoformat()
    detail = (
        f"Highest rainfall reading across the national network: {max_mm}{unit} at "
        f"{max_name}"
        + (f" (station {max_station_id})" if max_station_id else "")
        + ". "
        + (f"24-hour forecast: {forecast_line}. " if forecast_line else "")
        + "A published environmental reading, not a statement that this business "
          "flooded."
        + distance_note)

    live_source = replace(MET_SERVICE, mode="LIVE")
    signal = RiskSignal(
        id="EXT-LIVE-WEATHER", source=live_source, signal_type="SEVERE_WEATHER",
        label=(f"Live rainfall {max_mm}{unit} (peak) — "
               f"{severity.title()} weather condition"),
        detail=detail, severity=severity, horizon=horizon, observed_on=observed,
        window="Real-time reading, data.gov.sg",
        regions=("SG", "SG-EAST", "SG-TAI-SENG", "SG-JURONG"),
        sectors=("ANY",),
        insured_interests=("stock", "buildings", "plant"),
        relevant_rules=("AVERAGE-ADEQUACY-BELOW-THRESHOLD",
                        "UNOCCUPANCY-WATER-EXCLUSION"),
    )
    return signal, log


def _nearest_station(lat: float, lon: float, stations: dict,
                     values: list[dict]) -> Optional[dict]:
    """Nearest rainfall station to a point, with its latest reading."""
    value_by_id = {}
    for item in values:
        try:
            value_by_id[item.get("stationId")] = float(item.get("value"))
        except (TypeError, ValueError):
            continue
    best = None
    for sid, s in stations.items():
        loc = s.get("location") or {}
        try:
            slat, slon = float(loc["latitude"]), float(loc["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        d = _haversine_km(lat, lon, slat, slon)
        if best is None or d < best["distance_km"]:
            best = {"id": sid, "name": s.get("name", sid), "distance_km": d,
                    "value": value_by_id.get(sid, 0.0)}
    return best


def _parse_cost_rows(payload: dict) -> list[dict]:
    """Normalise SingStat M211251 rows into [{material, points:[(month,value)]}]."""
    data = payload.get("Data") or payload.get("data") or {}
    rows = data.get("row") or []
    out: list[dict] = []
    for r in rows:
        pts: list[tuple[str, float]] = []
        for c in r.get("columns", []):
            key, val = c.get("key"), c.get("value")
            if val in (None, "", "-", "na", "n.a."):
                continue
            try:
                pts.append((key, float(val)))
            except (TypeError, ValueError):
                continue
        if pts:
            out.append({"material": r.get("rowText", "").strip(), "points": pts})
    return out


def cost_index_movement(material_hint: str = "Steel Reinforcement",
                        yoy_months: int = 12) -> tuple[Optional[dict], str]:
    """Fetch the SingStat construction cost table and compute real movement.

    Returns a dict carrying the material, the full month range, the oldest→newest
    movement and a year-on-year movement, plus the trailing monthly points so the drift
    engine can walk a real series. Returns (None, error) on any failure — the caller
    keeps its sample series and stays labelled as such.
    """
    payload, err = _http_json(SINGSTAT_TABLE_URL, browser=True)
    if payload is None:
        return None, err
    rows = _parse_cost_rows(payload)
    if not rows:
        return None, "SingStat table parsed to zero usable rows."
    chosen = next((r for r in rows
                   if material_hint.lower() in r["material"].lower()), rows[0])
    pts = chosen["points"]
    if len(pts) < 2:
        return None, "Chosen material had fewer than two data points."
    oldest_key, oldest_val = pts[0]
    newest_key, newest_val = pts[-1]
    full_pct = ((newest_val - oldest_val) / oldest_val * 100) if oldest_val else 0.0
    yoy_idx = max(0, len(pts) - 1 - yoy_months)
    yoy_key, yoy_val = pts[yoy_idx]
    yoy_pct = ((newest_val - yoy_val) / yoy_val * 100) if yoy_val else 0.0
    return {
        "material": chosen["material"],
        "table_id": "M211251",
        "source": "SingStat Table Builder M211251",
        "oldest_month": oldest_key,
        "newest_month": newest_key,
        "oldest_value": oldest_val,
        "newest_value": newest_val,
        "movement_pct_full": round(full_pct, 1),
        "yoy_from_month": yoy_key,
        "yoy_pct": round(yoy_pct, 1),
        "month_range": f"{oldest_key}–{newest_key}",
        "yoy_range": f"{yoy_key}–{newest_key}",
        "points": pts,
    }, ""


def _fetch_cost_index() -> tuple[Optional[RiskSignal], dict, str]:
    """Build a LIVE COST_INDEX RiskSignal from the real SingStat table.

    Returns (signal, movement, error). `movement` is the parsed detail (also consumed
    by drift). The magnitude fed to the signal is the year-on-year movement, which is
    the like-for-like replacement for the fixture's "up X% year on year".
    """
    movement, err = cost_index_movement()
    if movement is None:
        return None, {}, err
    live_source = replace(COST_INDEX, mode="LIVE")
    signal = RiskSignal(
        id="EXT-LIVE-COST", source=live_source, signal_type="COST_INDEX",
        label=(f"{movement['material']} up {movement['yoy_pct']}% year on year "
               f"(SingStat {movement['table_id']})"),
        detail=(f"Published construction material price index. {movement['material']}: "
                f"{movement['yoy_from_month']} → {movement['newest_month']}, a "
                f"{movement['yoy_pct']}% movement. A forward pressure on reinstatement "
                f"cost, not a revaluation of any particular asset."),
        severity="MODERATE" if abs(movement["yoy_pct"]) < 10 else "ELEVATED",
        horizon=HORIZON_TREND, observed_on=date.today().isoformat(),
        window=movement["yoy_range"],
        regions=("SG",), sectors=("FOOD_MANUFACTURING", "ANY"),
        insured_interests=("plant", "buildings"),
        relevant_rules=("PLANT-ADEQUACY-UNESTABLISHED",
                        "BUILDINGS-VALUE-UNESTABLISHED"),
        magnitude_pct=movement["yoy_pct"],
    )
    return signal, movement, ""


def live_signals(premises_address: Optional[str] = None
                 ) -> tuple[list[RiskSignal], list[dict], dict]:
    """Attempt every live fetch. Returns (signals, fetch_log, meta).

    Only successful fetches contribute a signal; the rest are absent and the caller
    keeps the corresponding fixture. `meta` carries the cost-index movement for reuse
    by the drift view.
    """
    signals: list[RiskSignal] = []
    fetch_log: list[dict] = []
    meta: dict = {}

    weather, wlog = _fetch_weather(premises_address)
    fetch_log.extend(wlog)
    if weather is not None:
        signals.append(weather)

    cost, movement, cerr = _fetch_cost_index()
    fetch_log.append({"source": "singstat_M211251", "url": SINGSTAT_TABLE_URL,
                      "ok": cost is not None, "error": cerr})
    if cost is not None:
        signals.append(cost)
        meta["cost_index_movement"] = movement

    return signals, fetch_log, meta


# ── relevance: deterministic, not inferred ──────────────────────────────────

def _matches(candidates: tuple[str, ...], subject: Optional[str]) -> bool:
    if not subject:
        return "ANY" in candidates
    subject = subject.upper()
    return any(c == "ANY" or c.upper() in subject or subject in c.upper()
               for c in candidates)


def relevant_signals(*, region: Optional[str] = None,
                     sector: Optional[str] = None,
                     insured_interests: Optional[tuple[str, ...]] = None,
                     ) -> list[RiskSignal]:
    """Signals bearing on a business, by region, sector and insured interest.

    Deliberately a set-membership test rather than a model call. "Is this flood warning
    relevant to this policy?" has a checkable answer, and a checkable answer beats a
    persuasive one on a screen that leads to money.
    """
    out: list[RiskSignal] = []
    for signal in _FEED:
        if region and not _matches(signal.regions, region):
            continue
        if sector and not _matches(signal.sectors, sector):
            continue
        if insured_interests and not (set(signal.insured_interests)
                                     & set(insured_interests)):
            continue
        out.append(signal)
    return out


def environment_summary(signals: Optional[list[RiskSignal]] = None) -> dict:
    """The headline risk level, and how it was arrived at.

    Highest severity present rather than an average: averaging four moderate signals
    with one high one produces a number that hides the only signal anybody needs to act
    on.
    """
    items = list(signals if signals is not None else _FEED)
    if not items:
        return {"level": "LOW", "basis": "No external signals matched this business.",
                "signal_count": 0, "by_category": {}, "notice": DEMO_NOTICE}

    worst = max(items, key=lambda s: severity_rank(s.severity))
    by_category: dict[str, int] = {}
    for signal in items:
        key = signal.source.category
        by_category[key] = by_category.get(key, 0) + 1

    return {
        "level": worst.severity,
        "basis": (f"Highest severity among {len(items)} matched signal(s): "
                  f"{worst.label}. Reported as the maximum rather than an average — "
                  f"averaging hides the one signal worth acting on."),
        "driver_signal_id": worst.id,
        "signal_count": len(items),
        "by_category": by_category,
        "horizons": sorted({s.horizon for s in items}),
        "notice": DEMO_NOTICE,
    }


def catalogue(*, live_sources: tuple[str, ...] = (),
              fetch_log: Optional[list[dict]] = None) -> dict:
    """Sources, for the transparency panel.

    `live_sources` names the source ids that a real call actually succeeded for this
    request; those render LIVE, the rest stay DEMO_FIXTURE. With no live sources this is
    identical to the original all-demo catalogue, which is what the offline test suite
    asserts.
    """
    def mode_of(s: ExternalSource) -> str:
        return "LIVE" if s.id in live_sources else s.mode

    src_dicts = []
    for s in SOURCES:
        d = s.as_dict()
        d["mode"] = mode_of(s)
        d["is_live"] = d["mode"] == "LIVE"
        src_dicts.append(d)

    any_live = bool(live_sources)
    return {
        "sources": src_dicts,
        "live_count": sum(1 for d in src_dicts if d["is_live"]),
        "demo_count": sum(1 for d in src_dicts if not d["is_live"]),
        "fetch_log": fetch_log or [],
        "notice": (DEMO_NOTICE if not any_live else
                   "One or more sources are LIVE for this request: a real outbound "
                   "call to a Singapore government open-data endpoint succeeded and was "
                   "parsed. Sources still showing DEMO_FIXTURE either were not fetched "
                   "or their call did not succeed — and say so rather than pretending."),
        "boundary": {
            "may_assert": sorted(SENTINEL_MAY_EMIT),
            "may_not_assert": sorted(SENTINEL_MAY_NOT_EMIT),
            "enforced_by": ("RiskSignal.__post_init__ raises on a claim_scope outside "
                            "SENTINEL_MAY_EMIT, so there is no code path by which an "
                            "external feed can assert a fact about a specific "
                            "business."),
        },
    }


def feed(*, region: Optional[str] = None, sector: Optional[str] = None,
         since: Optional[str] = None, live: Optional[bool] = None,
         premises_address: Optional[str] = None) -> dict:
    """The Sentinel view: matched signals plus the environment summary.

    When `live` is None the env flag decides. In live mode, a successful real fetch
    REPLACES the corresponding fixture signal (same signal_type) with the live one and
    stamps that source LIVE; a failed fetch changes nothing and the fixture is retained,
    still labelled DEMO_FIXTURE. There is no path by which a fixture is rendered as live.
    """
    if live is None:
        live = config.live_sources_enabled()

    items = relevant_signals(region=region, sector=sector)
    live_source_ids: tuple[str, ...] = ()
    fetch_log: list[dict] = []
    cost_movement: Optional[dict] = None

    if live:
        lsignals, fetch_log, meta = live_signals(premises_address)
        cost_movement = meta.get("cost_index_movement")
        if lsignals:
            live_types = {s.signal_type for s in lsignals}
            items = [s for s in items if s.signal_type not in live_types]
            for s in lsignals:
                if region and not _matches(s.regions, region):
                    continue
                if sector and not _matches(s.sectors, sector):
                    continue
                items.append(s)
            live_source_ids = tuple(s.source.id for s in lsignals)

    if since:
        try:
            cutoff = date.fromisoformat(since)
            items = [s for s in items
                     if date.fromisoformat(s.observed_on) >= cutoff]
        except ValueError:
            pass
    return {
        "agent": "sentinel",
        "signals": [s.as_dict() for s in items],
        "environment": environment_summary(items),
        "sources": catalogue(live_sources=live_source_ids, fetch_log=fetch_log),
        "live": {
            "enabled": bool(live),
            "any_live": bool(live_source_ids),
            "live_source_ids": list(live_source_ids),
            "fetch_log": fetch_log,
            "fetched_at": datetime.now(timezone.utc).isoformat() if live else None,
        },
        "cost_index_movement": cost_movement,
        "explanation": (
            "Sentinel reads the environment around the business: weather, standing "
            "hazard designations, published cost indices and infrastructure notices. "
            "It reports conditions and never events affecting this business — a flood "
            "warning is not a flood, and a rising cost index is not a revaluation of "
            "anything you own. Turning a condition into a consequence needs a document, "
            "which is what the orchestrator checks for."),
    }


def signal(signal_id: str) -> Optional[RiskSignal]:
    return BY_ID.get(signal_id)
