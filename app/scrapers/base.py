from typing import Any, Protocol, runtime_checkable

from app.normalize.schema import NormalizedListing, RawListing
from app.scrapers.http import PoliteClient


@runtime_checkable
class Scraper(Protocol):
    name: str
    country: str

    def search(self, params: dict[str, Any], client: PoliteClient) -> list[RawListing]:
        """Fetch all result pages for `params`, deduplicated within the source."""
        ...

    def parse(self, raw: RawListing) -> NormalizedListing:
        """Map a raw listing (plus optional detail data) into the shared schema."""
        ...

    def fetch_detail(self, raw: RawListing, client: PoliteClient) -> dict[str, Any] | None:
        """Fetch the detail page. Returns None when the listing is gone from the source."""
        ...


_REGISTRY: dict[str, Scraper] = {}


def register(scraper: Scraper) -> Scraper:
    _REGISTRY[scraper.name] = scraper
    return scraper


def get_scraper(name: str) -> Scraper:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown scraper {name!r}; known: {sorted(_REGISTRY)}") from None


def all_scrapers() -> dict[str, Scraper]:
    return dict(_REGISTRY)
