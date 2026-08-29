"""Fixed-window rate limiter, keyed per caller.

Protects the scraping account as much as the service: without a ceiling, one
enthusiastic client can burn through a LinkedIn account's daily view budget in
minutes. In-process only - behind multiple replicas, move this to Redis.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> tuple[bool, int, int]:
        """Return ``(allowed, remaining, retry_after_seconds)``."""
        now = time.monotonic()
        async with self._lock:
            window_start, count = self._hits.get(key, (now, 0))
            elapsed = now - window_start

            if elapsed >= self._window:
                window_start, count = now, 0
                elapsed = 0.0

            if count >= self._limit:
                retry_after = max(1, int(self._window - elapsed) + 1)
                return False, 0, retry_after

            self._hits[key] = (window_start, count + 1)
            self._prune(now)
            return True, self._limit - (count + 1), 0

    def _prune(self, now: float) -> None:
        """Drop windows that have fully expired so the dict cannot grow forever."""
        if len(self._hits) < 1024:
            return
        stale = [k for k, (start, _) in self._hits.items() if now - start >= self._window]
        for key in stale:
            self._hits.pop(key, None)
