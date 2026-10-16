"""Persist normalized listings: upsert, price history, first/last seen, deactivation."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.costs import get_cost_config, landed_cost
from app.costs.repairs import estimate_repairs
from app.db.models import Listing, PriceHistory, SavedSearch
from app.insights.service import apply_rules
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
    "model_key",
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


# market signals only stored on comparables (see app/market)
_COMPARABLE_ONLY = {"listed_at", "demand", "reserved"}


def apply_costs(row: Listing, today: datetime) -> None:
    """(Re)compute the landed-cost estimate for one listing from its current fields."""
    lc = landed_cost(
        price_eur=row.price_eur,
        country=row.country,
        first_registration=row.first_registration,
        co2_g_km=row.co2_g_km,
        fuel=row.fuel,
        make=row.make,
        model=row.model,
        today=today.date(),
    )
    repairs = estimate_repairs(
        row.variant,
        row.description,
        row.description_en,
        ranges=get_cost_config().repairs,
        tuv_until=row.insight.tuv_until if row.insight is not None else None,
        today=today.date(),
    )
    row.repair_breakdown = repairs
    row.repair_cost_eur = repairs["estimate"] if repairs else None
    breakdown = lc.as_dict()
    breakdown["repair_eur"] = row.repair_cost_eur or 0
    breakdown["total_with_repairs_eur"] = (
        (lc.total_eur + (row.repair_cost_eur or 0)) if lc.total_eur is not None else None
    )
    row.landed_cost_eur = breakdown["total_with_repairs_eur"]
    row.bpm_eur = lc.bpm_eur
    row.cost_breakdown = breakdown
    row.cost_version = lc.config_version


def recompute_costs(session: Session, now: datetime, only_stale: bool = False) -> int:
    """Recompute every listing (or only those computed with an older costs.yaml version)."""
    stmt = select(Listing).order_by(Listing.id)
    if only_stale:
        version = get_cost_config().version
        stmt = stmt.where((Listing.cost_version.is_(None)) | (Listing.cost_version != version))
    n = 0
    for row in session.scalars(stmt):
        apply_costs(row, now)
        n += 1
    session.flush()
    return n


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
            row = Listing(
                **item.model_dump(exclude=_COMPARABLE_ONLY),
                first_seen=now,
                last_seen=now,
                is_active=True,
            )
            row.price_history.append(PriceHistory(price_eur=item.price_eur, seen_at=now))
            row.saved_searches.append(saved_search)
            apply_rules(row, now)  # tags first: the TÜV date feeds the repair estimate
            apply_costs(row, now)
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
                if f == "description":
                    row.description_en = None  # translated again by the worker
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
        apply_rules(row, now)
        apply_costs(row, now)
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
