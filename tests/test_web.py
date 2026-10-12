import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Alert, AlertStatus, Listing, PriceHistory, SavedSearch, SellerType, User
from app.web.main import app
from app.web.ratelimit import login_limiter
from app.web.security import hash_password

NOW = datetime.now(UTC)


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token not in page"
    return m.group(1)


@pytest.fixture
def seeded(db: Session) -> dict:
    u = User(username="q", password_hash=hash_password("pw12345678"))
    s = SavedSearch(name="e90", owner=u, source="autoscout24_de", source_params={"path": "/x"})
    a = Listing(
        source="autoscout24_de",
        source_listing_id="a",
        url="https://x/a",
        make="BMW",
        model="320i",
        variant="320i Touring",
        price_eur=6000,
        previous_price_eur=7000,
        price_changed_at=NOW,
        mileage_km=150000,
        first_registration=datetime(2010, 5, 1).date(),
        gearbox="manual",
        seller_type=SellerType.dealer,
        seller_name="Dealer GmbH",
        country="DE",
        city="Berlin",
        photos=["https://img/1.webp"],
        landed_cost_eur=7700,
        bpm_eur=1046,
        nl_value_eur=9300,
        nl_value_n=7,
        spread_eur=1600,
        spread_pct=20.8,
        nl_value_breakdown={
            "value_eur": 9300,
            "asking_median_eur": 10000,
            "asking_q1_eur": 9500,
            "asking_q3_eur": 10500,
            "n": 7,
            "year_window": 1,
            "mileage_window_km": 40000,
            "haircut_pct": 7.0,
            "km_rate_eur": 0.02,
            "nearest": [],
        },
        cost_breakdown={
            "purchase_eur": 6000,
            "transport_eur": 350,
            "bpm_eur": 1046,
            "fees_eur": 302,
            "total_eur": 7700,
            "config_version": 1,
            "fees": {},
            "bpm": {
                "bruto_eur": 7975,
                "tariff_year": 2015,
                "depreciation_pct": 86.89,
                "co2_g_km": 158,
                "co2_estimated": False,
                "notes": [],
            },
        },
        first_seen=NOW - timedelta(hours=2),
        last_seen=NOW,
        price_history=[
            PriceHistory(price_eur=7000, seen_at=NOW - timedelta(days=1)),
            PriceHistory(price_eur=6000, seen_at=NOW),
        ],
        saved_searches=[s],
    )
    b = Listing(
        source="autoscout24_de",
        source_listing_id="b",
        url="https://x/b",
        make="BMW",
        model="318i",
        price_eur=4000,
        mileage_km=220000,
        first_registration=datetime(2008, 1, 1).date(),
        gearbox="automatic",
        seller_type=SellerType.private,
        country="DE",
        first_seen=NOW - timedelta(days=3),
        last_seen=NOW,
        saved_searches=[s],
        is_active=False,
    )
    db.add_all([u, s, a, b])
    db.commit()
    login_limiter._hits.clear()
    return {"user": u, "search": s, "a": a, "b": b}


@pytest.fixture
def client(seeded: dict) -> TestClient:
    c = TestClient(app, base_url="http://localhost")
    page = c.get("/login").text
    r = c.post(
        "/login",
        data={"username": "q", "password": "pw12345678", "csrf_token": _csrf(page)},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    return c


def test_pages_require_login(db: Session) -> None:
    c = TestClient(app, base_url="http://localhost")
    for path in ("/", "/listings", "/listings/1", "/searches", "/status"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    r = c.get("/listings", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.headers["HX-Redirect"] == "/login"


def test_login_wrong_password_and_rate_limit(seeded: dict) -> None:
    c = TestClient(app, base_url="http://localhost")
    token = _csrf(c.get("/login").text)
    for _ in range(login_limiter.limit):
        r = c.post("/login", data={"username": "q", "password": "nope", "csrf_token": token})
        assert r.status_code == 401 and "Wrong username" in r.text
    r = c.post("/login", data={"username": "q", "password": "pw12345678", "csrf_token": token})
    assert r.status_code == 429 and "Too many" in r.text


def test_login_requires_csrf(seeded: dict) -> None:
    c = TestClient(app, base_url="http://localhost")
    c.get("/login")
    r = c.post("/login", data={"username": "q", "password": "pw12345678"})
    assert r.status_code == 403


def test_listings_filters_and_sort(client: TestClient, seeded: dict) -> None:
    r = client.get("/listings")
    assert r.status_code == 200 and "320i" in r.text and "318i" not in r.text  # inactive hidden
    assert "was € 7.000" in r.text and "1 listing" in r.text
    assert "318i" in client.get("/listings?include_inactive=true").text
    assert "No listings match" in client.get("/listings?price_max=5000").text
    assert "landed ≈ € 7.700" in client.get("/listings?landed_max=8000&sort=landed_asc").text
    assert "No listings match" in client.get("/listings?landed_max=7000").text
    assert "+€ 1.600 (20.8%)" in client.get("/listings?spread_min=1000&sort=spread_desc").text
    assert "No listings match" in client.get("/listings?spread_min=2000").text
    assert "No listings match" in client.get("/listings?gearbox=automatic").text
    assert "320i" in client.get("/listings?price_dropped=true&new_hours=24&sort=price_asc").text
    assert "No listings match" in client.get("/listings?new_hours=1").text
    partial = client.get("/listings?sort=mileage", headers={"HX-Request": "true"}).text
    assert (
        partial.lstrip().startswith('<div id="results">')
        and "<nav" not in partial.split("results")[0]
    )


def test_detail_and_status_buttons(client: TestClient, seeded: dict, db: Session) -> None:
    lid = seeded["a"].id
    r = client.get(f"/listings/{lid}")
    assert r.status_code == 200
    for needle in (
        "Open original ad",
        "https://x/a",
        "€ 7.000",
        "€ 6.000",
        "Price history",
        "Dealer GmbH",
        "Berlin",
    ):
        assert needle in r.text
    assert client.get("/listings/999").status_code == 404

    # HTMX call carries the token in a header
    token = _csrf(r.text)
    r = client.post(
        f"/listings/{lid}/status",
        data={"status": "claimed"},
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )
    assert (
        r.status_code == 200
        and "claimed · q" in r.text
        and "Contacted" in r.text
        and "Claim" not in r.text.replace("claimed", "")
    )
    db.expire_all()
    alert = db.scalar(select(Alert).where(Alert.listing_id == lid))
    assert (
        alert is not None
        and alert.status is AlertStatus.claimed
        and alert.claimed_by.username == "q"
    )
    assert alert.sent_at is not None  # created from the website, so nothing to send

    r = client.post(
        f"/listings/{lid}/status",
        data={"status": "passed", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    db.refresh(alert)
    assert alert.status is AlertStatus.passed
    assert (
        client.post(
            f"/listings/{lid}/status", data={"status": "bogus", "csrf_token": token}
        ).status_code
        == 400
    )
    assert client.post(f"/listings/{lid}/status", data={"status": "claimed"}).status_code == 403


def test_saved_searches_crud(client: TestClient, seeded: dict, db: Session) -> None:
    r = client.get("/searches")
    assert "e90" in r.text and "alerts on" in r.text
    token = _csrf(client.get("/searches/new").text)
    r = client.post(
        "/searches",
        data={
            "csrf_token": token,
            "name": "E91 manual",
            "source": "autoscout24_de",
            "p_path": "/lst/bmw/3er",
            "role": "deals",
            "interval_minutes": "45",
            "p_cat": "ma13gr100038ge25mt103",
            "p_fregfrom": "2010",
            "p_gear": "",
            "f_price_max": "8000",
            "f_gearbox": "manual",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = db.scalar(select(SavedSearch).where(SavedSearch.name == "E91 manual"))
    assert s is not None
    assert s.source_params == {
        "path": "/lst/bmw/3er",
        "cat": "ma13gr100038ge25mt103",
        "fregfrom": 2010,
    }
    assert s.filters == {"price_max": 8000, "gearbox": "manual"}
    assert s.role == "deals" and s.interval_minutes == 45 and s.alerts_enabled
    r = client.post(
        "/searches",
        data={
            "csrf_token": token,
            "name": "GP comps",
            "source": "gaspedaal",
            "role": "comparables",
            "interval_minutes": "360",
            "params_json": '{"path": "/bmw/3-serie/benzine", "bmin": 2008}',
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    gp = db.scalar(select(SavedSearch).where(SavedSearch.name == "GP comps"))
    assert (
        gp is not None
        and gp.role == "comparables"
        and not gp.alerts_enabled
        and gp.source_params["bmin"] == 2008
    )
    assert (
        client.post(
            "/searches",
            data={
                "csrf_token": token,
                "name": "bad",
                "source": "gaspedaal",
                "params_json": "{nope",
            },
        ).status_code
        == 400
    )

    r = client.post(
        f"/searches/{s.id}/toggle/alerts_enabled",
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )
    assert r.status_code == 200 and "alerts off" in r.text
    r = client.post(
        f"/searches/{s.id}",
        data={
            "csrf_token": token,
            "name": "E91 renamed",
            "source": "autoscout24_de",
            "p_path": "/y",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    db.refresh(s)
    assert (
        s.name == "E91 renamed"
        and s.source_params == {"path": "/y"}
        and s.filters == {}
        and not s.alerts_enabled
    )
    assert "E91 renamed" in client.get(f"/searches/{s.id}/edit").text
    assert (
        client.post(
            "/searches", data={"csrf_token": token, "name": "", "source": "autoscout24_de"}
        ).status_code
        == 400
    )


def test_status_page(client: TestClient, seeded: dict) -> None:
    r = client.get("/status")
    assert (
        r.status_code == 200
        and "autoscout24_de" in r.text
        and "never" in r.text
        and "No runs yet" in r.text
    )


def test_logout(client: TestClient) -> None:
    token = _csrf(client.get("/listings").text)
    r = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/listings", follow_redirects=False).status_code == 303
