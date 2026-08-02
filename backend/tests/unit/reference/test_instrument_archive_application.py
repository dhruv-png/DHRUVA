"""Daily instrument-master archival is explicit and transaction-owned."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from dhruva.contexts.reference.application.instrument_archive import (
    ArchiveOwnerInstrumentMaster,
    ArchiveOwnerInstrumentMasterCommand,
)
from dhruva.contexts.reference.domain.instrument_master import (
    ArchivedInstrumentDiscovery,
    InstrumentArchiveWrite,
    InstrumentDiscovery,
    InstrumentMasterSnapshot,
)
from dhruva.contexts.reference.infrastructure.owner_universe import load_owner_universe
from dhruva.contexts.reference.infrastructure.zerodha_instruments import parse_instrument_master
from dhruva.shared.identity import AccountId

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACCOUNT = AccountId.deterministic("owner-family")
MARKET_DATE = date(2026, 8, 2)
FETCHED_AT = datetime(2026, 8, 2, 6, 30, tzinfo=UTC)
FIXTURE = Path(__file__).parents[2] / "fixtures" / "zerodha" / "instruments_sanitized.csv"


class FakeSource:
    """Return the sanitized daily master without provider I/O."""

    def __init__(self, snapshot: InstrumentMasterSnapshot) -> None:
        self.snapshot = snapshot

    async def fetch(self, *, market_date: date) -> InstrumentMasterSnapshot:
        """Return the arranged snapshot for the requested date."""
        assert market_date == MARKET_DATE
        return self.snapshot


class FakeArchiveStore:
    """Capture the resolved discovery and report deterministic append counts."""

    def __init__(self) -> None:
        self.discoveries: list[InstrumentDiscovery] = []

    async def archive(self, discovery: InstrumentDiscovery) -> InstrumentArchiveWrite:
        """Record one application-layer archive request."""
        self.discoveries.append(discovery)
        return InstrumentArchiveWrite(
            snapshot_added=1,
            resolutions_added=len(discovery.resolutions),
            cash_mappings_added=sum(item.cash is not None for item in discovery.resolutions),
            contracts_added=sum(len(item.futures_observations) for item in discovery.resolutions),
            contract_revisions_added=sum(
                len(item.futures_observations) for item in discovery.resolutions
            ),
        )

    async def get(
        self,
        *,
        provider: str,
        market_date: date,
        resolver_revision: str,
    ) -> ArchivedInstrumentDiscovery:
        """Fail because the archival command test must never execute a query."""
        raise AssertionError((provider, market_date, resolver_revision))


class FakeUnitOfWork:
    """Expose the archive store and record the transaction outcome."""

    def __init__(self, store: FakeArchiveStore) -> None:
        self.instrument_archive = store
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
        """Record rollback-by-default behavior."""
        if not self.commits:
            self.rollbacks += 1

    async def commit(self) -> None:
        """Record a successful atomic commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Record an explicit rollback."""
        self.rollbacks += 1


class Factory:
    """Retain created units of work for transaction assertions."""

    def __init__(self, store: FakeArchiveStore) -> None:
        self.store = store
        self.created: list[FakeUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeUnitOfWork:
        """Create a transaction only for the approved family account."""
        assert account_id == ACCOUNT
        unit_of_work = FakeUnitOfWork(self.store)
        self.created.append(unit_of_work)
        return unit_of_work


def _snapshot() -> InstrumentMasterSnapshot:
    payload = FIXTURE.read_bytes()
    return InstrumentMasterSnapshot(
        provider="zerodha",
        market_date=MARKET_DATE,
        fetched_at=FETCHED_AT,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        raw_csv=payload,
        entries=parse_instrument_master(payload),
    )


async def test_refresh_resolves_then_commits_one_complete_archive() -> None:
    """The use case archives all cash identities and seven evidenced futures."""
    store = FakeArchiveStore()
    factory = Factory(store)
    universe = load_owner_universe(ACCOUNT, recorded_at=FETCHED_AT)

    result = await ArchiveOwnerInstrumentMaster(FakeSource(_snapshot()), factory).execute(
        ArchiveOwnerInstrumentMasterCommand(
            account_id=ACCOUNT,
            definitions=universe.definitions,
            market_date=MARKET_DATE,
        )
    )

    assert result.provider == "zerodha"
    assert result.resolver_revision == "instrument-discovery-v1"
    assert result.write == InstrumentArchiveWrite(1, 21, 21, 8, 8)
    assert len(store.discoveries) == 1
    assert factory.created[0].commits == 1
    assert factory.created[0].rollbacks == 0
