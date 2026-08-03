"""Inward-facing news persistence contracts.

No provider type crosses this line. A feed adapter hands the application a
:class:`~dhruva.contexts.intelligence.domain.news.NewsItem`; what a store sees is
that item, its content revision and the deterministic analysis of it. Nothing
here knows about RSS, XML, JSON or HTTP, which is what lets the ingestion rules
be tested without any of them.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.archive import (
        ArchivedNewsItem,
        NewsAnalysis,
        NewsArchiveWrite,
        NewsRevision,
    )
    from dhruva.contexts.intelligence.domain.news import NewsFingerprints, NewsItemIdentity

__all__ = ["IntelligenceUnitOfWork", "NewsStore"]


@runtime_checkable
class NewsStore(Protocol):
    """Append news revisions and analyses; never update, never commit."""

    async def append(
        self,
        revisions: tuple[tuple[NewsRevision, NewsAnalysis | None], ...],
    ) -> NewsArchiveWrite:
        """Stage unseen revisions and analyses, reporting identical retries."""
        ...

    async def list_known_at(
        self,
        *,
        known_at: datetime,
        published_from: datetime,
        published_to: datetime,
        canonical_symbol: str | None = None,
    ) -> tuple[ArchivedNewsItem, ...]:
        """Return the latest revision of each item observable at ``known_at``.

        A correction observed after the cutoff stays invisible, which is the
        whole reason both timestamps are stored.
        """
        ...

    async def fingerprints_seen_since(
        self,
        *,
        published_from: datetime,
        known_at: datetime,
    ) -> tuple[tuple[NewsItemIdentity, NewsFingerprints], ...]:
        """Return stored fingerprints so a new poll can be deduplicated against them.

        Bounded by publication date on purpose: the headline rules only ever
        compare within one day, so loading the whole archive to compare against
        it would be work nothing can use.
        """
        ...


@runtime_checkable
class IntelligenceUnitOfWork(Protocol):
    """One transaction containing news revisions and their analyses."""

    @property
    def news(self) -> NewsStore:
        """Return the news store bound to this transaction."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless commit succeeded."""
        ...

    async def commit(self) -> None:
        """Commit every staged news revision and analysis."""
        ...

    async def rollback(self) -> None:
        """Discard every staged news revision and analysis."""
        ...
