import re
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import app as cli
from app.db import Alert, AlertStatus, Deal, DealStage, Listing, SavedSearch, SellerType, User
from app.deals import calibrate, create_deal, set_stage, sync_from_alert_status, update_actuals
from app.web.main import app
from app.web.security import hash_password

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _listing(db: Session, sid: str = "a", **kw: object) -> Listing:
    base = dict(
        source="autoscout24_de",
        source_listing_id=sid,
        url=f"https://x/{sid}",
        make="BMW",
        model="320i",
        body_type="estate",
        price_eur=6000,
        mileage_km=150_000,
        first_registration=date(2012, 5, 1),
        seller_type=SellerType.dealer,
        country="DE",
        first_seen=NOW,
        last_seen=NOW,
        landed_cost_eur=7700,
        bpm_eur=1046,
        cost_breakdown={
            "purchase_eur": 6000,
            "transport_eur": 350,
            "fees_eur": 302,
            "bpm_eur": 1046,
            "total_eur": 7700,
        },
        nl_value_eur=9300,
        spread_eur=1600,
        nl_value_breakdown={"asking_median_eur": 10000, "nearest": []},
    )
    row = Listing(**{**base, **kw})
    db.add(row)
    db.flush()
    return row


def test_create_snapshot_stage_and_actuals(db: Session) -> None:
    u = User(username="q", password_hash="x")
    db.add(u)
    lst = _listing(db)
    deal = create_deal(db, lst, u, NOW)
    assert create_deal(db, lst, u, NOW) is deal
    assert (
        deal.stage is DealStage.spotted
        and deal.est_landed_cost_eur == 7700
        and deal.est_nl_asking_median_eur == 10000
    )
    assert (
        deal.est_transport_eur == 350
        and deal.est_fees_eur == 302
        and deal.events[0].kind == "created"
    )

    assert set_stage(deal, DealStage.bought, u, NOW + timedelta(days=3)) and deal.bought_at == date(
        2026, 10, 15
    )
    assert not set_stage(deal, DealStage.bought, u, NOW)
    changes = update_actuals(
        deal,
        {
            "purchase_price_eur": "5.500",
            "transport_eur": "300",
            "bpm_eur": "1100",
            "fees_eur": "",
            "notes": "nice",
        },
        u,
        NOW,
    )
    assert (
        changes["purchase_price_eur"] == [None, 5500]
        and "fees_eur" not in changes
        and deal.notes == "nice"
    )
    assert deal.actual_landed_cost_eur == 5500 + 300 + 1100 and deal.profit_eur is None
    set_stage(deal, DealStage.listed, u, NOW + timedelta(days=10))
    update_actuals(
        deal, {"repairs_eur": "400", "sale_price_eur": "9200", "sold_at": "2026-11-05"}, u, NOW
    )
    set_stage(deal, DealStage.sold, u, NOW + timedelta(days=20))
    assert deal.sold_at == date(2026, 11, 5) and deal.listed_at == date(2026, 10, 22)
    assert (
        deal.total_cost_eur == 6900 + 400
        and deal.profit_eur == 9200 - 7300
        and deal.days_to_sell == 14
    )
    assert [e.kind for e in deal.events] == [
        "created",
        "stage",
        "actuals",
        "stage",
        "actuals",
        "stage",
    ]


def test_alert_status_sync(db: Session) -> None:
    u = User(username="q", password_hash="x")
    db.add(u)
    lst = _listing(db)
    deal = create_deal(db, lst, u, NOW)
    sync_from_alert_status(db, lst.id, "claimed", u, NOW)
    assert deal.stage is DealStage.spotted
    sync_from_alert_status(db, lst.id, "contacted", u, NOW)
    assert deal.stage is DealStage.contacted and deal.events[-1].note == "via alert status"
    sync_from_alert_status(db, lst.id, "passed", u, NOW)
    assert deal.stage is DealStage.dropped
    sync_from_alert_status(db, 999_999, "contacted", u, NOW)  # no deal: no-op


def test_calibration_report(db: Session) -> None:
    u = User(username="q", password_hash="x")
    db.add(u)
    for i, (buy, transport, bpm, sale) in enumerate(
        [(5500, 300, 1100, 9200), (5800, 400, 950, 9000), (6000, 380, None, None)]
    ):
        lst = _listing(db, sid=f"c{i}")
        deal = create_deal(db, lst, u, NOW)
        set_stage(deal, DealStage.bought, u, NOW)
        update_actuals(
            deal,
            {
                "purchase_price_eur": buy,
                "transport_eur": transport,
                "bpm_eur": bpm or "",
                "fees_eur": 310,
            },
            u,
            NOW,
        )
        if sale:
            set_stage(deal, DealStage.listed, u, NOW + timedelta(days=5))
            update_actuals(deal, {"sale_price_eur": sale}, u, NOW)
            set_stage(deal, DealStage.sold, u, NOW + timedelta(days=25))
    db.commit()
    rep = calibrate(db)
    assert rep.deals_bought == 3 and rep.deals_sold == 2
    by = {m.name: m for m in rep.metrics}
    assert (
        by["transport DE"].n == 3
        and by["transport DE"].actual == 380
        and by["transport DE"].suggestion == "transport.DE: 380"
    )
    assert (
        by["BPM (actual ÷ estimate)"].n == 2
        and "within 10%" in by["BPM (actual ÷ estimate)"].suggestion
    )
    assert by["RDW and fees"].actual == 310
    assert (
        by["haircut (asking → sale)"].n == 2 and by["haircut (asking → sale)"].actual == 9.0
    )  # median(9200,9000)/10000
    assert by["days to sell"].actual == 20 and by["profit per sold car"].n == 2
    out = CliRunner().invoke(cli, ["calibrate"]).output
    assert "3 bought, 2 sold" in out and "transport.DE: 380" in out


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m
    return m.group(1)


def test_web_deal_flow(db: Session) -> None:
    u = User(username="q", password_hash=hash_password("pw12345678"))
    s = SavedSearch(name="e90", owner=u, source="autoscout24_de")
    db.add_all([u, s])
    lst = _listing(db, saved_searches=[s])
    db.commit()
    c = TestClient(app, base_url="http://localhost")
    c.post(
        "/login",
        data={"username": "q", "password": "pw12345678", "csrf_token": _csrf(c.get("/login").text)},
        follow_redirects=False,
    )
    page = c.get(f"/listings/{lst.id}").text
    assert "Track as deal" in page
    token = _csrf(page)
    r = c.post("/deals", data={"csrf_token": token, "listing_id": lst.id}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/deals/")
    deal_url = r.headers["location"]
    assert "Deal: spotted · q" in c.get(f"/listings/{lst.id}").text
    page = c.get(deal_url).text
    assert "Estimate vs actual" in page and "€ 7.700" in page and "tracked as deal" in page
    assert (
        c.post(
            f"{deal_url}/stage",
            data={"csrf_token": token, "stage": "bought"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    r = c.post(
        f"{deal_url}/actuals",
        data={
            "csrf_token": token,
            "purchase_price_eur": "5500",
            "transport_eur": "300",
            "bpm_eur": "1000",
            "fees_eur": "300",
            "bought_at": "2026-10-13",
            "notes": "deal!",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = c.get(deal_url).text
    assert "€ 7.100" in page and "deal!" in page and "purchase_price_eur" in page
    deals_page = c.get("/deals").text
    assert "bought" in deals_page and "actual € 7.100" in deals_page
    assert "Pick up the car" in deals_page and "→ imported" in deals_page
    assert "€ 7.100 invested" in deals_page  # summary tile: cars owned
    # owner filter, closed section, tracking by link
    assert "actual € 7.100" in c.get("/deals?owner=q").text
    assert "No open deals for nobody" in c.get("/deals?owner=nobody").text
    assert "0 sold, 0 dropped" in c.get("/deals?show_closed=true").text
    r = c.post(
        "/deals",
        data={"csrf_token": token, "listing_id": f"http://localhost/listings/{lst.id}"},
        follow_redirects=False,
    )
    assert r.headers["location"] == deal_url  # same car → same deal
    assert c.post("/deals", data={"csrf_token": token, "listing_id": "x"}).status_code == 404
    # quick advance from the board goes back to the board
    r = c.post(
        f"{deal_url}/stage",
        data={"csrf_token": token, "stage": "imported"},
        headers={"referer": "http://localhost/deals"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/deals"
    # hand the deal to another user
    other = User(username="w", password_hash="x")
    db.add(other)
    db.commit()
    r = c.post(
        f"{deal_url}/owner",
        data={"csrf_token": token, "owner_id": other.id},
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = c.get(deal_url).text
    assert "handed over from q to w" in page and "Our offer" in page
    # drop with a reason, then it shows under closed
    r = c.post(
        f"{deal_url}/stage",
        data={"csrf_token": token, "stage": "dropped", "note": "too rusty"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "too rusty" in c.get(deal_url).text
    closed = c.get("/deals?show_closed=true").text
    assert "1 dropped" in closed and "No open deals" in closed
    r = c.post(
        f"{deal_url}/stage", data={"csrf_token": token, "stage": "sold"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert (
        c.post(f"{deal_url}/stage", data={"csrf_token": token, "stage": "nope"}).status_code == 400
    )
    assert (
        c.post(
            f"{deal_url}/actuals", data={"csrf_token": token, "bought_at": "not-a-date"}
        ).status_code
        == 400
    )
    assert "Calibration" in c.get("/calibration").text
    # alert status set on the website moves the deal only for contacted/passed
    r = c.post(
        f"/listings/{lst.id}/status",
        data={"status": "contacted", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    deal = db.scalar(select(Deal))
    assert deal is not None and deal.stage is DealStage.sold  # already past contacted: unchanged
    assert db.scalar(select(Alert)).status is AlertStatus.contacted  # type: ignore[union-attr]


def test_pickup_checklist(db: Session) -> None:
    from app.deals import checklist_for, create_deal

    u = User(username="c", password_hash="x")
    db.add(u)
    deal = create_deal(db, _listing(db, sid="chk"), u, NOW)
    items = checklist_for(deal)
    assert [k for k, _l, _d in items][:3] == ["teil1", "teil2", "tuv"] and not any(
        d for *_, d in items
    )
    deal.checklist = {"teil1": True, "tuv": True}
    assert [d for *_, d in checklist_for(deal)][:3] == [True, False, True]


def test_next_action_stale_and_summary(db: Session) -> None:
    from app.deals import is_stale, next_action, next_stage, summary

    u = User(username="n", password_hash="x")
    db.add(u)
    d = create_deal(db, _listing(db, sid="n1"), u, NOW)
    assert next_stage(d) is DealStage.contacted
    assert next_action(d).startswith("Contact the seller")
    assert not is_stale(d, NOW + timedelta(days=3)) and is_stale(d, NOW + timedelta(days=4))
    assert d.expected_profit_eur == 1600
    update_actuals(d, {"offer_eur": "5500"}, u, NOW)
    assert d.expected_profit_eur == 2100  # cheaper purchase than the ask → more expected
    set_stage(d, DealStage.bought, u, NOW)
    assert "checklist items still open" in next_action(d)
    update_actuals(
        d, {"purchase_price_eur": "5500", "transport_eur": "300", "bpm_eur": "1000"}, u, NOW
    )
    assert d.total_cost_eur == 6800 and d.expected_profit_eur == 9300 - 6800
    sold = create_deal(db, _listing(db, sid="n2"), u, NOW)
    set_stage(sold, DealStage.sold, u, NOW)
    update_actuals(
        sold,
        {
            "purchase_price_eur": "5000",
            "sale_price_eur": "8000",
            "bought_at": "2026-09-01",
            "sold_at": "2026-09-21",
        },
        u,
        NOW,
    )
    assert sold.margin_pct == 60 and sold.profit_eur == 3000
    dropped = create_deal(db, _listing(db, sid="n3"), u, NOW)
    set_stage(dropped, DealStage.dropped, u, NOW)
    assert next_stage(dropped) is None and next_action(dropped) == "Dropped."
    s = summary([d, sold, dropped], NOW)
    assert s["open"] == 1 and s["owned"] == 1 and s["invested_eur"] == 6800
    assert s["expected_profit_eur"] == 2500 and s["realised_profit_eur"] == 3000
    assert s["sold"] == 1 and s["dropped"] == 1 and s["hit_rate_pct"] == 50
    assert s["avg_days_to_sell"] == 20 and s["stale"] == 0


def test_deal_reminders(db: Session) -> None:
    from app.db.models import Notification
    from app.deals.reminders import deal_reminders

    u = User(username="r", password_hash="x")
    db.add(u)
    fresh = create_deal(db, _listing(db, sid="r1"), u, NOW)
    stuck = create_deal(db, _listing(db, sid="r2"), u, NOW - timedelta(days=10))
    gone = create_deal(db, _listing(db, sid="r3", is_active=False), u, NOW)
    cheaper = create_deal(db, _listing(db, sid="r4"), u, NOW)
    cheaper.listing.price_eur = 5500
    bought = create_deal(db, _listing(db, sid="r5", is_active=False), u, NOW)
    set_stage(bought, DealStage.bought, u, NOW)  # ad gone after buying is normal: no reminder
    db.flush()
    assert deal_reminders(db, NOW) == 3
    rows = list(db.scalars(select(Notification).order_by(Notification.id)))
    assert [n.kind for n in rows] == ["deal"] * 3
    titles = " | ".join(n.title for n in rows)
    assert "10 days in 'spotted'" in titles and "ad is gone" in titles
    assert "asking price dropped to € 5.500" in titles
    assert all(n.url.startswith("/deals/") for n in rows)
    assert {n.listing_id for n in rows} == {stuck.listing_id, gone.listing_id, cheaper.listing_id}
    assert fresh.listing_id not in {n.listing_id for n in rows}
    # a second run the same day refreshes instead of duplicating
    assert deal_reminders(db, NOW) == 3
    assert db.scalar(select(Notification).where(Notification.id > rows[-1].id)) is None
