"""Polite HTTP client shared by all scrapers."""

import random
import time
from collections.abc import Callable

import httpx
import structlog

from app.scrapers.errors import BlockedError, ScrapeError

log = structlog.get_logger()

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}


class PoliteClient:
    """Sequential GETs with randomized delays, retries with backoff, and block detection.

    `sleep` and `transport` are injectable so tests run instantly and offline.
    """

    def __init__(
        self,
        *,
        min_delay: float = 2.0,
        max_delay: float = 5.0,
        retries: int = 3,
        timeout: float = 20.0,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_delay, self.max_delay, self.retries = min_delay, max_delay, retries
        self._sleep = sleep
        self._last_request_at: float | None = None
        # No keep-alive: with seconds between requests the server drops idle connections and
        # reusing one yields "server disconnected" errors. A fresh connection per request is
        # also what a casual browser session looks like.
        self._client = httpx.Client(
            headers={**DEFAULT_HEADERS, **(headers or {}), "Connection": "close"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=1),
        )
        self.requests_made = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _wait(self) -> None:
        if self._last_request_at is not None:
            delay = random.uniform(self.min_delay, self.max_delay)
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < delay:
                self._sleep(delay - elapsed)
        self._last_request_at = time.monotonic()

    def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """GET with retries. Returns 2xx/3xx responses; raises BlockedError / ScrapeError."""
        attempt = 0
        while True:
            attempt += 1
            self._wait()
            self.requests_made += 1
            try:
                resp = self._client.get(url, headers=headers)
            except httpx.HTTPError as e:
                if attempt > self.retries:
                    raise ScrapeError(f"network error after {attempt} attempts: {e}") from e
                self._backoff(attempt, url, str(e))
                continue
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                if attempt > self.retries:
                    if resp.status_code in (403, 429):
                        raise BlockedError(f"HTTP {resp.status_code} for {url}")
                    raise ScrapeError(f"HTTP {resp.status_code} for {url}")
                self._backoff(attempt, url, f"HTTP {resp.status_code}")
                continue
            return resp

    def _backoff(self, attempt: int, url: str, reason: str) -> None:
        delay = min(60.0, (2**attempt) + random.uniform(0, 1))
        log.warning("http.retry", url=url, attempt=attempt, reason=reason, sleep=round(delay, 1))
        self._sleep(delay)
