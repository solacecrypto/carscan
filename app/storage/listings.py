"""Persist normalized listings: upsert, price history, first/last seen, deactivation."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Listing, PriceHistory, SavedSearch
from app.normalize.schema import NormalizedListing


@dataclass
class UpsertResult:
    new: list[Listing] = field(default_factory=list)
    price_dropped: list[tuple[Listing, int]] = field(default_factory=list)  # (listing, old price)
    price_raised: list[Listing] = field(default_factory=list)
    unchanged: int = 0
    reactivated: int = 0

    @property
    def seen(self) -> int:
        return len(self.new) + len(self.price_dropped) + len(self.price_raised) + self.unchanged


_UPDATABLE = (
    "url",
    "make",
    "model",
    "variant",
    "body_type",
    "engine_code",
    "fuel",
    "gearbox",
    "power_kw",
    "first_registration",
    "mileage_km",
    "vat_deductible",
    "co2_g_km",
    "vin",
    "seller_type",
    "seller_name",
    "country",
    "city",
    "postcode",
    "photos",
    "description",
    "raw",
)


def upsert_listings(
    session: Session, items: list[NormalizedListing], saved_search: SavedSearch, now: datetime
) -> UpsertResult:
    result = UpsertResult()
    if not items:
        return result
    source = items[0].source
    ids = [i.source_listing_id for i in items]
    existing = {
        row.source_listing_id: row
        for row in session.scalars(
            select(Listing).where(Listing.source == source, Listing.source_listing_id.in_(ids))
        )
    }
    for item in items:
        row = existing.get(item.source_listing_id)
        if row is None:
            row = Listing(**item.model_dump(), first_seen=now, last_seen=now, is_active=True)
            row.price_history.append(PriceHistory(price_eur=item.price_eur, seen_at=now))
            row.saved_searches.append(saved_search)
            session.add(row)
            result.new.append(row)
            continue

        if not row.is_active:
            row.is_active = True
            result.reactivated += 1
        if saved_search not in row.saved_searches:
            row.saved_searches.append(saved_search)
        row.last_seen = now
        for f in _UPDATABLE:
            val = getattr(item, f)
            # don't erase detail-only fields when a later search-only pass lacks them
            if val is None and f in ("description", "co2_g_km", "vat_deductible", "vin"):
                continue
            if f == "raw" and "detail" in (row.raw or {}) and "detail" not in val:
                val = {**val, "detail": row.raw["detail"]}
            if f == "photos" and len(val) < len(row.photos or []):
                continue  # keep the richer full-size set
            if getattr(row, f) != val:
                setattr(row, f, val)

        if item.price_eur != row.price_eur:
            old = row.price_eur
            row.previous_price_eur = old
            row.price_changed_at = now
            row.price_eur = item.price_eur
            row.price_history.append(PriceHistory(price_eur=item.price_eur, seen_at=now))
            if item.price_eur < old:
                result.price_dropped.append((row, old))
            else:
                result.price_raised.append(row)
        else:
            result.unchanged += 1
    session.flush()
    return result


def deactivate_missing(
    session: Session, saved_search: SavedSearch, run_started_at: datetime
) -> list[Listing]:
    """Mark listings of this search inactive when the run that just finished didn't see them."""
    stale = list(
        session.scalars(
            select(Listing)
            .join(Listing.saved_searches)
            .where(
                SavedSearch.id == saved_search.id,
                Listing.is_active.is_(True),
                Listing.last_seen < run_started_at,
            )
        )
    )
    if stale:
        session.execute(
            update(Listing).where(Listing.id.in_([x.id for x in stale])).values(is_active=False)
        )
        for x in stale:
            x.is_active = False
    return stale


def get_by_source_id(session: Session, source: str, source_listing_id: str) -> Listing | None:
    return session.scalar(
        select(Listing).where(
            Listing.source == source, Listing.source_listing_id == source_listing_id
        )
    )
