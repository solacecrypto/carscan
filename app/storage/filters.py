"""Saved-search filter criteria (the `filters` JSON), applied in Python for alerting
and translated to SQL for the listings page."""

from typing import Any

from sqlalchemy import Select, extract

from app.db.models import Listing, SellerType

FILTER_KEYS = (
    "price_min",
    "price_max",
    "mileage_max",
    "year_min",
    "year_max",
    "gearbox",
    "seller_type",
    "country",
)


def matches(listing: Listing, filters: dict[str, Any]) -> bool:
    f = filters or {}
    if f.get("price_min") and listing.price_eur < int(f["price_min"]):
        return False
    if f.get("price_max") and listing.price_eur > int(f["price_max"]):
        return False
    if f.get("mileage_max") and (listing.mileage_km or 0) > int(f["mileage_max"]):
        return False
    year = listing.first_registration.year if listing.first_registration else None
    if f.get("year_min") and (year is None or year < int(f["year_min"])):
        return False
    if f.get("year_max") and (year is None or year > int(f["year_max"])):
        return False
    if f.get("gearbox") and listing.gearbox != f["gearbox"]:
        return False
    if f.get("seller_type") and listing.seller_type != SellerType(f["seller_type"]):
        return False
    return not (f.get("country") and listing.country != f["country"].upper())


def apply_sql(stmt: Select, filters: dict[str, Any]) -> Select:
    f = filters or {}
    if f.get("price_min"):
        stmt = stmt.where(Listing.price_eur >= int(f["price_min"]))
    if f.get("price_max"):
        stmt = stmt.where(Listing.price_eur <= int(f["price_max"]))
    if f.get("mileage_max"):
        stmt = stmt.where(Listing.mileage_km <= int(f["mileage_max"]))
    if f.get("year_min"):
        stmt = stmt.where(extract("year", Listing.first_registration) >= int(f["year_min"]))
    if f.get("year_max"):
        stmt = stmt.where(extract("year", Listing.first_registration) <= int(f["year_max"]))
    if f.get("gearbox"):
        stmt = stmt.where(Listing.gearbox == f["gearbox"])
    if f.get("seller_type"):
        stmt = stmt.where(Listing.seller_type == SellerType(f["seller_type"]))
    if f.get("country"):
        stmt = stmt.where(Listing.country == f["country"].upper())
    return stmt
