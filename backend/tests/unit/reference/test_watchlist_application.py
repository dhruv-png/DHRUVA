"""Shared-watchlist use cases stay transactional and idempotent."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from dhruva.contexts.reference.application.watchlist import (
    ConfigureReferenceUniverse,
    GetSharedWatchlist,
)
from dhruva.contexts.reference.domain.watchlist import (
    InstrumentIdentityRevision,
    WatchlistInstrument,
    WatchlistMembershipRevision,
)
from dhruva.contexts.reference.infrastructure.owner_universe import load_owner_universe
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACCOUNT = AccountId.deterministic("owner-family")
RECORDED = datetime(2026, 8, 2, 12, tzinfo=UTC)


class FakeReferenceStore:
    """In-memory append store keyed by source revision."""

    def __init__(self) -> None:
        self.identities: dict[tuple[str, str, str], InstrumentIdentityRevision] = {}
        self.memberships: dict[tuple[str, str, str, str], WatchlistMembershipRevision] = {}
        self.watchlist: tuple[WatchlistInstrument, ...] = ()
        self.last_query: tuple[AccountId, date, datetime] | None = None

    async def add_identity(self, revision: InstrumentIdentityRevision) -> bool:
        """Add once by stable instrument and source revision."""
        key = (str(revision.instrument_id), revision.source, revision.source_revision)
        if key in self.identities:
            return False
        self.identities[key] = revision
        return True

    async def add_membership(self, revision: WatchlistMembershipRevision) -> bool:
        """Add once by tenant, stable instrument and source revision."""
        key = (
            str(revision.account_id),
            str(revision.instrument_id),
            revision.source,
            revision.source_revision,
        )
        if key in self.memberships:
            return False
        self.memberships[key] = revision
        return True

    async def list_watchlist(
        self,
        account_id: AccountId,
        *,
        effective_on: date,
        known_at: datetime,
    ) -> tuple[WatchlistInstrument, ...]:
        """Capture the point-in-time query and return arranged rows."""
        self.last_query = (account_id, effective_on, known_at)
        return self.watchlist


class FakeReferenceUnitOfWork:
    """Transaction fake exposing one shared in-memory store."""

    def __init__(self, store: FakeReferenceStore) -> None:
        self.reference = store
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        """Open the fake transaction."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record rollback-by-default semantics."""
        if self.commits == 0:
            self.rollbacks += 1

    async def commit(self) -> None:
        """Record a commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Record an explicit rollback."""
        self.rollbacks += 1


class UnitOfWorkFactory:
    """Build transaction fakes while retaining evidence from each call."""

    def __init__(self, store: FakeReferenceStore) -> None:
        self.store = store
        self.created: list[FakeReferenceUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeReferenceUnitOfWork:
        """Create one fake transaction for the requested tenant."""
        assert account_id == ACCOUNT
        unit_of_work = FakeReferenceUnitOfWork(self.store)
        self.created.append(unit_of_work)
        return unit_of_work


async def test_owner_universe_writes_21_identities_and_20_memberships() -> None:
    """Nifty is persisted as benchmark identity, never as a stock watchlist item."""
    store = FakeReferenceStore()
    factory = UnitOfWorkFactory(store)
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)

    result = await ConfigureReferenceUniverse(factory).execute(command)

    assert result.identities_added == 21
    assert result.memberships_added == 20
    assert result.identities_unchanged == 0
    assert result.memberships_unchanged == 0
    assert factory.created[0].commits == 1


async def test_rerun_is_idempotent_even_when_observed_later() -> None:
    """The immutable owner revision keeps its first-observed instant."""
    store = FakeReferenceStore()
    factory = UnitOfWorkFactory(store)
    use_case = ConfigureReferenceUniverse(factory)
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    await use_case.execute(command)

    result = await use_case.execute(replace(command, recorded_at=RECORDED + timedelta(hours=1)))

    assert result.identities_added == 0
    assert result.memberships_added == 0
    assert result.identities_unchanged == 21
    assert result.memberships_unchanged == 20
    assert len(store.identities) == 21
    assert len(store.memberships) == 20


async def test_more_than_fifty_watchlist_members_is_refused_before_a_transaction() -> None:
    """The configured capacity is a hard input bound."""
    store = FakeReferenceStore()
    factory = UnitOfWorkFactory(store)
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    base = next(item for item in command.definitions if item.included_in_watchlist)
    definitions = tuple(
        replace(
            base,
            identity_key=f"fixture-{index}",
            canonical_symbol=f"FIX{index:02}",
        )
        for index in range(51)
    )

    with pytest.raises(ValidationError, match="exceeds"):
        await ConfigureReferenceUniverse(factory).execute(replace(command, definitions=definitions))

    assert factory.created == []


async def test_duplicate_symbols_are_refused() -> None:
    """One universe revision cannot give one canonical symbol two identities."""
    store = FakeReferenceStore()
    factory = UnitOfWorkFactory(store)
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    duplicate = replace(
        command.definitions[1], canonical_symbol=command.definitions[0].canonical_symbol
    )

    with pytest.raises(ValidationError, match="canonical symbols"):
        await ConfigureReferenceUniverse(factory).execute(
            replace(command, definitions=(command.definitions[0], duplicate))
        )


async def test_query_passes_both_effective_and_knowledge_time() -> None:
    """The application cannot accidentally collapse bitemporal lookup to one date."""
    store = FakeReferenceStore()
    factory = UnitOfWorkFactory(store)
    effective_on = date(2026, 8, 3)

    result = await GetSharedWatchlist(factory).execute(
        account_id=ACCOUNT,
        effective_on=effective_on,
        known_at=RECORDED,
    )

    assert result == ()
    assert store.last_query == (ACCOUNT, effective_on, RECORDED)
    assert factory.created[0].rollbacks == 1
