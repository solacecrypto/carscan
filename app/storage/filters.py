"""Saved-search filter criteria (the `filters` JSON), applied in Python for alerting
and translated to SQL for the listings page."""

from datetime import date
from typing import Any

from sqlalchemy import Select, extract, func

from app.db.models import Listing, ListingInsight, SellerType
from app.normalize.attrs import BODIES, COLORS

FILTER_KEYS = (
    "make",
    "model_key",
    "price_min",
    "price_max",
    "landed_max",
    "spread_min",
    "profitable",
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
    "papers_ok",
    "tuv_min_months",
    "color",
    "body",
    "starred",
    "q",
)


def _list(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list | tuple | set) else [value]
    return [str(v).strip() for v in items if str(v).strip()]


def _countries(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list | tuple | set) else str(value).split(",")
    return [str(c).strip().upper() for c in items if str(c).strip()]


def _profitable(params: Any) -> bool:
    getlist = getattr(params, "getlist", None)
    raw = getlist("profitable") if getlist else params.get("profitable")
    if raw is None or raw == []:
        return True
    values = [str(v).lower() for v in (raw if isinstance(raw, list) else [raw])]
    return any(v in ("1", "true", "on", "yes") for v in values)


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

    def many(key: str) -> list[str]:
        raw = getlist(key) if getlist else params.get(key)
        return _list(raw)

    countries = many("country")
    by_label = {label.lower(): key for key, label in (models or [])}
    model_keys = [by_label.get(m.lower(), m) for m in many("model") + many("model_key")]
    return {
        "make": many("make") or None,
        "model_key": model_keys or None,
        "price_min": num("price_min"),
        "price_max": num("price_max"),
        "landed_max": num("landed_max"),
        "spread_min": num("spread_min"),
        # on by default; the form sends an explicit "false" when the box is unticked
        "profitable": _profitable(params) or None,
        "mileage_max": num("mileage_max"),
        "year_min": num("year_min"),
        "year_max": num("year_max"),
        "gearbox": text("gearbox"),
        "seller_type": text("seller_type"),
        "country": _countries(countries) or None,
        "color": [c for c in many("color") if c in COLORS] or None,
        "body": [b for b in many("body") if b in BODIES] or None,
        "new_hours": num("new_hours"),
        "no_accident": flag("no_accident") or None,
        "no_export_only": flag("no_export_only") or None,
        "chain_replaced": flag("chain_replaced") or None,
        "service_full": flag("service_full") or None,
        "papers_ok": flag("papers_ok") or None,  # hide ads that say the papers are missing
        "starred": flag("starred") or None,  # only my starred cars (joined in the route)
        "q": text("q") or None,  # free words over make, model, variant, city, description
        "tuv_min_months": num("tuv_min_months"),  # inspection valid for at least this long
        "price_dropped": flag("price_dropped"),
        "include_inactive": flag("include_inactive"),
        "include_duplicates": flag("include_duplicates"),
    }


def matches(listing: Listing, filters: dict[str, Any]) -> bool:
    f = filters or {}
    makes = _list(f.get("make"))
    if makes and listing.make.lower() not in {m.lower() for m in makes}:
        return False
    models = _list(f.get("model_key"))
    if models and listing.model_key not in models:
        return False
    if f.get("price_min") and listing.price_eur < int(f["price_min"]):
        return False
    if f.get("price_max") and listing.price_eur > int(f["price_max"]):
        return False
    if f.get("landed_max") and (
        listing.landed_cost_eur is None or listing.landed_cost_eur > int(f["landed_max"])
    ):
        return False
    if f.get("profitable") and (listing.spread_eur is None or listing.spread_eur <= 0):
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
    if f.get("service_full") and (ins is None or ins.service_history != "full"):
        return False
    if f.get("papers_ok"):
        keys = {t.get("key") for t in ((ins.tags if ins else None) or [])}
        if "papers_missing" in keys:
            return False
    if f.get("q"):
        hay = " ".join(
            str(p)
            for p in (listing.make, listing.model, listing.variant, listing.city,
                      listing.description_en, listing.description)
            if p
        ).lower()  # fmt: skip
        if not all(w in hay for w in f["q"].lower().split()):
            return False
    if f.get("color") and listing.color not in f["color"]:
        return False
    if f.get("body") and listing.body_type not in f["body"]:
        return False
    if f.get("tuv_min_months"):
        until = ins.tuv_until if ins else None
        if until is None or until < _months_ahead(int(f["tuv_min_months"])):
            return False
    return True


def _months_ahead(months: int) -> date:
    today = date.today()
    y, m = divmod(today.month - 1 + months, 12)
    return date(today.year + y, m + 1, min(today.day, 28))


def apply_sql(stmt: Select, filters: dict[str, Any]) -> Select:
    f = filters or {}
    makes = _list(f.get("make"))
    if makes:
        stmt = stmt.where(func.lower(Listing.make).in_([m.lower() for m in makes]))
    models = _list(f.get("model_key"))
    if models:
        stmt = stmt.where(Listing.model_key.in_(models))
    if f.get("price_min"):
        stmt = stmt.where(Listing.price_eur >= int(f["price_min"]))
    if f.get("price_max"):
        stmt = stmt.where(Listing.price_eur <= int(f["price_max"]))
    if f.get("landed_max"):
        stmt = stmt.where(Listing.landed_cost_eur <= int(f["landed_max"]))
    if f.get("profitable"):
        stmt = stmt.where(Listing.spread_eur > 0)
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
    if f.get("q"):
        hay = func.lower(
            func.concat_ws(
                " ",
                Listing.make,
                Listing.model,
                Listing.variant,
                Listing.city,
                Listing.description_en,
                Listing.description,
            )
        )
        for word in f["q"].lower().split()[:6]:
            stmt = stmt.where(hay.contains(word, autoescape=True))
    if f.get("color"):
        stmt = stmt.where(Listing.color.in_(f["color"]))
    if f.get("body"):
        stmt = stmt.where(Listing.body_type.in_(f["body"]))
    insight_keys = (
        "no_accident",
        "no_export_only",
        "chain_replaced",
        "service_full",
        "papers_ok",
        "tuv_min_months",
    )
    if any(f.get(k) for k in insight_keys):
        stmt = stmt.outerjoin(ListingInsight, ListingInsight.listing_id == Listing.id)
        if f.get("tuv_min_months"):
            stmt = stmt.where(ListingInsight.tuv_until >= _months_ahead(int(f["tuv_min_months"])))
        if f.get("papers_ok"):
            stmt = stmt.where(
                (ListingInsight.tags.is_(None))
                | (~ListingInsight.tags.contains([{"key": "papers_missing"}]))
            )
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
