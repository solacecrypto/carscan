"""Scraper registry. Import this package to have every source registered."""

from app.scrapers import autoscout24_de, gaspedaal, marktplaats  # noqa: F401
from app.scrapers.base import Scraper, all_scrapers, get_scraper, register
from app.scrapers.errors import BlockedError, ParseError, ScrapeError
from app.scrapers.http import PoliteClient

__all__ = [
    "BlockedError",
    "ParseError",
    "PoliteClient",
    "ScrapeError",
    "Scraper",
    "all_scrapers",
    "get_scraper",
    "register",
]
