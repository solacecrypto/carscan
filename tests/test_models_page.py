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
