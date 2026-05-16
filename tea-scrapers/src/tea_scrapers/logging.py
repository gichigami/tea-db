"""structlog configuration.

Spec reference: specs/tea_scrapers_v1_spec.md §4 (Logging).

Standard event names emitted by scrapers include:
  - scrape.run.start    (run_id, source, mode)
  - scrape.request      (url, status, duration_ms)
  - scrape.record       (external_id, payload_bytes)   [debug]
  - scrape.run.end      (records_count, errors_count, duration_s)

Run-scoped context (``run_id``, ``source``, ``mode``) is injected via
``structlog.contextvars`` so every downstream log line inside a
:class:`tea_scrapers.storage.run_tracker.RunTracker` carries it automatically.
"""

from __future__ import annotations

import logging

import structlog

from tea_scrapers.config import get_settings


def configure_logging(level: str | None = None) -> None:
    """Wire up structlog with a JSON renderer at the configured level.

    Call once at process start (CLI entrypoint). Idempotent enough that
    repeated calls in tests don't blow up.
    """
    settings = get_settings()
    resolved = (level or settings.log_level).upper()

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, resolved, logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, resolved, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_run_context(run_id: str, source: str, mode: str) -> None:
    """Bind run-scoped context to the current execution context."""
    structlog.contextvars.bind_contextvars(run_id=run_id, source=source, mode=mode)


def unbind_run_context() -> None:
    """Clear the run-scoped context keys bound by :func:`bind_run_context`."""
    structlog.contextvars.unbind_contextvars("run_id", "source", "mode")


__all__ = ["configure_logging", "bind_run_context", "unbind_run_context"]
