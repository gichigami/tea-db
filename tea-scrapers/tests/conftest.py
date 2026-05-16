"""Shared pytest configuration.

VCR fixtures live here so integration tests can wrap their HTTP calls in
``with vcr_cassette.use_cassette(...)`` without re-declaring filter rules
in each test module. Spec reference: ``specs/tea_scrapers_v1_spec.md`` §9.

Also exposes :func:`reset_session_caches` and the matching
``session_cache_reset`` fixture — for CLI tests that override
``DATABASE_URL`` per-invocation and need the ``@lru_cache``-wrapped engine
/ settings / session factory in :mod:`tea_scrapers.storage.session` to
re-read the env. New tests should depend on the fixture. The original
inline version in ``test_bronze_loader.py::test_cli_terminal_failure_exit_2``
remains until next-touch of that file (bronze-loader follow-up §12 #3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import vcr


def reset_session_caches() -> None:
    """Clear all ``@lru_cache`` caches in :mod:`tea_scrapers.storage.session`.

    Used by CLI tests that need a per-invocation ``DATABASE_URL`` env
    override to be honored (e.g. terminal-failure exit-2 tests pointing
    at an unreachable DB). Safe to call at start and end of a test — it's
    a pure cache reset, no side effects on Postgres state.

    Exposed as both a plain callable (re-imported in tests that need it as
    a function) and via the :func:`session_cache_reset` pytest fixture for
    tests that prefer fixture-managed setup/teardown.
    """
    from tea_scrapers.storage import session as session_mod

    session_mod.get_engine.cache_clear()
    session_mod._session_factory.cache_clear()  # noqa: SLF001 — test reset
    session_mod._settings.cache_clear()  # noqa: SLF001 — test reset


@pytest.fixture
def session_cache_reset():
    """Pytest fixture: reset session caches around the test body.

    Use in any CLI test that overrides ``DATABASE_URL`` per-invocation so
    the module-level ``lru_cache``d engine doesn't shadow the override.
    Yields :func:`reset_session_caches` so the test can call it again
    mid-body if needed (rare).
    """
    reset_session_caches()
    yield reset_session_caches
    reset_session_caches()

# Cassettes live alongside the tests so they ship with the package and CI
# can replay offline. Hand-curated golden JSONL fixtures live one directory
# over under ``fixtures/golden/``.
_CASSETTE_DIR = Path(__file__).parent / "fixtures" / "cassettes"


def _scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    """Strip cookie-bearing headers from recorded responses.

    Shopify storefronts set ``cart_currency``, ``_shopify_*``, and analytics
    cookies on every ``/products.json`` call. None of them are required for
    replay, and committing them to source control is a PII / session-token
    risk (qa-engineer anti-pattern: "cassettes committed with cookies").
    """
    headers = response.get("headers")
    if isinstance(headers, dict):
        for key in list(headers.keys()):
            if key.lower() in {"set-cookie", "set-cookie2"}:
                del headers[key]
    return response


@pytest.fixture(scope="session")
def vcr_cassette() -> vcr.VCR:
    """A pre-configured :class:`vcr.VCR` instance.

    Default record mode is ``"none"`` — CI must never reach the network.
    To re-record, set ``VCR_RECORD_MODE=once`` (or ``new_episodes``) in the
    environment for the single ``pytest`` invocation that captures the
    cassette, then unset it before committing.
    """
    import os

    return vcr.VCR(
        cassette_library_dir=str(_CASSETTE_DIR),
        record_mode=os.environ.get("VCR_RECORD_MODE", "none"),
        match_on=("method", "scheme", "host", "path", "query"),
        # User-Agent carries a contact email per ``Settings.user_agent``;
        # filtering it keeps cassettes portable and PII-free.
        filter_headers=("authorization", "cookie", "set-cookie", "user-agent"),
        decode_compressed_response=True,
        before_record_response=_scrub_response,
    )
