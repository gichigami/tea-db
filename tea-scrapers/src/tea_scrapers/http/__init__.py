"""Shared HTTP client + per-host rate limiting (spec §4)."""

from tea_scrapers.http.client import HttpClient, ScrapeError
from tea_scrapers.http.ratelimit import HostRateLimiter, TokenBucket

__all__ = ["HttpClient", "ScrapeError", "HostRateLimiter", "TokenBucket"]
