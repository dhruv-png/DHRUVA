"""Seeding the approved watchlist into real PostgreSQL, twice.

The claim that matters is idempotency, and it cannot be settled anywhere but
here: the archive's uniqueness rules live in the schema, so "running this again
adds nothing" is a statement about constraints rather than about Python.

The second claim is that this closes the loop. Until this command existed the
read path was exercised only against rows a test had inserted, so the last test
seeds and then reads through ``GetSharedWatchlist`` — the same query
``dhruva-digest`` uses — to prove the two agree about what a watchlist is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.universe import linkable_universe
from dhruva.contexts.reference.api import (
    ConfigureReferenceUniverse,
    ConfigureReferenceUniverseResult,
    GetSharedWatchlist,
    InstrumentKind,
)
from dhruva.contexts.reference.infrastructure import (
    OWNER_UNIVERSE_REVISION,
    SqlAlchemyReferenceUnitOfWork,
    load_owner_universe,
)
from dhruva.contexts.reference.infrastructure.persistence.models import (
    InstrumentIdentityRevisionModel,
    WatchlistMembershipRevisionModel,
)
from dhruva.shared.identity import AccountId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
OTHER_ACCOUNT = AccountId.deterministic("other-family")
RECORDED = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
LATER = RECORDED + timedelta(days=1)

#: The committed configuration: twenty approved equities plus the Nifty 50
#: benchmark, which is stored as an identity but is not on the watchlist.
EXPECTED_DEFINITIONS = 21
EXPECTED_WATCHLIST = 20


def _factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyReferenceUnitOfWork]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return lambda account_id: SqlAlchemyReferenceUnitOfWork(sessions, account_id=account_id)


async def _seed(
    engine: AsyncEngine,
    *,
    account_id: AccountId = ACCOUNT,
    recorded_at: datetime = RECORDED,
) -> ConfigureReferenceUniverseResult:
    """Run the same use case the command runs."""
    return await ConfigureReferenceUniverse(_factory(engine)).execute(
        load_owner_universe(account_id, recorded_at=recorded_at)
    )


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as connection:
        identities = await connection.execute(
            select(func.count()).select_from(InstrumentIdentityRevisionModel)
        )
        memberships = await connection.execute(
            select(func.count()).select_from(WatchlistMembershipRevisionModel)
        )
        return int(identities.scalar_one()), int(memberships.scalar_one())


# --------------------------------------------------------------------------- #
# The first run
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_seeding_an_empty_database_writes_the_approved_universe(
    migrated: AsyncEngine,
) -> None:
    """The command's whole purpose, against a database that has never seen it."""
    result = await _seed(migrated)

    assert result.identities_added == EXPECTED_DEFINITIONS
    assert result.memberships_added == EXPECTED_WATCHLIST
    assert await _counts(migrated) == (EXPECTED_DEFINITIONS, EXPECTED_WATCHLIST)


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_benchmark_is_an_identity_without_a_membership(
    migrated: AsyncEngine,
) -> None:
    """Nifty 50 is market context, not something the owner asked to follow."""
    await _seed(migrated)

    async with migrated.connect() as connection:
        indices = await connection.execute(
            select(InstrumentIdentityRevisionModel.canonical_symbol).where(
                InstrumentIdentityRevisionModel.kind == InstrumentKind.INDEX.value
            )
        )
        symbols = list(indices.scalars().all())

    assert symbols == ["NIFTY 50"]


@pytest.mark.usefixtures("truncated_after_test")
async def test_every_written_row_records_the_configuration_that_produced_it(
    migrated: AsyncEngine,
) -> None:
    """Six months on, "where did this row come from?" has to be answerable."""
    await _seed(migrated)

    async with migrated.connect() as connection:
        revisions = await connection.execute(
            select(InstrumentIdentityRevisionModel.source_revision).distinct()
        )
        sources = await connection.execute(
            select(InstrumentIdentityRevisionModel.source).distinct()
        )

    assert list(revisions.scalars().all()) == [OWNER_UNIVERSE_REVISION]
    assert list(sources.scalars().all()) == ["owner_configuration"]


# --------------------------------------------------------------------------- #
# Running it again
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_seeding_twice_adds_nothing(migrated: AsyncEngine) -> None:
    """Idempotency, settled by the schema's own uniqueness rules."""
    await _seed(migrated)
    repeat = await _seed(migrated)

    assert repeat.identities_added == 0
    assert repeat.memberships_added == 0
    assert repeat.identities_unchanged == EXPECTED_DEFINITIONS
    assert repeat.memberships_unchanged == EXPECTED_WATCHLIST
    assert await _counts(migrated) == (EXPECTED_DEFINITIONS, EXPECTED_WATCHLIST)


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_later_rerun_does_not_move_the_original_recorded_instant(
    migrated: AsyncEngine,
) -> None:
    """The archive must keep saying when it first learned each fact.

    Re-seeding tomorrow with an unchanged configuration must not rewrite today's
    knowledge time, or every replay would believe DHRUVA learned everything on
    the day of the last run.
    """
    await _seed(migrated)
    await _seed(migrated, recorded_at=LATER)

    async with migrated.connect() as connection:
        recorded = await connection.execute(
            select(InstrumentIdentityRevisionModel.recorded_at).distinct()
        )
        instants = set(recorded.scalars().all())

    assert instants == {RECORDED}


@pytest.mark.usefixtures("truncated_after_test")
async def test_two_accounts_share_identities_but_not_memberships(
    migrated: AsyncEngine,
) -> None:
    """An instrument is one fact; who follows it is another.

    ADR-004 puts an account on every domain row, and identity is deliberately
    not one of the things an account owns -- SBIN is SBIN for both users.
    """
    await _seed(migrated)
    second = await _seed(migrated, account_id=OTHER_ACCOUNT)

    assert second.identities_added == 0
    assert second.memberships_added == EXPECTED_WATCHLIST
    assert await _counts(migrated) == (EXPECTED_DEFINITIONS, EXPECTED_WATCHLIST * 2)


# --------------------------------------------------------------------------- #
# Closing the loop with the read path
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_seeded_watchlist_is_what_the_read_path_returns(
    migrated: AsyncEngine,
) -> None:
    """The point of the whole slice.

    ``GetSharedWatchlist`` is the query ``dhruva-digest`` and ``dhruva-export``
    use. Before this command existed it could only ever return rows a test had
    inserted, so nothing proved the write path and the read path agreed.
    """
    await _seed(migrated)

    members = await GetSharedWatchlist(_factory(migrated)).execute(
        account_id=ACCOUNT, effective_on=LATER.date(), known_at=LATER
    )

    assert len(members) == EXPECTED_WATCHLIST
    assert {entry.identity.canonical_symbol for entry in members} >= {"SBIN", "HAL", "M&M"}


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_seeded_watchlist_projects_into_the_news_universe(
    migrated: AsyncEngine,
) -> None:
    """And onward into the shape entity linking and search planning consume."""
    await _seed(migrated)

    members = await GetSharedWatchlist(_factory(migrated)).execute(
        account_id=ACCOUNT, effective_on=LATER.date(), known_at=LATER
    )
    universe = linkable_universe(members)

    assert len(universe) == EXPECTED_WATCHLIST
    assert [entry.canonical_symbol for entry in universe] == sorted(
        entry.canonical_symbol for entry in universe
    )
    assert all(entry.company_name for entry in universe)


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_read_before_the_configuration_was_recorded_sees_nothing(
    migrated: AsyncEngine,
) -> None:
    """Point-in-time applies to configuration too, not only to observations."""
    await _seed(migrated)

    members = await GetSharedWatchlist(_factory(migrated)).execute(
        account_id=ACCOUNT,
        effective_on=RECORDED.date(),
        known_at=RECORDED - timedelta(days=1),
    )

    assert members == ()


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_unseeded_account_reads_an_empty_watchlist(
    migrated: AsyncEngine,
) -> None:
    """The exact symptom of seeding under one account and reading under another.

    It is not an error, and that is why it is worth a test: the read succeeds
    and returns nothing, which looks like a broken digest rather than a
    mismatched ``--account``.
    """
    await _seed(migrated)

    members = await GetSharedWatchlist(_factory(migrated)).execute(
        account_id=OTHER_ACCOUNT, effective_on=LATER.date(), known_at=LATER
    )

    assert members == ()
