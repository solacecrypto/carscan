import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.db.models import SellerType
from app.normalize.money import to_eur
from app.scrapers import PoliteClient, all_scrapers, get_scraper

FIX = Path(__file__).parent / "fixtures"


def _client(handler) -> PoliteClient:  # type: ignore[no-untyped-def]
    return PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda _: None)


def _one_page(name: str, html_or_json: str | dict, empty: str | dict | None = None):  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        first = len(calls) == 1
        body = html_or_json if first else (empty if empty is not None else html_or_json)
        if isinstance(body, dict):
            return httpx.Response(200, json=body)
        return httpx.Response(200, text=body)

    return handler, calls


def test_registry() -> None:
    assert {
        "autoscout24_be",
        "autoscout24_it",
        "tweedehands_be",
        "kleinanzeigen",
        "subito",
        "automobile_it",
        "bytbil",
    } <= set(all_scrapers())


def test_sek_conversion() -> None:
    assert to_eur(112000, "SEK") == 10000
    with pytest.raises(ValueError):
        to_eur(1, "NOK")


def test_autoscout24_be() -> None:
    html = (FIX / "autoscout24_be" / "search_page1.html").read_text()
    empty = (FIX / "autoscout24_de" / "search_empty.html").read_text()
    handler, calls = _one_page("be", html, empty)
    s = get_scraper("autoscout24_be")
    raws = s.search({"path": "/nl/lst/bmw", "cy": "B"}, _client(handler))
    assert calls[0].startswith("https://www.autoscout24.be/nl/lst/bmw?") and len(raws) == 20
    assert {s.parse(r).gearbox for r in raws} <= {"manual", "automatic", "semi-automatic", None}
    n = s.parse(raws[0])
    assert (
        n.source == "autoscout24_be"
        and n.country == "BE"
        and n.url.startswith("https://www.autoscout24.be/nl/aanbod/")
        and n.model in ("318i", "320i")
    )


def test_autoscout24_it_filters_models_client_side() -> None:
    html = (FIX / "autoscout24_it" / "search_page1.html").read_text()
    empty = (FIX / "autoscout24_de" / "search_empty.html").read_text()
    handler, calls = _one_page("it", html, empty)
    s = get_scraper("autoscout24_it")
    raws = s.search(
        {"path": "/lst/bmw/serie-3", "fuel": "B", "cy": "I", "models": ["318", "320"]},
        _client(handler),
    )
    assert "models" not in calls[0] and calls[0].startswith(
        "https://www.autoscout24.it/lst/bmw/serie-3?"
    )
    assert 0 < len(raws) < 20
    for r in raws:
        n = s.parse(r)
        assert (
            n.model in ("318i", "320i")
            and n.country == "IT"
            and n.fuel == "petrol"
            and n.price_eur > 0
        )


def test_tweedehands() -> None:
    data = json.loads((FIX / "tweedehands_be" / "search_page1.json").read_text())
    handler, calls = _one_page(
        "2dh", data, {"listings": [], "totalResultCount": 26, "maxAllowedPageNumber": 27}
    )
    s = get_scraper("tweedehands_be")
    raws = s.search(
        {"l2CategoryId": 96, "model": "3-Serie", "motor_regex": r"\b320i\b"}, _client(handler)
    )
    assert calls[0].startswith("https://www.2dehands.be/lrp/api/search?") and raws
    n = s.parse(raws[0])
    assert (
        n.source == "tweedehands_be"
        and n.country == "BE"
        and n.url.startswith("https://www.2dehands.be/v/")
        and n.model == "320i"
    )


def test_kleinanzeigen_parse_and_pagination() -> None:
    html = (FIX / "kleinanzeigen" / "search_page1.html").read_text()
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.path)
        return httpx.Response(
            200, text=html if len(pages) == 1 else "<html><body>k0c216</body></html>"
        )

    s = get_scraper("kleinanzeigen")
    params = {
        "make": "bmw",
        "query": "320i",
        "ez_from": 2008,
        "ez_to": 2014,
        "motor_regex": r"\b320i\b",
    }
    raws = s.search(params, _client(handler))
    assert pages[0] == "/s-autos/bmw/320i/k0c216+autos.ez_i:2008,2014+autos.marke_s:bmw"
    assert pages[1] == "/s-autos/bmw/seite:2/320i/k0c216+autos.ez_i:2008,2014+autos.marke_s:bmw"
    assert len(raws) >= 15
    parsed = [s.parse(r) for r in raws]
    assert all(
        p.price_eur > 0
        and p.model == "320i"
        and p.url.startswith("https://www.kleinanzeigen.de/s-anzeige/")
        for p in parsed
    )
    with_reg = [p for p in parsed if p.first_registration]
    assert len(with_reg) >= len(parsed) - 2 and all(
        2008 <= p.first_registration.year <= 2014 for p in with_reg
    )  # type: ignore[union-attr]
    assert (
        sum(1 for p in parsed if p.mileage_km) >= len(parsed) - 2
        and sum(1 for p in parsed if p.postcode) >= len(parsed) - 2
    )
    assert all(p.seller_type is SellerType.private and p.seller_name is None for p in parsed)
    sample = next(p for p in parsed if p.raw["search"]["title"] == "BMW 320i F30 Sportline")
    assert (
        sample.price_eur == 8900
        and sample.mileage_km == 142200
        and sample.first_registration == date(2012, 7, 1)
        and sample.postcode == "66111"
        and sample.raw["negotiable"]
    )


def test_subito_parse() -> None:
    html = (FIX / "subito" / "search_page1.html").read_text()
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(str(request.url))
        return httpx.Response(200, text=html)

    s = get_scraper("subito")
    raws = s.search(
        {
            "q": "bmw 320i",
            "ys": 2008,
            "ye": 2014,
            "motor_regex": r"\b320i\b",
            "bodies": ["Berlina", "Station Wagon"],
        },
        _client(handler),
    )
    assert (
        pages[0] == "https://www.subito.it/annunci-italia/vendita/auto/?q=bmw+320i&ys=2008&ye=2014"
        and pages[1].endswith("&o=2")
        and len(pages) == 2
    )
    assert 0 < len(raws) <= 30
    for r in raws:
        assert "phone" not in r.data["advertiser"]
        n = s.parse(r)
        assert (
            n.model == "320i"
            and n.country == "IT"
            and n.body_type in ("sedan", "estate")
            and n.price_eur > 500
            and n.first_registration
        )
        assert n.url.startswith("https://www.subito.it/auto/")
    first = (
        s.parse(next(r for r in raws if r.source_listing_id == "660769846"))
        if any(r.source_listing_id == "660769846" for r in raws)
        else None
    )
    if first:  # the coupé sample is filtered out by bodies; only check when present
        assert first.first_registration == date(2008, 4, 1)


def test_automobile_it_parse_and_fuel_filter() -> None:
    html = (FIX / "automobile_it" / "search_page1.html").read_text()
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(str(request.url))
        return httpx.Response(200, text=html)

    s = get_scraper("automobile_it")
    raws = s.search(
        {
            "path": "/bmw-serie_3",
            "attr_m_year_min": 2008,
            "attr_m_year_max": 2014,
            "fuel": "benzina",
            "max_pages": 2,
        },
        _client(handler),
    )
    assert (
        pages[0]
        == "https://www.automobile.it/bmw-serie_3?attr_m_year_min=2008&attr_m_year_max=2014&p=1"
        and len(pages) == 2
    )
    for r in raws:
        n = s.parse(r)
        assert (
            n.fuel == "petrol"
            and n.country == "IT"
            and n.first_registration
            and n.price_eur > 0
            and n.url.startswith("https://www.automobile.it/")
        )
    # the fixture page is mostly diesel; the parser itself is checked on a diesel row
    all_raws = s.search(
        {"path": "/bmw-serie_3", "max_pages": 1}, _client(lambda r: httpx.Response(200, text=html))
    )
    n = s.parse(next(r for r in all_raws if r.source_listing_id == "179368987"))
    assert (
        n.model == "320d"
        and n.price_eur == 6300
        and n.first_registration == date(2011, 5, 1)
        and n.mileage_km == 200000
        and n.gearbox == "automatic"
    )
    assert n.seller_type is SellerType.dealer and n.city == "Catania" and n.fuel == "diesel"


def test_bytbil_parse() -> None:
    html = (FIX / "bytbil" / "search_page1.html").read_text()
    s = get_scraper("bytbil")
    raws = s.search(
        {
            "Makes": "BMW",
            "Models": "3-serien",
            "ModelYearRange.From": 2008,
            "ModelYearRange.To": 2014,
            "Fuels": "Bensin",
        },
        _client(lambda r: httpx.Response(200, text=html)),
    )
    assert len(raws) == 3
    n = s.parse(next(r for r in raws if r.source_listing_id == "16429570"))
    assert (
        n.model == "320i"
        and n.first_registration == date(2008, 1, 1)
        and n.mileage_km == 200000
        and n.country == "SE"
        and n.city == "Gävle"
    )
    assert (
        n.body_type == "coupe"
        and n.gearbox == "automatic"
        and n.raw["price_sek"] > 0
        and n.price_eur == to_eur(n.raw["price_sek"], "SEK")
    )
