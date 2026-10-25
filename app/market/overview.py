"""Per-model overview for the Models pages: live numbers from the active cars (supply by
country, valued and positive share, medians, best car, churn), NL comparables, deals and
accuracy, plus the nightly history for trends. Everything is computed on request from a
handful of column queries; nothing is stored."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Comparable, Deal, DealStage, Listing, ModelHistory, Watch
from app.market.history import model_series, sparkline

COUNTRIES = ("DE", "BE", "IT", "SE")


def _med(values: list[int]) -> int | None:
    return round(median(values)) if values else None


def _pct(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def _quantile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


@dataclass
class Car:
    id: int
    make: str
    model: str
    variant: str | None
    year: int | None
    mileage_km: int | None
    country: str
    city: str | None
    price_eur: int
    landed_eur: int | None
    value_eur: int | None
    spread_eur: int | None
    first_seen: datetime
    photo: str | None


@dataclass
class ModelStats:
    key: str
    label: str
    make: str | None = None
    years: list[int] = field(default_factory=list)
    motors: list[str] = field(default_factory=list)
    bodies: list[str] = field(default_factory=list)
    cars: list[Car] = field(default_factory=list)
    n_active: int = 0
    n_valued: int = 0
    n_positive: int = 0
    positive_pct: int | None = None
    by_country: dict[str, int] = field(default_factory=dict)
    per_country: dict[str, dict[str, Any]] = field(default_factory=dict)
    median_price_eur: int | None = None
    median_landed_eur: int | None = None
    median_value_eur: int | None = None
    median_spread_eur: int | None = None
    p75_spread_eur: int | None = None
    best: Car | None = None
    new_7d: int = 0
    gone_7d: int = 0
    n_comparables: int = 0
    comp_median_price_eur: int | None = None
    n_deals: int = 0
    n_sold: int = 0
    n_favorites: int = 0
    accuracy: dict[str, Any] | None = None
    history: list[ModelHistory] = field(default_factory=list)
    spread_change_7d: int | None = None
    supply_change_7d: int | None = None
    spread_points: str = ""
    positive_points: str = ""
    supply_points: str = ""

    @property
    def tracked(self) -> bool:
        return bool(self.years)


def _labels() -> dict[str, dict[str, Any]]:
    from app.watchlist import load_models

    try:
        return {m["key"]: m for m in load_models()}
    except FileNotFoundError:
        return {}


def _cars(session: Session, key: str | None = None) -> dict[str, list[Car]]:
    stmt = select(
        Listing.id,
        Listing.model_key,
        Listing.make,
        Listing.model,
        Listing.variant,
        Listing.first_registration,
        Listing.mileage_km,
        Listing.country,
        Listing.city,
        Listing.price_eur,
        Listing.landed_cost_eur,
        Listing.nl_value_eur,
        Listing.spread_eur,
        Listing.first_seen,
        Listing.photos[0].astext,
    ).where(
        Listing.is_active.is_(True),
        Listing.duplicate_of_id.is_(None),
        Listing.model_key.isnot(None),
    )
    if key:
        stmt = stmt.where(Listing.model_key == key)
    out: dict[str, list[Car]] = defaultdict(list)
    for row in session.execute(stmt):
        (i, mk, make, model, var, reg, km, cc, city, price, landed, val, spr, seen, photo) = row
        out[mk].append(
            Car(
                i,
                make,
                model,
                var,
                reg.year if reg else None,
                km,
                cc,
                city,
                price,
                landed,
                val,
                spr,
                seen,
                photo,
            )  # fmt: skip
        )
    return out


def _churn(session: Session, now: datetime, key: str | None) -> tuple[Counter, Counter]:
    week = now - timedelta(days=7)
    base = [Listing.model_key.isnot(None), Listing.duplicate_of_id.is_(None)]
    if key:
        base.append(Listing.model_key == key)
    new = Counter(
        dict(
            session.execute(
                select(Listing.model_key, func.count())
                .where(*base, Listing.is_active.is_(True), Listing.first_seen >= week)
                .group_by(Listing.model_key)
            ).all()
        )
    )
    gone = Counter(
        dict(
            session.execute(
                select(Listing.model_key, func.count())
                .where(*base, Listing.is_active.is_(False), Listing.last_seen >= week)
                .group_by(Listing.model_key)
            ).all()
        )
    )
    return new, gone


def _comps(session: Session, key: str | None) -> dict[str, list[int]]:
    stmt = select(Comparable.model_key, Comparable.price_eur).where(
        Comparable.is_active.is_(True), Comparable.model_key.isnot(None)
    )
    if key:
        stmt = stmt.where(Comparable.model_key == key)
    out: dict[str, list[int]] = defaultdict(list)
    for mk, price in session.execute(stmt):
        out[mk].append(price)
    return out


def _deals(session: Session, key: str | None) -> dict[str, list[Deal]]:
    stmt = select(Deal).options(selectinload(Deal.listing), selectinload(Deal.owner))
    deals = list(session.scalars(stmt))
    out: dict[str, list[Deal]] = defaultdict(list)
    for d in deals:
        mk = d.listing.model_key
        if mk and (key is None or mk == key):
            out[mk].append(d)
    return out


def _favorites(session: Session, key: str | None) -> Counter:
    stmt = (
        select(Listing.model_key, func.count(func.distinct(Watch.listing_id)))
        .join(Listing, Listing.id == Watch.listing_id)
        .where(Listing.model_key.isnot(None))
        .group_by(Listing.model_key)
    )
    if key:
        stmt = stmt.where(Listing.model_key == key)
    return Counter(dict(session.execute(stmt).all()))


def _fill(stats: ModelStats, now: datetime) -> None:
    cars = stats.cars
    spreads = [c.spread_eur for c in cars if c.spread_eur is not None]
    stats.n_active = len(cars)
    stats.n_valued = sum(1 for c in cars if c.value_eur is not None)
    stats.n_positive = sum(1 for s in spreads if s > 0)
    stats.positive_pct = _pct(stats.n_positive, stats.n_valued)
    stats.by_country = {cc: sum(1 for c in cars if c.country == cc) for cc in COUNTRIES}
    stats.per_country = {}
    for cc in COUNTRIES:
        mine = [c for c in cars if c.country == cc]
        if not mine:
            continue
        sp = [c.spread_eur for c in mine if c.spread_eur is not None]
        stats.per_country[cc] = {
            "n": len(mine),
            "n_valued": len(sp),
            "n_positive": sum(1 for x in sp if x > 0),
            "positive_pct": _pct(sum(1 for x in sp if x > 0), len(sp)),
            "median_price_eur": _med([c.price_eur for c in mine]),
            "median_landed_eur": _med([c.landed_eur for c in mine if c.landed_eur is not None]),
            "median_value_eur": _med([c.value_eur for c in mine if c.value_eur is not None]),
            "median_spread_eur": _med(sp),
            "best_spread_eur": max(sp) if sp else None,
        }
    stats.median_price_eur = _med([c.price_eur for c in cars])
    stats.median_landed_eur = _med([c.landed_eur for c in cars if c.landed_eur is not None])
    stats.median_value_eur = _med([c.value_eur for c in cars if c.value_eur is not None])
    stats.median_spread_eur = _med(spreads)
    stats.p75_spread_eur = _quantile(spreads, 0.75)
    valued = [c for c in cars if c.spread_eur is not None]
    stats.best = max(valued, key=lambda c: c.spread_eur or 0) if valued else None
    hist = stats.history
    if hist:
        week_ago = [h for h in hist if h.day <= (now - timedelta(days=7)).date()]
        ref = week_ago[-1] if week_ago else hist[0]
        latest = hist[-1]
        if ref is not latest:
            if ref.median_spread_eur is not None and latest.median_spread_eur is not None:
                stats.spread_change_7d = latest.median_spread_eur - ref.median_spread_eur
            stats.supply_change_7d = latest.n_active - ref.n_active
        stats.spread_points = sparkline([h.median_spread_eur for h in hist])
        stats.positive_points = sparkline(
            [_pct(h.n_positive, h.n_valued) for h in hist]  # type: ignore[misc]
        )
        stats.supply_points = sparkline([h.n_active for h in hist])


def model_overview(session: Session, now: datetime, days: int = 30) -> list[ModelStats]:
    """One ModelStats per watchlist model (plus any key with active cars), unsorted."""
    from app.deals.calibration import model_accuracy

    meta = _labels()
    cars = _cars(session)
    new, gone = _churn(session, now, None)
    comps = _comps(session, None)
    deals = _deals(session, None)
    favs = _favorites(session, None)
    acc = model_accuracy(session)
    series = model_series(session, days)
    out = []
    for key in sorted(set(meta) | set(cars)):
        m = meta.get(key, {})
        st = ModelStats(
            key=key,
            label=m.get("label", key),
            make=m.get("make"),
            years=list(m.get("years") or []),
            motors=list(m.get("motors") or []),
            bodies=list(m.get("bodies") or []),
            cars=cars.get(key, []),
            new_7d=int(new.get(key, 0)),
            gone_7d=int(gone.get(key, 0)),
            n_comparables=len(comps.get(key, [])),
            comp_median_price_eur=_med(comps.get(key, [])),
            n_deals=len(deals.get(key, [])),
            n_sold=sum(1 for d in deals.get(key, []) if d.stage is DealStage.sold),
            n_favorites=int(favs.get(key, 0)),
            accuracy=acc.get(key),
            history=series.get(key, []),
        )
        _fill(st, now)
        out.append(st)
    return out


def overview_totals(models: list[ModelStats]) -> dict[str, Any]:
    valued = [m for m in models if m.median_spread_eur is not None]
    best = max(valued, key=lambda m: m.median_spread_eur or 0) if valued else None
    return {
        "models": len(models),
        "cars": sum(m.n_active for m in models),
        "positive": sum(m.n_positive for m in models),
        "valued": sum(m.n_valued for m in models),
        "comparables": sum(m.n_comparables for m in models),
        "new_7d": sum(m.new_7d for m in models),
        "gone_7d": sum(m.gone_7d for m in models),
        "best": best,
    }


def buckets(
    values: list[int], step: int, lo: int | None = None, hi: int | None = None
) -> list[dict[str, Any]]:
    """Histogram bars: [{lo, hi, n, pct}] with `pct` relative to the tallest bar."""
    if not values:
        return []
    lo = (min(values) // step) * step if lo is None else lo
    hi = ((max(values) // step) + 1) * step if hi is None else hi
    counts: Counter[int] = Counter()
    for v in values:
        b = min(max(lo, (v // step) * step), hi - step)
        counts[b] += 1
    top = max(counts.values())
    return [
        {"lo": b, "hi": b + step, "n": counts.get(b, 0), "pct": round(100 * counts.get(b, 0) / top)}
        for b in range(lo, hi, step)
    ]


def _step(values: list[int], target_bars: int = 10) -> int:
    span = (max(values) - min(values)) or 1
    raw = span / target_bars
    for nice in (100, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000):
        if nice >= raw:
            return nice
    return 50000


def model_detail(session: Session, key: str, now: datetime) -> dict[str, Any] | None:
    """Everything the detail page shows for one model, or None when the key is unknown."""
    from app.deals.calibration import model_accuracy

    meta = _labels()
    cars = _cars(session, key).get(key, [])
    if key not in meta and not cars:
        return None
    m = meta.get(key, {})
    new, gone = _churn(session, now, key)
    comps = _comps(session, key).get(key, [])
    deals = _deals(session, key).get(key, [])
    st = ModelStats(
        key=key,
        label=m.get("label", key),
        make=m.get("make"),
        years=list(m.get("years") or []),
        motors=list(m.get("motors") or []),
        bodies=list(m.get("bodies") or []),
        cars=cars,
        new_7d=int(new.get(key, 0)),
        gone_7d=int(gone.get(key, 0)),
        n_comparables=len(comps),
        comp_median_price_eur=_med(comps),
        n_deals=len(deals),
        n_sold=sum(1 for d in deals if d.stage is DealStage.sold),
        n_favorites=int(_favorites(session, key).get(key, 0)),
        accuracy=model_accuracy(session).get(key),
        history=model_series(session, 90).get(key, []),
    )
    _fill(st, now)
    prices = [c.price_eur for c in cars]
    spreads = [c.spread_eur for c in cars if c.spread_eur is not None]
    years = Counter(c.year for c in cars if c.year)
    mileages = [c.mileage_km for c in cars if c.mileage_km]
    top = sorted((c for c in cars if c.spread_eur is not None), key=lambda c: -(c.spread_eur or 0))
    deals.sort(key=lambda d: d.created_at, reverse=True)
    return {
        "stats": st,
        "price_hist": buckets(prices, _step(prices)) if prices else [],
        "spread_hist": buckets(spreads, _step(spreads)) if spreads else [],
        "year_hist": [
            {"year": y, "n": years[y], "pct": round(100 * years[y] / max(years.values()))}
            for y in sorted(years)
        ],
        "mileage_hist": buckets(mileages, 25_000, 0) if mileages else [],
        "median_km": _med(mileages),
        "top": top[:10],
        "newest": sorted(cars, key=lambda c: c.first_seen, reverse=True)[:6],
        "deals": deals[:10],
        "comp_quartiles": (
            (_quantile(comps, 0.25), _med(comps), _quantile(comps, 0.75)) if comps else None
        ),
    }
