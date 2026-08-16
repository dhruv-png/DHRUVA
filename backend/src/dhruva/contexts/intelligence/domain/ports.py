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
    from dhruva.contexts.intelligence.domain.candidate_observation import (
        CandidateObservation,
        CandidateObservationAppendResult,
        StoredCandidateObservation,
    )
    from dhruva.contexts.intelligence.domain.candidate_outcome import (
        CandidateOutcome,
        CandidateOutcomeAppendResult,
    )
    from dhruva.contexts.intelligence.domain.news import NewsFingerprints, NewsItemIdentity
    from dhruva.contexts.intelligence.domain.research_observation import (
        ObservationAppendResult,
        ResearchObservation,
        StoredResearchObservation,
    )

__all__ = [
    "CandidateObservationStore",
    "CandidateObservationUnitOfWork",
    "CandidateOutcomeStore",
    "IntelligenceUnitOfWork",
    "NewsStore",
    "ResearchObservationStore",
    "ResearchObservationUnitOfWork",
]


@runtime_checkable
class CandidateObservationStore(Protocol):
    """Append complete immutable candidate-ranking freezes."""

    async def append(self, observation: CandidateObservation) -> CandidateObservationAppendResult:
        """Append a new candidate fact or return its identical prior row."""
        ...

    async def list_recent(self, *, limit: int) -> tuple[StoredCandidateObservation, ...]:
        """Return newest candidate freezes for the bound account."""
        ...


@runtime_checkable
class CandidateOutcomeStore(Protocol):
    """Append and inspect matured candidate outcomes for one account."""

    async def append(self, outcome: CandidateOutcome) -> CandidateOutcomeAppendResult:
        """Append a matured fact or return its identical prior row."""
        ...

    async def list_all(self) -> tuple[CandidateOutcome, ...]:
        """Return all outcome facts for the bound account in stable order."""
        ...


@runtime_checkable
class CandidateObservationUnitOfWork(Protocol):
    """The narrow transaction required by an official candidate freeze."""

    @property
    def candidate_observations(self) -> CandidateObservationStore:
        """Return the account-scoped candidate observation store."""
        ...

    @property
    def candidate_outcomes(self) -> CandidateOutcomeStore:
        """Return the account-scoped matured outcome store."""
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
        """Commit the appended candidate observation."""
        ...

    async def rollback(self) -> None:
        """Discard the candidate observation."""
        ...


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
        """Return stored fingerprints for bounded ingest deduplication."""
        ...


@runtime_checkable
class ResearchObservationStore(Protocol):
    """Append and inspect immutable, account-owned research observations."""

    async def append(self, observation: ResearchObservation) -> ObservationAppendResult:
        """Append a new logical fact or return the identical stored observation."""
        ...

    async def list_recent(self, *, limit: int) -> tuple[StoredResearchObservation, ...]:
        """Return this Unit of Work's account observations, newest cutoff first."""
        ...


@runtime_checkable
class IntelligenceUnitOfWork(Protocol):
    """One transaction containing archived intelligence facts."""

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


@runtime_checkable
class ResearchObservationUnitOfWork(Protocol):
    """The narrow transaction required by the research evidence clock."""

    @property
    def observations(self) -> ResearchObservationStore:
        """Return the account-scoped observation store."""
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
        """Commit the appended observation."""
        ...

    async def rollback(self) -> None:
        """Discard the appended observation."""
        ...
