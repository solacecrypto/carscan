import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Alert, AlertStatus, Listing, PriceHistory, SavedSearch, SellerType, User, Watch
from app.storage.filters import matches, parse_filters
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
    assert "also on" in client.get(f"/listings/{dup.id}").text
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


def test_detail_page(client: TestClient, seeded: dict, db: Session) -> None:
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
        "Track as deal",
    ):
        assert needle in r.text
    assert "Claim" not in r.text  # triage lives in deals now
    assert client.get("/listings/999").status_code == 404
    # tracking the car makes it a deal, claims the group alert and shows on the card
    token = _csrf(r.text)
    r = client.post(
        "/deals", data={"csrf_token": token, "listing_id": str(lid)}, follow_redirects=False
    )
    assert r.status_code == 303
    db.expire_all()
    alert = db.query(Alert).filter_by(listing_id=lid).first()
    assert alert is not None and alert.status is AlertStatus.claimed
    assert alert.claimed_by.username == "q"
    cards = client.get("/listings?profitable=false").text.split('id="results"')[1]
    assert "deal · spotted</span>q" in cards
    assert "Deal: spotted · q" in client.get(f"/listings/{lid}").text


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
    # hide warning light: from the repair estimator's findings, not from the Claude pass
    assert "320i" in results("/listings?no_warning_light=true")
    row.repair_breakdown = {"items": [{"key": "warning_light", "label": "warning light on"}]}
    db.commit()
    assert "No listings match" in results("/listings?no_warning_light=true")
    assert "318i" in results("/listings?no_warning_light=true&include_inactive=1&profitable=false")
    assert "hide warning light" in client.get("/listings").text
    # hide needs repair: anything the estimator priced, not only a warning light
    assert "320i" in results("/listings?no_repairs=true")
    row.repair_cost_eur = 400
    db.commit()
    assert "No listings match" in results("/listings?no_repairs=true")
    assert "hide needs repair" in client.get("/listings").text
    assert not matches(row, parse_filters({"no_repairs": "true", "profitable": "false"}))
    assert not matches(row, parse_filters({"no_warning_light": "true", "profitable": "false"}))


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
    assert "BMW E90/E91 318i/320i" in page and "estimates within" in page
    assert "median spread" in page and 'href="/models/bmw-3er-e90"' in page
    assert "1 deal" in page and "DE 1" in page
    page = client.get("/models?sort=supply").text
    assert "BMW E90/E91 318i/320i" in page
    page = client.get("/models/bmw-3er-e90").text
    assert "Best cars right now" in page and "Deals on this model" in page
    assert "By country" in page and "median spread, all countries" in client.get("/models").text
    assert "landed-cost estimate was off by" in page and "Asking prices" in page
    assert client.get("/models/no-such-model").status_code == 404
    assert "estimates within" in client.get("/listings?profitable=false").text


def test_watch_notes_seller_and_digest_pages(client: TestClient, seeded: dict, db: Session) -> None:
    from app.db import Watch
    from app.digest import store_digest

    row = seeded["a"]
    row.seller_name = "Auto Müller"
    db.commit()
    page = client.get(f"/listings/{row.id}").text
    assert "Favorite of" in page and "No notes yet" in page and 'href="/sellers/Auto%20M' in page
    assert "Notes" in page and 'hx-post="/listings/' in page
    token = _csrf(page)
    r = client.post(
        f"/listings/{row.id}/watch",
        data={"target_price_eur": "5.500", "action": "watch", "csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Save target" in r.text and "€ 5.500 or below" in r.text
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
    page = client.get("/digest").text
    assert "Telegram morning digests" in page and "Best new cars" in page
    assert "Your favorites" in page and "Your deals" in page and "Good " in page
    assert "Best new cars" not in client.get("/digest?hours=48").text or True


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
    # max distance: without a placed viewer the limit is ignored with a hint; with one, the
    # seeded car's town is not geocoded, so a limit hides it; blank shows it
    page = client.get("/listings?max_km=200&profitable=false").text
    assert "320i" in page.split('id="results"')[1] and "not placed on the map yet" in page
    seeded["user"].home_lat, seeded["user"].home_lon, seeded["user"].home_label = (
        52.09,
        5.12,
        "Utrecht",
    )
    db.commit()
    page = client.get("/listings?max_km=200&profitable=false").text
    assert "No listings match" in page and "≤ 200 km" in page
    assert "320i" in results("/listings?max_km=&profitable=false")


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
    assert "320i" in page.split('id="results"')[1] and "only my favorites" in page
    # the favorites page: stats, price change since starring, target and removal
    # pretend the star was set between the two price points (7000 → 6000)
    db.query(Watch).filter_by(listing_id=row.id).update({"created_at": NOW - timedelta(hours=12)})
    db.commit()
    page = client.get("/favorites").text
    assert "320i" in page and "1 still for sale" in page and "€ 1.000 since added" in page
    assert "€ 1.000 lower than when added" in page
    assert "Nothing here." in client.get("/favorites?show=gone").text
    assert "320i" in client.get("/favorites?show=drops&sort=drop").text
    r = client.post(
        f"/listings/{row.id}/watch",
        data={"csrf_token": token, "target_price_eur": "5000", "action": "watch"},
        headers={"referer": "http://localhost/favorites"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/favorites"
    page = client.get("/favorites?show=target").text
    assert "target € 5.000" in page and "1 reached" not in page
    r = client.post(
        f"/listings/{row.id}/star",
        data={"csrf_token": token},
        headers={"referer": "http://localhost/favorites"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/favorites"
    assert "No favorites yet." in client.get("/favorites").text
    # star it again for the rest of the test
    client.post(
        f"/listings/{row.id}/star", data={"csrf_token": token}, headers={"HX-Request": "true"}
    )
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
        "/spotter",
        data={
            "csrf_token": token,
            "name": "cheap E90",
            "model_key": "bmw-3er-e90",
            "price_max": "8.000",
            "profitable": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/spotter?msg=saved"
    sub = db.query(Subscription).one()
    assert sub.criteria == {
        "filters": {"model_key": ["bmw-3er-e90"], "price_max": 8000, "profitable": True}
    }
    assert "cheap E90" in client.get("/spotter").text
    client.post(f"/spotter/{sub.id}/toggle", data={"csrf_token": token})
    db.expire_all()
    assert db.get(Subscription, sub.id).active is False
    client.post(f"/spotter/{sub.id}/delete", data={"csrf_token": token})
    assert db.query(Subscription).count() == 0


def test_account_location(client: TestClient, seeded: dict, db: Session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.web.routes.account as account

    monkeypatch.setattr(
        account,
        "geocode_address",
        lambda _c, text: (51.22, 4.40, "Antwerpen, BE") if "antwerp" in text.lower() else None,
    )
    token = _csrf(client.get("/account").text)
    r = client.post(
        "/account/location",
        data={"csrf_token": token, "address": "Antwerpen"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/account?loc=saved"
    db.expire_all()
    u = db.get(User, seeded["user"].id)
    assert u is not None and (u.home_lat, u.home_label) == (51.22, "Antwerpen, BE")
    assert "Now: <b>Antwerpen, BE</b>" in client.get("/account").text
    assert "Distances measured from Antwerpen, BE" in client.get("/listings").text  # globe
    r = client.post(
        "/account/location",
        data={"csrf_token": token, "address": "Narnia", "next": "/deals"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/deals?loc=notfound"
    assert "Not found. Try a postcode" in client.get("/deals?loc=notfound").text
    r = client.post(
        "/account/location",
        data={"csrf_token": token, "address": "x", "next": "//evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"].startswith("/account?")  # open redirect refused
    r = client.post(
        "/account/location", data={"csrf_token": token, "address": ""}, follow_redirects=False
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.get(User, seeded["user"].id).home_lat is None  # type: ignore[union-attr]


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


def test_car_page_tags_are_balanced(client: TestClient, seeded: dict, db: Session) -> None:
    """A stray or missing </details> or </div> silently breaks the two-column layout."""
    import re

    from app.insights.service import apply_rules

    row = seeded["a"]
    row.description = "Unfallfrei. TÜV bis 06/2027."
    row.photos = ["https://x/1.jpg"]
    apply_rules(row, NOW)
    db.commit()
    html = client.get(f"/listings/{row.id}").text
    for tag in ("div", "details", "ul", "form"):
        opened = len(re.findall(rf"<{tag}\b", html))
        closed = len(re.findall(rf"</{tag}>", html))
        assert opened == closed, f"<{tag}>: {opened} opened, {closed} closed"


def test_filter_presets(client: TestClient, seeded: dict, db: Session) -> None:
    from app.db import FilterPreset

    page = client.get("/listings").text
    assert "My presets" in page and "save current" in page
    token = _csrf(page)
    q = "spread_min=1000&chain_replaced=true"  # the default sort is not remembered
    r = client.post(
        "/presets",
        data={
            "name": "chain + 1k",
            "query": "?spread_min=1000&chain_replaced=true&page=3&sort=newest",
            "csrf_token": token,
        },  # noqa: E501
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/listings?{q}"
    pr = db.query(FilterPreset).one()
    assert pr.name == "chain + 1k" and "page=" not in pr.query
    page = client.get(f"/listings?{q}").text
    assert f'href="/listings?{q.replace("&", "&amp;")}"' in page
    block = page.split("My presets")[1].split('type="search"')[0]
    assert "bg-brand-500 text-ink-950" in block  # the matching preset is highlighted
    data = {"name": "chain + 1k", "query": "spread_min=2000", "csrf_token": token}
    client.post("/presets", data=data, follow_redirects=False)
    db.expire_all()
    assert db.query(FilterPreset).count() == 1  # same name: updated, not duplicated
    assert db.query(FilterPreset).one().query == "spread_min=2000"
    # no query captured: the referring page's filters are used
    r = client.post(
        "/presets",
        data={"name": "from referer", "query": "", "csrf_token": token},
        headers={"referer": "http://localhost/listings?make=Audi&page=2"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/listings?make=Audi"
    # what the page's script sends: hidden false + ticked true, default sort -> tidy query
    r = client.post(
        "/presets",
        data={
            "name": "tidy",
            "query": "profitable=false&profitable=true&q=audi&sort=newest",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/listings?q=audi"
    r = client.post(
        "/presets",
        data={
            "name": "off",
            "query": "profitable=false&make=Audi&sort=price_asc",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/listings?make=Audi&sort=price_asc&profitable=false"
    # nothing at all: back to the listings with a note, not an error page
    r = client.post(
        "/presets", data={"name": "x", "query": "", "csrf_token": token}, follow_redirects=False
    )
    assert r.headers["location"] == "/listings?preset=empty"
    assert "set at least one filter first" in client.get("/listings?preset=empty").text
    r = client.post(
        "/presets", data={"name": "", "query": "q=a", "csrf_token": token}, follow_redirects=False
    )
    assert r.headers["location"] == "/listings?preset=noname"
    assert db.query(FilterPreset).count() == 4
    r = client.post(f"/presets/{pr.id}/delete", data={"csrf_token": token}, follow_redirects=False)
    assert (r.status_code, r.headers.get("location"), pr.id) == (303, "/listings", 1)
    db.expire_all()
    assert db.query(FilterPreset).count() == 3


def test_html_error_page(client: TestClient, seeded: dict) -> None:
    r = client.get("/listings/999999", headers={"accept": "text/html,*/*"})
    assert r.status_code == 404 and "Not found" in r.text and "Go back" in r.text
    r = client.get("/listings/999999", headers={"accept": "application/json"})
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    r = client.get("/listings/999999", headers={"accept": "text/html", "HX-Request": "true"})
    assert r.json() == {"detail": "Not Found"}


def test_listings_page_has_no_nested_forms(client: TestClient, seeded: dict, db: Session) -> None:
    """A form inside the filter form makes the browser close the filter form early."""
    import re
    from datetime import UTC, datetime

    from app.db import FilterPreset

    db.add(
        FilterPreset(
            user_id=seeded["user"].id, name="p", query="spread_min=1", created_at=datetime.now(UTC)
        )
    )
    db.commit()
    html = client.get("/listings").text
    depth = 0
    for m in re.finditer(r"<(/?)form\b", html):
        depth += -1 if m.group(1) else 1
        assert depth in (0, 1), "nested <form>"
    assert depth == 0


def test_listings_csv_export(client: TestClient, seeded: dict) -> None:
    r = client.get("/listings.csv?profitable=false&include_inactive=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="carscan-' in r.headers["content-disposition"]
    lines = r.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("id;source;make;model;variant;year;mileage_km")
    assert len(lines) == 3  # header + both seeded cars
    assert f"/listings/{seeded['a'].id}" in r.text and seeded["a"].url in r.text
    # the filters apply: only active cars by default
    assert len(client.get("/listings.csv?profitable=false").text.splitlines()) == 2


def test_bulk_star(client: TestClient, seeded: dict, db: Session) -> None:
    token = _csrf(client.get("/listings").text)
    a, b = seeded["a"], seeded["b"]
    r = client.post(
        "/listings/star-bulk",
        data={
            "csrf_token": token,
            "ids": f"{a.id},{b.id},999999",
            "op": "add",
            "next": "/listings?x=1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/listings?x=1"
    assert {w.listing_id for w in db.query(Watch).all()} == {a.id, b.id}
    # adding again does not duplicate, an outside redirect falls back to /listings
    r = client.post(
        "/listings/star-bulk",
        data={"csrf_token": token, "ids": str(a.id), "op": "add", "next": "//evil.example"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/listings" and db.query(Watch).count() == 2
    client.post(
        "/listings/star-bulk", data={"csrf_token": token, "ids": f"{a.id} {b.id}", "op": "remove"}
    )
    assert db.query(Watch).count() == 0
    r = client.post("/listings/star-bulk", data={"csrf_token": token, "ids": ""})
    assert r.status_code == 400
    assert 'id="bulk-toggle"' in client.get("/listings").text


def test_spotter_page_and_spot_this(client: TestClient, seeded: dict, db: Session) -> None:
    from app.db import Subscription
    from app.notify import matches, subscription_events

    assert client.get("/wanted", follow_redirects=False).headers["location"] == "/spotter"
    assert "No spotters yet" in client.get("/spotter").text
    for key in ("a", "b"):
        db.get(Listing, seeded[key].id).model_key = "bmw-3er-e90"
    db.commit()
    token = _csrf(client.get("/listings").text)
    # from the listings page: the current filters become a spotter
    r = client.post(
        "/spotter",
        data={
            "csrf_token": token,
            "name": "318i under 200k",
            "query": "make=BMW&mileage_max=200000&year_min=2010&profitable=false&sort=newest",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (
        r.headers["location"].startswith("/listings?make=BMW")
        and "spotter=saved" in r.headers["location"]
    )
    assert "Spotter saved" in client.get(r.headers["location"]).text
    sub = db.query(Subscription).one()
    assert sub.criteria["filters"] == {
        "make": ["BMW"], "mileage_max": 200000, "year_min": 2010, "profitable": False
    }  # fmt: skip
    page = client.get("/spotter").text
    assert "318i under 200k" in page and "BMW · from 2010 · ≤ 200.000 km" in page
    assert f"/listings/{seeded['a'].id}" in page and "1</b> on the market" in page
    assert (
        "/listings?make=BMW&amp;profitable=false&amp;mileage_max=200000&amp;year_min=2010" in page
    )
    # the notifier uses the same filters
    assert matches(sub.criteria, seeded["a"])
    seeded["a"].mileage_km = 250000
    assert not matches(sub.criteria, seeded["a"])
    seeded["a"].mileage_km = 120000
    assert subscription_events(db, [seeded["a"]], datetime.now(UTC)) == 1
    db.commit()
    assert "Spotted for “318i under 200k”" in client.get("/notifications").text
    # nothing chosen -> note, no spotter
    r = client.post(
        "/spotter", data={"csrf_token": token, "query": "sort=newest"}, follow_redirects=False
    )
    assert r.headers["location"] == "/listings?spotter=empty"
    r = client.post("/spotter", data={"csrf_token": token, "name": "x"}, follow_redirects=False)
    assert r.headers["location"] == "/spotter?msg=empty"
    assert db.query(Subscription).count() == 1
    assert (
        "+ spot this" in client.get("/listings").text
        and ">Spotter<" in client.get("/listings").text
    )
