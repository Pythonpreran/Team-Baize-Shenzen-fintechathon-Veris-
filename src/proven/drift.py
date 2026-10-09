"""The drift clock — adequacy decaying while nothing about the policy changes.

WHY THIS EXISTS
---------------
Every finding elsewhere in the engine is triggered by something the business did: it
took a unit, hired staff, paid late. This one fires when the business does NOTHING.

Marsh Singapore makes the argument in its own words: an asset fully insured at the
start of a policy period becomes partially insured as costs move, and the average
clause then engages without anything about the policy having changed.
(marsh.com/sg, "How the Average Clause affects your business insurance coverage" —
rephrased for licensing compliance.)

So: hold the sum insured constant, move a published cost index, and report the date
adequacy crossed the carrier's own threshold. The insured did nothing wrong. They are
now under-insured, and no notification exists today that would tell them.

WHAT IS AND IS NOT CLAIMED
--------------------------
  * The index series is DECLARED SAMPLE DATA in this build and labelled as such on
    every response. Wiring a real published series (BCA tender price index, SingStat
    manufacturing equipment prices) is an integration, not a redesign — the interface
    below is what a real series would populate.
  * The threshold comes from the carrier's own aligned clause. There is no market
    default: Chubb engages below 85%, MSIG from the first dollar. Using one number for
    both would be wrong in one direction or the other.
  * The crossing date is arithmetic on the series, not a forecast. We never project
    forward past the last observation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

# Basis labels that travel with every response, so a reader always knows whether the
# series behind a crossing date is real.
INDEX_SAMPLE = "DECLARED_SAMPLE_SERIES"
INDEX_PUBLISHED = "PUBLISHED_SERIES"


@dataclass(frozen=True)
class IndexPoint:
    as_at: date
    value: Decimal


@dataclass
class CostIndex:
    """A cost series against which a fixed sum insured is measured.

    `basis` is deliberately part of the data rather than a footnote: a crossing date
    computed from sample numbers must not be presentable as one computed from a
    published series.
    """

    name: str
    unit: str
    basis: str
    source_note: str
    points: tuple[IndexPoint, ...]

    def value_at(self, when: date) -> Optional[Decimal]:
        best = None
        for p in self.points:
            if p.as_at <= when:
                best = p
        return best.value if best else None

    @property
    def first(self) -> IndexPoint:
        return self.points[0]

    @property
    def last(self) -> IndexPoint:
        return self.points[-1]


# Sample series. Shaped like the real thing — quarterly observations, index rebased to
# 100 at the policy inception — so that substituting a published series changes the
# numbers and nothing else. Labelled DECLARED_SAMPLE_SERIES everywhere it surfaces.
SAMPLE_CONSTRUCTION_INDEX = CostIndex(
    name="Building reinstatement cost index (sample)",
    unit="index, 100 = 2026-03-01",
    basis=INDEX_SAMPLE,
    source_note=(
        "DECLARED SAMPLE DATA, shaped after a quarterly published tender-price "
        "series. Not a real index. Substituting a published series such as the BCA "
        "tender price index changes these figures and nothing else in the method."),
    points=(
        IndexPoint(date(2026, 3, 1), Decimal("100.0")),
        IndexPoint(date(2026, 6, 1), Decimal("104.2")),
        IndexPoint(date(2026, 9, 1), Decimal("109.6")),
        IndexPoint(date(2026, 12, 1), Decimal("115.1")),
        IndexPoint(date(2027, 3, 1), Decimal("119.4")),
    ),
)

SAMPLE_EQUIPMENT_INDEX = CostIndex(
    name="Manufacturing equipment cost index (sample)",
    unit="index, 100 = 2026-03-01",
    basis=INDEX_SAMPLE,
    source_note=(
        "DECLARED SAMPLE DATA. Marsh Singapore reports chemical manufacturing "
        "machinery costs in Korea rising 27% within a year, which is the order of "
        "movement this series is shaped to illustrate."),
    points=(
        IndexPoint(date(2026, 3, 1), Decimal("100.0")),
        IndexPoint(date(2026, 6, 1), Decimal("106.5")),
        IndexPoint(date(2026, 9, 1), Decimal("113.8")),
        IndexPoint(date(2026, 12, 1), Decimal("121.0")),
        IndexPoint(date(2027, 3, 1), Decimal("127.2")),
    ),
)

INDICES = {
    "construction": SAMPLE_CONSTRUCTION_INDEX,
    "equipment": SAMPLE_EQUIPMENT_INDEX,
}

# Material hints per index key, so a live SingStat pull picks the right row.
_LIVE_MATERIAL_HINT = {
    "construction": "Steel Reinforcement",
    "equipment": "Steel Reinforcement",   # M211251 is a construction table; reuse it
}

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
     "Dec"], start=1)}


def _parse_singstat_month(key: str) -> Optional[date]:
    """'2026 Jun' -> date(2026, 6, 1). Returns None on anything unexpected."""
    try:
        year_s, mon_s = key.split()
        return date(int(year_s), _MONTHS[mon_s[:3].title()], 1)
    except (ValueError, KeyError):
        return None


def build_live_index(movement: dict, *, window_months: int = 24) -> Optional[CostIndex]:
    """Construct a real CostIndex from a SingStat movement payload.

    Rebased to 100 at the window start so the drift arithmetic is IDENTICAL to the
    sample series — only the numbers behind it change from declared to published. The
    trailing `window_months` are used so the crossing reflects recent movement rather
    than a multi-decade drift.
    """
    pts = movement.get("points") or []
    parsed = [(d, v) for k, v in pts if (d := _parse_singstat_month(k)) is not None]
    if len(parsed) < 2:
        return None
    window = parsed[-window_months:] if len(parsed) > window_months else parsed
    base = window[0][1]
    if base <= 0:
        return None
    from decimal import Decimal as D
    points = tuple(
        IndexPoint(d, (D(str(v)) / D(str(base)) * D("100")).quantize(D("0.1")))
        for d, v in window)
    material = movement.get("material", "construction material")
    table_id = movement.get("table_id", "M211251")
    return CostIndex(
        name=f"{material} price index (SingStat {table_id})",
        unit=f"index, 100 = {window[0][0].isoformat()}",
        basis=INDEX_PUBLISHED,
        source_note=(
            f"PUBLISHED SERIES. SingStat Table Builder {table_id}, {material}, "
            f"{window[0][0].isoformat()} to {window[-1][0].isoformat()}. Real monthly "
            f"observations, rebased to 100 at the window start. Fetched live."),
        points=points,
    )


@dataclass
class DriftObservation:
    as_at: str
    index_value: str
    implied_value_at_risk: str
    adequacy_ratio: str
    engaged: bool

    def as_dict(self) -> dict:
        return {
            "as_at": self.as_at,
            "index_value": self.index_value,
            "implied_value_at_risk": self.implied_value_at_risk,
            "adequacy_ratio": self.adequacy_ratio,
            "engaged": self.engaged,
        }


@dataclass
class DriftResult:
    item: str
    sum_insured: str
    threshold_pct: Optional[str]
    threshold_basis: str
    index_name: str
    index_basis: str
    source_note: str
    observations: list[DriftObservation] = field(default_factory=list)
    crossed_on: Optional[str] = None
    headline: str = ""
    caveat: str = ""

    def as_dict(self) -> dict:
        return {
            "item": self.item,
            "sum_insured": self.sum_insured,
            "threshold_pct": self.threshold_pct,
            "threshold_basis": self.threshold_basis,
            "index_name": self.index_name,
            "index_basis": self.index_basis,
            "source_note": self.source_note,
            "observations": [o.as_dict() for o in self.observations],
            "crossed_on": self.crossed_on,
            "headline": self.headline,
            "caveat": self.caveat,
        }


def compute_drift(
    item: str,
    sum_insured: Decimal,
    value_at_inception: Decimal,
    threshold_pct: Optional[Decimal],
    index: CostIndex,
) -> DriftResult:
    """Walk the index forward with the sum insured held constant.

    `threshold_pct` of None means the carrier's clause states no threshold, so any
    shortfall engages it. That is MSIG's position and it must not be defaulted to
    Chubb's 85% — the whole point of reading each wording is that this number differs.
    """
    base = index.first.value
    result = DriftResult(
        item=item,
        sum_insured=str(sum_insured),
        threshold_pct=(None if threshold_pct is None
                       else f"{(threshold_pct * 100):.1f}%"),
        threshold_basis=("NO_THRESHOLD_STATED" if threshold_pct is None
                         else "THRESHOLD_STATED"),
        index_name=index.name,
        index_basis=index.basis,
        source_note=index.source_note,
    )

    for point in index.points:
        implied = (value_at_inception * point.value / base).quantize(Decimal("1"))
        ratio = (sum_insured / implied) if implied > 0 else Decimal("0")
        engaged = (ratio < Decimal("1") if threshold_pct is None
                   else ratio < threshold_pct)
        result.observations.append(DriftObservation(
            as_at=point.as_at.isoformat(),
            index_value=str(point.value),
            implied_value_at_risk=str(implied),
            adequacy_ratio=f"{(ratio * 100):.1f}%",
            engaged=engaged,
        ))
        if engaged and result.crossed_on is None:
            result.crossed_on = point.as_at.isoformat()

    if result.crossed_on:
        trigger = ("any shortfall at all" if threshold_pct is None
                   else f"the {(threshold_pct * 100):.0f}% threshold")
        result.headline = (
            f"{item}: the sum insured has not changed, and on {result.crossed_on} "
            f"adequacy crossed {trigger}. Nothing was done wrong — the cost of the "
            f"asset moved and the policy did not.")
    else:
        result.headline = (
            f"{item}: adequacy stayed above the carrier's trigger across the whole "
            f"series. No drift crossing on this index.")

    result.caveat = (
        "The index behind this is " +
        ("DECLARED SAMPLE DATA and not a real published series. "
         if index.basis == INDEX_SAMPLE else "a published series. ") +
        "The threshold is read from this carrier's own aligned clause, never from a "
        "market default. No value beyond the last observation is projected.")
    return result


def drift_for_case(facts=None, carrier: str = "CHUBB",
                   index_key: str = "construction",
                   live: Optional[bool] = None) -> dict:
    """Build the drift view for a live case.

    Uses the plant sum insured where the bundle establishes one, because plant is the
    item whose replacement cost moves with a published equipment series. Falls back to
    stock so the view is never empty in a demo, and says which it used.

    When live sources are enabled and the SingStat pull succeeds, the index is a REAL
    published series and the arithmetic is unchanged — only the input moves from
    declared-sample to published. On any failure it falls back to the labelled sample
    series and says so.
    """
    from . import config
    from .taxonomy import Taxonomy

    if live is None:
        live = config.live_sources_enabled()

    index = INDICES.get(index_key, SAMPLE_CONSTRUCTION_INDEX)
    live_source_meta: Optional[dict] = None
    live_error: Optional[str] = None
    if live:
        from . import sentinel
        movement, err = sentinel.cost_index_movement(
            material_hint=_LIVE_MATERIAL_HINT.get(index_key, "Steel Reinforcement"))
        if movement is not None:
            built = build_live_index(movement)
            if built is not None:
                index = built
                live_source_meta = {
                    "material": movement["material"],
                    "table_id": movement["table_id"],
                    "source": movement["source"],
                    "month_range": movement["month_range"],
                    "movement_pct_full": movement["movement_pct_full"],
                    "yoy_pct": movement["yoy_pct"],
                    "yoy_range": movement["yoy_range"],
                    "presentation": (
                        f"{movement['material']} · SingStat {movement['table_id']} · "
                        f"{movement['yoy_range']}"),
                }
            else:
                live_error = "SingStat series could not be rebased into an index."
        else:
            live_error = err

    tax = Taxonomy.load()
    threshold = tax.average_threshold(carrier)

    item, sum_insured, value_now = "Plant, machinery and equipment", None, None
    if facts is not None:
        for name, label in (("plant_sum_insured", "Plant, machinery and equipment"),
                            ("stock_sum_insured", "Stock in trade")):
            f = facts.get(name)
            if f is not None and getattr(f, "established", False):
                try:
                    sum_insured = Decimal(str(f.value))
                    item = label
                    break
                except Exception:
                    continue
    if sum_insured is None:
        return {
            "available": False,
            "reason": ("No sum insured was established for any item, so there is "
                       "nothing to measure drift against. Upload a policy schedule."),
        }

    # At inception the asset is assumed fully insured — the honest starting point for a
    # drift argument, and the one Marsh describes. The claim is about what MOVEMENT
    # does, not about the adequacy of the original figure.
    value_now = sum_insured
    res = compute_drift(item, sum_insured, value_now, threshold, index)
    out = res.as_dict()
    out["available"] = True
    out["carrier"] = carrier
    out["live"] = {
        "enabled": bool(live),
        "is_live": live_source_meta is not None,
        "source": live_source_meta,
        "error": live_error,
    }
    if live_source_meta is not None:
        out["source_presentation"] = live_source_meta["presentation"]
    out["assumption"] = (
        "The asset is taken to be fully insured at inception, so the movement is the "
        "only variable. This isolates drift from any pre-existing shortfall, which is "
        "reported separately by the adequacy finding.")
    return out
