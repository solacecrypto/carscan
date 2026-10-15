"""Persist NL comparables (asking prices) scraped by 'comparables' saved searches."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Comparable, SavedSearch
from app.normalize.schema import NormalizedListing


@dataclass
class CompUpsertResult:
    new: int = 0
    updated: int = 0
    reactivated: int = 0

    @property
    def seen(self) -> int:
        return self.new + self.updated


_FIELDS = (
    "url", "make", "model", "model_key", "variant", "body_type", "fuel", "gearbox", "power_kw",
    "first_registration", "mileage_km", "price_eur", "seller_type", "seller_name", "city",
    "photos", "raw",
)  # fmt: skip


def upsert_comparables(
    session: Session, items: list[NormalizedListing], saved_search: SavedSearch, now: datetime
) -> CompUpsertResult:
    result = CompUpsertResult()
    if not items:
        return result
    source = items[0].source
    existing = {
        row.source_listing_id: row
        for row in session.scalars(
            select(Comparable).where(
                Comparable.source == source,
                Comparable.source_listing_id.in_([i.source_listing_id for i in items]),
            )
        )
    }
    for item in items:
        row = existing.get(item.source_listing_id)
        if row is None:
            data = {k: getattr(item, k) for k in _FIELDS}
            session.add(
                Comparable(
                    source=item.source,
                    source_listing_id=item.source_listing_id,
                    saved_search_id=saved_search.id,
                    first_seen=now,
                    last_seen=now,
                    is_active=True,
                    **data,
                )
            )
            result.new += 1
            continue
        if not row.is_active:
            row.is_active = True
            result.reactivated += 1
        row.last_seen = now
        for k in _FIELDS:
            val = getattr(item, k)
            if getattr(row, k) != val:
                setattr(row, k, val)
        result.updated += 1
    session.flush()
    return result


def deactivate_missing_comparables(
    session: Session, saved_search: SavedSearch, run_started_at: datetime
) -> int:
    res = session.execute(
        update(Comparable)
        .where(
            Comparable.saved_search_id == saved_search.id,
            Comparable.is_active.is_(True),
            Comparable.last_seen < run_started_at,
        )
        .values(is_active=False)
    )
    return int(res.rowcount or 0)
