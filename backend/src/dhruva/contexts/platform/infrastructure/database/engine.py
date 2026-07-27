"""Async engine and session factory construction.

Built once at a composition root and injected. Nothing here is a module-level
global: an ambient engine is an ambient connection pool, which is impossible to
size, isolate in tests, or dispose of deterministically (ADR-056).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from dhruva.shared.config.settings import DatabaseSettings

__all__ = ["build_engine", "build_session_factory", "database_url"]


def database_url(settings: DatabaseSettings, *, driver: str = "postgresql+asyncpg") -> str:
    """Build a connection URL from validated settings.

    The password is revealed here and nowhere else. This is one of the few
    sanctioned ``SecretValue.reveal()`` call sites, and it is named so it can be
    found (ADR-033).
    """
    return (
        f"{driver}://{settings.user}:{settings.password.reveal()}"
        f"@{settings.host}:{settings.port}/{settings.name}"
    )


def build_engine(settings: DatabaseSettings, *, echo: bool = False) -> AsyncEngine:
    """Create the async engine.

    Notes
    -----
    ``pool_pre_ping`` is on. A connection idle across a database restart, a
    failover, or an idle-timeout is otherwise handed to a caller dead, and the
    resulting error appears at a random point in a transaction rather than at
    checkout. One round trip per checkout is a fair price for that.

    ``pool_timeout`` is bounded rather than infinite, so exhaustion raises
    instead of hanging. A hung request is harder to diagnose than a failed one,
    and it holds a worker slot while being useless.
    """
    return create_async_engine(
        database_url(settings),
        echo=echo,
        pool_size=settings.pool_size,
        max_overflow=max(settings.pool_size // 2, 1),
        pool_pre_ping=True,
        pool_timeout=10,
        pool_recycle=1800,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.statement_timeout_ms),
                "application_name": "dhruva",
            }
        },
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory used by every Unit of Work.

    ``expire_on_commit=False`` because expiry triggers a refresh query after
    commit that nobody asked for, and an error on a detached object. Since
    domain objects are reconstructed through the mapping layer rather than held
    attached (ADR-052), there is nothing to expire anyway.

    ``autoflush=False`` so a read never silently writes. An implicit flush mid
    transaction makes the order of statements depend on when SQLAlchemy decided
    to synchronise, which is not a property to reason about in a system that
    places orders.
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )
