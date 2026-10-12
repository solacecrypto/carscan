from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from app.db.models import SellerType
from app.normalize import RawListing
from app.scrapers import PoliteClient, get_scraper
from app.scrapers.autoscout24_de import AutoScout24DE, build_search_url, extract_next_data
from app.scrapers.errors import ParseError

FIX = Path(__file__).parent / "fixtures" / "autoscout24_de"
NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
PARAMS = {
    "path": "/lst/bmw/3er",
    "cat": "ma13gr100038ge25mt102,ma13gr100038ge25mt103",
    "fuel": "B",
    "body": "5,6",
    "fregfrom": 2008,
    "fregto": 2014,
    "cy": "D",
}


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if request.url.path.startswith("/angebote/"):
            if "00000000" in request.url.path:
                return httpx.Response(
                    301, headers={"location": "https://www.autoscout24.de/lst/bmw/3er"}
                )
            return httpx.Response(200, text=fixture("detail_dealer.html"))
        files = {"1": "search_page1.html", "2": "search_page2.html"}
        return httpx.Response(200, text=fixture(files.get(page, "search_empty.html")))

    return httpx.MockTransport(handler)


@pytest.fixture
def client() -> PoliteClient:
    return PoliteClient(transport=fake_transport(), sleep=lambda _: None)


def test_build_search_url() -> None:
    url = build_search_url(PARAMS, 3)
    assert url.startswith("https://www.autoscout24.de/lst/bmw/3er?")
    assert "cat=ma13gr100038ge25mt102,ma13gr100038ge25mt103" in url
    assert "body=5,6" in url and "page=3" in url and "sort=age" in url and "atype=C" in url


def test_extract_next_data_rejects_other_html() -> None:
    with pytest.raises(ParseError):
        extract_next_data("<html><body>Access denied</body></html>")


def test_search_paginates_and_dedups(client: PoliteClient) -> None:
    scraper = get_scraper("autoscout24_de")
    raws = scraper.search(PARAMS, client)
    assert client.requests_made == 3  # page 1, page 2, then the empty page 3 (numberOfPages=15)
    ids = [r.source_listing_id for r in raws]
    assert len(ids) == len(set(ids))
    assert 20 < len(ids) <= 40
    assert all(r.url.startswith("https://www.autoscout24.de/angebote/") for r in raws)


def test_search_scrubs_private_sellers(client: PoliteClient) -> None:
    raws = get_scraper("autoscout24_de").search(PARAMS, client)
    for r in raws:
        seller = r.data["seller"]
        assert "phones" not in seller and "contactName" not in seller
        if seller["type"] == "PrivateSeller":
            assert "street" not in r.data.get("location", {})


def test_parse_search_listing(client: PoliteClient) -> None:
    scraper = get_scraper("autoscout24_de")
    pp = extract_next_data(fixture("search_page1.html"))
    item = pp["listings"][0]
    raw = RawListing(
        source="autoscout24_de",
        source_listing_id=item["id"],
        url="https://www.autoscout24.de" + item["url"],
        data=item,
        fetched_at=NOW,
    )
    n = scraper.parse(raw)
    assert n.make == "BMW" and n.model in ("318i", "320i")
    assert n.fuel == "petrol" and n.gearbox in ("manual", "automatic", "semi-automatic")
    assert n.price_eur == item["price"]["priceRaw"] and isinstance(n.price_eur, int)
    assert n.mileage_km == int(item["tracking"]["mileage"])
    mm, yyyy = item["tracking"]["firstRegistration"].split("-")
    assert n.first_registration == date(int(yyyy), int(mm), 1)
    assert n.power_kw and 90 <= n.power_kw <= 130
    assert n.country == "DE" and n.city and n.postcode
    assert n.seller_type in (SellerType.dealer, SellerType.private)
    assert n.photos and n.photos[0].endswith("/1280x960.webp")
    assert n.vin is None and n.raw["search"]["id"] == item["id"]


def test_parse_all_fixture_listings_without_error() -> None:
    scraper = get_scraper("autoscout24_de")
    for name in ("search_page1.html", "search_page2.html"):
        for item in extract_next_data(fixture(name))["listings"]:
            raw = RawListing(
                source="autoscout24_de",
                source_listing_id=item["id"],
                url="https://www.autoscout24.de" + item["url"],
                data=item,
                fetched_at=NOW,
            )
            n = scraper.parse(raw)
            assert n.price_eur > 0 and n.first_registration and n.mileage_km is not None


def test_fetch_detail_and_parse_enriched(client: PoliteClient) -> None:
    scraper = get_scraper("autoscout24_de")
    pp = extract_next_data(fixture("search_page1.html"))
    item = next(x for x in pp["listings"] if x["id"].startswith("9c4717b6"))
    raw = RawListing(
        source="autoscout24_de",
        source_listing_id=item["id"],
        url="https://www.autoscout24.de" + item["url"],
        data=item,
        fetched_at=NOW,
    )
    detail = scraper.fetch_detail(raw, client)
    assert detail is not None and detail["status"] == "Active"
    assert "phones" not in detail["seller"] and "whatsappNumber" not in detail
    raw.detail = detail
    n = scraper.parse(raw)
    assert n.model == "318i" and n.body_type == "sedan" and n.gearbox == "automatic"
    assert n.first_registration == date(2011, 8, 1) and n.mileage_km == 145000
    assert n.power_kw == 105 and n.price_eur == 10900 and n.vat_deductible is False
    assert n.co2_g_km == 159
    assert n.seller_type is SellerType.dealer and n.seller_name == "GSK Handelshaus"
    assert n.description and "<br" not in n.description and "Sonderausstattung" in n.description
    assert n.photos == [u for u in n.photos if u.endswith("/1280x960.webp")]
    assert n.raw["detail"]["id"] == item["id"]


def test_fetch_detail_gone_returns_none(client: PoliteClient) -> None:
    raw = RawListing(
        source="autoscout24_de",
        source_listing_id="0",
        url="https://www.autoscout24.de/angebote/x-00000000-0000-0000-0000-000000000000",
        data={},
        fetched_at=NOW,
    )
    assert AutoScout24DE().fetch_detail(raw, client) is None
