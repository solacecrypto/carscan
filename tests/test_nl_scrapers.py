import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from app.db.models import SellerType
from app.scrapers import PoliteClient, get_scraper
from app.scrapers.autoscout24_de import build_search_url as as24_url
from app.scrapers.marktplaats import build_api_url

FIX = Path(__file__).parent / "fixtures"


def _client(handler) -> PoliteClient:  # type: ignore[no-untyped-def]
    return PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda _: None)


def test_autoscout24_nl_uses_nl_origin_and_same_parser() -> None:
    assert as24_url(
        {"path": "/lst/bmw", "cy": "NL"}, 1, origin="https://www.autoscout24.nl"
    ).startswith("https://www.autoscout24.nl/lst/bmw?")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.autoscout24.nl"
        if request.url.params.get("page") == "1":
            return httpx.Response(
                200, text=(FIX / "autoscout24_nl" / "search_page1.html").read_text()
            )
        return httpx.Response(200, text=(FIX / "autoscout24_de" / "search_empty.html").read_text())

    s = get_scraper("autoscout24_nl")
    raws = s.search({"path": "/lst/bmw", "cy": "NL", "fuel": "B"}, _client(handler))
    assert len(raws) == 20 and all(r.url.startswith("https://www.autoscout24.nl/") for r in raws)
    n = s.parse(raws[0])
    assert (
        n.source == "autoscout24_nl"
        and n.country == "NL"
        and n.model in ("318i", "320i")
        and n.price_eur > 0
    )


def test_marktplaats_url_and_parse() -> None:
    url = build_api_url(
        {
            "l2CategoryId": 96,
            "model": "3-Serie",
            "fuel": "Benzine",
            "year_from": 2008,
            "year_to": 2014,
            "query": "320i",
        },
        30,
    )
    assert (
        "attributesByKey%5B%5D=model%3A3-Serie" in url
        and "constructionYear%3A2008%3A2014" in url
        and "offset=30" in url
    )

    data = json.loads((FIX / "marktplaats" / "search_page1.json").read_text())
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("offset"))
        if request.url.params.get("offset") == "0":
            return httpx.Response(200, json=data)
        return httpx.Response(
            200,
            json={
                "listings": [],
                "totalResultCount": data["totalResultCount"],
                "maxAllowedPageNumber": 27,
            },
        )

    s = get_scraper("marktplaats")
    params = {
        "l2CategoryId": 96,
        "model": "3-Serie",
        "fuel": "Benzine",
        "motor_regex": r"\b(318i|320i)\b",
        "bodies": ["Sedan", "Stationwagon"],
    }
    raws = s.search(params, _client(handler))
    assert calls[:2] == ["0", "30"]
    assert 0 < len(raws) < 30  # coupés, cabrios and non-matching titles dropped
    for r in raws:
        assert "sellerName" not in (r.data.get("sellerInformation") or {})
        n = s.parse(r)
        assert n.source == "marktplaats" and n.country == "NL" and n.model in ("318i", "320i")
        assert (
            n.body_type in ("sedan", "estate")
            and n.fuel == "petrol"
            and n.gearbox in ("manual", "automatic")
        )
        assert (
            n.first_registration
            and 2008 <= n.first_registration.year <= 2014
            and n.raw["year_only"]
        )
        assert (
            (n.mileage_km is None or n.mileage_km > 0)
            and n.price_eur > 1000
            and n.url.startswith("https://www.marktplaats.nl/v/")
        )
        assert n.seller_name is None and n.seller_type in (SellerType.dealer, SellerType.private)
    # a known row from the fixture
    row = next(s.parse(r) for r in raws if r.source_listing_id == "m2450398943")
    assert (
        row.price_eur == 6650
        and row.mileage_km == 292813
        and row.gearbox == "automatic"
        and row.first_registration == date(2012, 1, 1)
    )


def test_gaspedaal_parse_and_pagination() -> None:
    html = (FIX / "gaspedaal" / "search_page1.html").read_text()
    pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params.get("page"))
        return httpx.Response(200, text=html)

    s = get_scraper("gaspedaal")
    params = {
        "path": "/bmw/3-serie/benzine",
        "bmin": 2008,
        "bmax": 2014,
        "motor_regex": r"\b(318i|320i)\b",
        "bodies": ["Sedan", "Stationwagon"],
    }
    raws = s.search(params, _client(handler))
    assert pages[0] is None and pages[1] == "2" and len(pages) == 9  # 880 items / 100 per page
    assert 0 < len(raws) < 100
    for r in raws:
        n = s.parse(r)
        assert (
            n.source == "gaspedaal"
            and n.model in ("318i", "320i")
            and n.body_type in ("sedan", "estate")
        )
        assert (
            n.first_registration
            and n.mileage_km is not None
            and n.price_eur > 500
            and n.seller_type is SellerType.dealer
        )
        assert n.fuel == "petrol" and n.power_kw


def test_registry_has_all_sources() -> None:
    from app.scrapers import all_scrapers

    assert set(all_scrapers()) >= {"autoscout24_de", "autoscout24_nl", "marktplaats", "gaspedaal"}
    assert datetime.now(UTC).year >= 2026
