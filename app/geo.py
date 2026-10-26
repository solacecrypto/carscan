"""Where is the car, and what does fetching it cost?

Cities are geocoded once through OpenStreetMap's Nominatim (free, one request per second,
cached in ``geo_cache``). Road distance is the great-circle distance times a detour factor;
fuel, time and tolls come from ``travel:`` in costs.yaml and the base location in .env."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import GeoCache, Listing

log = structlog.get_logger()

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "carscan (private car import tool; contact via repository)"
NEARBY_KM = 100


def cache_key(city: str | None, postcode: str | None, country: str) -> str | None:
    city = (city or "").strip().lower()
    postcode = (postcode or "").strip().lower()
    if not city and not postcode:
        return None
    return f"{country.lower()}|{postcode}|{city}"[:200]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode_live(
    client: httpx.Client, city: str | None, postcode: str | None, country: str
) -> tuple[float, float] | None:
    params: dict[str, str] = {"format": "json", "limit": "1", "countrycodes": country.lower()}
    if postcode:
        params["postalcode"] = postcode
    if city:
        params["city"] = city
    r = client.get(NOMINATIM, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
    if r.status_code != 200:
        raise httpx.HTTPStatusError(
            f"nominatim HTTP {r.status_code}", request=r.request, response=r
        )
    data = r.json()
    if not data and postcode and city:  # retry on the city alone (postcode typos are common)
        params.pop("postalcode")
        data = client.get(NOMINATIM, params=params, headers={"User-Agent": USER_AGENT}).json()
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def geocode_address(client: httpx.Client, text: str) -> tuple[float, float, str] | None:
    """Free-text geocoding for a person's home: any address, postcode or city in Europe.
    Returns (lat, lon, display name) or None when Nominatim finds nothing."""
    text = " ".join(text.split())
    if not text:
        return None
    r = client.get(
        NOMINATIM,
        params={"format": "json", "limit": "1", "q": text},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    if r.status_code != 200:
        raise httpx.HTTPStatusError(
            f"nominatim HTTP {r.status_code}", request=r.request, response=r
        )
    data = r.json()
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"]), str(data[0].get("display_name") or text)


def base_point(
    session: Session, user: Any, base: tuple[str | None, str | None, str]
) -> tuple[float, float, str] | None:
    """Where distances are measured from: the user's own location when set, otherwise the
    configured base (BASE_CITY etc.) from the geocode cache."""
    if user is not None and getattr(user, "home_lat", None) is not None:
        return float(user.home_lat), float(user.home_lon), str(user.home_label or "home")
    here = lookup(session, *base)
    if here is None or here.lat is None:
        return None
    return here.lat, here.lon, " ".join(x for x in base[:2] if x) or base[2]  # type: ignore[return-value]


def lookup(
    session: Session, city: str | None, postcode: str | None, country: str
) -> GeoCache | None:
    key = cache_key(city, postcode, country)
    if key is None:
        return None
    return session.scalar(select(GeoCache).where(GeoCache.key == key))


def geocode_backlog(
    session: Session,
    client: httpx.Client | None = None,
    limit: int = 30,
    sleep: Callable[[float], None] = time.sleep,
    base: tuple[str | None, str | None, str] | None = None,
) -> dict[str, int]:
    """Resolve places for active cars (and the base) that are not cached yet."""
    counts = {"resolved": 0, "unknown": 0, "failed": 0}
    wanted: dict[str, tuple[str | None, str | None, str]] = {}
    if base is not None and (key := cache_key(*base)):
        wanted[key] = base
    rows = session.execute(
        select(Listing.city, Listing.postcode, Listing.country)
        .where(Listing.is_active.is_(True))
        .distinct()
    ).all()
    for city, postcode, country in rows:
        key = cache_key(city, postcode, country)
        if key and key not in wanted:
            wanted[key] = (city, postcode, country)
    known = set(session.scalars(select(GeoCache.key).where(GeoCache.key.in_(list(wanted)))))
    todo = [(k, v) for k, v in wanted.items() if k not in known][:limit]
    if not todo:
        return counts
    own = client is None
    client = client or httpx.Client()
    try:
        for key, (city, postcode, country) in todo:
            try:
                hit = geocode_live(client, city, postcode, country)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("geo.failed", key=key, error=str(e))
                counts["failed"] += 1
                break  # rate limited or down: try again next run
            session.add(
                GeoCache(
                    key=key,
                    country=country.upper(),
                    lat=hit[0] if hit else None,
                    lon=hit[1] if hit else None,
                    resolved_at=datetime.now(UTC),
                )
            )
            counts["resolved" if hit else "unknown"] += 1
            session.commit()
            sleep(1.1 + random.random() * 0.5)  # Nominatim asks for at most one request a second
    finally:
        if own:
            client.close()
    return counts


def trip(
    session: Session,
    listing: Listing,
    base: tuple[str | None, str | None, str],
    travel: dict[str, Any],
    user: Any = None,
) -> dict[str, Any] | None:
    """Distance, drive time and cost from the base to the car, or None when either is unknown.
    `user` with a saved home location overrides the configured base."""
    here = base_point(session, user, base)
    there = lookup(session, listing.city, listing.postcode, listing.country)
    if not here or not there or there.lat is None:
        return None
    straight = haversine_km(here[0], here[1], there.lat, there.lon)  # type: ignore[arg-type]
    km = round(straight * float(travel.get("detour_factor", 1.25)))
    one_way_h = km / float(travel.get("avg_speed_kmh", 85))
    fuel = km * 2 * float(travel.get("fuel_eur_per_km", 0.16))  # there by car, back driving the buy
    tolls = float((travel.get("tolls_eur") or {}).get(listing.country, 0))
    return {
        "km": km,
        "hours_one_way": round(one_way_h, 1),
        "fuel_eur": round(fuel),
        "tolls_eur": round(tolls),
        "total_eur": round(fuel + tolls),
        "from": here[2],
        "lat": there.lat,
        "lon": there.lon,
    }


def _key_sql():  # type: ignore[no-untyped-def]
    """The geo_cache key as SQL, matching cache_key()."""
    from sqlalchemy import func, literal

    return (
        func.lower(func.trim(Listing.country))
        + literal("|")
        + func.lower(func.trim(func.coalesce(Listing.postcode, "")))
        + literal("|")
        + func.lower(func.trim(func.coalesce(Listing.city, "")))
    )


def nearby_listings(
    session: Session,
    listing: Listing,
    radius_km: float,
    positive_only: bool = True,
    limit: int = 80,
) -> tuple[GeoCache | None, list[tuple[Listing, int]]]:
    """Active cars within ``radius_km`` of this car, nearest first. A bounding box on the
    geocoded places does the heavy lifting in SQL; the exact distance is computed after."""
    here = lookup(session, listing.city, listing.postcode, listing.country)
    if here is None or here.lat is None or here.lon is None:
        return None, []
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(here.lat))))
    stmt = (
        select(Listing, GeoCache.lat, GeoCache.lon)
        .join(GeoCache, GeoCache.key == _key_sql())
        .where(
            Listing.is_active.is_(True),
            Listing.duplicate_of_id.is_(None),
            Listing.id != listing.id,
            GeoCache.lat.between(here.lat - dlat, here.lat + dlat),
            GeoCache.lon.between(here.lon - dlon, here.lon + dlon),
        )
    )
    if positive_only:
        stmt = stmt.where(Listing.spread_eur > 0)
    out = []
    for x, lat, lon in session.execute(stmt).all():
        km = haversine_km(here.lat, here.lon, lat, lon)
        if km <= radius_km:
            out.append((x, round(km)))
    out.sort(key=lambda t: t[1])
    return here, out[:limit]


def nearby(
    session: Session, listing: Listing, candidates: list[Listing], radius_km: float = NEARBY_KM
) -> list[tuple[Listing, int]]:
    """Other cars within ``radius_km`` of this one, nearest first."""
    here = lookup(session, listing.city, listing.postcode, listing.country)
    if not here or here.lat is None:
        return []
    out = []
    for c in candidates:
        if c.id == listing.id:
            continue
        there = lookup(session, c.city, c.postcode, c.country)
        if not there or there.lat is None:
            continue
        km = haversine_km(here.lat, here.lon, there.lat, there.lon)  # type: ignore[arg-type]
        if km <= radius_km:
            out.append((c, round(km)))
    return sorted(out, key=lambda t: t[1])
