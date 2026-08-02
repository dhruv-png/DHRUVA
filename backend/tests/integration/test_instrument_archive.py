"""Daily instrument-master archive behavior against real PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.reference.application.instrument_archive import (
    ArchiveOwnerInstrumentMaster,
    ArchiveOwnerInstrumentMasterCommand,
    GetArchivedInstrumentDiscovery,
)
from dhruva.contexts.reference.application.instrument_discovery import (
    DiscoverOwnerInstruments,
    DiscoverOwnerInstrumentsCommand,
)
from dhruva.contexts.reference.application.watchlist import ConfigureReferenceUniverse
from dhruva.contexts.reference.domain.instrument_master import InstrumentMasterSnapshot
from dhruva.contexts.reference.infrastructure.owner_universe import load_owner_universe
from dhruva.contexts.reference.infrastructure.persistence.models import (
    CashInstrumentMappingRevisionModel,
    FuturesContractModel,
    FuturesContractRevisionModel,
    InstrumentMasterSnapshotModel,
    InstrumentResolutionRevisionModel,
)
from dhruva.contexts.reference.infrastructure.persistence.unit_of_work import (
    SqlAlchemyReferenceUnitOfWork,
)
from dhruva.contexts.reference.infrastructure.zerodha_instruments import parse_instrument_master
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
MARKET_DATE = date(2026, 8, 2)
FETCHED_AT = datetime(2026, 8, 2, 6, 30, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "zerodha" / "instruments_sanitized.csv"


class FakeSource:
    """Return one arranged provider snapshot without network access."""

    def __init__(self, snapshot: InstrumentMasterSnapshot) -> None:
        self.snapshot = snapshot

    async def fetch(self, *, market_date: date) -> InstrumentMasterSnapshot:
        """Require the requested market date to match the arranged snapshot."""
        assert market_date == self.snapshot.market_date
        return self.snapshot


def _factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyReferenceUnitOfWork]:
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)

    def create(account_id: AccountId) -> SqlAlchemyReferenceUnitOfWork:
        return SqlAlchemyReferenceUnitOfWork(sessions, account_id=account_id)

    return create


def _snapshot(
    *,
    market_date: date = MARKET_DATE,
    fetched_at: datetime = FETCHED_AT,
    payload: bytes | None = None,
) -> InstrumentMasterSnapshot:
    raw = payload if payload is not None else FIXTURE.read_bytes()
    return InstrumentMasterSnapshot(
        provider="zerodha",
        market_date=market_date,
        fetched_at=fetched_at,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        raw_csv=raw,
        entries=parse_instrument_master(raw),
    )


def _command(snapshot: InstrumentMasterSnapshot) -> ArchiveOwnerInstrumentMasterCommand:
    universe = load_owner_universe(ACCOUNT, recorded_at=FETCHED_AT)
    return ArchiveOwnerInstrumentMasterCommand(
        account_id=ACCOUNT,
        definitions=universe.definitions,
        market_date=snapshot.market_date,
    )


async def _configure(engine: AsyncEngine) -> None:
    await ConfigureReferenceUniverse(_factory(engine)).execute(
        load_owner_universe(ACCOUNT, recorded_at=FETCHED_AT)
    )


async def _counts(engine: AsyncEngine) -> tuple[int, int, int, int, int]:
    async with engine.connect() as connection:
        models = (
            InstrumentMasterSnapshotModel,
            InstrumentResolutionRevisionModel,
            CashInstrumentMappingRevisionModel,
            FuturesContractModel,
            FuturesContractRevisionModel,
        )
        counts: list[int] = []
        for model in models:
            count = await connection.scalar(select(func.count()).select_from(model))
            counts.append(int(count or 0))
        return (counts[0], counts[1], counts[2], counts[3], counts[4])


async def test_archive_round_trips_raw_evidence_and_mapped_facts_idempotently(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """A retry adds nothing while the full source bytes and mappings replay."""
    await _configure(migrated)
    factory = _factory(migrated)
    snapshot = _snapshot()
    use_case = ArchiveOwnerInstrumentMaster(FakeSource(snapshot), factory)

    first = await use_case.execute(_command(snapshot))
    retry = await ArchiveOwnerInstrumentMaster(
        FakeSource(replace(snapshot, fetched_at=FETCHED_AT + timedelta(minutes=5))),
        factory,
    ).execute(_command(snapshot))
    archived = await GetArchivedInstrumentDiscovery(factory).execute(
        account_id=ACCOUNT,
        provider="zerodha",
        market_date=MARKET_DATE,
        resolver_revision="instrument-discovery-v1",
    )

    assert first.write.snapshot_added == 1
    assert first.write.resolutions_added == 21
    assert first.write.cash_mappings_added == 21
    assert first.write.contracts_added == 8
    assert first.write.contract_revisions_added == 8
    assert retry.write.snapshot_added == 0
    assert retry.write.resolutions_added == 0
    assert retry.write.cash_mappings_added == 0
    assert retry.write.contracts_added == 0
    assert retry.write.contract_revisions_added == 0
    assert await _counts(migrated) == (1, 21, 21, 8, 8)
    assert archived.snapshot.raw_csv == FIXTURE.read_bytes()
    assert archived.snapshot.row_count == 31
    by_symbol = {item.canonical_symbol: item for item in archived.resolutions}
    assert len(by_symbol["ADANIENT"].futures.contracts) == 3
    assert len(by_symbol["NIFTY 50"].futures.contracts) == 3
    assert by_symbol["HAL"].cash is not None
    assert by_symbol["HAL"].futures.contracts == ()
    assert any(
        observation.status.value == "EXPIRED"
        for observation in by_symbol["ADANIENT"].futures_observations
    )


async def test_same_daily_key_with_changed_source_bytes_fails_closed(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """A provider correction cannot overwrite the first archived daily evidence."""
    await _configure(migrated)
    factory = _factory(migrated)
    original = _snapshot()
    await ArchiveOwnerInstrumentMaster(FakeSource(original), factory).execute(_command(original))
    changed = _snapshot(payload=FIXTURE.read_bytes() + b"\n")

    with pytest.raises(ConflictError, match="different content"):
        await ArchiveOwnerInstrumentMaster(FakeSource(changed), factory).execute(_command(changed))

    assert await _counts(migrated) == (1, 21, 21, 8, 8)


async def test_token_turnover_creates_a_revision_not_a_new_contract_identity(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """Provider tokens may rotate daily while the actual expiry identity remains stable."""
    await _configure(migrated)
    factory = _factory(migrated)
    first = _snapshot()
    second_payload = FIXTURE.read_bytes().replace(
        b"300001,400001,ADANIENT26AUGFUT",
        b"800001,800002,ADANIENT26AUGFUT",
    )
    second = _snapshot(
        market_date=MARKET_DATE + timedelta(days=1),
        fetched_at=FETCHED_AT + timedelta(days=1),
        payload=second_payload,
    )

    await ArchiveOwnerInstrumentMaster(FakeSource(first), factory).execute(_command(first))
    await ArchiveOwnerInstrumentMaster(FakeSource(second), factory).execute(_command(second))
    first_archive = await GetArchivedInstrumentDiscovery(factory).execute(
        account_id=ACCOUNT,
        provider="zerodha",
        market_date=first.market_date,
        resolver_revision="instrument-discovery-v1",
    )
    second_archive = await GetArchivedInstrumentDiscovery(factory).execute(
        account_id=ACCOUNT,
        provider="zerodha",
        market_date=second.market_date,
        resolver_revision="instrument-discovery-v1",
    )
    first_contract = next(
        item for item in first_archive.resolutions if item.canonical_symbol == "ADANIENT"
    ).futures.contracts[0]
    second_contract = next(
        item for item in second_archive.resolutions if item.canonical_symbol == "ADANIENT"
    ).futures.contracts[0]

    assert first_contract.contract_id == second_contract.contract_id
    assert first_contract.instrument_token == 300001
    assert second_contract.instrument_token == 800001
    assert await _counts(migrated) == (2, 42, 42, 8, 16)


async def test_archive_unit_of_work_rolls_back_by_default(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """Leaving the transaction uncommitted persists none of the archive aggregate."""
    await _configure(migrated)
    factory = _factory(migrated)
    snapshot = _snapshot()
    discovery = await DiscoverOwnerInstruments(FakeSource(snapshot)).execute(
        DiscoverOwnerInstrumentsCommand(
            definitions=_command(snapshot).definitions,
            market_date=MARKET_DATE,
        )
    )

    async with factory(ACCOUNT) as unit_of_work:
        await unit_of_work.instrument_archive.archive(discovery)

    assert await _counts(migrated) == (0, 0, 0, 0, 0)


def _run_alembic(command: str, revision: str) -> None:
    """Run one migration command in Alembic's required worker thread."""
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper
    from alembic.config import Config  # noqa: PLC0415 - test helper

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test helper

    root = find_repo_root()
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    getattr(alembic_command, command)(config, revision)


async def test_archive_migration_downgrades_and_reapplies_cleanly(
    migrated: AsyncEngine,
) -> None:
    """Revision 0014 is reversible and restores the sole current head."""
    await asyncio.to_thread(_run_alembic, "downgrade", "0013_reference_watchlist")
    try:
        async with migrated.connect() as connection:
            remaining = await connection.scalar(
                sql_text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('instrument_master_snapshot', 'instrument_resolution_revision', "
                    "'cash_instrument_mapping_revision', 'futures_contract', "
                    "'futures_contract_revision')"
                )
            )
        assert remaining == 0
    finally:
        await asyncio.to_thread(_run_alembic, "upgrade", "head")

    async with migrated.connect() as connection:
        current = await connection.scalar(sql_text("SELECT version_num FROM alembic_version"))
    assert current == "0014_instrument_archive"
