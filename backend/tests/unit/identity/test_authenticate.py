"""Authenticating a principal: what is issued, what is refused, what is recorded.

The tests that matter most are the ones about **failure**. Plan §15.1 requires an
audit record for every authentication and says why the failures are the point: a
run of them is the signal, and it exists only if it was written down. So the
assertions here are not only "the wrong password was refused" but "the wrong
password was refused *and the transaction committed*".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest

from dhruva.contexts.platform.application.identity import AuthenticateUseCase
from dhruva.contexts.platform.application.identity.sessions import SessionPolicy
from dhruva.contexts.platform.domain.audit import (
    UNATTRIBUTED_ACCOUNT,
    AuditAction,
    AuditOutcome,
)
from dhruva.contexts.platform.domain.identity import Principal
from dhruva.contexts.platform.infrastructure.identity import MINIMUM_KEY_BYTES, JwtTokenIssuer
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import AuthenticationError
from dhruva.shared.identity import AccountId, PrincipalId
from dhruva.shared.time import FrozenClock
from tests.unit.identity.fakes import (
    FakePasswordHasher,
    FakeRefreshTokenMinter,
    FakeUnitOfWork,
)

if TYPE_CHECKING:
    from dhruva.contexts.platform.application.identity.sessions import IssuedSession

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
PRINCIPAL: Final = PrincipalId(UUID("22222222-2222-2222-2222-222222222222"))
NOW: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
SUBJECT: Final = "operator@dhruva.local"
PASSWORD: Final = "a-correct-password"
POLICY: Final = SessionPolicy(access_token_seconds=900, refresh_token_days=30)


@pytest.fixture
def hasher() -> FakePasswordHasher:
    """Return the fake hasher, which also counts wasted verifications."""
    return FakePasswordHasher()


@pytest.fixture
def uow() -> FakeUnitOfWork:
    """Return a fake transaction that remembers whether it committed."""
    return FakeUnitOfWork()


@pytest.fixture
def use_case(hasher: FakePasswordHasher, uow: FakeUnitOfWork) -> AuthenticateUseCase:
    """Wire the use case over fakes and a real JWT issuer.

    The issuer is real because it is cheap and because a fake one would let a
    malformed claim set pass unnoticed -- the access token is a deliverable of
    this use case, and it should be a token.
    """
    return AuthenticateUseCase(
        lambda: uow,
        hasher=hasher,
        tokens=JwtTokenIssuer(SecretValue("k" * MINIMUM_KEY_BYTES, register=False)),
        minter=FakeRefreshTokenMinter(),
        policy=POLICY,
        clock=FrozenClock(NOW),
    )


def seed_principal(
    uow: FakeUnitOfWork, hasher: FakePasswordHasher, **overrides: object
) -> Principal:
    """Insert a principal whose password is :data:`PASSWORD`."""
    defaults: dict[str, object] = {
        "principal_id": PRINCIPAL,
        "account_id": ACCOUNT,
        "subject": SUBJECT,
        "password_hash": hasher.hash(SecretValue(PASSWORD, register=False)),
        "created_at": NOW,
        "updated_at": NOW,
    }
    return uow.principals.seed(Principal(**{**defaults, **overrides}))  # type: ignore[arg-type]


async def authenticate(
    use_case: AuthenticateUseCase, *, password: str = PASSWORD, subject: str = SUBJECT
) -> IssuedSession:
    """Run the use case with a fresh correlation id."""
    return await use_case.execute(
        subject=subject,
        password=SecretValue(password, register=False),
        correlation_id=uuid4(),
    )


# --------------------------------------------------------------------------- #
# The successful path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_correct_password_issues_a_session(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork, hasher: FakePasswordHasher
) -> None:
    """Both tokens, and the access token expires when the policy says."""
    seed_principal(uow, hasher)

    session = await authenticate(use_case)

    assert session.access_token
    assert session.refresh_token.reveal()
    assert session.access_expires_at == NOW + timedelta(seconds=900)
    assert session.principal_id == PRINCIPAL
    assert session.account_id == ACCOUNT


@pytest.mark.asyncio
async def test_the_refresh_token_is_stored_as_a_digest_not_a_secret(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork, hasher: FakePasswordHasher
) -> None:
    """A stolen database must be useless hashes, not live sessions (ADR-033)."""
    seed_principal(uow, hasher)

    session = await authenticate(use_case)
    stored = next(iter(uow.refresh_tokens.by_id.values()))

    assert session.refresh_token.reveal().encode() != stored.token_hash
    assert stored.token_hash


@pytest.mark.asyncio
async def test_a_fresh_login_anchors_its_own_lineage(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork, hasher: FakePasswordHasher
) -> None:
    """A login starts a chain; only a rotation continues one."""
    seed_principal(uow, hasher)

    await authenticate(use_case)
    stored = next(iter(uow.refresh_tokens.by_id.values()))

    assert stored.lineage_id == stored.token_id.value
    assert stored.parent_token_id is None


@pytest.mark.asyncio
async def test_a_success_is_audited_and_committed(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork, hasher: FakePasswordHasher
) -> None:
    """Plan §15.1: every authentication, not only the interesting ones."""
    seed_principal(uow, hasher)

    await authenticate(use_case)

    assert uow.commits == 1
    assert len(uow.audit.records) == 1
    record = uow.audit.records[0]
    assert record.action is AuditAction.AUTHENTICATION
    assert record.outcome is AuditOutcome.SUCCEEDED
    assert record.actor == SUBJECT
    assert record.account_id == ACCOUNT


# --------------------------------------------------------------------------- #
# Failures commit. This is the decision the use case exists to enforce.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("scenario", "expected_subject"),
    [
        ("unknown", "unknown-subject"),
        ("wrong-password", "wrong-password"),
        ("disabled", "disabled-principal"),
    ],
)
@pytest.mark.asyncio
async def test_every_failure_is_recorded_and_committed(
    use_case: AuthenticateUseCase,
    uow: FakeUnitOfWork,
    hasher: FakePasswordHasher,
    scenario: str,
    expected_subject: str,
) -> None:
    """The inversion of ADR-053, and the reason for it.

    A rollback here would leave the audit log showing successes only -- which is
    the log that cannot show a brute-force attempt, the single most likely thing
    anybody will read it for. So the transaction commits and *then* the error is
    raised.
    """
    if scenario != "unknown":
        seed_principal(uow, hasher, disabled_at=NOW if scenario == "disabled" else None)
    password = PASSWORD if scenario != "wrong-password" else "not-the-password"

    with pytest.raises(AuthenticationError):
        await authenticate(use_case, password=password)

    assert uow.commits == 1, "a failed authentication must still commit its audit record"
    assert len(uow.audit.records) == 1
    record = uow.audit.records[0]
    assert record.outcome is AuditOutcome.FAILED
    assert record.subject == expected_subject


@pytest.mark.parametrize("scenario", ["unknown", "wrong-password", "disabled"])
@pytest.mark.asyncio
async def test_every_failure_looks_identical_to_the_caller(
    use_case: AuthenticateUseCase,
    uow: FakeUnitOfWork,
    hasher: FakePasswordHasher,
    scenario: str,
) -> None:
    """ADR-072: an error distinguishing them is a user-enumeration oracle.

    Same type, same code, same message. The differences live in the audit log,
    which is where they are useful and where an attacker cannot read them.
    """
    if scenario != "unknown":
        seed_principal(uow, hasher, disabled_at=NOW if scenario == "disabled" else None)
    password = PASSWORD if scenario != "wrong-password" else "not-the-password"

    with pytest.raises(AuthenticationError) as raised:
        await authenticate(use_case, password=password)

    assert str(raised.value) == "[DHR-PRM-002] authentication failed"


@pytest.mark.asyncio
async def test_an_unknown_subject_still_pays_for_a_verification(
    use_case: AuthenticateUseCase, hasher: FakePasswordHasher
) -> None:
    """The half of "indistinguishable" that an error message cannot provide.

    Identical errors returned in visibly different times are the same oracle with
    extra steps: a lookup that misses returns in microseconds while a real argon2
    verification spends deliberate milliseconds.
    """
    with pytest.raises(AuthenticationError):
        await authenticate(use_case, subject="nobody@dhruva.local")

    assert hasher.wasted_verifications == 1


@pytest.mark.asyncio
async def test_a_disabled_principal_also_pays_for_a_verification(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork, hasher: FakePasswordHasher
) -> None:
    """Otherwise the timing distinguishes a disabled account from an absent one."""
    seed_principal(uow, hasher, disabled_at=NOW)

    with pytest.raises(AuthenticationError):
        await authenticate(use_case)

    assert hasher.wasted_verifications == 1


@pytest.mark.asyncio
async def test_an_unknown_subject_is_audited_against_the_reserved_account(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork
) -> None:
    """ADR-004 requires the column; an unknown subject has no tenant.

    A nullable column was the obvious escape and the wrong one -- it would make
    "unattributed" and "nobody filled this in" the same state on the one table
    that exists to be evidence.
    """
    with pytest.raises(AuthenticationError):
        await authenticate(use_case, subject="nobody@dhruva.local")

    assert uow.audit.records[0].account_id == UNATTRIBUTED_ACCOUNT


@pytest.mark.asyncio
async def test_the_attempted_subject_is_recorded_so_a_pattern_is_visible(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork
) -> None:
    """What lets the log distinguish a brute force from a scan.

    The uniform error deliberately cannot; the audit record must, or a run of
    failures is a run of identical rows with nothing to group by.
    """
    with pytest.raises(AuthenticationError):
        await authenticate(use_case, subject="victim@dhruva.local")

    assert uow.audit.records[0].actor == "victim@dhruva.local"


@pytest.mark.asyncio
async def test_a_blank_subject_is_audited_rather_than_crashing(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork
) -> None:
    """An empty login form is ordinary and still has to be auditable.

    ``AuditRecord`` refuses a blank actor, rightly -- so passing the blank
    through would fire that invariant, roll the transaction back, and lose the
    very record the attempt should have produced.
    """
    with pytest.raises(AuthenticationError):
        await authenticate(use_case, subject="   ")

    assert uow.commits == 1
    assert uow.audit.records[0].actor == "(blank)"


@pytest.mark.asyncio
async def test_a_failed_authentication_issues_no_refresh_token(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork, hasher: FakePasswordHasher
) -> None:
    """Committing the audit record must not commit a session with it."""
    seed_principal(uow, hasher)

    with pytest.raises(AuthenticationError):
        await authenticate(use_case, password="not-the-password")

    assert uow.refresh_tokens.by_id == {}


@pytest.mark.asyncio
async def test_one_authentication_is_one_transaction(
    use_case: AuthenticateUseCase, uow: FakeUnitOfWork, hasher: FakePasswordHasher
) -> None:
    """ADR-053: one use case, one Unit of Work, one transaction."""
    seed_principal(uow, hasher)

    await authenticate(use_case)

    assert uow.entered == 1
    assert uow.commits == 1
