"""Per-host token bucket (spec §4).

Single-process, thread-safe via a coarse lock. V1 scrapers run sequentially
under cron and share no state across processes; if that ever changes, swap
the dict-of-buckets for a redis-backed bucket — the public API stays the same.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class TokenBucket:
    rate_per_sec: float
    capacity: float
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        # Monotonic clock — wall-clock jumps (NTP, DST) must not poison the bucket.
        self._last_refill = time.monotonic()

    def acquire(self, sleep: Callable[[float], None] = time.sleep) -> float:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._last_refill = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_sec)

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0

            deficit = 1.0 - self._tokens
            wait = deficit / self.rate_per_sec
            self._tokens = 0.0

        sleep(wait)
        return wait


class HostRateLimiter:
    """Lazily-created per-host token buckets."""

    def __init__(self, default_rps: float = 2.0, burst: float | None = None) -> None:
        self._default_rps = default_rps
        self._default_burst = burst if burst is not None else default_rps
        self._overrides: dict[str, float] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def set_host_rps(self, host: str, rps: float) -> None:
        self._overrides[host.lower()] = rps
        self._buckets.pop(host.lower(), None)

    def acquire(self, url: str, sleep: Callable[[float], None] = time.sleep) -> float:
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                rps = self._overrides.get(host, self._default_rps)
                bucket = TokenBucket(rate_per_sec=rps, capacity=max(rps, 1.0))
                self._buckets[host] = bucket
        return bucket.acquire(sleep=sleep)


__all__ = ["HostRateLimiter", "TokenBucket"]
