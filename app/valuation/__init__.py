"""Apply the valuation engine to stored listings."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.costs import get_cost_config
from app.db.models import Comparable, Listing
from app.valuation.engine import Comp, Target, ValuationConfig, ValueEstimate, estimate_value

__all__ = [
    "Comp",
    "Target",
    "ValuationConfig",
    "ValueEstimate",
    "estimate_value",
    "recompute_values",
]


def load_comps(session: Session) -> list[Comp]:
    rows = session.scalars(select(Comparable).where(Comparable.is_active.is_(True)))
    return [
        Comp(
            id=r.id,
            source=r.source,
            model=r.model,
            body_type=r.body_type,
            year=r.first_registration.year if r.first_registration else None,
            mileage_km=r.mileage_km,
            price_eur=r.price_eur,
            url=r.url,
            variant=r.variant,
        )
        for r in rows
    ]


def apply_value(row: Listing, comps: list[Comp], cfg: ValuationConfig) -> ValueEstimate | None:
    target = Target(
        model=row.model,
        body_type=row.body_type,
        year=row.first_registration.year if row.first_registration else None,
        mileage_km=row.mileage_km,
    )
    est = estimate_value(target, comps, cfg)
    if est is None:
        row.nl_value_eur = row.nl_value_n = None
        row.nl_value_breakdown = None
        row.spread_eur = row.spread_pct = None
        return None
    row.nl_value_eur = est.value_eur
    row.nl_value_n = est.n
    row.nl_value_breakdown = est.as_dict()
    if row.landed_cost_eur:
        row.spread_eur = est.value_eur - row.landed_cost_eur
        row.spread_pct = round(100 * row.spread_eur / row.landed_cost_eur, 1)
    else:
        row.spread_eur = row.spread_pct = None
    return est


def recompute_values(session: Session, now: datetime, active_only: bool = True) -> int:
    cfg = ValuationConfig.from_mapping(get_cost_config().valuation)
    comps = load_comps(session)
    stmt = select(Listing)
    if active_only:
        stmt = stmt.where(Listing.is_active.is_(True))
    n = 0
    for row in session.scalars(stmt):
        apply_value(row, comps, cfg)
        n += 1
    session.flush()
    return n
