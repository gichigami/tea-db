"""Shared pytest configuration.

VCR fixtures live here so integration tests can wrap their HTTP calls in
``with vcr_cassette.use_cassette(...)`` without re-declaring filter rules
in each test module. Spec reference: ``specs/tea_scrapers_v1_spec.md`` §9.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import vcr

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
