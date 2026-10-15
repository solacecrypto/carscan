from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Comparable, Listing, SavedSearch, ScrapeStatus, SellerType, User
from app.scrapers import PoliteClient
from app.valuation import recompute_values
from app.valuation.engine import (
    Comp,
    Target,
    ValuationConfig,
    dedup,
    engine_token,
    estimate_value,
)
from app.worker.jobs import due_search_ids, run_saved_search

FIX = Path(__file__).parent / "fixtures"
CFG = ValuationConfig(min_comps=3)


def _c(
    i: int, year: int, km: int, price: int, model: str = "320i", body: str | None = "sedan"
) -> Comp:
    return Comp(
        id=i, source="x", model=model, body_type=body, year=year, mileage_km=km, price_eur=price
    )


def test_estimate_median_with_mileage_adjustment_and_haircut() -> None:
    comps = [
        _c(1, 2012, 150_000, 8000),
        _c(2, 2012, 170_000, 7600),
        _c(3, 2013, 130_000, 8400),
        _c(4, 2012, 600_000, 1000),
    ]
    est = estimate_value(Target("320i", "sedan", 2012, 160_000), comps, CFG)
    assert est is not None
    # adjusted: 8000-200=7800, 7600+200=7800, 8400-600=7800 (600k car outside the mileage window)
    assert est.n == 3 and est.asking_median_eur == 7800 and est.value_eur == round(7800 * 0.93)
    assert est.year_window == 1 and est.mileage_window_km == 40_000
    assert [n["id"] for n in est.nearest] == [1, 2, 3]


def test_windows_widen_until_enough_comps() -> None:
    comps = [_c(1, 2012, 100_000, 9000), _c(2, 2015, 100_000, 12000), _c(3, 2009, 100_000, 6000)]
    assert (
        estimate_value(
            Target("320i", "sedan", 2012, 100_000),
            comps,
            ValuationConfig(min_comps=3, year_window_max=2),
        )
        is None
    )
    est = estimate_value(Target("320i", "sedan", 2012, 100_000), comps, CFG)
    assert est is not None and est.year_window == 3 and est.n == 3 and est.asking_median_eur == 9000


def test_model_and_body_filters_and_dedup() -> None:
    comps = [
        _c(1, 2012, 100_000, 9000),
        _c(2, 2012, 100_500, 9000),
        _c(3, 2012, 100_000, 9000, model="318i"),
        _c(4, 2012, 100_000, 9000, body="estate"),
        _c(5, 2012, 100_000, 9100, body=None),
    ]
    assert len(dedup(comps)) == 4  # 1 and 2 are the same car on two sites
    est = estimate_value(
        Target("320i", "sedan", 2012, 100_000), comps, ValuationConfig(min_comps=2)
    )
    assert est is not None and est.n == 2 and {n["id"] for n in est.nearest} == {1, 5}
    assert estimate_value(Target("320i", "sedan", None, 100_000), comps, CFG) is None


def test_comparables_run_and_value_recompute(db: Session) -> None:
    u = User(username="u", password_hash="x")
    comps_search = SavedSearch(
        name="gp",
        owner=u,
        source="gaspedaal",
        role="comparables",
        alerts_enabled=False,
        interval_minutes=360,
        source_params={
            "path": "/bmw/3-serie/benzine",
            "bmin": 2008,
            "bmax": 2014,
            "motor_regex": r"\b(318i|320i)\b",
            "bodies": ["Sedan", "Stationwagon"],
        },
    )
    deals = SavedSearch(
        name="deals", owner=u, source="autoscout24_de", source_params={"path": "/x"}
    )
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    car = Listing(
        source="autoscout24_de",
        source_listing_id="a",
        url="https://x/a",
        make="BMW",
        model="320i",
        body_type="estate",
        price_eur=5000,
        landed_cost_eur=6000,
        mileage_km=180_000,
        first_registration=datetime(2011, 5, 1).date(),
        seller_type=SellerType.dealer,
        country="DE",
        first_seen=now,
        last_seen=now,
        saved_searches=[deals],
    )
    db.add_all([u, comps_search, deals, car])
    db.commit()

    html = (FIX / "gaspedaal" / "search_page1.html").read_text()
    client = PoliteClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)),
        sleep=lambda _: None,
    )
    r = run_saved_search(comps_search.id, client=client)
    assert r.status is ScrapeStatus.ok and r.new > 0 and r.alerts_queued == 0
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Comparable)) == r.new
    assert (
        db.scalar(select(func.count()).select_from(Comparable).where(Comparable.model == "320i"))
        > 0
    )
    car = db.scalar(select(Listing).where(Listing.source_listing_id == "a"))
    assert car is not None and car.nl_value_eur and car.nl_value_n and car.nl_value_n >= 5
    assert car.spread_eur == car.nl_value_eur - 6000 and car.spread_pct == round(
        100 * car.spread_eur / 6000, 1
    )
    assert car.nl_value_breakdown["nearest"][0]["url"]

    # second identical run: nothing new, nothing gone; interval logic marks it not due
    r2 = run_saved_search(comps_search.id, client=client)
    assert r2.new == 0 and r2.deactivated == 0
    assert due_search_ids(db, datetime.now(UTC), due_only=True) == [deals.id]
    assert set(due_search_ids(db, datetime.now(UTC), due_only=False)) == {comps_search.id, deals.id}
    assert recompute_values(db, now) == 1


def test_cli_seed_comparables(db: Session) -> None:
    from typer.testing import CliRunner

    from app.cli import app as cli

    db.add(User(username="q", password_hash="x"))
    db.commit()
    r = CliRunner().invoke(cli, ["searches", "seed-comparables", "--owner", "q"])
    assert r.exit_code == 0 and "created 4" in r.output, r.output
    assert (
        "created 0"
        in CliRunner().invoke(cli, ["searches", "seed-comparables", "--owner", "q"]).output
    )
    out = CliRunner().invoke(cli, ["searches", "list"]).output
    assert "comparables" in out and "every 360 min" in out and "gaspedaal" in out


def test_engine_token_reads_common_designations() -> None:
    assert engine_token("318i", "BMW 318 i Touring") == "318i"
    assert engine_token("3er", "320i xDrive Touring 135 kW") == "320i"
    assert engine_token("c-klasse", "Mercedes C 180 CGI BlueEfficiency W204") == "c180"
    assert engine_token("golf", "Volkswagen Golf 1.4 TSI Highline 90kW") == "1.4tsi"
    assert engine_token("v60", "Volvo V60 T4 Summum") == "t4"
    assert engine_token("118 kW automaat") is None  # power is not an engine
    assert engine_token(None, None) is None
    # the first text wins: the variant is more specific than a site's model field
    assert engine_token("Avant 3.0 TFSI S4 quattro", "2.0 TFSI") == "3.0tfsi"
    assert engine_token(None, "2.0 TFSI") == "2.0tfsi"
    # diesel stays distinct from petrol
    assert engine_token("C 200 CDI T BlueEFFICIENCY") == "c200d"
    assert engine_token("C 200 BlueEFFICIENCY") == "c200"
    assert engine_token("Golf 2.0 TDI") == "2.0tdi"


def _cx(i: int, year: int, km: int, price: int, **kw: object) -> Comp:
    base = dict(id=i, source="x", model="320i", body_type="sedan", year=year, mileage_km=km)
    return Comp(price_eur=price, **{**base, **kw})  # type: ignore[arg-type]


def test_close_match_becomes_the_benchmark() -> None:
    target = Target("320i", "sedan", 2012, 160_000, engine="320i", gearbox="manual", power_kw=125)
    comps = [
        _cx(1, 2012, 150_000, 8000, engine="320i", gearbox="manual", power_kw=125),  # close
        _cx(2, 2012, 165_000, 9500, engine="320i", gearbox="automatic"),  # wrong gearbox
        _cx(3, 2013, 130_000, 8400, engine="318i", gearbox="manual"),  # wrong engine
        _cx(4, 2011, 200_000, 6000, engine="320i", gearbox="manual"),  # too many km
        _cx(5, 2012, 158_000, 7000, engine="320i", gearbox="manual", power_kw=125),  # closer
    ]
    est = estimate_value(target, comps, CFG)
    assert est is not None and est.method == "benchmark"
    assert est.benchmark is not None and est.benchmark["id"] == 5  # 2% km gap beats 6%
    assert (
        est.benchmark["perfect"] and est.benchmark["same_engine"] and est.benchmark["same_gearbox"]
    )
    assert est.benchmark["close_matches"] == 2
    # value = adjusted asking of the benchmark (7000 + (158000-160000)*0.02 = 6960) minus 7%
    assert est.value_eur == round(6960 * 0.93)
    assert est.median_value_eur is not None and est.median_value_eur != est.value_eur
    assert [n["id"] for n in est.nearest][:2] == [5, 1]  # close matches listed first
    assert est.nearest[0]["close"] and not est.nearest[-1]["close"]


def test_without_close_match_the_median_method_is_unchanged() -> None:
    target = Target("320i", "sedan", 2012, 160_000, engine="320i", gearbox="manual")
    comps = [
        _cx(1, 2012, 150_000, 8000, engine="320i", gearbox="automatic"),
        _cx(2, 2012, 170_000, 7600, engine="318i", gearbox="manual"),
        _cx(3, 2013, 130_000, 8400, engine="320i", gearbox="manual"),  # 30k km off
    ]
    est = estimate_value(target, comps, CFG)
    assert est is not None and est.method == "median" and est.benchmark is None
    assert est.value_eur == round(7800 * 0.93) and est.median_value_eur == est.value_eur


def test_outlier_match_is_not_used_as_benchmark() -> None:
    target = Target("320i", "sedan", 2012, 160_000, engine="320i", gearbox="manual")
    comps = [
        _cx(1, 2012, 158_000, 19_000, engine="320i", gearbox="manual"),  # mispriced "match"
        _cx(2, 2012, 150_000, 8000, engine="318i", gearbox="manual"),
        _cx(3, 2012, 170_000, 7600, engine="318i", gearbox="manual"),
        _cx(4, 2013, 130_000, 8400, engine="318i", gearbox="manual"),
    ]
    est = estimate_value(target, comps, CFG)
    assert est is not None and est.method == "median" and est.benchmark is None
    # unknown engine on the target: never a close match
    est = estimate_value(Target("320i", "sedan", 2012, 160_000), comps, CFG)
    assert est is not None and est.method == "median"
