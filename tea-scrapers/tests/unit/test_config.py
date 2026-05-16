"""``Settings`` env-driven config tests.

Spec reference: specs/tea_scrapers_v1_spec.md §4, §12 (Shopify storefront
bot mitigation). The placeholder-UA validator is the V1.1 ops guardrail
that turns a silent Cloudflare 403 into a loud startup error.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tea_scrapers.config import Settings

REAL_LOOKING_UA = (
    "tea-db-scraper/0.1 "
    "(https://github.com/gichigami/tea-db; contact: gjohnson@pioneer-aero.com)"
)


def test_settings_rejects_placeholder_user_agent():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            user_agent=(
                "tea-rec-engine/0.1 "
                "(https://github.com/gary/tea; contact: gary@...)"
            ),
            reddit_user_agent=REAL_LOOKING_UA,
        )
    assert "user_agent" in str(exc_info.value)
    assert "github.com/gary/tea" in str(exc_info.value)


def test_settings_rejects_placeholder_reddit_user_agent():
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            user_agent=REAL_LOOKING_UA,
            reddit_user_agent="tea-rec-engine/0.1 (contact: gary@...)",
        )
    assert "reddit_user_agent" in str(exc_info.value)
    assert "contact: gary@" in str(exc_info.value)


def test_settings_accepts_real_looking_user_agent():
    settings = Settings(
        user_agent=REAL_LOOKING_UA,
        reddit_user_agent=REAL_LOOKING_UA,
    )
    assert settings.user_agent == REAL_LOOKING_UA
    assert settings.reddit_user_agent == REAL_LOOKING_UA


def test_class_default_user_agent_is_still_placeholder():
    """Guard against a future maintainer setting a working default.

    The class-default ``user_agent`` must contain a placeholder marker so
    missing-env (no ``.env`` override) trips the validator at startup. If
    this test fails, the validator has been silently bypassed by changing
    the default to a real-looking value.
    """
    default_ua = Settings.model_fields["user_agent"].default
    assert "github.com/gary/tea" in default_ua or "contact: gary@" in default_ua, (
        f"Settings.user_agent default no longer matches a placeholder "
        f"marker: {default_ua!r}. Either update _PLACEHOLDER_UA_MARKERS or "
        f"restore a placeholder default."
    )


def test_class_default_reddit_user_agent_is_still_placeholder():
    """Same guard for ``reddit_user_agent``."""
    default_ra = Settings.model_fields["reddit_user_agent"].default
    assert "contact: gary@" in default_ra, (
        f"Settings.reddit_user_agent default no longer matches a placeholder "
        f"marker: {default_ra!r}."
    )


def test_validator_is_case_insensitive():
    """An operator who uppercases the placeholder doesn't bypass the check."""
    with pytest.raises(ValidationError):
        Settings(
            user_agent="MyBot/1.0 (GITHUB.COM/GARY/TEA; contact: ME@me.com)",
            reddit_user_agent=REAL_LOOKING_UA,
        )
