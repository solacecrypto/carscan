from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.db import Listing, Notification, SavedSearch, Subscription, User, Watch
from app.notify import (
    favorite_events,
    matches,
    push,
    spread_events,
    starred_spreads,
    subscription_events,
    unread_count,
)
from app.storage.listings import UpsertResult

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)


def _car(db: Session, sid: str, **kw: object) -> Listing:
    base = dict(
        source="autoscout24_de", source_listing_id=sid, url="", make="BMW", model="320i",
        model_key="bmw-3er-e90", price_eur=6000, mileage_km=150_000, seller_type="dealer",
        country="DE", first_seen=NOW, last_seen=NOW, is_active=True, spread_eur=500,
    )  # fmt: skip
    row = Listing(**{**base, **kw})
    db.add(row)
    db.flush()
    return row


def test_favorite_events_and_dedupe(db: Session) -> None:
    u = User(username="n", password_hash="x")
    s = SavedSearch(name="s", owner=u, source="autoscout24_de")
    db.add_all([u, s])
    a = _car(db, "a", previous_price_eur=7000, price_eur=6500)
    b = _car(db, "b", price_eur=5000, previous_price_eur=4500)
    c = _car(db, "c", is_active=False)
    d = _car(db, "d")
    for x in (a, b, c, d):
        x.watches.append(Watch(user=u, created_at=NOW, target_price_eur=6500 if x is a else None))
    db.flush()
    result = UpsertResult()
    result.price_dropped.append((a, 7000))
    result.price_raised.append(b)
    result.reactivated_listings.append(d)
    assert favorite_events(db, result, [c], NOW) == 4
    kinds = sorted(n.kind for n in db.query(Notification))
    assert kinds == ["back", "gone", "price_up", "target"]
    assert unread_count(db, u.id) == 4
    # a second drop refreshes the unread entry instead of adding one
    result2 = UpsertResult()
    result2.price_dropped.append((a, 6500))
    favorite_events(db, result2, [], NOW + timedelta(hours=1))
    assert unread_count(db, u.id) == 4
    assert db.query(Notification).filter_by(kind="target").one().created_at == NOW + timedelta(
        hours=1
    )


def test_spread_crossings(db: Session) -> None:
    u = User(username="n", password_hash="x")
    db.add(u)
    a = _car(db, "a", spread_eur=-200)
    a.watches.append(Watch(user=u, created_at=NOW))
    db.flush()
    before = starred_spreads(db, "bmw-3er-e90")
    assert before == {a.id: -200}
    a.spread_eur = 900
    assert spread_events(db, before, NOW) == 1
    assert db.query(Notification).one().kind == "spread_up"
    a.spread_eur = 950  # still positive: nothing new
    assert spread_events(db, {a.id: 900}, NOW) == 0


def test_subscription_matching(db: Session) -> None:
    u = User(username="n", password_hash="x")
    db.add(u)
    golf = _car(
        db,
        "g",
        make="Volkswagen",
        model="Golf",
        model_key="vw-golf-7",
        variant="Golf GTI",
        price_eur=9000,
        spread_eur=800,
    )
    bmw = _car(db, "x", spread_eur=-100)
    assert matches({"model_keys": ["vw-golf-7"]}, golf) and not matches(
        {"model_keys": ["vw-golf-7"]}, bmw
    )
    assert matches({"query": "golf gti"}, golf) and not matches({"query": "golf r"}, golf)
    assert not matches({"max_price": 8000}, golf) and matches({"max_price": 9000}, golf)
    assert matches({"positive_only": True}, golf) and not matches({"positive_only": True}, bmw)
    assert not matches({"countries": ["IT"]}, golf) and matches({"countries": ["DE", "IT"]}, golf)
    db.add(
        Subscription(
            user=u, name="GTI", criteria={"query": "gti", "positive_only": True}, created_at=NOW
        )
    )
    db.flush()
    assert subscription_events(db, [golf, bmw], NOW) == 1
    n = db.query(Notification).one()
    assert n.kind == "wanted" and "GTI" in n.title and n.listing_id == golf.id
    sub = db.query(Subscription).one()
    assert sub.hits == 1 and sub.last_hit_at == NOW
    push(db, u.id, "note", "t", None, NOW)  # notifications without a car are fine too
    assert unread_count(db, u.id) == 2
