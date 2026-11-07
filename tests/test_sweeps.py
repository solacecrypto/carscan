"""Recency sweeps: bytes accounting, early stop at known ads, the watchlist classifier,
the brand sweep specs and a full sweep run."""

from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Listing, SavedSearch, ScrapeRun, ScrapeStatus, User
from app.scrapers import PoliteClient, get_scraper
from app.watchlist import build_sweep_specs, classify, load_models, sync_sweeps
from app.worker.jobs import run_saved_search

FIX = Path(__file__).parent / "fixtures"
MODELS = load_models()


def _as24(pages: dict[str, str]) -> tuple[PoliteClient, list[str]]:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path + "?" + str(request.url.query, "ascii"))
        if request.url.path.startswith("/angebote/"):
            return httpx.Response(200, text=(FIX / "autoscout24_de/detail_dealer.html").read_text())
        page = request.url.params.get("page", "1")
        name = pages.get(page, "search_empty.html")
        return httpx.Response(200, text=(FIX / "autoscout24_de" / name).read_text())

    return PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda _: None), hits


def test_client_counts_bytes() -> None:
    body = "x" * 5000
    client = PoliteClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)),
        sleep=lambda _: None,
    )
    client.get("https://example.com/a")
    client.get("https://example.com/b")
    assert client.requests_made == 2 and client.bytes_received >= 10000


def test_as24_sweep_stops_at_known_ads() -> None:
    scraper = get_scraper("autoscout24_de")
    client, hits = _as24({"1": "search_page1.html", "2": "search_page2.html"})
    first = scraper.search({"path": "/lst/bmw", "max_pages": 5}, client)
    assert len(first) > 20 and len(hits) >= 2
    # every ad of page 1 is known: the sweep fetches page 1 only
    client, hits = _as24({"1": "search_page1.html", "2": "search_page2.html"})
    known = [r.source_listing_id for r in first]
    again = scraper.search({"path": "/lst/bmw", "max_pages": 5, "known_ids": known}, client)
    assert len(hits) == 1 and len(again) <= 20
    assert "known_ids" not in hits[0] and "max_pages" not in hits[0]


def test_kleinanzeigen_brand_sweep_urls() -> None:
    from app.scrapers.kleinanzeigen import build_search_url

    p = {"make": "bmw", "ez_from": 2007, "ez_to": ""}
    assert build_search_url(p, 1).endswith("/s-autos/bmw/k0c216+autos.ez_i:2007,+autos.marke_s:bmw")
    assert build_search_url(p, 2).endswith(
        "/s-autos/bmw/seite:2/bmw/k0c216+autos.ez_i:2007,+autos.marke_s:bmw"
    )
    q = {"make": "bmw", "query": "320i", "ez_from": 2008, "ez_to": 2014}
    assert "/s-autos/bmw/seite:2/320i/k0c216+autos.ez_i:2008,2014" in build_search_url(q, 2)


def test_kleinanzeigen_sweep_stops_at_known_ads() -> None:
    scraper = get_scraper("kleinanzeigen")
    html = next(FIX.glob("kleinanzeigen/*search*.html")).read_text()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=html)

    client = PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda _: None)
    first = scraper.search({"make": "bmw", "max_pages": 3}, client)
    assert first
    calls.clear()
    known = [r.source_listing_id for r in first]
    scraper.search({"make": "bmw", "max_pages": 3, "known_ids": known}, client)
    assert len(calls) == 1


def test_classify_against_the_watchlist() -> None:
    url = "/angebote/bmw-3er-318i-touring-benzin-schwarz-cat_ma13-0e69e002"
    assert (
        classify("BMW", "318", "i Touring", url, 2010, "petrol", "estate", MODELS) == "bmw-3er-e90"
    )
    # same model line, later year -> the F30 entry
    assert classify("BMW", "320", "i", url, 2014, "petrol", None, MODELS) == "bmw-3er-f30"
    # Kleinanzeigen: the title carries the engine, the URL nothing useful
    assert classify("BMW", "320i F30 M-Paket", None, "/s-anzeige/x/1", 2013, None, None, MODELS)
    assert (
        classify(
            "Volkswagen",
            "Golf",
            "1.4 TSI",
            "/angebote/vw-golf-1-4-tsi",
            2014,
            "petrol",
            None,
            MODELS,
        )
        == "vw-golf-7"
    )
    # not tracked: wrong make, diesel, or a model line we do not follow
    assert (
        classify("Opel", "Astra", "1.4", "/angebote/opel-astra", 2012, "petrol", None, MODELS)
        is None
    )
    assert classify("BMW", "320", "d", url, 2010, "diesel", None, MODELS) is None
    assert (
        classify("BMW", "X5", "3.0", "/angebote/bmw-x5-3-0", 2010, "petrol", None, MODELS) is None
    )


def test_sweep_specs_one_per_brand_per_site() -> None:
    specs = build_sweep_specs(MODELS)
    names = {sp.name for sp in specs}
    assert "Sweep · BMW · autoscout24_de" in names and "Sweep · BMW · kleinanzeigen" in names
    as24 = next(sp for sp in specs if sp.name == "Sweep · BMW · autoscout24_de")
    assert as24.role == "sweep" and as24.interval_minutes == 15
    assert as24.params["path"] == "/lst/bmw" and as24.params["fregfrom"] == 2007
    assert as24.params["max_pages"] == 5 and as24.params.get("fuel") == "B"
    ka = next(sp for sp in specs if sp.name == "Sweep · BMW · kleinanzeigen")
    assert ka.params["make"] == "bmw" and ka.params["ez_from"] == 2007
    makes = {m["make"] for m in MODELS}
    assert len([sp for sp in specs if sp.source == "autoscout24_de"]) == len(makes)
    # long intervals: split into year bands under the site's page cap
    banded = build_sweep_specs(MODELS, interval=720, max_pages=20, band_years=4)
    bmw = [
        sp for sp in banded if sp.source == "autoscout24_de" and sp.name.startswith("Sweep · BMW")
    ]
    assert len(bmw) >= 5 and all(sp.interval_minutes == 720 for sp in bmw)
    assert bmw[0].params["fregfrom"] == 2007 and bmw[0].params["fregto"] == 2010
    assert "fregto" not in bmw[-1].params and bmw[-1].params["max_pages"] == 20
    assert "2007–2010" in bmw[0].name and bmw[-1].name.endswith("+ · autoscout24_de")
    ka = [sp for sp in banded if sp.source == "kleinanzeigen" and "BMW" in sp.name]
    assert ka[0].params["ez_to"] == 2010 and ka[-1].params["ez_to"] == ""


def test_sweep_run_stores_everything_and_never_deactivates(db: Session) -> None:
    u = User(username="u", password_hash="x")
    db.add(u)
    db.commit()
    assert sync_sweeps(db, u, MODELS)["created"] > 0
    db.commit()
    sweep = db.scalar(select(SavedSearch).where(SavedSearch.name == "Sweep · BMW · autoscout24_de"))
    assert sweep is not None and sweep.role == "sweep" and not sweep.alerts_enabled
    client, hits = _as24({"1": "search_page1.html", "2": "search_page2.html"})
    r1 = run_saved_search(sweep.id, client=client, detail_budget=2)
    assert r1.status is ScrapeStatus.ok and r1.new > 20 and r1.bytes > 0
    db.expire_all()
    run = db.get(ScrapeRun, r1.run_id)
    assert run is not None and run.bytes_fetched == r1.bytes and run.pages_fetched == len(hits)
    keyed = db.scalar(
        select(func.count()).select_from(Listing).where(Listing.model_key.isnot(None))
    )
    assert keyed and keyed <= r1.new  # the 3er pages classify, nothing is thrown away
    assert db.scalar(select(func.count()).select_from(Listing)) == r1.new
    # second sweep: page 1 only, nothing new, and nothing goes inactive
    client, hits = _as24({"1": "search_page1.html", "2": "search_page2.html"})
    r2 = run_saved_search(sweep.id, client=client, detail_budget=0)
    assert r2.status is ScrapeStatus.ok and r2.new == 0 and r2.deactivated == 0
    assert len(hits) == 1
    db.expire_all()
    assert (
        db.scalar(select(func.count()).select_from(Listing).where(Listing.is_active.is_(False)))
        == 0
    )
    # idempotent sync
    n = len(build_sweep_specs(MODELS))
    assert sync_sweeps(db, u, MODELS) == {"created": 0, "updated": 0, "paused": 0, "sweeps": n}
    # reshaping to 12-hour banded sweeps pauses the old layout and creates the new one
    r = sync_sweeps(db, u, MODELS, interval=720, max_pages=20, band_years=4)
    assert r["paused"] == n and r["created"] == r["sweeps"] > n
    db.expire_all()
    assert db.get(SavedSearch, sweep.id).active is False
