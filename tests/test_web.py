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


def test_listings_filters_and_sort(client: TestClient, seeded: dict, db: Session) -> None:
    r = client.get("/listings")
    cards = r.text.split('id="results"')[1]
    assert r.status_code == 200 and "320i" in cards and "318i" not in cards  # inactive hidden
    assert "was € 7.000" in r.text and "1 listing" in r.text
    assert (
        "318i"
        in client.get("/listings?include_inactive=true&profitable=false").text.split(
            'id="results"'
        )[1]
    )
    dup = Listing(
        source="kleinanzeigen",
        source_listing_id="dup",
        url="https://x/dup",
        make="BMW",
        model="320i",
        price_eur=6050,
        seller_type=SellerType.private,
        country="DE",
        first_seen=NOW,
        last_seen=NOW,
        duplicate_of=seeded["a"],
    )
    db.add(dup)
    db.commit()
    page = client.get("/listings").text
    assert "also on kleinanzeigen" in page and page.count("<article") == 1
    assert (
        client.get("/listings?include_duplicates=true&profitable=false").text.count("<article") == 2
    )
    assert "Same car on:" in client.get(f"/listings/{dup.id}").text
    assert "No listings match" in client.get("/listings?price_max=5000").text
    assert "landed ≈ € 7.700" in client.get("/listings?landed_max=8000&sort=landed_asc").text
    assert "No listings match" in client.get("/listings?landed_max=7000").text
    page = client.get("/listings?spread_min=1000&sort=spread_desc").text
    assert "+€ 1.600" in page and "(20.8%)" in page
    assert "No listings match" in client.get("/listings?make=Audi").text
    assert "320i" in client.get("/listings?make=bmw").text
    assert "No listings match" in client.get("/listings?model_key=vw-golf-6").text
    page = client.get("/listings").text
    assert 'data-name="model"' in page and "VW Golf VI" in page and 'data-name="make"' in page
    results = lambda url: client.get(url).text.split('id="results"')[1]  # noqa: E731
    assert "320i" in results(
        "/listings?price_min=&price_max=&gearbox=&model=BMW+E90%2FE91+318i%2F320i&country=DE&country=BE"
    )
    assert "No listings match" in results("/listings?model=VW+Golf+VI+1.2%2F1.4+TSI")
    assert "No listings match" in results("/listings?country=IT&country=SE")
    assert "320i" in results("/listings?new_hours=24&price_min=abc")
    assert "320i" in results("/listings?make=Audi&make=bmw")  # several brands
    assert "320i" in results(
        "/listings?model=VW+Golf+VI+1.2%2F1.4+TSI&model=BMW+E90%2FE91+318i%2F320i"
    )
    assert "No listings match" in results("/listings?make=Audi&make=Volvo")
    assert "No listings match" in client.get("/listings?spread_min=2000").text
    assert "+€ 1.600" in results("/listings?profitable=true")  # spread 1600 > 0
    assert "Reset filters" in client.get("/listings?profitable=true").text
    # positive return only is on by default; the form sends profitable=false to switch it off
    assert "+€ 1.600" in results("/listings")
    assert 'name="profitable" value="true" checked' in client.get("/listings").text
    assert (
        'name="profitable" value="true" checked'
        not in client.get("/listings?profitable=false").text
    )
    assert "+€ 1.600" in results("/listings?profitable=false&profitable=true")
    assert "No listings match" in client.get("/listings?gearbox=automatic").text
    assert "320i" in client.get("/listings?price_dropped=true&new_hours=24&sort=price_asc").text
    assert "No listings match" in client.get("/listings?new_hours=1").text
    pushed = client.get(
        "/listings?make=&gearbox=automatic&price_min=", headers={"HX-Request": "true"}
    )
    assert pushed.headers["HX-Push-Url"] == "/listings?gearbox=automatic"
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


def test_new_cars_poller(client: TestClient, seeded: dict, db: Session) -> None:
    from datetime import UTC, datetime, timedelta

    page = client.get("/listings?make=BMW").text
    assert 'id="poller"' in page and "new car" not in page
    m = re.search(r'/listings/new-count\?since=([^&"]+)&amp;make=BMW', page)
    assert m, page[:3000]
    r = client.get(f"/listings/new-count?since={m.group(1)}&make=BMW")
    assert r.status_code == 200 and "new car" not in r.text
    old = datetime.now(UTC) - timedelta(minutes=5)
    r = client.get(f"/listings/new-count?since={old.isoformat()}&make=BMW")
    assert "new car" not in r.text  # listing a was first seen 2 hours ago
    r = client.get(
        f"/listings/new-count?since={(datetime.now(UTC) - timedelta(hours=3)).isoformat()}&make=BMW"
    )
    assert (
        "1 new car since you opened this page" in r.text and 'hx-get="/listings?make=BMW"' in r.text
    )
    early = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    assert "new car" not in client.get(f"/listings/new-count?since={early}&make=Audi").text
    assert client.get("/listings/new-count?since=garbage").status_code == 400


def test_detail_page_shows_the_repair_box(client: TestClient, seeded: dict, db: Session) -> None:
    from app.storage.listings import apply_costs

    row = seeded["a"]
    row.description = "Motorschaden, muss abgeschleppt werden."
    apply_costs(row, NOW)
    db.commit()
    page = client.get(f"/listings/{row.id}").text
    assert "Repair costs: what the ad admits is wrong" in page
    assert "engine damage" in page and "needs a trailer or tow" in page
    assert "Very rough indication only" in page
    from app.web.templating import eur

    assert f"incl. ≈ {eur(row.repair_cost_eur)} repairs" in page


def test_paperwork_filter_and_box(client: TestClient, seeded: dict, db: Session) -> None:
    from app.insights.service import apply_rules

    row = seeded["a"]
    row.description = "Unfallfrei. TÜV bis 06/2027."
    apply_rules(row, NOW)
    db.commit()
    page = client.get(f"/listings/{row.id}").text
    assert "Paperwork" in page and "valid until 06/2027" in page
    assert "Nothing about missing papers" in page
    results = lambda url: client.get(url).text.split('id="results"')[1]  # noqa: E731
    assert "320i" in results("/listings?papers_ok=true")
    row.description = "TÜV bis 06/2027. Verkauf ohne Brief."
    apply_rules(row, NOW)
    db.commit()
    assert "Seller says papers are missing" in client.get(f"/listings/{row.id}").text
    assert "No listings match" in results("/listings?papers_ok=true")
    assert "320i" in results("/listings")


def test_tuv_months_filter(client: TestClient, seeded: dict, db: Session) -> None:
    from app.insights.service import apply_rules

    row = seeded["a"]
    row.description = "TÜV bis 06/2027."
    apply_rules(row, NOW)
    db.commit()
    results = lambda url: client.get(url).text.split('id="results"')[1]  # noqa: E731
    assert "320i" in results("/listings?tuv_min_months=3")
    assert "No listings match" in results("/listings?tuv_min_months=36")


def test_models_page_and_accuracy_on_cards(client: TestClient, seeded: dict, db: Session) -> None:
    from datetime import date

    from app.db import DealStage
    from app.deals import create_deal, set_stage, update_actuals
    from app.market.history import snapshot_models

    row = seeded["a"]
    row.model_key = "bmw-3er-e90"
    row.landed_cost_eur = 7700
    deal = create_deal(db, row, seeded["user"], NOW)
    set_stage(deal, DealStage.bought, seeded["user"], NOW)
    update_actuals(
        deal,
        {"purchase_price_eur": 6000, "transport_eur": 350, "bpm_eur": 1200, "fees_eur": 304},
        seeded["user"],
        NOW,
    )
    snapshot_models(db, date(2026, 10, 16))
    db.commit()
    page = client.get("/models").text
    assert "closed deal" in page and "landed cost off by" in page and "median spread" in page
    assert "estimates within" in client.get("/listings?profitable=false").text


def test_watch_notes_seller_and_digest_pages(client: TestClient, seeded: dict, db: Session) -> None:
    from app.db import Watch
    from app.digest import store_digest

    row = seeded["a"]
    row.seller_name = "Auto Müller"
    db.commit()
    page = client.get(f"/listings/{row.id}").text
    assert "Starred by" in page and "No notes yet" in page and 'href="/sellers/Auto%20M' in page
    token = _csrf(page)
    r = client.post(
        f"/listings/{row.id}/watch",
        data={"target_price_eur": "5.500", "action": "watch", "csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Update target" in r.text and "€ 5.500 or below" in r.text
    assert db.query(Watch).filter_by(listing_id=row.id).one().target_price_eur == 5500
    r = client.post(
        f"/listings/{row.id}/watch",
        data={"action": "unwatch", "csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert db.query(Watch).filter_by(listing_id=row.id).count() == 0
    r = client.post(
        f"/listings/{row.id}/notes",
        data={"text": "called, wants 5.800", "csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "called, wants 5.800" in r.text and "q ·" in r.text
    page = client.get("/sellers/Auto%20M%C3%BCller").text
    assert "Auto Müller" in page and "cars seen" in page and "320i" in page
    assert client.get("/sellers/Nobody").status_code == 404
    store_digest(db, NOW, "http://localhost")
    db.commit()
    assert "Morning digest" in client.get("/digest").text


def test_color_and_body_filters(client: TestClient, seeded: dict, db: Session) -> None:
    row = seeded["a"]
    row.color, row.body_type = "black", "estate"
    db.commit()
    results = lambda url: client.get(url).text.split('id="results"')[1]  # noqa: E731
    assert "320i" in results("/listings?color=black&color=blue")
    assert "No listings match" in results("/listings?color=red")
    assert "320i" in results("/listings?body=estate")
    assert "No listings match" in results("/listings?body=sedan")
    page = client.get("/listings?color=black").text
    assert 'name="color" value="black" class="peer sr-only" checked' in page
    assert "Body and colour" in page


def test_star_notifications_and_wanted(client: TestClient, seeded: dict, db: Session) -> None:
    from app.db import Notification, Subscription, Watch
    from app.notify import push

    row = seeded["a"]
    token = _csrf(client.get(f"/listings/{row.id}").text)
    r = client.post(
        f"/listings/{row.id}/star", data={"csrf_token": token}, headers={"HX-Request": "true"}
    )
    assert r.status_code == 200 and 'aria-pressed="true"' in r.text
    assert db.query(Watch).filter_by(listing_id=row.id, user_id=seeded["user"].id).count() == 1
    page = client.get("/listings?starred=true&profitable=false").text
    assert "320i" in page.split('id="results"')[1] and "only cars I starred" in page
    r = client.get("/favorites", follow_redirects=False)
    assert r.status_code == 307 and "starred=true" in r.headers["location"]
    r = client.post(
        f"/listings/{row.id}/star", data={"csrf_token": token}, headers={"HX-Request": "true"}
    )
    assert 'aria-pressed="false"' in r.text and db.query(Watch).count() == 0

    push(db, seeded["user"].id, "price_down", "BMW 320i: € 7.000 → € 6.500", "DE", NOW, row)
    db.commit()
    page = client.get("/listings").text
    assert 'id="bell"' in page and ">1</span>" in page
    page = client.get("/notifications").text
    assert "€ 7.000 → € 6.500" in page and "Mark all 1 read" in page
    n = db.query(Notification).one()
    r = client.get(f"/notifications/{n.id}/open", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/listings/{row.id}"
    db.expire_all()
    assert db.get(Notification, n.id).read_at is not None
    assert ">1</span>" not in client.get("/listings").text

    r = client.post(
        "/wanted",
        data={
            "csrf_token": token,
            "name": "cheap E90",
            "model_key": "bmw-3er-e90",
            "max_price": "8.000",
            "positive_only": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    sub = db.query(Subscription).one()
    assert sub.criteria == {"model_keys": ["bmw-3er-e90"], "max_price": 8000, "positive_only": True}
    assert "cheap E90" in client.get("/notifications").text
    client.post(f"/wanted/{sub.id}/toggle", data={"csrf_token": token})
    db.expire_all()
    assert db.get(Subscription, sub.id).active is False
    client.post(f"/wanted/{sub.id}/delete", data={"csrf_token": token})
    assert db.query(Subscription).count() == 0


def test_account_and_user_management(client: TestClient, seeded: dict, db: Session) -> None:
    from app.db import User
    from app.web.security import verify_password

    me = seeded["user"]
    token = _csrf(client.get("/account").text)
    r = client.post(
        "/account/password",
        data={"current": "wrong", "new": "newpass123", "repeat": "newpass123", "csrf_token": token},
    )
    assert r.status_code == 400 and "current password is wrong" in r.text
    r = client.post(
        "/account/password",
        data={
            "current": "pw12345678",
            "new": "newpass123",
            "repeat": "newpass123",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert verify_password("newpass123", db.get(User, me.id).password_hash)

    assert client.get("/users").status_code == 403  # not an admin yet
    me.is_admin = True
    db.commit()
    page = client.get("/users").text
    assert "Add a user" in page and "(you)" in page
    r = client.post(
        "/users",
        data={"username": "pat", "password": "patpass123", "csrf_token": token, "is_admin": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    pat = db.scalar(select(User).where(User.username == "pat"))
    assert pat is not None and not pat.is_admin
    client.post(f"/users/{pat.id}/reset", data={"password": "another123", "csrf_token": token})
    db.expire_all()
    assert verify_password("another123", db.get(User, pat.id).password_hash)
    client.post(f"/users/{pat.id}/toggle_admin", data={"csrf_token": token})
    db.expire_all()
    assert db.get(User, pat.id).is_admin
    assert client.post(f"/users/{me.id}/delete", data={"csrf_token": token}).status_code == 400
    client.post(f"/users/{pat.id}/delete", data={"csrf_token": token})
    assert db.scalar(select(User).where(User.username == "pat")) is None


def test_push_routes_and_static_assets(client: TestClient, seeded: dict, db: Session) -> None:
    from app.db import PushSubscription

    assert client.get("/push/key").text == ""  # no keys configured in tests
    token = _csrf(client.get("/account").text)
    page = client.get("/account").text
    assert 'id="push-enable"' in page and 'rel="manifest"' in page
    r = client.post(
        "/push/subscribe",
        json={"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 200 and db.query(PushSubscription).count() == 1
    r = client.post(
        "/push/unsubscribe",
        json={"endpoint": "https://push.example/abc"},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 200 and db.query(PushSubscription).count() == 0
    assert client.get("/sw.js").headers["service-worker-allowed"] == "/"
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/icon-192.png").headers["content-type"] == "image/png"


def test_free_text_search(client: TestClient, seeded: dict, db: Session) -> None:
    row = seeded["a"]
    row.variant, row.description_en = "320i Touring Xenon", "full history, two keys"
    db.commit()
    results = lambda url: client.get(url).text.split('id="results"')[1]  # noqa: E731
    assert "320i" in results("/listings?q=touring+xenon")
    assert "320i" in results("/listings?q=two+keys")
    assert "No listings match" in results("/listings?q=cabrio")


def test_filter_sidebar_scrolls_within_the_viewport(client: TestClient, seeded: dict) -> None:
    aside = client.get("/listings").text.split('id="filter-panel"')[1].split(">")[0]
    assert "lg:sticky" in aside and "lg:overflow-y-auto" in aside and "100vh" in aside


def test_translate_button_translates_once(
    client: TestClient, seeded: dict, db: Session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from app.translate import TranslateError

    row = seeded["a"]
    row.description = "Getriebe laut, rauscht. Unfallfrei."
    db.commit()
    page = client.get(f"/listings/{row.id}").text
    assert "Translate to English" in page
    token = _csrf(page)
    hx = {"HX-Request": "true"}
    calls: list[str] = []

    class Fake:
        name = "fake"

        def translate(self, text: str) -> str:
            calls.append(text)
            return "EN: " + text

    monkeypatch.setattr("app.translate.default_translator", lambda: Fake())
    r = client.post(f"/listings/{row.id}/translate", data={"csrf_token": token}, headers=hx)
    assert r.status_code == 200 and "EN: Getriebe laut" in r.text
    assert "Translate to English" not in r.text
    db.expire_all()
    assert db.get(Listing, row.id).description_en.startswith("EN:")
    assert "Translate to English" not in client.get(f"/listings/{row.id}").text
    n = len(calls)
    client.post(f"/listings/{row.id}/translate", data={"csrf_token": token}, headers=hx)
    assert len(calls) == n  # already translated: no new request

    class Refusing:
        name = "fake"

        def translate(self, text: str) -> str:
            raise TranslateError("HTTP 302")

    row2 = seeded["b"]
    row2.description = "Noch ein Auto."
    db.commit()
    monkeypatch.setattr("app.translate.default_translator", lambda: Refusing())
    r = client.post(f"/listings/{row2.id}/translate", data={"csrf_token": token}, headers=hx)
    assert "refused" in r.text and "Translate to English" in r.text


def test_nearby_panel_and_map(client: TestClient, seeded: dict, db: Session) -> None:
    from datetime import UTC, datetime

    from app.db import GeoCache

    row = seeded["a"]
    row.city, row.postcode = "Hannover", None
    db.add(
        GeoCache(
            key="de||hannover", country="DE", lat=52.37, lon=9.73, resolved_at=datetime.now(UTC)
        )
    )
    db.commit()
    page = client.get(f"/listings/{row.id}").text
    assert 'id="map"' in page and "Open in Google Maps" in page and "/nearby?km=50" in page
    part = client.get(f"/listings/{row.id}/nearby?km=100").text
    assert 'data-km="100"' in part and "data-markers" in part and 'name="page"' in part
    part = client.get(f"/listings/{row.id}/nearby?km=100&min_spread=0").text
    assert 'name="min_spread" value="0" class="peer sr-only" checked' in part
    part = client.get(
        f"/listings/{row.id}/nearby?km=100&cond=chain_replaced&make=BMW&same_model=1"
    ).text
    assert 'value="chain_replaced" class="peer sr-only" checked' in part
    assert 'name="same_model" value="1" class="peer sr-only" checked' in part
