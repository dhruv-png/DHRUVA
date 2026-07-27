"""Integration harness: real PostgreSQL with TimescaleDB (ADR-058).

No SQLite substitute. SQLite would make this suite fast and portable and would
also make it lie -- different `TIMESTAMPTZ` semantics, different `BIGINT`
overflow, no hypertables, different isolation, different constraint timing. A
suite that passes on SQLite and fails on PostgreSQL converts a caught bug into a
deployed one.

Isolation strategy
------------------
The container is **session-scoped**, because starting it is the expensive part.
Each test runs inside a transaction that is **rolled back at teardown**, so tests
are isolated and order-independent without re-creating the schema between them.

Tests that must observe *committed* state -- concurrency, isolation levels,
outbox visibility -- opt out via the ``committed_session`` fixture and clean up
after themselves explicitly. That distinction is deliberate: the fast path covers
most tests, and the tests that genuinely need commits pay for them.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

#: Set by CI, or by a developer with Docker available. When absent the whole
#: suite skips with a reason rather than failing, so a machine without a
#: container runtime still runs the unit suite cleanly.
DATABASE_URL_ENV = "DHRUVA_TEST_DATABASE_URL"

#: TimescaleDB rather than plain PostgreSQL: hypertable DDL is part of what this
#: suite verifies, and it does not exist without the extension.
POSTGRES_IMAGE = "timescale/timescaledb:2.17.2-pg16"

requires_database = pytest.mark.skipif(
    not os.environ.get(DATABASE_URL_ENV),
    reason=(
        f"integration tests require PostgreSQL with TimescaleDB. Set {DATABASE_URL_ENV}, "
        f"or run with Docker available so the container fixture can start one. "
        f"No SQLite substitute is permitted (ADR-058)."
    ),
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as integration, and skip without a database.

    A module-level ``pytestmark`` in a conftest does **not** propagate to the test
    modules beside it -- a subtlety that had these tests erroring on fixture setup
    rather than skipping cleanly. Applying the markers at collection is the
    mechanism that actually works, and it means a new integration module inherits
    them without having to remember.
    """
    for item in items:
        if "tests/integration/" not in item.nodeid.replace("\\", "/"):
            continue
        item.add_marker(pytest.mark.integration)
        item.add_marker(requires_database)
        # asyncio_mode is "strict", so an `async def test_` without this marker
        # is not run at all -- pytest warns and moves on, which reads as a
        # passing suite that executed nothing. loop_scope="session" so the tests
        # share the loop that the session-scoped engine fixture was created on;
        # a function-scoped loop cannot use a connection opened on another.
        if inspect.iscoroutinefunction(getattr(item, "function", None)):
            item.add_marker(pytest.mark.asyncio(loop_scope="session"))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Provide a database URL, starting a container when one is not supplied.

    Honours an externally supplied URL first, so CI can point the suite at a
    service container rather than starting one per job.
    """
    supplied = os.environ.get(DATABASE_URL_ENV)
    if supplied:
        export_database_settings(supplied)
        yield supplied
        return

    # Defensive: the collection hook should already have skipped, but a fixture
    # that starts a container on a machine without Docker fails in a way that
    # looks like a test failure rather than a missing prerequisite.
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415 - optional dependency

    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        url = container.get_connection_url()
        export_database_settings(url)
        yield url


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Create the engine once and dispose of it at session end."""
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415 - test-only import

    created = create_async_engine(database_url, poolclass=None)
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def migrated(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Apply the full migration chain once, then hand back the engine.

    Migrations rather than ``metadata.create_all``. The schema under test must be
    the schema migrations produce, or the suite verifies a schema that will never
    exist in production -- and the up/down/up test would be checking something
    different from what every other test runs against.
    """
    from sqlalchemy import text  # noqa: PLC0415 - test-only import

    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
    _run_alembic("upgrade", "head")
    try:
        yield engine
    finally:
        _run_alembic("downgrade", "base")


@pytest_asyncio.fixture(loop_scope="session")
async def connection(migrated: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Open a connection inside a transaction that is always rolled back."""
    async with migrated.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Bind a session to the rolled-back connection.

    Anything the test writes is discarded at teardown, so tests are isolated
    without truncating tables between them.
    """
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415 - test-only import

    async with AsyncSession(bind=connection, expire_on_commit=False) as bound:
        yield bound


@pytest_asyncio.fixture(loop_scope="session")
async def committed_session(migrated: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Provide a session whose commits genuinely commit, for concurrency tests.

    Cleans up explicitly at teardown, because nothing rolls it back. Tests using
    this fixture are the exception rather than the default -- committed state is
    what makes a suite order-dependent if it leaks.
    """
    from sqlalchemy import text  # noqa: PLC0415 - test-only import
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415 - test-only import

    factory = async_sessionmaker(bind=migrated, expire_on_commit=False)
    async with factory() as bound:
        try:
            yield bound
        finally:
            async with migrated.begin() as cleanup:
                await cleanup.execute(text("TRUNCATE daily_snapshot, outbox CASCADE"))


def export_database_settings(url: str) -> None:
    """Translate a connection URL into the ``DHRUVA_DB__*`` settings variables.

    Alembic's ``env.py`` builds its URL from validated settings, because ADR-031
    makes settings the single source of process configuration. That means it does
    **not** read ``DHRUVA_TEST_DATABASE_URL`` -- and without this translation it
    silently falls back to the defaults (user ``dhruva``), which is an
    authentication failure against anybody else's database.

    Rather than give Alembic a second configuration mechanism, the harness
    exports one URL into the variables settings already understands. One knob for
    the operator; one source of truth for the application.
    """
    parts = urlsplit(url)
    if parts.hostname:
        os.environ["DHRUVA_DB__HOST"] = parts.hostname
    if parts.port:
        os.environ["DHRUVA_DB__PORT"] = str(parts.port)
    if parts.username:
        os.environ["DHRUVA_DB__USER"] = parts.username
    if parts.password:
        os.environ["DHRUVA_DB__PASSWORD"] = parts.password
    database = parts.path.lstrip("/")
    if database:
        os.environ["DHRUVA_DB__NAME"] = database


def _run_alembic(command: str, revision: str) -> None:
    """Run an Alembic command against the database the fixtures are using."""
    from alembic import command as alembic_command  # noqa: PLC0415 - test-only import
    from alembic.config import Config  # noqa: PLC0415 - test-only import

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test-only import

    config = Config(str(find_repo_root() / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(find_repo_root() / "backend" / "alembic"))
    getattr(alembic_command, command)(config, revision)
