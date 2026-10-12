"""Tiny in-memory sliding-window limiter for the login form. One web process, so this is enough."""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int = 8, window_seconds: int = 900) -> None:
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits[key]
        while q and q[0] < now - self.window:
            q.popleft()
        return q

    def is_limited(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        self._prune(key, time.monotonic()).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


login_limiter = RateLimiter()
