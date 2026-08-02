"""Authentication and refresh end to end against real PostgreSQL (ADR-058).

The unit suite runs both use cases over fakes and proves the decisions. This file
runs the identical code over the real Unit of Work, the real repositories, the
real audit recorder and the real outbox, and proves the things a fake cannot:

* that a **failed** authentication's audit row is genuinely durable -- committed,
  visible from another connection, and still there after the error was raised;
* that a success writes the audit row, the outbox event and the refresh row in
  **one** transaction;
* that reuse detection revokes a lineage in the database rather than in memory.

The first is the one worth the container. "The transaction committed" is a fake's
counter in the unit suite; here it is a row another connection can see.

Why this file leaves audit rows behind, and why that is acceptable
------------------------------------------------------------------
``test_audit_log.py`` goes to some trouble never to commit an audit row, because
``audit_log`` is guarded against ``TRUNCATE`` as well as ``DELETE`` (ADR-071) and
a committed row is therefore permanent. These tests **must** commit one: the
whole claim under test is that a failed authentication's record survives the
failure, and a rolled-back transaction cannot demonstrate that.

The accumulation is bounded rather than unbounded. The ``migrated`` fixture runs
``alembic downgrade base`` at session teardown, which drops the table and takes
its contents with it -- the outcome migration 0008's rollback note describes as
an incident in production and which is exactly right for a disposable test
database. So rows persist for one session and no longer.

Nothing here reads the audit log without filtering by ``correlation_id``, so
leftovers from an earlier test in the same session cannot be mistaken for this
one's. The outbox is truncated between tests by ``truncated_after_test``, which
is why counting ``AuditRecorded`` rows globally is safe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.application.identity import (
    AuthenticateUseCase,
    RefreshSessionUseCase,
)
from dhruva.contexts.platform.application.identity.sessions import SessionPolicy
from dhruva.contexts.platform.domain.identity import Principal
from dhruva.contexts.platform.infrastructure.database.identity_unit_of_work import (
    SqlAlchemyIdentityUnitOfWork,
)
from dhruva.contexts.platform.infrastructure.identity import (
    MINIMUM_KEY_BYTES,
    Argon2PasswordHasher,
    JwtTokenIssuer,
    PrometheusIdentityMetrics,
    Sha256RefreshTokenMinter,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import AuthenticationError, TokenRevokedError
from dhruva.shared.identity import AccountId, PrincipalId
from dhruva.shared.observability import MetricsRegistry
from dhruva.shared.time import FrozenClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

ACCOUNT: Final = AccountId.deterministic("primary")
NOW: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
PASSWORD: Final = "a-correct-password"
POLICY: Final = SessionPolicy(access_token_seconds=900, refresh_token_days=30)

#: One hasher for the module. Argon2 is deliberately slow, and every test here
#: would otherwise pay for its construction.
HASHER: Final = Argon2PasswordHasher()
MINTER: Final = Sha256RefreshTokenMinter()


def _issuer() -> JwtTokenIssuer:
    return JwtTokenIssuer(SecretValue("k" * MINIMUM_KEY_BYTES, register=False))


def _unit_of_work(engine: AsyncEngine, at: datetime = NOW) -> SqlAlchemyIdentityUnitOfWork:
    return SqlAlchemyIdentityUnitOfWork(
        async_sessionmaker(bind=engine, expire_on_commit=False),
        clock=FrozenClock(at),
    )


def _authenticate(engine: AsyncEngine, at: datetime = NOW) -> AuthenticateUseCase:
    return AuthenticateUseCase(
        lambda: _unit_of_work(engine, at),
        hasher=HASHER,
        tokens=_issuer(),
        minter=MINTER,
        metrics=PrometheusIdentityMetrics(MetricsRegistry()),
        policy=POLICY,
        clock=FrozenClock(at),
    )


def _refresher(engine: AsyncEngine, at: datetime = NOW) -> RefreshSessionUseCase:
    return RefreshSessionUseCase(
        lambda: _unit_of_work(engine, at),
        tokens=_issuer(),
        minter=MINTER,
        metrics=PrometheusIdentityMetrics(MetricsRegistry()),
        policy=POLICY,
        clock=FrozenClock(at),
    )


async def _seed_principal(engine: AsyncEngine, **overrides: object) -> Principal:
    """Commit one principal, so the use cases can find it in their own transaction."""
    defaults: dict[str, object] = {
        "principal_id": PrincipalId.new(),
        "account_id": ACCOUNT,
        "subject": f"operator-{uuid4().hex[:12]}@dhruva.local",
        "password_hash": HASHER.hash(SecretValue(PASSWORD, register=False)),
        "created_at": NOW,
        "updated_at": NOW,
    }
    principal = Principal(**{**defaults, **overrides})  # type: ignore[arg-type]
    async with _unit_of_work(engine) as uow:
        await uow.principals.add(principal)
        await uow.commit()
    return principal


async def _audit_rows(engine: AsyncEngine, correlation_id: object) -> list[tuple[str, str]]:
    """Read the audit log from a fresh connection: durable or not there."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT subject, outcome FROM audit_log WHERE correlation_id = :c "
                "ORDER BY occurred_at"
            ),
            {"c": correlation_id},
        )
        return [(row.subject, row.outcome) for row in rows]


# --------------------------------------------------------------------------- #
# The failure path is durable. This is why the container is worth starting.
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_failed_authentication_leaves_a_committed_audit_row(
    migrated: AsyncEngine,
) -> None:
    """The decision this step turned on, proven against a real transaction.

    In the unit suite "it committed" is a counter on a fake. Here it is a row a
    *different connection* can see after the error was raised -- which is the
    only form of the claim that means anything, because the whole point is that
    the record survives the failure.
    """
    principal = await _seed_principal(migrated)
    correlation = uuid4()

    with pytest.raises(AuthenticationError):
        await _authenticate(migrated).execute(
            subject=principal.subject,
            password=SecretValue("not-the-password", register=False),
            correlation_id=correlation,
        )

    assert await _audit_rows(migrated, correlation) == [("wrong-password", "failed")]


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_unknown_subject_leaves_a_committed_audit_row(
    migrated: AsyncEngine,
) -> None:
    """Including when there is no tenant, which the reserved account covers.

    ``account_id`` is NOT NULL, so this row exists only because
    ``UNATTRIBUTED_ACCOUNT`` gave it somewhere to belong.
    """
    correlation = uuid4()

    with pytest.raises(AuthenticationError):
        await _authenticate(migrated).execute(
            subject="nobody@dhruva.local",
            password=SecretValue(PASSWORD, register=False),
            correlation_id=correlation,
        )

    assert await _audit_rows(migrated, correlation) == [("unknown-subject", "failed")]


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_failed_authentication_issues_no_session(migrated: AsyncEngine) -> None:
    """Committing the audit record must not commit a session alongside it."""
    principal = await _seed_principal(migrated)

    with pytest.raises(AuthenticationError):
        await _authenticate(migrated).execute(
            subject=principal.subject,
            password=SecretValue("not-the-password", register=False),
            correlation_id=uuid4(),
        )

    async with migrated.connect() as connection:
        issued = await connection.scalar(text("SELECT count(*) FROM refresh_token"))

    assert issued == 0


# --------------------------------------------------------------------------- #
# The success path writes three rows in one transaction
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_successful_login_writes_the_session_the_audit_row_and_the_event(
    migrated: AsyncEngine,
) -> None:
    """One transaction, three rows, and the event staged for the S05 relay.

    The outbox row is the assertion a fake cannot make: ``AuditRecorded`` is
    staged by the real recorder through the real Unit of Work, so its presence
    proves the audit write and the event write shared this transaction.
    """
    principal = await _seed_principal(migrated)
    correlation = uuid4()

    session = await _authenticate(migrated).execute(
        subject=principal.subject,
        password=SecretValue(PASSWORD, register=False),
        correlation_id=correlation,
    )

    assert session.access_token
    async with migrated.connect() as connection:
        tokens = await connection.scalar(
            text("SELECT count(*) FROM refresh_token WHERE principal_id = :p"),
            {"p": principal.principal_id.value},
        )
        events = await connection.scalar(
            text("SELECT count(*) FROM outbox WHERE event_type = 'AuditRecorded'")
        )

    assert tokens == 1
    assert events == 1
    assert await _audit_rows(migrated, correlation) == [("session", "succeeded")]


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_issued_access_token_verifies(migrated: AsyncEngine) -> None:
    """The deliverable is a token, not a string that looks like one."""
    principal = await _seed_principal(migrated)

    session = await _authenticate(migrated).execute(
        subject=principal.subject,
        password=SecretValue(PASSWORD, register=False),
        correlation_id=uuid4(),
    )
    claims = _issuer().verify(session.access_token, clock=FrozenClock(NOW))

    assert claims.subject == principal.subject
    assert claims.account_id == ACCOUNT


# --------------------------------------------------------------------------- #
# Rotation and reuse, against the real conditional updates
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_session_rotates_and_the_old_token_stops_working(
    migrated: AsyncEngine,
) -> None:
    """Rotation invalidates what it replaced, which is the point of rotating."""
    principal = await _seed_principal(migrated)
    first = await _authenticate(migrated).execute(
        subject=principal.subject,
        password=SecretValue(PASSWORD, register=False),
        correlation_id=uuid4(),
    )

    second = await _refresher(migrated).execute(
        presented=first.refresh_token, correlation_id=uuid4()
    )

    assert second.refresh_token.reveal() != first.refresh_token.reveal()
    with pytest.raises(TokenRevokedError):
        await _refresher(migrated).execute(presented=first.refresh_token, correlation_id=uuid4())


@pytest.mark.usefixtures("truncated_after_test")
async def test_reuse_revokes_the_lineage_in_the_database(migrated: AsyncEngine) -> None:
    """Not in memory. The successor must stop working too, from another connection.

    This is the assertion that would pass against a fake while the real
    ``revoke_lineage`` predicate was wrong -- and a lineage half-revoked during a
    compromise is the worst possible outcome, because the thief keeps the half
    that still works.
    """
    principal = await _seed_principal(migrated)
    first = await _authenticate(migrated).execute(
        subject=principal.subject,
        password=SecretValue(PASSWORD, register=False),
        correlation_id=uuid4(),
    )
    second = await _refresher(migrated).execute(
        presented=first.refresh_token, correlation_id=uuid4()
    )

    with pytest.raises(TokenRevokedError):
        await _refresher(migrated).execute(presented=first.refresh_token, correlation_id=uuid4())

    async with migrated.connect() as connection:
        unrevoked = await connection.scalar(
            text("SELECT count(*) FROM refresh_token WHERE revoked_at IS NULL")
        )
    assert unrevoked == 0, "part of a compromised lineage is still live"

    with pytest.raises(TokenRevokedError):
        await _refresher(migrated).execute(presented=second.refresh_token, correlation_id=uuid4())


@pytest.mark.usefixtures("truncated_after_test")
async def test_reuse_is_recorded_distinctly_from_the_error_it_raises(
    migrated: AsyncEngine,
) -> None:
    """The caller learns "revoked"; the audit log learns "reused"."""
    principal = await _seed_principal(migrated)
    first = await _authenticate(migrated).execute(
        subject=principal.subject,
        password=SecretValue(PASSWORD, register=False),
        correlation_id=uuid4(),
    )
    await _refresher(migrated).execute(presented=first.refresh_token, correlation_id=uuid4())
    correlation = uuid4()

    with pytest.raises(TokenRevokedError):
        await _refresher(migrated).execute(
            presented=first.refresh_token, correlation_id=correlation
        )

    assert await _audit_rows(migrated, correlation) == [("refresh-reused", "failed")]


@pytest.mark.usefixtures("truncated_after_test")
async def test_one_stolen_session_does_not_end_the_others(migrated: AsyncEngine) -> None:
    """Two logins are two lineages. Revoking one must leave the other alone."""
    principal = await _seed_principal(migrated)
    stolen = await _authenticate(migrated).execute(
        subject=principal.subject,
        password=SecretValue(PASSWORD, register=False),
        correlation_id=uuid4(),
    )
    other_device = await _authenticate(migrated).execute(
        subject=principal.subject,
        password=SecretValue(PASSWORD, register=False),
        correlation_id=uuid4(),
    )
    await _refresher(migrated).execute(presented=stolen.refresh_token, correlation_id=uuid4())

    with pytest.raises(TokenRevokedError):
        await _refresher(migrated).execute(presented=stolen.refresh_token, correlation_id=uuid4())

    still_working = await _refresher(migrated).execute(
        presented=other_device.refresh_token, correlation_id=uuid4()
    )
    assert still_working.access_token


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_disabled_principal_cannot_refresh(migrated: AsyncEngine) -> None:
    """And its lineage is revoked on the way out (ADR-072's consequences)."""
    principal = await _seed_principal(migrated)
    session = await _authenticate(migrated).execute(
        subject=principal.subject,
        password=SecretValue(PASSWORD, register=False),
        correlation_id=uuid4(),
    )

    async with _unit_of_work(migrated) as uow:
        loaded = await uow.principals.get(principal.principal_id)
        assert loaded is not None
        await uow.principals.update(loaded.disabled(at=NOW + timedelta(minutes=1)))
        await uow.commit()

    with pytest.raises(AuthenticationError):
        await _refresher(migrated).execute(presented=session.refresh_token, correlation_id=uuid4())

    async with migrated.connect() as connection:
        live = await connection.scalar(
            text("SELECT count(*) FROM refresh_token WHERE revoked_at IS NULL")
        )
    assert live == 0
