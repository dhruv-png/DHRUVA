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

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

#: Set by CI, or by a developer with Docker available. When absent the whole
#: suite skips with a reason rather than failing, so a machine without a
#: container runtime still runs the unit suite cleanly.
DATABASE_URL_ENV = "DHRUVA_TEST_DATABASE_URL"

#: Set by the canonical validation runner. Under canonical validation a skip is
#: not an acceptable outcome: the entire purpose of the run is to produce
#: database-backed evidence, and a suite that skips every test reports success
#: while verifying nothing. With this set, an unavailable database is a hard
#: error at collection rather than twelve green-looking skips.
REQUIRE_DATABASE_ENV = "DHRUVA_REQUIRE_DATABASE"

#: TimescaleDB rather than plain PostgreSQL, so that hypertable coverage can be
#: added without changing the harness. Nothing in this suite requires it *yet* --
#: see the ``timescale_available`` fixture -- so the suite also runs against a
#: plain PostgreSQL 16. What is not negotiable is that the database is real
#: PostgreSQL (ADR-058).
POSTGRES_IMAGE = "timescale/timescaledb:2.17.2-pg16"

#: Set by CI, or by a developer with a Redis they would rather reuse. Same
#: contract as the database variable above: supplied wins, container is the
#: fallback.
REDIS_URL_ENV = "DHRUVA_TEST_REDIS_URL"

#: The Redis counterpart of ``DHRUVA_REQUIRE_DATABASE``. Under canonical
#: validation a skipped transport suite proves nothing about the transport.
REQUIRE_REDIS_ENV = "DHRUVA_REQUIRE_REDIS"

#: Pinned rather than ``redis:latest``, for the same reason every other version
#: in this project is pinned (ADR-032). 7.4 is what the deployment target runs;
#: the adapter is written to also read Redis 6.2's shorter ``XAUTOCLAIM`` reply,
#: and a unit test covers that shape because pinning here means this suite never
#: sees it.
REDIS_IMAGE = "redis:7.4.1-alpine"

#: The port inside the container. Named rather than read off the container
#: object, so the harness does not depend on a testcontainers attribute name.
REDIS_PORT = 6379


def _docker_is_available() -> bool:
    """Report whether a container runtime can actually start the fixture.

    ``testcontainers`` imports cleanly without a daemon, so importability proves
    nothing; the daemon is pinged instead. Any failure is treated as "no Docker",
    because every failure mode here has the same consequence for the suite.
    """
    try:
        import docker  # noqa: PLC0415 - optional dependency, probed deliberately
    except ImportError:
        return False
    # Broad excepts are deliberate: a daemon that will not talk to us is simply
    # absent, and docker-py raises several unrelated types for that one fact.
    # No `noqa` -- BLE001 is not in the enabled rule set, so a suppression for it
    # is itself a lint error (RUF100).
    try:
        client = docker.from_env()
    except Exception:
        return False
    try:
        client.ping()
    except Exception:
        return False
    else:
        return True
    finally:
        client.close()


def database_is_available() -> bool:
    """Report whether the suite has any way to reach a database.

    Two ways, and the check must cover both. The previous version tested only
    the environment variable, which made the container fallback in
    :func:`database_url` unreachable -- the skip fired first, every time, while
    the skip reason claimed Docker was sufficient. It was not.
    """
    return bool(os.environ.get(DATABASE_URL_ENV)) or _docker_is_available()


def redis_is_available() -> bool:
    """Report whether the suite has any way to reach a Redis."""
    return bool(os.environ.get(REDIS_URL_ENV)) or _docker_is_available()


_SKIP_REASON = (
    f"integration tests require PostgreSQL with TimescaleDB. Set {DATABASE_URL_ENV}, "
    f"or start Docker so the container fixture can provision one. "
    f"No SQLite substitute is permitted (ADR-058)."
)

_REDIS_SKIP_REASON = (
    f"transport integration tests require a real Redis. Set {REDIS_URL_ENV}, or start "
    f"Docker so the container fixture can provision one. No fake substitute is "
    f"permitted here: a fake cannot have a consumer group (ADR-067)."
)

requires_database = pytest.mark.skipif(not database_is_available(), reason=_SKIP_REASON)
requires_redis = pytest.mark.skipif(not redis_is_available(), reason=_REDIS_SKIP_REASON)

#: Fixtures whose presence means a test genuinely needs PostgreSQL.
_DATABASE_FIXTURES = frozenset(
    {
        "committed_session",
        "connection",
        "database_url",
        "engine",
        "migrated",
        "session",
        "timescale_available",
        "truncated_after_test",
    }
)

#: Fixtures whose presence means a test genuinely needs Redis.
_REDIS_FIXTURES = frozenset({"namespace", "redis_client", "redis_url"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as integration, and skip without a database.

    A module-level ``pytestmark`` in a conftest does **not** propagate to the test
    modules beside it -- a subtlety that had these tests erroring on fixture setup
    rather than skipping cleanly. Applying the markers at collection is the
    mechanism that actually works, and it means a new integration module inherits
    them without having to remember.

    The ``asyncio`` marker is deliberately **not** applied here. pytest-asyncio
    has already decided how to invoke each item by the time this hook runs, so a
    marker added now is seen too late and reported as
    "marked with '@pytest.mark.asyncio' but it is not an async function" --
    which, under ``filterwarnings = ["error"]``, fails every test in the suite.
    Each integration module declares it in its own ``pytestmark`` instead.
    """
    selected = [i for i in items if "tests/integration/" in i.nodeid.replace("\\", "/")]

    needs_database = [i for i in selected if _fixtures(i) & _DATABASE_FIXTURES]
    needs_redis = [i for i in selected if _fixtures(i) & _REDIS_FIXTURES]

    # Under canonical validation, refuse to skip. A run whose evidence consists
    # of twelve SKIPPED lines proves nothing, and the failure is far cheaper to
    # diagnose here -- with the reason attached -- than in a log file three days
    # later.
    if needs_database and os.environ.get(REQUIRE_DATABASE_ENV) and not database_is_available():
        raise pytest.UsageError(
            f"{REQUIRE_DATABASE_ENV} is set, so the integration suite must run, "
            f"but no database is reachable. {_SKIP_REASON}"
        )
    if needs_redis and os.environ.get(REQUIRE_REDIS_ENV) and not redis_is_available():
        raise pytest.UsageError(
            f"{REQUIRE_REDIS_ENV} is set, so the transport suite must run, "
            f"but no Redis is reachable. {_REDIS_SKIP_REASON}"
        )

    # An async test with no asyncio marker is silently not run in strict mode, so
    # a module that forgets it reports a passing suite that executed nothing.
    # Refuse to collect rather than allow that.
    unmarked = [
        item.nodeid
        for item in selected
        if inspect.iscoroutinefunction(getattr(item, "function", None))
        and not any(mark.name == "asyncio" for mark in item.iter_markers())
    ]
    if unmarked:
        raise pytest.UsageError(
            "async integration tests without an asyncio marker would be silently "
            "skipped under asyncio_mode=strict. Add "
            "`pytest.mark.asyncio(loop_scope='session')` to the module's "
            "pytestmark. Offending tests: " + ", ".join(unmarked)
        )

    # Applied per dependency rather than to the directory. Marking every
    # integration test as requiring PostgreSQL was correct while PostgreSQL was
    # the only external dependency; it stopped being correct the moment a
    # transport suite arrived, because a Redis test would then have skipped on a
    # machine with Redis and no database -- and, under
    # ``DHRUVA_REQUIRE_DATABASE``, refused to collect for wanting something it
    # never touches.
    for item in selected:
        item.add_marker(pytest.mark.integration)
        uses = _fixtures(item)
        if uses & _DATABASE_FIXTURES:
            item.add_marker(requires_database)
        if uses & _REDIS_FIXTURES:
            item.add_marker(requires_redis)


def _fixtures(item: pytest.Item) -> frozenset[str]:
    """Return the fixture names an item requests, or an empty set if it has none."""
    return frozenset(getattr(item, "fixturenames", ()))


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
async def timescale_available(engine: AsyncEngine) -> bool:
    """Report whether TimescaleDB is installed, without trying to install it.

    Detection rather than creation, for three reasons.

    The extension is not *created* here because the supported container image
    installs it into ``template1``, so every database made from that template
    already has it, and the image's own init script creates it as well. Racing
    that init is how ``CREATE EXTENSION IF NOT EXISTS`` -- a statement whose
    entire purpose is to be safe when the thing exists -- came to fail with a
    duplicate key on ``pg_extension_name_index``. ``IF NOT EXISTS`` reads the
    catalogue and is not atomic against a concurrent creator.

    It is not *required* here because nothing in this suite uses it. Neither the
    migration chain nor any of the tests below creates a hypertable. Demanding a
    capability that nothing under test exercises made the whole suite
    unrunnable against a plain PostgreSQL for no gain in what it verifies.

    When hypertable coverage arrives, those tests take this fixture and skip on
    it explicitly. That keeps the requirement attached to the tests that have it,
    which is where a reader will look for it.
    """
    from sqlalchemy import text  # noqa: PLC0415 - test-only import

    async with engine.connect() as connection:
        installed = await connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        )
    return bool(installed)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def migrated(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Apply the full migration chain once, then hand back the engine.

    Migrations rather than ``metadata.create_all``. The schema under test must be
    the schema migrations produce, or the suite verifies a schema that will never
    exist in production -- and the up/down/up test would be checking something
    different from what every other test runs against.

    Alembic is run in a worker thread. ``alembic/env.py`` drives the async
    migration with ``asyncio.run()``, which refuses to start a loop when one is
    already running -- and this fixture is async, so the session loop is running.
    Calling it directly raised ``RuntimeError: asyncio.run() cannot be called
    from a running event loop`` and errored every test in the suite at setup.
    The thread gives ``env.py`` the bare thread it expects, without this harness
    reaching into how migrations manage their own loop.
    """
    await asyncio.to_thread(_run_alembic, "upgrade", "head")
    try:
        yield engine
    finally:
        await asyncio.to_thread(_run_alembic, "downgrade", "base")


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
async def truncated_after_test(migrated: AsyncEngine) -> AsyncIterator[None]:
    """Empty the worked-example tables after a test that genuinely commits.

    Any test that commits must request this, whether or not it also wants
    :func:`committed_session`. That was previously not possible -- the truncation
    was welded inside ``committed_session``, so a test taking ``migrated``
    directly had no way to clean up and simply did not.
    ``test_deadlock_surfaces_as_a_driver_error`` is exactly that test: it commits
    two rows and left them behind, and the next test's first insert then
    collided with the unique constraint and failed for a reason that had nothing
    to do with what it was asserting.

    Separating the cleanup from the session makes the requirement something a
    test can opt into on its own.
    """
    from sqlalchemy import text  # noqa: PLC0415 - test-only import

    try:
        yield
    finally:
        async with migrated.begin() as cleanup:
            # A bounded wait, so a lock holder can never again present as a hang.
            # If some future fixture reintroduces one, the suite fails in five
            # seconds naming the table, which is a diagnosis rather than a
            # symptom.
            await cleanup.execute(text("SET LOCAL lock_timeout = '5s'"))
            # Every table a test can commit to, named in one place. Adding a
            # table to the schema and forgetting it here is how order-dependence
            # gets reintroduced: the row survives into the next test, which then
            # fails for a reason unrelated to its subject.
            #
            # `audit_log` is deliberately absent and must stay absent. It is
            # guarded against TRUNCATE as well as UPDATE and DELETE (ADR-071),
            # so adding it here would not clean up -- it would raise, and take
            # every committing test in the suite down with it. Tests touching
            # that table therefore never commit; see `test_audit_log.py`.
            await cleanup.execute(
                text(
                    "TRUNCATE daily_snapshot, outbox, example_tick, processed_event, "
                    "credential, principal, refresh_token, role, reference_instrument, "
                    "instrument_master_snapshot, daily_market_bar_revision CASCADE"
                )
            )


@pytest_asyncio.fixture(loop_scope="session")
async def committed_session(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requested for its teardown, not its value
) -> AsyncIterator[AsyncSession]:
    """Provide a session whose commits genuinely commit, for concurrency tests.

    Cleans up explicitly at teardown, because nothing rolls it back. Tests using
    this fixture are the exception rather than the default -- committed state is
    what makes a suite order-dependent if it leaks.

    Depends on :func:`truncated_after_test` rather than truncating inline, so
    that the session is closed before the TRUNCATE runs. When the cleanup lived
    inside ``async with factory() as bound``, the TRUNCATE ran while ``bound``
    was still open: a test that finished with a read left that session idle in
    transaction holding ACCESS SHARE on ``daily_snapshot``, and TRUNCATE wants
    ACCESS EXCLUSIVE. There is no cycle for PostgreSQL to detect -- one side is
    simply never going to be asked to give the lock up -- so the suite hung
    forever instead of failing. It passed when the test ran alone, because alone
    the session's last act was a commit and it held nothing.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415 - test-only import

    factory = async_sessionmaker(bind=migrated, expire_on_commit=False)
    bound = factory()
    try:
        yield bound
    finally:
        await bound.close()


# --------------------------------------------------------------------------- #
# Redis (ADR-002, ADR-067)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """Provide a Redis URL, starting a container when one is not supplied.

    No fake, and no in-process substitute, for the same reason ADR-058 refuses
    SQLite. The behaviour under test *is* the broker's: consumer groups, pending
    entries, ``XAUTOCLAIM``, capped trimming. A stand-in would be asserting that
    the adapter calls the methods it calls.

    ``redislite`` was tried and withdrawn -- it publishes no Windows wheel, and a
    harness that only starts on one platform splits the suite in two.
    """
    supplied = os.environ.get(REDIS_URL_ENV)
    if supplied:
        yield supplied
        return

    from testcontainers.redis import RedisContainer  # noqa: PLC0415 - optional dependency

    with RedisContainer(REDIS_IMAGE) as container:
        host = container.get_container_host_ip()
        yield f"redis://{host}:{container.get_exposed_port(REDIS_PORT)}/0"


@pytest_asyncio.fixture(loop_scope="session")
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    """Open a client on the session loop and close it at teardown.

    Function-scoped deliberately. A session-scoped pool outlives the tests that
    fail, and a connection left open by a failing test then produces its
    ``ResourceWarning`` inside whichever unrelated test triggers collection --
    which under ``filterwarnings = ["error"]`` is a failure that moves around
    between runs.
    """
    from redis.asyncio import Redis as AsyncRedis  # noqa: PLC0415 - test-only import

    client: Redis = AsyncRedis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture(loop_scope="session")
async def namespace(redis_client: Redis) -> AsyncIterator[str]:
    """Give each test its own key prefix, and delete it afterwards.

    Isolation without ``FLUSHALL``: the URL may point at a Redis somebody else is
    using, and a harness that empties a developer's cache to run its own tests
    will be run once. A prefix per test also means these tests are safe to run
    in parallel, which the database suite cannot be.
    """
    prefix = f"dhruvatest:{uuid4().hex[:12]}"
    try:
        yield prefix
    finally:
        keys = [key async for key in redis_client.scan_iter(match=f"{prefix}*")]
        if keys:
            await redis_client.delete(*keys)


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
