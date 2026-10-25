from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.db import GeoCache, Listing, SellerType
from app.geo import cache_key, geocode_backlog, geocode_live, haversine_km, nearby, trip

NOW = datetime(2026, 10, 16, tzinfo=UTC)
TRAVEL = {
    "detour_factor": 1.25,
    "avg_speed_kmh": 85,
    "fuel_eur_per_km": 0.16,
    "tolls_eur": {"IT": 90},
}
PLACES = {
    "nl||utrecht": (52.09, 5.12),
    "de|38112|braunschweig": (52.27, 10.52),
    "de||hannover": (52.37, 9.73),
    "it||milano": (45.46, 9.19),
}


def _handler(request: httpx.Request) -> httpx.Response:
    p = request.url.params
    key = f"{p['countrycodes']}|{p.get('postalcode', '')}|{p.get('city', '').lower()}"
    hit = PLACES.get(key)
    return httpx.Response(200, json=[{"lat": str(hit[0]), "lon": str(hit[1])}] if hit else [])


def _car(
    db: Session, sid: str, city: str, country: str, postcode: str | None = None, **kw: object
) -> Listing:
    row = Listing(
        source="autoscout24_de", source_listing_id=sid, url="", make="BMW", model="320i",
        price_eur=5000, seller_type=SellerType.dealer, country=country, city=city,
        postcode=postcode, first_seen=NOW, last_seen=NOW, is_active=True, **kw,
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


def test_haversine_and_keys() -> None:
    assert round(haversine_km(52.09, 5.12, 52.37, 9.73)) == 315  # Utrecht - Hannover
    assert cache_key(" Braunschweig ", "38112", "DE") == "de|38112|braunschweig"
    assert cache_key(None, None, "DE") is None


def test_geocode_live_retries_on_city_alone() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    assert geocode_live(client, "Hannover", "99999", "DE") == (52.37, 9.73)
    assert geocode_live(client, "Nowhere", None, "DE") is None


def test_backlog_trip_and_nearby(db: Session) -> None:
    a = _car(db, "g1", "Braunschweig", "DE", "38112", spread_eur=500)
    b = _car(db, "g2", "Hannover", "DE", spread_eur=800)
    c = _car(db, "g3", "Milano", "IT", spread_eur=100)
    _car(db, "g4", "Atlantis", "DE")
    db.commit()
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    base = ("Utrecht", "", "NL")
    counts = geocode_backlog(db, client, limit=10, sleep=lambda _: None, base=base)
    assert counts == {"resolved": 4, "unknown": 1, "failed": 0}
    assert db.query(GeoCache).count() == 5
    # cached: a second run makes no requests
    assert geocode_backlog(db, client, limit=10, sleep=lambda _: None, base=base)["resolved"] == 0
    t = trip(db, a, base, TRAVEL)
    assert t is not None and 450 < t["km"] < 500 and t["tolls_eur"] == 0
    assert t["total_eur"] == round(t["km"] * 2 * 0.16)
    assert trip(db, c, base, TRAVEL)["tolls_eur"] == 90
    assert trip(db, db.get(Listing, 4), base, TRAVEL) is None  # unknown place
    near = nearby(db, a, [b, c], radius_km=100)
    assert [(x.id, km) for x, km in near] == [(b.id, 55)]


def test_nearby_listings_uses_the_geocode_cache(db: Session) -> None:
    from app.geo import nearby_listings

    a = _car(db, "n1", "Braunschweig", "DE", "38112", spread_eur=500)
    b = _car(db, "n2", "Hannover", "DE", spread_eur=800)
    c = _car(db, "n3", "Hannover", "DE", spread_eur=-50)
    d = _car(db, "n4", "Milano", "IT", spread_eur=900)
    for key, (lat, lon) in PLACES.items():
        country = key.split("|")[0].upper()
        db.add(GeoCache(key=key, country=country, lat=lat, lon=lon, resolved_at=NOW))
    db.commit()
    here, rows = nearby_listings(db, a, 100)
    assert here is not None and [(x.id, km) for x, km in rows] == [(b.id, 55)]
    _, rows = nearby_listings(db, a, 100, positive_only=False)
    assert sorted(x.id for x, _ in rows) == sorted([b.id, c.id])
    _, rows = nearby_listings(db, a, 1000)
    assert d.id in [x.id for x, _ in rows]
    _, rows = nearby_listings(db, a, 25)
    assert rows == []
    unknown = _car(db, "n5", "Atlantis", "DE")
    db.commit()
    assert nearby_listings(db, unknown, 100) == (None, [])


def test_nearby_condition_filters(db: Session) -> None:
    from app.insights.service import apply_rules
    from app.web.routes.extras import _passes_condition

    x = _car(db, "cf", "Hannover", "DE", spread_eur=300)
    x.description = "Unfallfrei, Steuerkette erneuert, Scheckheftgepflegt."
    apply_rules(x, NOW)
    db.flush()
    assert _passes_condition(x, ["accident_free", "chain_replaced", "full_history", "papers_ok"])
    x.repair_cost_eur = 900
    assert not _passes_condition(x, ["no_defects"])
    y = _car(db, "cg", "Hannover", "DE")
    y.description = "Unfallwagen, ohne Brief."
    apply_rules(y, NOW)
    assert not _passes_condition(y, ["accident_free"]) and not _passes_condition(y, ["papers_ok"])
