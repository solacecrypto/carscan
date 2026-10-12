"""Cross-source dedup. No source exposes a VIN, so the same car on two sites is matched on
make, model, body, first registration, mileage and price (see docs/dedup.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing

MILEAGE_TOLERANCE = 0.02
PRICE_TOLERANCE = 0.03


@dataclass(frozen=True)
class DedupResult:
    linked: int
    checked: int


def _year_only(row: Listing) -> bool:
    return bool((row.raw or {}).get("year_only"))


def is_same_car(a: Listing, b: Listing) -> bool:
    if a.source == b.source or a.country != b.country:
        return False
    if a.vin and b.vin:
        return a.vin == b.vin
    if a.make.lower() != b.make.lower() or a.model.lower() != b.model.lower():
        return False
    if a.body_type and b.body_type and a.body_type != b.body_type:
        return False
    if not (a.first_registration and b.first_registration):
        return False
    if a.first_registration.year != b.first_registration.year:
        return False
    if (
        not (_year_only(a) or _year_only(b))
        and a.first_registration.month != b.first_registration.month
    ):
        return False
    if a.mileage_km and b.mileage_km:
        if abs(a.mileage_km - b.mileage_km) > MILEAGE_TOLERANCE * max(a.mileage_km, b.mileage_km):
            return False
    elif a.mileage_km or b.mileage_km:
        return False
    return abs(a.price_eur - b.price_eur) <= PRICE_TOLERANCE * max(a.price_eur, b.price_eur)


def canonical(row: Listing) -> Listing:
    seen = {row.id}
    while row.duplicate_of is not None and row.duplicate_of.id not in seen:
        seen.add(row.duplicate_of.id)
        row = row.duplicate_of
    return row


def link_duplicates(session: Session, candidates: list[Listing], now: datetime) -> DedupResult:
    """Link each candidate to an earlier-seen active listing of the same car on another source."""
    linked = 0
    for row in candidates:
        if row.duplicate_of_id is not None or not row.first_registration:
            continue
        others = session.scalars(
            select(Listing).where(
                Listing.id != row.id,
                Listing.is_active.is_(True),
                Listing.source != row.source,
                Listing.country == row.country,
                Listing.make.ilike(row.make),
                Listing.model.ilike(row.model),
            )
        )
        for other in others:
            if is_same_car(row, other):
                target = canonical(other)
                if target.id == row.id:
                    continue
                # the listing seen first stays canonical
                if target.first_seen <= row.first_seen:
                    row.duplicate_of = target
                else:
                    target.duplicate_of = row
                linked += 1
                break
    session.flush()
    return DedupResult(linked=linked, checked=len(candidates))


def relink_all(session: Session, now: datetime) -> DedupResult:
    rows = list(
        session.scalars(
            select(Listing).where(Listing.is_active.is_(True)).order_by(Listing.first_seen)
        )
    )
    for r in rows:
        r.duplicate_of = None
    session.flush()
    return link_duplicates(session, rows, now)
