from __future__ import annotations

import logging

import httpx
import pytest
import structlog

from tea_scrapers.http.client import HttpClient, ScrapeError
from tea_scrapers.http.ratelimit import HostRateLimiter


@pytest.fixture
def captured_logs():
    cap = structlog.testing.LogCapture()
    structlog.configure(
        processors=[cap],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        cache_logger_on_first_use=False,
    )
    yield cap
    structlog.reset_defaults()


@pytest.fixture
def sleeps():
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, fake_sleep


def _client(httpx_mock, **overrides):
    sleeps_list, fake_sleep = overrides.pop("_sleep_pair", ([], lambda _s: None))
    limiter = overrides.pop("rate_limiter", HostRateLimiter(default_rps=2.0))
    return (
        HttpClient(
            rate_limiter=limiter,
            sleep=fake_sleep,
            client=httpx.Client(),
            **overrides,
        ),
        sleeps_list,
    )


def test_get_returns_response_on_2xx(httpx_mock):
    httpx_mock.add_response(url="https://example.test/a", json={"ok": True})
    client, _ = _client(httpx_mock)
    response = client.get("https://example.test/a")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_get_json_returns_parsed_body(httpx_mock):
    httpx_mock.add_response(url="https://example.test/j", json={"x": 1})
    client, _ = _client(httpx_mock)
    assert client.get_json("https://example.test/j") == {"x": 1}


def test_retries_on_5xx_then_succeeds(httpx_mock, captured_logs):
    sleeps_list: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps_list.append(seconds)

    httpx_mock.add_response(url="https://example.test/r", status_code=503)
    httpx_mock.add_response(url="https://example.test/r", status_code=503)
    httpx_mock.add_response(url="https://example.test/r", json={"ok": True})

    client, _ = _client(httpx_mock, _sleep_pair=(sleeps_list, fake_sleep))
    response = client.get("https://example.test/r")
    assert response.status_code == 200

    backoff_sleeps = [s for s in sleeps_list if s > 0]
    assert backoff_sleeps[:2] == [1.0, 2.0]

    request_events = [e for e in captured_logs.entries if e["event"] == "scrape.request"]
    assert [e["status"] for e in request_events] == [503, 503, 200]
    assert all("duration_ms" in e for e in request_events)


def test_terminal_5xx_raises_scrape_error(httpx_mock):
    for _ in range(4):
        httpx_mock.add_response(url="https://example.test/x", status_code=500)
    client, _ = _client(httpx_mock)
    with pytest.raises(ScrapeError) as exc_info:
        client.get("https://example.test/x")
    assert "500" in str(exc_info.value)


def test_auth_failure_is_terminal_without_retry(httpx_mock):
    httpx_mock.add_response(url="https://example.test/u", status_code=403)
    client, _ = _client(httpx_mock)
    with pytest.raises(ScrapeError):
        client.get("https://example.test/u")


def test_rate_limiter_invoked_per_request(httpx_mock):
    httpx_mock.add_response(url="https://slow.test/a", json={})
    httpx_mock.add_response(url="https://slow.test/b", json={})

    sleeps_list: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps_list.append(seconds)

    limiter = HostRateLimiter(default_rps=1.0)
    client, _ = _client(
        httpx_mock,
        rate_limiter=limiter,
        _sleep_pair=(sleeps_list, fake_sleep),
    )
    client.get("https://slow.test/a")
    client.get("https://slow.test/b")

    waits = [s for s in sleeps_list if s > 0]
    assert waits, "expected the limiter to throttle the second request"
    assert all(0 < w <= 1.0 + 1e-6 for w in waits)


def test_per_host_rps_override(httpx_mock):
    httpx_mock.add_response(url="https://throttled.test/a", json={})
    httpx_mock.add_response(url="https://throttled.test/b", json={})

    sleeps_list: list[float] = []
    limiter = HostRateLimiter(default_rps=10.0)
    client, _ = _client(
        httpx_mock,
        rate_limiter=limiter,
        _sleep_pair=(sleeps_list, lambda s: sleeps_list.append(s)),
    )
    client.set_host_rps("throttled.test", 1.0)
    client.get("https://throttled.test/a")
    client.get("https://throttled.test/b")

    waits = [s for s in sleeps_list if s > 0]
    assert any(0.5 < w <= 1.0 + 1e-6 for w in waits)


def test_retry_after_header_honored(httpx_mock):
    httpx_mock.add_response(
        url="https://example.test/q",
        status_code=429,
        headers={"Retry-After": "7"},
    )
    httpx_mock.add_response(url="https://example.test/q", json={})

    sleeps_list: list[float] = []
    client, _ = _client(
        httpx_mock,
        _sleep_pair=(sleeps_list, lambda s: sleeps_list.append(s)),
    )
    client.get("https://example.test/q")
    assert 7.0 in sleeps_list


def test_log_event_names_are_canonical(httpx_mock, captured_logs):
    httpx_mock.add_response(url="https://example.test/l", json={})
    client, _ = _client(httpx_mock)
    client.get("https://example.test/l")
    events = {e["event"] for e in captured_logs.entries}
    assert "scrape.request" in events


def test_retries_on_transport_error_then_succeeds(httpx_mock, captured_logs):
    sleeps_list: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps_list.append(seconds)

    httpx_mock.add_exception(httpx.ReadError("boom"), url="https://example.test/t")
    httpx_mock.add_exception(httpx.ConnectError("nope"), url="https://example.test/t")
    httpx_mock.add_response(url="https://example.test/t", json={"ok": True})

    client, _ = _client(httpx_mock, _sleep_pair=(sleeps_list, fake_sleep))
    response = client.get("https://example.test/t")
    assert response.status_code == 200

    backoff_sleeps = [s for s in sleeps_list if s > 0]
    assert backoff_sleeps[:2] == [1.0, 2.0]

    request_events = [e for e in captured_logs.entries if e["event"] == "scrape.request"]
    assert [e["status"] for e in request_events] == [None, None, 200]
    assert request_events[0]["error"] == "ReadError"
    assert request_events[1]["error"] == "ConnectError"


def test_non_retryable_4xx_raises_without_retry(httpx_mock):
    httpx_mock.add_response(url="https://example.test/n", status_code=404)
    client, _ = _client(httpx_mock)
    with pytest.raises(ScrapeError) as exc_info:
        client.get("https://example.test/n")
    assert "404" in str(exc_info.value)
    assert len(httpx_mock.get_requests()) == 1


def test_per_host_bucket_isolation(httpx_mock):
    httpx_mock.add_response(url="https://a.example.com/x", json={})
    httpx_mock.add_response(url="https://b.example.com/x", json={})

    sleeps_list: list[float] = []
    limiter = HostRateLimiter(default_rps=1.0)
    client, _ = _client(
        httpx_mock,
        rate_limiter=limiter,
        _sleep_pair=(sleeps_list, lambda s: sleeps_list.append(s)),
    )
    client.get("https://a.example.com/x")
    client.get("https://b.example.com/x")

    waits = [s for s in sleeps_list if s > 0]
    assert waits == [], (
        "second host should not throttle while first host's bucket is empty; "
        f"observed waits={waits}"
    )
