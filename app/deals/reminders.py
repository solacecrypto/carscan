"""Daily reminders for deal owners: deals stuck in a stage, and open deals whose car changed
(gone from the site, or the asking price moved) since they were tracked."""

from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Deal, DealStage
from app.deals.service import STAGE_ORDER, days_in_stage, is_stale, next_action
from app.notify import push

log = structlog.get_logger()

# stages where the car still belongs to the seller, so changes to the ad matter
PRE_PURCHASE = (DealStage.spotted, DealStage.contacted, DealStage.inspected)


def _eur(v: int | None) -> str:
    return f"€ {v:,}".replace(",", ".") if v is not None else "–"


def _title(deal: Deal) -> str:
    x = deal.listing
    year = x.first_registration.year if x.first_registration else ""
    return " ".join(str(p) for p in (x.make, x.model, year) if p)


def deal_reminders(session: Session, now: datetime) -> int:
    """Create notifications for the owner of every open deal that needs attention.
    Returns the number of notifications created or refreshed."""
    deals = list(
        session.scalars(
            select(Deal)
            .where(Deal.stage.in_(STAGE_ORDER[:-1]))
            .options(selectinload(Deal.listing), selectinload(Deal.owner))
        )
    )
    n = 0
    for d in deals:
        url = f"/deals/{d.id}"
        if is_stale(d, now):
            push(
                session,
                d.owner_id,
                "deal",
                f"{_title(d)}: {days_in_stage(d, now)} days in '{d.stage.value}'",
                next_action(d),
                now,
                d.listing,
                url=url,
            )
            n += 1
        if d.stage in PRE_PURCHASE:
            if not d.listing.is_active:
                push(
                    session,
                    d.owner_id,
                    "deal",
                    f"{_title(d)}: the ad is gone",
                    "Sold elsewhere or withdrawn? Confirm with the seller or drop the deal.",
                    now,
                    d.listing,
                    url=url,
                )
                n += 1
            elif d.listing.price_eur != d.est_price_eur:
                direction = "dropped" if d.listing.price_eur < d.est_price_eur else "rose"
                push(
                    session,
                    d.owner_id,
                    "deal",
                    f"{_title(d)}: asking price {direction} to {_eur(d.listing.price_eur)}",
                    f"was {_eur(d.est_price_eur)} when tracked",
                    now,
                    d.listing,
                    url=url,
                )
                n += 1
    if n:
        log.info("deals.reminders", count=n)
    return n
