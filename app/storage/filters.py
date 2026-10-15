"""Saved-search filter criteria (the `filters` JSON), applied in Python for alerting
and translated to SQL for the listings page."""

from typing import Any

from sqlalchemy import Select, extract

from app.db.models import Listing, ListingInsight, SellerType

FILTER_KEYS = (
    "make",
    "model_key",
    "price_min",
    "price_max",
    "landed_max",
    "spread_min",
    "mileage_max",
    "new_hours",
    "year_min",
    "year_max",
    "gearbox",
    "seller_type",
    "country",
    "no_accident",
    "no_export_only",
    "chain_replaced",
    "service_full",
)


def _countries(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list | tuple | set) else str(value).split(",")
    return [str(c).strip().upper() for c in items if str(c).strip()]


def parse_filters(params: Any, models: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Tolerant reading of the filter form / query string: empty strings mean 'not set',
    numbers that fail to parse are ignored, `country` may repeat, and `model` accepts a
    watchlist key or its label."""

    def num(key: str) -> int | None:
        raw = params.get(key)
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).replace(".", "").replace(",", "").strip())
        except ValueError:
            return None

    def text(key: str) -> str | None:
        raw = params.get(key)
        return str(raw).strip() or None if raw is not None else None

    def flag(key: str) -> bool:
        return str(params.get(key, "")).lower() in ("1", "true", "on", "yes")

    getlist = getattr(params, "getlist", None)
    countries = getlist("country") if getlist else params.get("country")
    model = text("model") or text("model_key")
    if model and models:
        by_label = {label.lower(): key for key, label in models}
        model = by_label.get(model.lower(), model)
    return {
        "make": text("make"),
        "model_key": model,
        "price_min": num("price_min"),
        "price_max": num("price_max"),
        "landed_max": num("landed_max"),
        "spread_min": num("spread_min"),
        "mileage_max": num("mileage_max"),
        "year_min": num("year_min"),
        "year_max": num("year_max"),
        "gearbox": text("gearbox"),
        "seller_type": text("seller_type"),
        "country": _countries(countries) or None,
        "new_hours": num("new_hours"),
        "no_accident": flag("no_accident") or None,
        "no_export_only": flag("no_export_only") or None,
        "chain_replaced": flag("chain_replaced") or None,
        "service_full": flag("service_full") or None,
        "price_dropped": flag("price_dropped"),
        "include_inactive": flag("include_inactive"),
        "include_duplicates": flag("include_duplicates"),
    }


def matches(listing: Listing, filters: dict[str, Any]) -> bool:
    f = filters or {}
    if f.get("make") and listing.make.lower() != str(f["make"]).lower():
        return False
    if f.get("model_key") and listing.model_key != f["model_key"]:
        return False
    if f.get("price_min") and listing.price_eur < int(f["price_min"]):
        return False
    if f.get("price_max") and listing.price_eur > int(f["price_max"]):
        return False
    if f.get("landed_max") and (
        listing.landed_cost_eur is None or listing.landed_cost_eur > int(f["landed_max"])
    ):
        return False
    if f.get("spread_min") and (
        listing.spread_eur is None or listing.spread_eur < int(f["spread_min"])
    ):
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
    countries = _countries(f.get("country"))
    if countries and listing.country not in countries:
        return False
    ins = listing.insight
    if f.get("no_accident") and ins is not None and ins.accident == "yes":
        return False
    if f.get("no_export_only") and ins is not None and ins.export_only:
        return False
    if f.get("chain_replaced") and (ins is None or ins.timing_chain != "replaced"):
        return False
    return not (f.get("service_full") and (ins is None or ins.service_history != "full"))


def apply_sql(stmt: Select, filters: dict[str, Any]) -> Select:
    f = filters or {}
    if f.get("make"):
        stmt = stmt.where(Listing.make.ilike(str(f["make"])))
    if f.get("model_key"):
        stmt = stmt.where(Listing.model_key == f["model_key"])
    if f.get("price_min"):
        stmt = stmt.where(Listing.price_eur >= int(f["price_min"]))
    if f.get("price_max"):
        stmt = stmt.where(Listing.price_eur <= int(f["price_max"]))
    if f.get("landed_max"):
        stmt = stmt.where(Listing.landed_cost_eur <= int(f["landed_max"]))
    if f.get("spread_min"):
        stmt = stmt.where(Listing.spread_eur >= int(f["spread_min"]))
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
    countries = _countries(f.get("country"))
    if countries:
        stmt = stmt.where(Listing.country.in_(countries))
    if any(f.get(k) for k in ("no_accident", "no_export_only", "chain_replaced", "service_full")):
        stmt = stmt.outerjoin(ListingInsight, ListingInsight.listing_id == Listing.id)
        if f.get("no_accident"):
            stmt = stmt.where(
                (ListingInsight.accident.is_(None)) | (ListingInsight.accident != "yes")
            )
        if f.get("no_export_only"):
            stmt = stmt.where(
                (ListingInsight.export_only.is_(None)) | (ListingInsight.export_only.is_(False))
            )
        if f.get("chain_replaced"):
            stmt = stmt.where(ListingInsight.timing_chain == "replaced")
        if f.get("service_full"):
            stmt = stmt.where(ListingInsight.service_history == "full")
    return stmt
