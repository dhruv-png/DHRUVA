"""Rotation, reuse detection, and what the caller is allowed to learn.

Two properties carry this file. Rotation must invalidate what it replaced, and
reuse must kill the whole family -- both are ADR-072. The third, quieter one is
that the caller cannot tell reuse from ordinary revocation, because an error that
distinguished them would tell a thief they had been noticed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest

from dhruva.contexts.platform.application.identity import RefreshSessionUseCase
from dhruva.contexts.platform.application.identity.sessions import IssuedSession, SessionPolicy
from dhruva.contexts.platform.domain.audit import UNATTRIBUTED_ACCOUNT, AuditOutcome
from dhruva.contexts.platform.domain.identity import Principal, RefreshToken
from dhruva.contexts.platform.infrastructure.identity import MINIMUM_KEY_BYTES, JwtTokenIssuer
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import AuthenticationError, TokenExpiredError, TokenRevokedError
from dhruva.shared.identity import AccountId, PrincipalId, RefreshTokenId
from dhruva.shared.time import FrozenClock
from tests.unit.identity.fakes import (
    FakePasswordHasher,
    FakeRefreshTokenMinter,
    FakeUnitOfWork,
)

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
PRINCIPAL: Final = PrincipalId(UUID("22222222-2222-2222-2222-222222222222"))
NOW: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
SUBJECT: Final = "operator@dhruva.local"
POLICY: Final = SessionPolicy(access_token_seconds=900, refresh_token_days=30)


@pytest.fixture
def uow() -> FakeUnitOfWork:
    """Return a fake transaction seeded with an active principal."""
    unit = FakeUnitOfWork()
    unit.principals.seed(
        Principal(
            principal_id=PRINCIPAL,
            account_id=ACCOUNT,
            subject=SUBJECT,
            password_hash=FakePasswordHasher().hash(SecretValue("x", register=False)),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return unit


@pytest.fixture
def minter() -> FakeRefreshTokenMinter:
    """Return a minter whose tokens a test can predict."""
    return FakeRefreshTokenMinter()


@pytest.fixture
def use_case(
    uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter, request: pytest.FixtureRequest
) -> RefreshSessionUseCase:
    """Wire the use case, at ``NOW`` unless a test asks for another instant."""
    at = getattr(request, "param", NOW)
    return RefreshSessionUseCase(
        lambda: uow,
        tokens=JwtTokenIssuer(SecretValue("k" * MINIMUM_KEY_BYTES, register=False)),
        minter=minter,
        policy=POLICY,
        clock=FrozenClock(at),
    )


def seed_token(
    uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter, **overrides: object
) -> tuple[RefreshToken, SecretValue]:
    """Seed a usable root token and return it with the secret that opens it."""
    minted = minter.mint()
    token_id = RefreshTokenId.new()
    defaults: dict[str, object] = {
        "token_id": token_id,
        "account_id": ACCOUNT,
        "principal_id": PRINCIPAL,
        "lineage_id": token_id.value,
        "token_hash": minted.digest,
        "issued_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=29),
    }
    token = uow.refresh_tokens.seed(RefreshToken(**{**defaults, **overrides}))  # type: ignore[arg-type]
    return token, minted.secret


async def refresh(use_case: RefreshSessionUseCase, secret: SecretValue) -> IssuedSession:
    """Run the use case with a fresh correlation id."""
    return await use_case.execute(presented=secret, correlation_id=uuid4())


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_usable_token_rotates_into_a_new_session(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """The ordinary path: a new pair, and the old token closed."""
    original, secret = seed_token(uow, minter)

    session = await refresh(use_case, secret)

    assert session.access_token
    assert uow.refresh_tokens.by_id[original.token_id.value].is_spent
    assert len(uow.refresh_tokens.by_id) == 2


@pytest.mark.asyncio
async def test_the_successor_inherits_the_lineage(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """One chain, one lineage value -- which is what makes revocation one query."""
    original, secret = seed_token(uow, minter)

    await refresh(use_case, secret)
    successor = next(
        t for t in uow.refresh_tokens.by_id.values() if t.token_id != original.token_id
    )

    assert successor.lineage_id == original.lineage_id
    assert successor.parent_token_id == original.token_id


@pytest.mark.asyncio
async def test_a_successful_refresh_is_audited_and_committed(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """A refresh establishes who the caller is, so it is an authentication."""
    _, secret = seed_token(uow, minter)

    await refresh(use_case, secret)

    assert uow.commits == 1
    assert uow.audit.records[0].outcome is AuditOutcome.SUCCEEDED
    assert uow.audit.records[0].actor == SUBJECT


# --------------------------------------------------------------------------- #
# Reuse detection. This is what ADR-072 decided.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_presenting_a_spent_token_revokes_the_whole_lineage(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """The theft response, and the reason lineage is stored.

    Revoking only the presented token would leave the thief's successor -- or the
    victim's -- still working, which is the whole family minus one.
    """
    original, secret = seed_token(uow, minter)
    await refresh(use_case, secret)

    with pytest.raises(TokenRevokedError):
        await refresh(use_case, secret)

    lineage = [t for t in uow.refresh_tokens.by_id.values() if t.lineage_id == original.lineage_id]
    assert len(lineage) == 2
    assert all(t.is_revoked for t in lineage), "a token in a revoked lineage is still usable"


@pytest.mark.asyncio
async def test_reuse_is_indistinguishable_from_ordinary_revocation(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """The quiet requirement, and the easiest one to lose in a refactor.

    Telling a thief they were detected is the one thing worth withholding at
    that moment.
    """
    _, reused = seed_token(uow, minter)
    await refresh(use_case, reused)

    _, revoked = seed_token(uow, minter, revoked_at=NOW - timedelta(hours=1))

    with pytest.raises(TokenRevokedError) as from_reuse:
        await refresh(use_case, reused)
    with pytest.raises(TokenRevokedError) as from_revocation:
        await refresh(use_case, revoked)

    assert str(from_reuse.value) == str(from_revocation.value)


@pytest.mark.asyncio
async def test_reuse_is_audited_distinctly_even_though_the_error_is_not(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """The distinction lives where an attacker cannot read it."""
    _, secret = seed_token(uow, minter)
    await refresh(use_case, secret)
    uow.audit.records.clear()

    with pytest.raises(TokenRevokedError):
        await refresh(use_case, secret)

    assert uow.audit.records[0].subject == "refresh-reused"
    assert uow.audit.records[0].outcome is AuditOutcome.FAILED


@pytest.mark.asyncio
async def test_repeated_reuse_revokes_once_and_then_reports_revoked(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """Idempotence, so hammering a stolen token is not one incident per request."""
    _, secret = seed_token(uow, minter)
    await refresh(use_case, secret)

    with pytest.raises(TokenRevokedError):
        await refresh(use_case, secret)
    uow.audit.records.clear()
    with pytest.raises(TokenRevokedError):
        await refresh(use_case, secret)

    assert uow.audit.records[0].subject == "refresh-revoked"


@pytest.mark.asyncio
async def test_one_compromised_lineage_does_not_log_out_the_others(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """The boundary. Two devices are two chains, and only one was stolen."""
    _, stolen = seed_token(uow, minter)
    innocent, _ = seed_token(uow, minter)
    await refresh(use_case, stolen)

    with pytest.raises(TokenRevokedError):
        await refresh(use_case, stolen)

    assert uow.refresh_tokens.by_id[innocent.token_id.value].is_revoked is False


# --------------------------------------------------------------------------- #
# The other refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("use_case", [NOW + timedelta(days=40)], indirect=True)
@pytest.mark.asyncio
async def test_an_expired_token_gets_its_own_error(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """Distinct from revocation because the client's recovery is distinct.

    A client that could not tell would have to treat every ordinary session end
    as a possible compromise.
    """
    _, secret = seed_token(uow, minter)

    with pytest.raises(TokenExpiredError):
        await refresh(use_case, secret)

    assert uow.commits == 1


@pytest.mark.asyncio
async def test_an_unrecognised_token_is_refused_and_audited(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork
) -> None:
    """A token this platform never issued has no tenant to attribute it to."""
    with pytest.raises(AuthenticationError):
        await refresh(use_case, SecretValue("never-issued", register=False))

    assert uow.commits == 1
    assert uow.audit.records[0].account_id == UNATTRIBUTED_ACCOUNT


@pytest.mark.asyncio
async def test_a_disabled_principal_has_its_lineage_revoked(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """ADR-072's consequences section, enforced here rather than remembered.

    Disabling a principal does not invalidate outstanding access tokens; it must
    at least stop the refresh path minting more. Doing it here means it happens
    even when whoever disabled the account forgot to revoke the sessions.
    """
    original, secret = seed_token(uow, minter)
    disabled = uow.principals.by_id[PRINCIPAL.value].disabled(at=NOW)
    uow.principals.seed(disabled)

    with pytest.raises(AuthenticationError):
        await refresh(use_case, secret)

    assert uow.refresh_tokens.by_id[original.token_id.value].is_revoked
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_losing_a_rotation_race_does_not_revoke_the_lineage(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """A double-click is not a theft.

    Both requests read the token as current; one closes it and one does not. The
    loser presented something genuinely valid, so treating it as reuse would log
    people out for using two tabs.
    """
    original, secret = seed_token(uow, minter)
    # The token is current when read, but another request wins the conditional
    # update before this request can close it.
    uow.refresh_tokens.lose_next_rotation = True

    with pytest.raises(AuthenticationError):
        await refresh(use_case, secret)

    assert uow.refresh_tokens.by_id[original.token_id.value].is_revoked is False


@pytest.mark.asyncio
async def test_every_refusal_commits_its_audit_record(
    use_case: RefreshSessionUseCase, uow: FakeUnitOfWork, minter: FakeRefreshTokenMinter
) -> None:
    """Same inversion as authentication, for the same reason.

    A reuse detection that rolled back would revoke the lineage in memory and
    leave no evidence it had -- the worst of both, since the user is logged out
    and nobody can say why.
    """
    _, secret = seed_token(uow, minter, revoked_at=NOW - timedelta(hours=1))

    with pytest.raises(TokenRevokedError):
        await refresh(use_case, secret)

    assert uow.commits == 1
    assert len(uow.audit.records) == 1
