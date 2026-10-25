from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.db import ModelHistory
from app.market.history import model_series, snapshot_models, sparkline
from tests.test_deals import NOW, _listing


def test_snapshot_and_series(db: Session) -> None:
    _listing(db, "m1", model_key="bmw-3er-e90", spread_eur=1600)
    _listing(db, "m2", model_key="bmw-3er-e90", spread_eur=-200)
    _listing(db, "m3", model_key="vw-golf-6", spread_eur=None, nl_value_eur=None)
    db.commit()
    today = date(2026, 10, 16)
    assert snapshot_models(db, today) == 2
    assert snapshot_models(db, today) == 2  # same day: overwritten, not duplicated
    rows = {h.model_key: h for h in db.query(ModelHistory).all()}
    assert len(rows) == 2
    bmw = rows["bmw-3er-e90"]
    assert (bmw.n_active, bmw.n_valued, bmw.n_positive, bmw.median_spread_eur) == (2, 2, 1, 700)
    assert rows["vw-golf-6"].n_valued == 0 and rows["vw-golf-6"].median_spread_eur is None
    snapshot_models(db, today - timedelta(days=1))
    series = model_series(db)
    assert [h.day for h in series["bmw-3er-e90"]] == [today - timedelta(days=1), today]


def test_sparkline_points() -> None:
    assert sparkline([None]) == ""
    pts = sparkline([0, 10, 5], width=100, height=36)
    assert pts.startswith("0.0,34.0") and "50.0,2.0" in pts


def test_model_accuracy_from_a_bought_deal(db: Session) -> None:
    from app.db import DealStage, User
    from app.deals import create_deal, set_stage, update_actuals
    from app.deals.calibration import model_accuracy

    u = User(username="q", password_hash="x")
    db.add(u)
    lst = _listing(db, "d1", model_key="bmw-3er-e90")
    deal = create_deal(db, lst, u, NOW)
    set_stage(deal, DealStage.bought, u, NOW)
    update_actuals(
        deal,
        {"purchase_price_eur": 6000, "transport_eur": 350, "bpm_eur": 1200, "fees_eur": 302},
        u,
        NOW,
    )
    db.commit()
    acc = model_accuracy(db)
    assert acc["bmw-3er-e90"]["n"] == 1 and acc["bmw-3er-e90"]["landed_err_pct"] == 2.0
    assert acc["bmw-3er-e90"]["value_err_pct"] is None


def test_model_overview_and_detail(db: Session) -> None:
    from app.market.overview import buckets, model_detail, model_overview, overview_totals

    now = NOW + timedelta(days=1)  # the cars were first seen at NOW
    _listing(db, "o1", model_key="bmw-3er-e90", spread_eur=1600, country="DE")
    _listing(db, "o2", model_key="bmw-3er-e90", spread_eur=-300, country="BE", price_eur=9000)
    _listing(db, "o3", model_key="bmw-3er-e90", spread_eur=None, nl_value_eur=None, country="IT")
    _listing(db, "o4", model_key="bmw-3er-e90", spread_eur=2000, is_active=False)
    db.commit()
    models = {m.key: m for m in model_overview(db, now)}
    bmw = models["bmw-3er-e90"]
    assert bmw.label == "BMW E90/E91 318i/320i" and bmw.years == [2008, 2014]
    assert (bmw.n_active, bmw.n_valued, bmw.n_positive, bmw.positive_pct) == (3, 2, 1, 50)
    assert bmw.by_country == {"DE": 1, "BE": 1, "IT": 1, "SE": 0}
    assert bmw.median_spread_eur == 650 and bmw.best.spread_eur == 1600
    assert bmw.new_7d == 3 and bmw.gone_7d == 1 and bmw.history == []
    golf = models["vw-golf-6"]  # on the watchlist but without cars
    assert golf.n_active == 0 and golf.tracked and golf.median_spread_eur is None
    totals = overview_totals(list(models.values()))
    assert totals["cars"] == 3 and totals["positive"] == 1 and totals["best"] is bmw
    detail = model_detail(db, "bmw-3er-e90", now)
    assert detail is not None and [c.id for c in detail["top"]][0] == bmw.best.id
    assert sum(b["n"] for b in detail["price_hist"]) == 3
    assert detail["year_hist"] == [{"year": 2012, "n": 3, "pct": 100}]
    assert model_detail(db, "nope", now) is None
    assert buckets([100, 150, 900], 500) == [
        {"lo": 0, "hi": 500, "n": 2, "pct": 100},
        {"lo": 500, "hi": 1000, "n": 1, "pct": 50},
    ]
