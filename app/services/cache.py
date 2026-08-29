"""In-process TTL cache.

Caching is not an optimisation here, it is a defence: every cache hit is a
LinkedIn page load that does not happen, which is the single biggest factor in
how long a scraping account survives. Repeated lookups of the same profile are
the common access pattern, so the default TTL is a full day.

Single-process only. A multi-instance deployment should swap this for Redis -
the interface is deliberately small enough that it is a drop-in change.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, *, ttl_seconds: int, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    async def get(self, key: str) -> T | None:
        if self._ttl <= 0:
            return None
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)  # LRU touch
            return value

    async def set(self, key: str, value: T) -> None:
        if self._ttl <= 0:
            return
        async with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
