"""Principals and refresh tokens against real PostgreSQL (ADR-058, ADR-072).

The unit suite proves the mapping is lossless and the verdict table is right.
What it cannot prove is what this file exists for: that the unique constraints
hold, that optimistic locking detects a lost update, that two simultaneous
rotations produce exactly one winner, and that revoking a lineage reaches every
token in it and stops at the boundary of the next.

The concurrency tests are the ones a fake could not have caught. ``mark_replaced``
returns a boolean derived from ``rowcount``, and a fake returning ``True`` twice
would pass every unit test while letting both halves of a double-click succeed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from dhruva.contexts.platform.domain.identity import (
    EncryptedSecret,
    PasswordHash,
    Principal,
    RefreshToken,
)
from dhruva.contexts.platform.infrastructure.persistence.factories import (
    PrincipalFactory,
    RefreshTokenFactory,
)
from dhruva.contexts.platform.infrastructure.persistence.identity import (
    PrincipalRepository,
    RefreshTokenRepository,
)
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId, PrincipalId, RefreshTokenId

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

PRINCIPALS: Final = PrincipalFactory()
TOKENS: Final = RefreshTokenFactory()

ACCOUNT: Final = AccountId.deterministic("primary")
OTHER_ACCOUNT: Final = AccountId.deterministic("secondary")
CREATED: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
ENCODED: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
SEALED: Final = EncryptedSecret(ciphertext=b"sealed-totp", wrapped_data_key=b"wrapped-key")


def _principal(**overrides: object) -> Principal:
    """Build a principal with a unique subject unless one is supplied."""
    defaults: dict[str, object] = {
        "principal_id": PrincipalId.new(),
        "account_id": ACCOUNT,
        "subject": f"operator-{uuid4().hex[:12]}@dhruva.local",
        "password_hash": PasswordHash(ENCODED),
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    return Principal(**{**defaults, **overrides})  # type: ignore[arg-type]


def _root_token(
    principal: Principal, *, token_id: RefreshTokenId | None = None, **overrides: object
) -> RefreshToken:
    """Build the first token of a chain for a principal."""
    identity = RefreshTokenId.new() if token_id is None else token_id
    defaults: dict[str, object] = {
        "token_id": identity,
        "account_id": principal.account_id,
        "principal_id": principal.principal_id,
        "lineage_id": identity.value,
        "token_hash": uuid4().bytes,
        "issued_at": CREATED,
        "expires_at": CREATED + timedelta(days=30),
    }
    return RefreshToken(**{**defaults, **overrides})  # type: ignore[arg-type]


def _rotate(token: RefreshToken, *, minutes: int = 5) -> tuple[RefreshToken, RefreshToken]:
    moment = token.issued_at + timedelta(minutes=minutes)
    return token.succeeded_by(
        token_id=RefreshTokenId.new(),
        token_hash=uuid4().bytes,
        issued_at=moment,
        expires_at=moment + timedelta(days=30),
    )


# --------------------------------------------------------------------------- #
# Principals
# --------------------------------------------------------------------------- #


async def test_a_principal_survives_a_round_trip(session: AsyncSession) -> None:
    """Every field back exactly as written, including the sealed second factor."""
    repository = PrincipalRepository(session, PRINCIPALS)
    original = _principal(totp_secret=SEALED)

    await repository.add(original)
    await session.flush()
    session.expunge_all()

    assert await repository.get(original.principal_id) == original


async def test_a_principal_is_found_by_subject(session: AsyncSession) -> None:
    """The authentication path's only lookup."""
    repository = PrincipalRepository(session, PRINCIPALS)
    original = _principal()

    await repository.add(original)
    await session.flush()

    assert await repository.get_by_subject(original.subject) == original


async def test_an_unknown_subject_returns_none(session: AsyncSession) -> None:
    """An ordinary answer on a login form, not an error.

    ADR-072 requires the caller to render this exactly as it renders a wrong
    password, so raising here would only have to be flattened one layer up.
    """
    repository = PrincipalRepository(session, PRINCIPALS)

    assert await repository.get_by_subject("nobody@dhruva.local") is None


async def test_two_principals_cannot_share_a_subject(session: AsyncSession) -> None:
    """Globally unique, because authentication is presented with a subject alone.

    Scoping uniqueness per account would make the lookup ambiguous at exactly
    the moment there is more than one account.
    """
    repository = PrincipalRepository(session, PRINCIPALS)
    first = _principal()
    await repository.add(first)
    await session.flush()

    async with session.begin_nested():
        await repository.add(_principal(subject=first.subject, account_id=OTHER_ACCOUNT))

        with pytest.raises(IntegrityError):
            await session.flush()


async def test_a_concurrent_update_is_refused(session: AsyncSession) -> None:
    """ADR-057, on the aggregate where a silent merge would be worst.

    Two password changes resolving by last-write-wins would leave the operator
    holding a password the platform does not have.
    """
    repository = PrincipalRepository(session, PRINCIPALS)
    original = _principal()
    await repository.add(original)
    await session.flush()

    changed = original.with_password(PasswordHash("$argon2id$v=19$other"), at=CREATED)
    await repository.update(changed)

    stale = original.with_password(PasswordHash("$argon2id$v=19$third"), at=CREATED)
    with pytest.raises(ConflictError):
        await repository.update(stale)


async def test_enrolment_can_be_written_and_read_back(session: AsyncSession) -> None:
    """Storage only, per this step's scope -- but the storage has to work.

    Step 7 verifies codes against this secret; if the bytes did not survive
    ``BYTEA`` intact, that would surface as every code being wrong.
    """
    repository = PrincipalRepository(session, PRINCIPALS)
    original = _principal()
    await repository.add(original)
    await session.flush()

    enrolled = original.enrolled(SEALED, at=CREATED + timedelta(minutes=1))
    await repository.update(enrolled)
    await session.flush()
    session.expunge_all()

    loaded = await repository.get(original.principal_id)
    assert loaded is not None
    assert loaded.is_two_factor_enrolled is True
    assert loaded.totp_secret == SEALED


async def test_the_subject_cannot_be_changed_by_an_update(session: AsyncSession) -> None:
    """The column is absent from the SET clause, so the statement cannot express it.

    Changing a subject would orphan every audit record naming the old one. That
    is a new principal, not an update.
    """
    repository = PrincipalRepository(session, PRINCIPALS)
    original = _principal()
    await repository.add(original)
    await session.flush()

    renamed = Principal(
        principal_id=original.principal_id,
        account_id=original.account_id,
        subject="someone-else@dhruva.local",
        password_hash=original.password_hash,
        created_at=original.created_at,
        updated_at=CREATED + timedelta(minutes=1),
        version=original.version + 1,
    )
    await repository.update(renamed)
    await session.flush()
    session.expunge_all()

    loaded = await repository.get(original.principal_id)
    assert loaded is not None
    assert loaded.subject == original.subject


async def test_half_a_sealed_secret_is_refused_by_the_table(session: AsyncSession) -> None:
    """A ciphertext with no wrapped key is a secret nobody can open.

    Refused here so the bad row never exists, rather than discovered when
    somebody tries to log in.
    """
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO principal (id, account_id, subject, password_hash, "
                " totp_secret, created_at, updated_at, version) VALUES "
                "(:id, :account, :subject, :hash, :secret, :at, :at, 1)"
            ),
            {
                "id": uuid4(),
                "account": ACCOUNT.value,
                "subject": f"half-{uuid4().hex[:8]}@dhruva.local",
                "hash": ENCODED,
                "secret": b"ciphertext-with-no-key",
                "at": CREATED,
            },
        )


# --------------------------------------------------------------------------- #
# Refresh tokens
# --------------------------------------------------------------------------- #


async def test_a_refresh_token_survives_a_round_trip(session: AsyncSession) -> None:
    """Including the digest through ``BYTEA``, which is what presentation matches on."""
    principal = _principal()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RefreshTokenRepository(session, TOKENS)
    original = _root_token(principal)

    await repository.add(original)
    await session.flush()
    session.expunge_all()

    assert await repository.get_by_hash(original.token_hash) == original


async def test_two_tokens_cannot_share_a_hash(session: AsyncSession) -> None:
    """Two rows with one digest would make "which session is this?" unanswerable."""
    principal = _principal()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RefreshTokenRepository(session, TOKENS)
    first = _root_token(principal)
    await repository.add(first)
    await session.flush()

    async with session.begin_nested():
        await repository.add(_root_token(principal, token_hash=first.token_hash))

        with pytest.raises(IntegrityError):
            await session.flush()


async def test_exactly_one_of_two_simultaneous_rotations_wins(session: AsyncSession) -> None:
    """The concurrency control that replaces a version column.

    A double-clicked refresh presents the same token twice. Both requests read
    it as current, both attempt to close it, and exactly one must succeed --
    the other is a loser rather than a thief, so it must not revoke the lineage.
    A fake returning ``True`` twice would pass every unit test in the suite.
    """
    principal = _principal()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RefreshTokenRepository(session, TOKENS)
    root = _root_token(principal)
    await repository.add(root)
    await session.flush()

    first_spent, _ = _rotate(root)
    second_spent, _ = _rotate(root)

    assert await repository.mark_replaced(first_spent) is True
    assert await repository.mark_replaced(second_spent) is False


async def test_revoking_a_lineage_reaches_every_token_in_it(session: AsyncSession) -> None:
    """One statement, one predicate, and no token in the family left usable."""
    principal = _principal()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RefreshTokenRepository(session, TOKENS)

    root = _root_token(principal)
    await repository.add(root)
    spent, child = _rotate(root)
    await repository.mark_replaced(spent)
    await repository.add(child)
    spent_child, grandchild = _rotate(child)
    await repository.mark_replaced(spent_child)
    await repository.add(grandchild)
    await session.flush()

    revoked = await repository.revoke_lineage(root.lineage_id, at=CREATED + timedelta(hours=1))
    await session.flush()
    session.expunge_all()

    assert revoked == 3
    for token in (root, child, grandchild):
        loaded = await repository.get_by_hash(token.token_hash)
        assert loaded is not None
        assert loaded.is_revoked, "a token in a revoked lineage is still usable"


async def test_revoking_a_lineage_does_not_touch_another(session: AsyncSession) -> None:
    """The boundary. One compromised session must not log out every session."""
    principal = _principal()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RefreshTokenRepository(session, TOKENS)
    compromised = _root_token(principal)
    innocent = _root_token(principal)
    await repository.add(compromised)
    await repository.add(innocent)
    await session.flush()

    await repository.revoke_lineage(compromised.lineage_id, at=CREATED + timedelta(hours=1))
    await session.flush()
    session.expunge_all()

    survivor = await repository.get_by_hash(innocent.token_hash)
    assert survivor is not None
    assert survivor.is_revoked is False


async def test_revoking_an_already_dead_lineage_revokes_nothing(session: AsyncSession) -> None:
    """What makes repeated reuse detection cheap.

    An attacker hammering a stolen token triggers one real revocation and then a
    series of no-ops, rather than one audit record per request.
    """
    principal = _principal()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RefreshTokenRepository(session, TOKENS)
    root = _root_token(principal)
    await repository.add(root)
    await session.flush()

    first = await repository.revoke_lineage(root.lineage_id, at=CREATED + timedelta(hours=1))
    second = await repository.revoke_lineage(root.lineage_id, at=CREATED + timedelta(hours=2))

    assert first == 1
    assert second == 0


async def test_the_first_revocation_instant_is_preserved(session: AsyncSession) -> None:
    """The instant a lineage was killed is what an incident review reads."""
    principal = _principal()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RefreshTokenRepository(session, TOKENS)
    root = _root_token(principal)
    await repository.add(root)
    await session.flush()

    killed_at = CREATED + timedelta(hours=1)
    await repository.revoke_lineage(root.lineage_id, at=killed_at)
    await repository.revoke_lineage(root.lineage_id, at=CREATED + timedelta(hours=5))
    await session.flush()
    session.expunge_all()

    loaded = await repository.get_by_hash(root.token_hash)
    assert loaded is not None
    assert loaded.revoked_at == killed_at


async def test_a_root_token_must_anchor_its_own_lineage(session: AsyncSession) -> None:
    """Enforced by the table as well as the aggregate.

    An orphan lineage is a token that revocation reaches by no predicate, and it
    would still authenticate.
    """
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO refresh_token (id, account_id, principal_id, lineage_id, "
                " token_hash, issued_at, expires_at) VALUES "
                "(:id, :account, :principal, :lineage, :hash, :issued, :expires)"
            ),
            {
                "id": uuid4(),
                "account": ACCOUNT.value,
                "principal": uuid4(),
                "lineage": uuid4(),
                "hash": uuid4().bytes,
                "issued": CREATED,
                "expires": CREATED + timedelta(days=30),
            },
        )


async def test_a_token_cannot_expire_before_it_is_issued(session: AsyncSession) -> None:
    """A zero-length session is a configuration defect, refused at the table."""
    token_id = uuid4()
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO refresh_token (id, account_id, principal_id, lineage_id, "
                " token_hash, issued_at, expires_at) VALUES "
                "(:id, :account, :principal, :id, :hash, :issued, :expires)"
            ),
            {
                "id": token_id,
                "account": ACCOUNT.value,
                "principal": uuid4(),
                "hash": uuid4().bytes,
                "issued": CREATED,
                "expires": CREATED,
            },
        )


async def test_the_table_carries_row_level_security_policies(session: AsyncSession) -> None:
    """ADR-074: every table with an ``account_id`` has one, authored from the start."""
    for table in ("principal", "refresh_token"):
        policies = await session.scalar(
            text("SELECT count(*) FROM pg_policies WHERE tablename = :t"), {"t": table}
        )
        enabled = await session.scalar(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
        )

        assert policies == 1, f"{table} has no policy"
        assert enabled is True, f"{table} does not have RLS enabled"
