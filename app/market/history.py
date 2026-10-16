"""Daily snapshot per watchlist model: supply, how many cars clear the bar, and the median
spread. A few weeks of rows show whether a model's margin is widening or collapsing."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Comparable, Listing, ModelHistory


def _med(values: list[int]) -> int | None:
    return round(median(values)) if values else None


def snapshot_models(session: Session, day: date) -> int:
    """Write (or overwrite) today's row for every model with active cars."""
    rows = session.execute(
        select(
            Listing.model_key,
            Listing.price_eur,
            Listing.landed_cost_eur,
            Listing.nl_value_eur,
            Listing.spread_eur,
        ).where(
            Listing.is_active.is_(True),
            Listing.duplicate_of_id.is_(None),
            Listing.model_key.isnot(None),
        )
    ).all()
    comps = dict(
        session.execute(
            select(Comparable.model_key, func.count())
            .where(Comparable.is_active.is_(True), Comparable.model_key.isnot(None))
            .group_by(Comparable.model_key)
        ).all()
    )
    groups: dict[str, list[tuple[int, int | None, int | None, int | None]]] = defaultdict(list)
    for key, price, landed, value, spread in rows:
        groups[key].append((price, landed, value, spread))
    existing = {
        h.model_key: h for h in session.scalars(select(ModelHistory).where(ModelHistory.day == day))
    }
    for key, cars in groups.items():
        spreads = [s for _, _, _, s in cars if s is not None]
        data = dict(
            n_active=len(cars),
            n_valued=sum(1 for _, _, v, _ in cars if v is not None),
            n_positive=sum(1 for s in spreads if s > 0),
            median_price_eur=_med([p for p, _, _, _ in cars]),
            median_landed_eur=_med([lc for _, lc, _, _ in cars if lc is not None]),
            median_nl_value_eur=_med([v for _, _, v, _ in cars if v is not None]),
            median_spread_eur=_med(spreads),
            n_comparables=int(comps.get(key, 0)),
        )
        row = existing.get(key)
        if row is None:
            session.add(ModelHistory(model_key=key, day=day, **data))
        else:
            for k, v in data.items():
                setattr(row, k, v)
    session.flush()
    return len(groups)


def model_series(session: Session, days: int = 60) -> dict[str, list[ModelHistory]]:
    """History per model, oldest first."""
    out: dict[str, list[ModelHistory]] = defaultdict(list)
    for h in session.scalars(select(ModelHistory).order_by(ModelHistory.day)):
        out[h.model_key].append(h)
    return {k: v[-days:] for k, v in out.items()}


def sparkline(values: list[int | None], width: int = 160, height: int = 36) -> str:
    """Points for an SVG polyline; None gaps are skipped."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1
    n = max(len(values) - 1, 1)
    return " ".join(
        f"{i / n * width:.1f},{height - (v - lo) / span * (height - 4) - 2:.1f}" for i, v in pts
    )
