"""Persistence contracts, expressed without any persistence framework.

Three protocols, all framework-free, all implemented in infrastructure:

:class:`Repository`
    Loads and stores **aggregates**. Deals in domain objects. Never commits
    (ADR-053).
:class:`UnitOfWork`
    Owns the transaction. One use case, one Unit of Work, one transaction.
:class:`TimeSeriesStorage`
    The ORM-bypass path for append-only market data (ADR-054). Deals in
    **column rows**, never in domain objects — boundary rule R8 makes that
    enforceable by forbidding the implementing package from importing any domain
    layer.

Why these live in the shared kernel
-----------------------------------
An application layer in any context needs to depend on the *idea* of a unit of
work without depending on the Platform context, which the section 5 dependency
matrix does not permit. The shared kernel is a leaf that every context may
import, which makes it the only correct home for a contract this universal.

The implementations live in ``platform.infrastructure.database`` and are injected
at composition roots, so nothing here knows SQLAlchemy exists (rule R7).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any, Generic, Protocol, Self, TypeVar, runtime_checkable

__all__ = ["Repository", "TimeSeriesRow", "TimeSeriesStorage", "UnitOfWork"]

#: Aggregate root type handled by a repository.
AggregateT = TypeVar("AggregateT")

#: Identifier type addressing that aggregate.
#:
#: Contravariant because it appears only in parameter position: a repository
#: accepting a broader identifier type is substitutable wherever one accepting a
#: narrower type is expected.
IdT_contra = TypeVar("IdT_contra", contravariant=True)

#: One row on the timeseries path: column names to primitive values.
#:
#: Deliberately a plain mapping rather than a typed object. A type on this path
#: could carry domain meaning, and ADR-054 exists to ensure none does.
TimeSeriesRow = Mapping[str, Any]


@runtime_checkable
class Repository(Protocol, Generic[AggregateT, IdT_contra]):  # noqa: UP046 - see note
    """Loads and stores aggregates.

    Implementations translate through a mapping layer (ADR-052); the protocol
    itself names no persistence type, so a domain or application module can
    depend on it without violating rule R7.

    **Repositories never commit.** The Unit of Work owns transaction lifetime
    (ADR-053). A repository that commits makes its caller's atomicity depend on
    which methods happened to be called.

    Notes
    -----
    Written with ``Generic`` and explicit ``TypeVar`` rather than PEP 695 syntax
    for the same reason as TD-03: the verification interpreter is 3.10 and cannot
    parse ``class Repository[A, I]``. Adopting it would mean shipping this module
    without the test suite or the type checker ever having read it. Converts to
    the modern form together with TD-02.
    """

    async def get(self, identifier: IdT_contra) -> AggregateT | None:
        """Return the aggregate, or ``None`` if it does not exist.

        ``None`` rather than raising, because "not found" is an ordinary answer
        to a lookup. Callers that require existence raise their own
        :class:`~dhruva.shared.errors.NotFoundError` with context the repository
        does not have.
        """
        ...

    async def add(self, aggregate: AggregateT) -> None:
        """Stage a new aggregate for insertion.

        Staged, not written: nothing reaches the database until the Unit of Work
        commits.
        """
        ...

    async def update(self, aggregate: AggregateT) -> None:
        """Stage changes to an existing aggregate.

        Required explicitly (ADR-057). Detached domain objects have no dirty
        tracking, and the failure mode of implicit tracking is a mutation that is
        silently never persisted.

        Raises
        ------
        ConflictError
            If the aggregate's version no longer matches the stored one, meaning
            another writer committed first.
        """
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Owns a transaction and the repositories participating in it.

    Used as an async context manager. Entering begins a transaction; leaving
    without an explicit :meth:`commit` rolls back.

    Examples
    --------
    ::

        async with uow_factory() as uow:
            instrument = await uow.instruments.get(instrument_id)
            instrument.rename(new_symbol)
            await uow.instruments.update(instrument)
            await uow.commit()

    Notes
    -----
    Rolling back on exit unless committed is deliberate. The alternative --
    committing on clean exit -- means a use case that returns early commits
    partial work, and the failure is silent.
    """

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless :meth:`commit` succeeded."""
        ...

    async def commit(self) -> None:
        """Commit, then publish any domain events the aggregates collected.

        Events publish **after** the commit succeeds, never inside it. Publishing
        inside means a consumer can observe an event for a transaction that later
        rolls back (ADR-053).
        """
        ...

    async def rollback(self) -> None:
        """Discard everything staged in this transaction."""
        ...


@runtime_checkable
class TimeSeriesStorage(Protocol):
    """Append-only bulk writes that bypass the mapping layer (ADR-054).

    Exists because mapping every tick through a domain object would make S10's
    ingest path 10-50x more expensive for no benefit: a tick has no identity, no
    lifecycle, and no invariant beyond its column constraints.

    **This is not a general second persistence path.** The exception applies only
    to data that is append-only, immutable, not an aggregate, carrying no
    business invariant, participating in no transactional decision, raising no
    domain events, and having no repository semantics. Anything else goes through
    a :class:`Repository`, whatever its volume.

    The interface deals in column rows precisely so that no domain type can
    travel this way, and boundary rule R8 fails the build if the implementing
    package imports a domain layer.
    """

    async def append(self, table: str, rows: Sequence[TimeSeriesRow]) -> int:
        """Append rows, returning how many were written.

        No update, no delete, no lookup by identity. The absence of those methods
        is the interface enforcing what ADR-054 says.
        """
        ...
