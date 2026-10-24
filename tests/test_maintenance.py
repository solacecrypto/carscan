from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.db import (
    Comparable,
    Listing,
    Notification,
    SavedSearch,
    ScrapeRun,
    ScrapeStatus,
    SellerType,
    User,
)
from app.maintenance import prune
from app.worker.jobs import due_search_ids

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def _car(db: Session, sid: str, days_gone: int | None) -> Listing:
    row = Listing(
        source="autoscout24_de", source_listing_id=sid, url="", make="BMW", model="320i",
        price_eur=5000, seller_type=SellerType.dealer, country="DE",
        first_seen=NOW - timedelta(days=60), last_seen=NOW - timedelta(days=days_gone or 0),
        is_active=days_gone is None, raw={"search": {"big": "x" * 1000}}, photos=["a", "b", "c"],
    )  # fmt: skip
    db.add(row)
    return row


def _run(db: Session, search: SavedSearch, days_ago: int) -> None:
    db.add(
        ScrapeRun(
            source=search.source,
            saved_search_id=search.id,
            started_at=NOW - timedelta(days=days_ago),
            status=ScrapeStatus.ok,
        )  # fmt: skip
    )


def test_prune_trims_old_rows_only(db: Session) -> None:
    live = _car(db, "live", None)
    fresh = _car(db, "fresh", 5)
    old = _car(db, "old", 45)
    u = User(username="m", password_hash="x")
    s = SavedSearch(name="s", owner=u, source="autoscout24_de")
    db.add_all([u, s])
    db.flush()
    _run(db, s, 100)
    _run(db, s, 10)
    db.add(
        Notification(user_id=u.id, kind="system", title="t", created_at=NOW - timedelta(days=40))
    )
    db.add(
        Comparable(
            source="marktplaats",
            source_listing_id="c",
            url="",
            make="BMW",
            model="320i",
            price_eur=1,
            seller_type=SellerType.dealer,
            first_seen=NOW - timedelta(days=90),
            last_seen=NOW - timedelta(days=50),
            is_active=False,
            raw={"search": {"x": 1}},
            photos=["p", "q"],
        )  # fmt: skip
    )
    db.commit()
    counts = prune(db, NOW)
    assert counts == {
        "listings_trimmed": 1,
        "comparables_trimmed": 1,
        "runs_deleted": 1,
        "notifications_deleted": 1,
    }
    db.expire_all()
    assert old.raw == {} and old.photos == ["a"]
    assert live.raw != {} and fresh.raw != {} and len(live.photos) == 3
    assert prune(db, NOW)["listings_trimmed"] == 0  # idempotent


def test_dormant_mode_keeps_only_weekly_comparables(db: Session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import Settings

    u = User(username="d", password_hash="x")
    deals = SavedSearch(name="d", owner=u, source="autoscout24_de", interval_minutes=30)
    comps = SavedSearch(
        name="c", owner=u, source="marktplaats", role="comparables", interval_minutes=720
    )
    db.add_all([u, deals, comps])
    db.flush()
    _run(db, comps, 2)
    db.commit()

    def settings(dormant: bool) -> Settings:
        return Settings(database_url="postgresql://x/y", secret_key="s", dormant=dormant)

    monkeypatch.setattr("app.config.get_settings", lambda: settings(True))
    assert due_search_ids(db, NOW, True) == []  # ran 2 days ago: not due for a week
    assert due_search_ids(db, NOW + timedelta(days=6), True) == [comps.id]
    monkeypatch.setattr("app.config.get_settings", lambda: settings(False))
    assert sorted(due_search_ids(db, NOW, True)) == sorted([deals.id, comps.id])
