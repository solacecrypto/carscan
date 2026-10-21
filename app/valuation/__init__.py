"""Apply the valuation engine to stored listings."""

from dataclasses import replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.costs import get_cost_config
from app.db.models import Comparable, Listing
from app.valuation.engine import (
    Comp,
    Target,
    ValuationConfig,
    ValueEstimate,
    engine_token,
    estimate_value,
)

__all__ = [
    "engine_token",
    "Comp",
    "Target",
    "ValuationConfig",
    "ValueEstimate",
    "estimate_value",
    "load_comps",
    "recompute_values",
]


def load_comps(session: Session, model_key: str | None = None) -> list[Comp]:
    """Active comparables as engine inputs; limited to one model when the pass is scoped,
    which keeps a per-run recompute to a few hundred rows instead of all of them."""
    stmt = select(Comparable).where(Comparable.is_active.is_(True))
    if model_key:
        stmt = stmt.where(Comparable.model_key == model_key)
    rows = session.scalars(stmt)
    return [
        Comp(
            id=r.id,
            source=r.source,
            model=r.model_key or r.model,
            body_type=r.body_type,
            year=r.first_registration.year if r.first_registration else None,
            mileage_km=r.mileage_km,
            price_eur=r.price_eur,
            url=r.url,
            variant=r.variant,
            engine=engine_token(r.variant, r.model),
            gearbox=r.gearbox,
            fuel=r.fuel,
            power_kw=r.power_kw,
        )
        for r in rows
    ]


def apply_value(row: Listing, comps: list[Comp], cfg: ValuationConfig) -> ValueEstimate | None:
    target = Target(
        model=row.model_key or row.model,
        body_type=row.body_type,
        year=row.first_registration.year if row.first_registration else None,
        mileage_km=row.mileage_km,
        engine=engine_token(row.variant, row.model),
        gearbox=row.gearbox,
        fuel=row.fuel,
        power_kw=row.power_kw,
        price_eur=row.price_eur,
    )
    est = estimate_value(target, comps, cfg)
    name = row.model or engine_token(row.variant)  # some sites leave the model blank
    if est is None and row.model_key and name:
        # comparables stored before the watchlist key existed only carry the site's model name
        est = estimate_value(replace(target, model=name), comps, cfg)
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


def recompute_values(
    session: Session,
    now: datetime,
    active_only: bool = True,
    model_key: str | None = None,
    batch_commit: int | None = None,
) -> int:
    """Re-value listings; ``model_key`` limits it to one watchlist model (a scrape run only
    changes that model's cars or comparables). Rows are taken in id order so concurrent
    recomputes never deadlock each other."""
    cfg = ValuationConfig.from_mapping(get_cost_config().valuation)
    comps = load_comps(session, model_key)
    stmt = select(Listing).order_by(Listing.id)
    if active_only:
        stmt = stmt.where(Listing.is_active.is_(True))
    if model_key:
        stmt = stmt.where(Listing.model_key == model_key)
    n = 0
    for row in session.scalars(stmt):
        apply_value(row, comps, cfg)
        n += 1
        if batch_commit and n % batch_commit == 0:
            session.commit()  # the CLI runs next to the worker: keep lock windows short
    session.flush()
    return n
