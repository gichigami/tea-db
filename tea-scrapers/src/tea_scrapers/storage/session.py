"""SQLAlchemy engine + session factory.

One module-level engine bound to ``Settings.database_url``. Callers should
use the ``get_session()`` context manager so commit / rollback / close are
handled uniformly. Loaders and normalizers are the only intended callers —
scrapers never touch Postgres directly (see scrapers spec §11).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from tea_scrapers.config import Settings


@lru_cache(maxsize=1)
def _settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""
    return create_engine(
        _settings().database_url,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


# Public alias — mirrors common SQLAlchemy idiom.
SessionLocal = _session_factory


@contextmanager
def get_session() -> Iterator[Session]:
    """Context-managed session with commit/rollback semantics.

    Usage::

        with get_session() as session:
            session.add(obj)
            # commit happens on clean exit; rollback on exception.
    """
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["get_engine", "SessionLocal", "get_session"]
