from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.alerts.watch import check_watches
from app.db import Alert, Digest, Listing, SavedSearch, User, Watch
from app.digest import build_digest, render_text, store_digest
from app.storage.listings import UpsertResult

NOW = datetime(2026, 10, 16, 8, tzinfo=UTC)


def _setup(db: Session) -> tuple[User, SavedSearch, Listing, Listing]:
    u = User(username="w", password_hash="x")
    s = SavedSearch(name="e90", owner=u, source="autoscout24_de", alerts_enabled=True)
    a = Listing(
        source="autoscout24_de", source_listing_id="w1", url="", make="BMW", model="320i",
        price_eur=5000, previous_price_eur=6000, price_changed_at=NOW, seller_type="dealer",
        seller_name="Auto Müller", country="DE", first_seen=NOW - timedelta(hours=2), last_seen=NOW,
        is_active=True, spread_eur=1500, saved_searches=[s],
    )  # fmt: skip
    b = Listing(
        source="autoscout24_de", source_listing_id="w2", url="", make="BMW", model="318i",
        price_eur=4000, seller_type="dealer", seller_name="Auto Müller", country="DE",
        first_seen=NOW - timedelta(days=9), last_seen=NOW, is_active=False, saved_searches=[s],
    )  # fmt: skip
    db.add_all([u, s, a, b])
    db.flush()
    return u, s, a, b


def test_watch_alerts_on_target_price_and_when_gone(db: Session) -> None:
    u, s, a, b = _setup(db)
    a.watches.append(Watch(user=u, target_price_eur=5500, created_at=NOW))
    b.watches.append(Watch(user=u, created_at=NOW))
    db.flush()
    result = UpsertResult()
    result.price_dropped.append((a, 6000))
    alerts = check_watches(db, result, [b], NOW)
    assert sorted(x.kind for x in alerts) == ["gone", "watch"]
    assert a.watches[0].triggered_at == NOW and b.watches[0].triggered_at == NOW
    # a drop that stays above the target does not fire
    a.watches[0].target_price_eur, a.watches[0].triggered_at = 4000, None
    db.flush()
    assert check_watches(db, result, [], NOW) == [] and a.watches[0].triggered_at is None
    # the same kind is re-armed, not duplicated
    a.watches[0].target_price_eur = None
    again = check_watches(db, result, [], NOW + timedelta(hours=1))
    assert len(again) == 1 and db.query(Alert).filter_by(kind="watch").count() == 1
    assert again[0].sent_at is None


def test_digest_lists_new_cars_drops_and_flags(db: Session) -> None:
    from app.insights.service import apply_rules

    u, s, a, b = _setup(db)
    a.description = "Verkauf ohne Brief."
    apply_rules(a, NOW)
    db.flush()
    items = build_digest(db, NOW)
    assert items["n_new"] == 1
    assert [c["id"] for c in items["top_new"]] == [a.id]
    assert [c["id"] for c in items["price_drops"]] == [a.id]
    assert items["flagged"][0]["flags"] == ["papers missing"]
    text = render_text(items, "https://x", NOW)
    assert "Top new cars by spread" in text and "papers missing" in text and "€ 6.000" in text
    row = store_digest(db, NOW, "https://x")
    assert row.day == NOW.date() and store_digest(db, NOW, "https://x").id == row.id
    assert db.query(Digest).count() == 1
    assert "Nothing worth a look" in render_text(build_digest(db, NOW + timedelta(days=3)), "", NOW)


def test_catch_up_is_personal(db: Session) -> None:
    from app.db import DealStage, Notification, Watch
    from app.deals import create_deal, set_stage
    from app.digest import catch_up

    u, s, a, b = _setup(db)
    other = User(username="other", password_hash="x")
    db.add(other)
    db.flush()
    db.add(Watch(listing=a, user=u, created_at=NOW - timedelta(days=2)))
    h1 = NOW - timedelta(hours=1)
    n1 = Notification(user_id=u.id, kind="price_down", title="x: price dropped", body="was 7000")
    n1.listing, n1.created_at = a, h1
    n2 = Notification(user_id=u.id, kind="wanted", title="new car", listing=b, created_at=h1)
    n3 = Notification(
        user_id=other.id, kind="price_down", title="not mine", listing=a, created_at=NOW
    )
    n4 = Notification(user_id=u.id, kind="gone", title="old", listing=a)
    n4.created_at = NOW - timedelta(days=3)
    db.add_all([n1, n2, n3, n4])
    deal = create_deal(db, a, u, NOW - timedelta(days=10))
    set_stage(deal, DealStage.contacted, u, NOW - timedelta(days=9))
    create_deal(db, b, other, NOW)
    db.commit()
    cu = catch_up(db, u, NOW, 24)
    assert [x.id for x in cu["top_new"]] == [a.id] and [x.id for x in cu["drops"]] == [a.id]
    assert cu["counts"]["new"] == 1 and cu["counts"]["drops"] == 1
    assert len(cu["favorites"]) == 1 and [n.kind for n in cu["favorites"][0]["events"]] == [
        "price_down"
    ]
    assert [n.listing_id for n in cu["wanted"]] == [b.id] and cu["counts"]["wanted"] == 1
    assert [r["deal"].id for r in cu["deals"]] == [deal.id] and cu["deals"][0]["stale"]
    assert cu["counts"]["deals_attention"] == 1 and cu["counts"]["unread"] == 2
    assert cu["movers_up"] == [] and cu["movers_down"] == []
    wide = catch_up(db, u, NOW, 168)
    assert [n.kind for n in wide["favorites"][0]["events"]] == ["price_down", "gone"]
