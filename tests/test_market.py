from datetime import UTC, date, datetime, timedelta

from app.db import Comparable, SellerType
from app.market import compute
from app.scrapers.marktplaats import parse_listed_date

TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, tzinfo=UTC)


def test_parse_marktplaats_dates() -> None:
    assert parse_listed_date("Vandaag", TODAY) == TODAY
    assert parse_listed_date("Gisteren", TODAY) == TODAY - timedelta(days=1)
    assert parse_listed_date("Eergisteren", TODAY) == TODAY - timedelta(days=2)
    assert parse_listed_date("9 okt 26", TODAY) == date(2026, 10, 9)
    assert parse_listed_date("29 sep '26", TODAY) == date(2026, 9, 29)
    assert parse_listed_date("31 feb 26", TODAY) is None
    assert parse_listed_date(None, TODAY) is None


def _comp(i: int, **kw: object) -> Comparable:
    base = dict(
        id=i,
        source="marktplaats",
        source_listing_id=str(i),
        url="",
        make="BMW",
        model="320i",
        model_key="bmw-3er-e90",
        variant="BMW 320i Touring",
        gearbox="manual",
        price_eur=7000,
        seller_type=SellerType.private,
        first_seen=NOW - timedelta(days=10),
        last_seen=NOW,
        is_active=True,
    )
    return Comparable(**{**base, **kw})  # type: ignore[arg-type]


def test_fast_market_gets_a_good_grade_and_slow_market_a_bad_one() -> None:
    fast = [_comp(i, listed_at=TODAY - timedelta(days=5), demand="Many") for i in range(6)]
    m = compute(fast, "320i", "manual", TODAY)
    assert m is not None and m["grade"] == "A" and m["expected_days"] == 5
    assert m["expected_basis"] == "ad age" and m["n_close"] == 6
    keys = {s["key"]: s for s in m["signals"]}
    assert keys["sell_through"]["score"] is None  # not enough disappeared ads yet
    assert "6 for sale in NL, 6 same engine" in keys["supply"]["value"]

    slow = [_comp(i, listed_at=TODAY - timedelta(days=150), demand="Zero") for i in range(120)]
    m2 = compute(slow, "320i", "manual", TODAY)
    assert m2 is not None and m2["grade"] == "E"


def test_sell_through_switches_on_with_enough_gone_ads() -> None:
    comps = [_comp(i, listed_at=TODAY - timedelta(days=20), demand="Some") for i in range(10)]
    comps += [
        _comp(
            100 + i,
            is_active=False,
            first_seen=NOW - timedelta(days=40),
            last_seen=NOW - timedelta(days=40 - 12),
        )
        for i in range(25)
    ]
    m = compute(comps, "320i", "manual", TODAY)
    assert m is not None and m["expected_basis"] == "sell-through" and m["expected_days"] == 12
    assert next(s for s in m["signals"] if s["key"] == "sell_through")["score"] is not None


def test_no_active_comps_means_no_grade() -> None:
    assert compute([], "320i", None, TODAY) is None
    assert compute([_comp(1, is_active=False)], "320i", None, TODAY) is None
