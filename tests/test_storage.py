from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Listing, PriceHistory, SavedSearch, SellerType, User
from app.normalize import NormalizedListing
from app.storage import deactivate_missing, upsert_listings

T0 = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


def _item(i: str, price: int, **kw: object) -> NormalizedListing:
    base = dict(
        source="autoscout24_de",
        source_listing_id=i,
        url=f"https://x/{i}",
        make="BMW",
        model="320i",
        price_eur=price,
        seller_type=SellerType.dealer,
        country="DE",
    )
    return NormalizedListing(**{**base, **kw})


def _search(db: Session) -> SavedSearch:
    u = User(username="u", password_hash="x")
    s = SavedSearch(name="e90", owner=u, source="autoscout24_de")
    db.add_all([u, s])
    db.flush()
    return s


def test_insert_then_unchanged_then_price_drop(db: Session) -> None:
    s = _search(db)
    r = upsert_listings(db, [_item("a", 5000), _item("b", 7000)], s, T0)
    assert len(r.new) == 2 and r.seen == 2
    db.commit()

    t1 = T0 + timedelta(minutes=30)
    r = upsert_listings(db, [_item("a", 5000), _item("b", 6500)], s, t1)
    assert (
        not r.new
        and r.unchanged == 1
        and [(x.source_listing_id, old) for x, old in r.price_dropped] == [("b", 7000)]
    )
    db.commit()

    b = db.scalar(select(Listing).where(Listing.source_listing_id == "b"))
    assert b is not None
    assert b.price_eur == 6500 and b.previous_price_eur == 7000 and b.price_changed_at == t1
    assert b.last_seen == t1 and b.first_seen == T0
    assert [h.price_eur for h in b.price_history] == [7000, 6500]
    assert db.scalar(
        select(PriceHistory).where(PriceHistory.listing_id == b.id, PriceHistory.price_eur == 6500)
    )


def test_deactivate_missing_and_reactivate(db: Session) -> None:
    s = _search(db)
    upsert_listings(db, [_item("a", 5000), _item("b", 7000)], s, T0)
    db.commit()

    t1 = T0 + timedelta(minutes=30)
    upsert_listings(db, [_item("a", 5000)], s, t1)
    stale = deactivate_missing(db, s, run_started_at=t1)
    db.commit()
    assert [x.source_listing_id for x in stale] == ["b"]
    b = db.scalar(select(Listing).where(Listing.source_listing_id == "b"))
    assert b is not None and not b.is_active

    t2 = t1 + timedelta(minutes=30)
    r = upsert_listings(db, [_item("a", 5000), _item("b", 7000)], s, t2)
    db.commit()
    db.refresh(b)
    assert r.reactivated == 1 and b.is_active and b.last_seen == t2


def test_detail_fields_survive_search_only_pass(db: Session) -> None:
    s = _search(db)
    rich = _item(
        "a",
        5000,
        description="nice car",
        co2_g_km=150,
        photos=["1", "2", "3"],
        raw={"search": {}, "detail": {"x": 1}},
    )
    upsert_listings(db, [rich], s, T0)
    db.commit()
    poor = _item("a", 5000, photos=["1"], raw={"search": {"y": 2}})
    upsert_listings(db, [poor], s, T0 + timedelta(minutes=30))
    db.commit()
    a = db.scalar(select(Listing).where(Listing.source_listing_id == "a"))
    assert a is not None
    assert a.description == "nice car" and a.co2_g_km == 150 and a.photos == ["1", "2", "3"]
    assert a.raw == {"search": {"y": 2}, "detail": {"x": 1}}


def test_costs_computed_on_upsert_and_recompute(db: Session) -> None:
    from datetime import date

    from app.storage import recompute_costs

    s = _search(db)
    item = _item("c", 6000, first_registration=date(2014, 10, 1), co2_g_km=158, fuel="petrol")
    upsert_listings(db, [item], s, datetime(2026, 10, 12, tzinfo=UTC))
    db.commit()
    c = db.scalar(select(Listing).where(Listing.source_listing_id == "c"))
    assert c is not None and c.bpm_eur == 1046 and c.landed_cost_eur == 6000 + 350 + 302 + 1046
    assert c.cost_breakdown["bpm"]["tariff_year"] == 2015 and c.cost_version == 1

    c.cost_version = 0
    db.commit()
    assert recompute_costs(db, datetime(2026, 10, 12, tzinfo=UTC), only_stale=True) == 1
    db.commit()
    db.refresh(c)
    assert c.cost_version == 1 and c.landed_cost_eur == 7698


def test_parse_filters_is_tolerant() -> None:
    from app.storage import parse_filters

    class P(dict):  # type: ignore[type-arg]
        def getlist(self, k: str) -> list[str]:
            v = self.get(k)
            return v if isinstance(v, list) else ([v] if v else [])

    f = parse_filters(
        P(
            {
                "price_min": "",
                "price_max": "8.000",
                "year_min": "x",
                "country": ["de", "BE"],
                "model": "VW Golf VI 1.2/1.4 TSI",
                "no_accident": "true",
                "new_hours": "168",
            }
        ),
        [("vw-golf-6", "VW Golf VI 1.2/1.4 TSI")],
    )
    assert f["price_min"] is None and f["price_max"] == 8000 and f["year_min"] is None
    assert (
        f["country"] == ["DE", "BE"]
        and f["model_key"] == "vw-golf-6"
        and f["no_accident"] is True
        and f["new_hours"] == 168
    )
    assert parse_filters(P({}), [])["country"] is None
