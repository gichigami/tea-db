"""Shared HTTP client (spec §4)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from tea_scrapers.config import Settings, get_settings
from tea_scrapers.http.ratelimit import HostRateLimiter


class ScrapeError(Exception):
    """Terminal failure raised after retries are exhausted or on auth errors."""


_DEFAULT_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0
_DEFAULT_TIMEOUT = 30.0


class HttpClient:
    """httpx wrapper with per-host rate limiting, retries, UA injection, and structured logs."""

    RETRYABLE_STATUSES: frozenset[int] = frozenset(
        {408, 425, 429, 500, 502, 503, 504}
    )

    def __init__(
        self,
        settings: Settings | None = None,
        per_host_rps: float = 2.0,
        max_retries: int = _DEFAULT_RETRIES,
        backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE,
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        rate_limiter: HostRateLimiter | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._rate_limiter = rate_limiter or HostRateLimiter(default_rps=per_host_rps)
        self._sleep = sleep
        self._log = log or structlog.get_logger()
        self._client = client or httpx.Client(
            headers={"User-Agent": self._settings.user_agent},
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    @property
    def rate_limiter(self) -> HostRateLimiter:
        return self._rate_limiter

    def set_host_rps(self, host: str, rps: float) -> None:
        self._rate_limiter.set_host_rps(host, rps)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        response = self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise ScrapeError(f"non-JSON response from {url}: {exc}") from exc

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            self._rate_limiter.acquire(url, sleep=self._sleep)
            started = time.monotonic()
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                duration_ms = int((time.monotonic() - started) * 1000)
                self._log.warning(
                    "scrape.request",
                    url=url,
                    status=None,
                    duration_ms=duration_ms,
                    error=type(exc).__name__,
                    attempt=attempt,
                )
                if attempt >= self._max_retries:
                    raise ScrapeError(
                        f"{method} {url} failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                self._sleep(self._backoff_base * (2**attempt))
                attempt += 1
                continue

            duration_ms = int((time.monotonic() - started) * 1000)
            self._log.info(
                "scrape.request",
                url=url,
                status=response.status_code,
                duration_ms=duration_ms,
                attempt=attempt,
            )

            if response.status_code in (401, 403):
                raise ScrapeError(
                    f"{method} {url} returned {response.status_code} (auth/terminal)"
                )

            if response.status_code in self.RETRYABLE_STATUSES:
                if attempt >= self._max_retries:
                    raise ScrapeError(
                        f"{method} {url} returned {response.status_code} "
                        f"after {attempt + 1} attempts"
                    )
                retry_after = self._retry_after_seconds(response)
                delay = retry_after if retry_after is not None else self._backoff_base * (2**attempt)
                self._sleep(delay)
                attempt += 1
                continue

            if response.status_code >= 400:
                raise ScrapeError(
                    f"{method} {url} returned {response.status_code} (non-retryable)"
                )

            return response

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        header = response.headers.get("Retry-After")
        if not header:
            return None
        try:
            return max(0.0, float(header))
        except ValueError:
            return None


__all__ = ["HttpClient", "ScrapeError"]
