from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Listing, SavedSearch, ScrapeRun, ScrapeStatus, SellerType, User
from app.normalize import NormalizedListing, clean_text
from app.storage import is_same_car
from app.worker.jobs import _apply_watchlist, cleanup_interrupted_runs, due_search_ids

NOW = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)


def test_lone_surrogates_are_dropped_everywhere() -> None:
    bad = "Km 🔹 versnellingsbak \ud83d manueel"
    assert clean_text(bad) == "Km 🔹 versnellingsbak  manueel"
    n = NormalizedListing(
        source="x",
        source_listing_id="1",
        url="u",
        make="VW",
        model="golf",
        price_eur=1,
        seller_type=SellerType.dealer,
        country="NL",
        description=bad,
        variant="\ud83d solo",
        raw={"search": {"title": bad, "nested": [bad]}},
    )
    assert "🔹" in n.description and "\ud83d " not in n.description  # type: ignore[operator]
    assert (
        n.variant == " solo"
        and "\ud83d " not in n.raw["search"]["title"]
        and "\ud83d " not in n.raw["search"]["nested"][0]
    )
    n.raw["search"]["title"].encode("utf-8")  # encodable, so Postgres jsonb accepts it


def test_watchlist_stamp_and_grouping() -> None:
    items = [
        NormalizedListing(
            source="kleinanzeigen",
            source_listing_id="1",
            url="u",
            make="Vw",
            model="",
            price_eur=1,
            seller_type=SellerType.private,
            country="DE",
        )
    ]
    out = _apply_watchlist({"make_label": "Volkswagen", "model_key": "vw-golf-6"}, items)
    assert out[0].make == "Volkswagen" and out[0].model_key == "vw-golf-6"
    a = Listing(
        source="autoscout24_de",
        source_listing_id="a",
        url="u",
        make="Volkswagen",
        model="1.4 TSI",
        model_key="vw-golf-6",
        price_eur=5000,
        mileage_km=100_000,
        first_registration=datetime(2011, 3, 1).date(),
        seller_type=SellerType.dealer,
        country="DE",
        first_seen=NOW,
        last_seen=NOW,
        raw={},
    )
    b = Listing(
        source="kleinanzeigen",
        source_listing_id="b",
        url="u",
        make="Volkswagen",
        model="",
        model_key="vw-golf-6",
        price_eur=5050,
        mileage_km=101_000,
        first_registration=datetime(2011, 3, 1).date(),
        seller_type=SellerType.private,
        country="DE",
        first_seen=NOW,
        last_seen=NOW,
        raw={},
    )
    assert is_same_car(a, b)
    b.model_key = "vw-golf-7"
    assert not is_same_car(a, b)


def test_due_order_is_fair_and_interrupted_runs_close(db: Session) -> None:
    u = User(username="u", password_hash="x")
    old = SavedSearch(name="old", owner=u, source="autoscout24_de", interval_minutes=30)
    fresh = SavedSearch(name="fresh", owner=u, source="autoscout24_de", interval_minutes=30)
    never = SavedSearch(name="never", owner=u, source="kleinanzeigen", interval_minutes=60)
    comps = SavedSearch(
        name="comps", owner=u, source="marktplaats", role="comparables", interval_minutes=720
    )
    db.add_all([u, old, fresh, never, comps])
    db.flush()
    db.add_all(
        [
            ScrapeRun(
                source="autoscout24_de",
                saved_search_id=old.id,
                started_at=NOW - timedelta(hours=3),
                status=ScrapeStatus.ok,
            ),
            ScrapeRun(
                source="autoscout24_de",
                saved_search_id=fresh.id,
                started_at=NOW - timedelta(minutes=10),
                status=ScrapeStatus.ok,
            ),
            ScrapeRun(
                source="autoscout24_de",
                saved_search_id=fresh.id,
                started_at=NOW - timedelta(hours=5),
                status=ScrapeStatus.running,
            ),
        ]
    )
    db.commit()
    # never-run first (comparables before deals), then longest waiting
    assert due_search_ids(db, NOW, due_only=True) == [comps.id, never.id, old.id]
    assert due_search_ids(db, NOW, due_only=False) == [comps.id, never.id, old.id, fresh.id]
    assert cleanup_interrupted_runs(db, NOW, max_age_minutes=120) == 1
    db.commit()
    r = db.scalar(select(ScrapeRun).where(ScrapeRun.error.isnot(None)))
    assert r is not None and r.status is ScrapeStatus.failed and "interrupted" in (r.error or "")


def test_one_unparseable_ad_does_not_fail_the_run() -> None:
    from datetime import UTC, datetime

    import pytest

    from app.normalize import RawListing
    from app.scrapers import ParseError, get_scraper
    from app.worker.jobs import _parse_all

    scraper = get_scraper("marktplaats")
    now = datetime.now(UTC)

    def ad(sid: str, cents: int) -> RawListing:
        data = {"itemId": sid, "title": "BMW 320i", "priceInfo": {"priceCents": cents}}
        data.update({"attributes": [], "location": {}})
        return RawListing(
            source="marktplaats", source_listing_id=sid, url="u", fetched_at=now, data=data
        )

    good, bad = ad("m1", 500_000), ad("m2", 0)
    items = _parse_all(scraper, [good, bad])
    assert [i.source_listing_id for i in items] == ["m1"]
    with pytest.raises(ParseError):
        _parse_all(scraper, [bad])
    assert _parse_all(scraper, []) == []


def test_blocked_source_cools_down(db: Session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    import httpx

    from app.scrapers import PoliteClient
    from app.worker import jobs

    u = User(username="b", password_hash="x")
    s1 = SavedSearch(name="ka", owner=u, source="kleinanzeigen", source_params={"make": "bmw"})
    s2 = SavedSearch(name="as", owner=u, source="autoscout24_de", source_params={"path": "/x"})
    db.add_all([u, s1, s2])
    db.commit()
    jobs._SOURCE_COOLDOWN.clear()
    client = PoliteClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, text="gesperrt")),
        sleep=lambda _: None,
    )
    summary = jobs.run_saved_search(s1.id, client=client)
    assert summary.status is ScrapeStatus.failed and "403" in (summary.error or "")
    now = datetime.now(UTC)
    assert jobs.source_cooling("kleinanzeigen", now) and not jobs.source_cooling(
        "autoscout24_de", now
    )
    assert not jobs.source_cooling("kleinanzeigen", now + timedelta(hours=3))  # expired
    jobs._SOURCE_COOLDOWN.clear()
