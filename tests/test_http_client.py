import httpx
import pytest

from app.scrapers.errors import BlockedError, ScrapeError
from app.scrapers.http import PoliteClient


def _client(responses: list[int], sleeps: list[float]) -> PoliteClient:
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(it), text="x")

    return PoliteClient(transport=httpx.MockTransport(handler), sleep=sleeps.append, retries=2)


def test_delays_between_requests() -> None:
    sleeps: list[float] = []
    c = _client([200, 200, 200], sleeps)
    for _ in range(3):
        c.get("https://example.com/")
    assert len(sleeps) == 2 and all(1.5 < s <= 5.0 for s in sleeps)


def test_retries_then_succeeds_on_5xx() -> None:
    sleeps: list[float] = []
    c = _client([503, 503, 200], sleeps)
    assert c.get("https://example.com/").status_code == 200
    assert c.requests_made == 3 and len(sleeps) >= 2


def test_blocked_after_retries() -> None:
    c = _client([429, 429, 429], [])
    with pytest.raises(BlockedError):
        c.get("https://example.com/")


def test_server_error_after_retries() -> None:
    c = _client([500, 500, 500], [])
    with pytest.raises(ScrapeError):
        c.get("https://example.com/")


def test_redirects_are_not_followed() -> None:
    c = _client([301], [])
    assert c.get("https://example.com/").status_code == 301


def test_realistic_headers() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200)

    PoliteClient(transport=httpx.MockTransport(handler), sleep=lambda _: None).get("https://e.com/")
    assert "Chrome" in seen["user-agent"] and seen["accept-language"].startswith("de-DE")
