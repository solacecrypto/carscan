"""Popularity grade: how crowded the NL market is for a car and how fast it moves.

Inputs, all from the comparables we already collect:
- supply: active NL ads for the model, and for the same engine (+ gearbox)
- ad age: Marktplaats sends the listing date, so the median age of the ads on sale today
- demand: AutoScout24.nl's enquiry band per ad (Zero / Some / Many / VeryMany)
- sell-through: how many days our tracked ads stayed up before they disappeared
  (only used once there are enough disappeared ads; withdrawn counts as sold)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Comparable, Listing
from app.valuation.engine import engine_token

MIN_GONE_SAMPLES = 20
DEMAND_LEVEL = {"Zero": 0, "Some": 1, "Many": 2, "VeryMany": 3}


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    value: str
    score: int | None  # 0-100, None = unknown
    note: str = ""


def _interp(x: float, points: list[tuple[float, float]]) -> int:
    """Piecewise-linear lookup, clamped to the first/last point."""
    if x <= points[0][0]:
        return round(points[0][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x <= x1:
            return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return round(points[-1][1])


def _grade(score: int) -> str:
    return (
        "A"
        if score >= 80
        else "B"
        if score >= 65
        else "C"
        if score >= 50
        else "D"
        if score >= 35
        else "E"
    )


def compute(
    comps: list[Comparable], engine: str | None, gearbox: str | None, today: date
) -> dict[str, Any] | None:
    """Grade the market for one model from its comparables (active and gone)."""
    active = [c for c in comps if c.is_active]
    if not active:
        return None
    close = [
        c
        for c in active
        if engine
        and engine_token(c.variant, c.model) == engine
        and (not gearbox or not c.gearbox or c.gearbox == gearbox)
    ]
    signals: list[Signal] = []

    n_close = len(close) if engine else len(active)
    signals.append(
        Signal(
            "supply",
            "Supply",
            f"{len(active)} for sale in NL" + (f", {len(close)} same engine" if engine else ""),
            _interp(n_close, [(5, 100), (20, 60), (60, 30), (150, 10)]),
            "fewer rivals = easier sale",
        )
    )

    pool = close if len(close) >= 8 else active
    ages = [(today - c.listed_at).days for c in pool if c.listed_at]
    if len(ages) >= 5:
        med_age = median(ages)
        stale = sum(a > 60 for a in ages) / len(ages)
        signals.append(
            Signal(
                "age",
                "Age of ads on sale",
                f"median {med_age:.0f} days, {stale:.0%} older than 60 days",
                _interp(med_age, [(14, 100), (30, 70), (60, 40), (120, 10)]),
                f"Marktplaats dates, {len(ages)} ads",
            )
        )
    else:
        signals.append(Signal("age", "Age of ads on sale", "not enough dated ads", None))

    levels = [DEMAND_LEVEL[c.demand] for c in pool if c.demand in DEMAND_LEVEL]
    if len(levels) >= 5:
        some = sum(v >= 1 for v in levels) / len(levels)
        many = sum(v >= 2 for v in levels) / len(levels)
        signals.append(
            Signal(
                "demand",
                "Buyer enquiries",
                f"{some:.0%} of ads get enquiries, {many:.0%} get many",
                min(100, round(100 * (some * 0.7 + many * 1.5))),
                f"AutoScout24.nl, {len(levels)} ads",
            )
        )
    else:
        signals.append(Signal("demand", "Buyer enquiries", "not enough data", None))

    gone = [
        (c.last_seen - c.first_seen).days
        for c in comps
        if not c.is_active and c.first_seen and c.last_seen
    ]
    if len(gone) >= MIN_GONE_SAMPLES:
        med_gone = median(gone)
        signals.append(
            Signal(
                "sell_through",
                "Days until an ad disappears",
                f"median {med_gone:.0f} days ({len(gone)} ads)",
                _interp(med_gone, [(10, 100), (30, 60), (60, 30), (120, 10)]),
                "our own tracking; withdrawn counts as sold",
            )
        )
    else:
        signals.append(
            Signal(
                "sell_through",
                "Days until an ad disappears",
                f"collecting ({len(gone)}/{MIN_GONE_SAMPLES} ads so far)",
                None,
            )
        )

    weights = {"supply": 0.3, "age": 0.35, "demand": 0.35, "sell_through": 0.3}
    known = [(s, weights[s.key]) for s in signals if s.score is not None]
    if not known:
        return None
    total_w = sum(w for _, w in known)
    score = round(sum(s.score * w for s, w in known) / total_w)  # type: ignore[operator]
    days = next(
        (median(gone) for s in signals if s.key == "sell_through" and s.score is not None), None
    )
    if days is None:
        days = median(ages) if len(ages) >= 5 else None
    return {
        "grade": _grade(score),
        "score": score,
        "expected_days": round(days) if days is not None else None,
        "expected_basis": "sell-through" if len(gone) >= MIN_GONE_SAMPLES else "ad age",
        "signals": [s.__dict__ for s in signals],
        "n_active": len(active),
        "n_close": len(close) if engine else None,
        "reserved": sum(1 for c in active if c.reserved),
    }


def market_for_listing(session: Session, listing: Listing, now: datetime) -> dict[str, Any] | None:
    key = listing.model_key or listing.model
    if not key:
        return None
    stmt = select(Comparable).where(
        or_(Comparable.model_key == key, Comparable.model == key)
        if listing.model_key
        else Comparable.model == key
    )
    comps = list(session.scalars(stmt))
    return compute(comps, engine_token(listing.variant, listing.model), listing.gearbox, now.date())
