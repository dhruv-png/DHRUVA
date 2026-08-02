"""The credential store against a real database (ADR-058, ADR-070, ADR-057).

Everything here needs PostgreSQL. The unit suite already proves the cipher binds
and the mapping is lossless; what it cannot prove is that the bytes survive
``BYTEA``, that the unique constraint holds, that optimistic locking detects a
lost update, and -- the test this whole step exists for -- that a ciphertext
physically moved between two rows by SQL no longer opens.

That last one is TD-S06-6 closed against the thing it was always about. A unit
test can construct a credential holding another's sealed values; only a database
can be made to actually hold them.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from dhruva.contexts.platform.domain.identity.credentials import Credential
from dhruva.contexts.platform.infrastructure.crypto import (
    MASTER_KEY_BYTES,
    MasterKeyProvider,
    open_credential,
    seal_credential,
)
from dhruva.contexts.platform.infrastructure.persistence.credentials import CredentialRepository
from dhruva.contexts.platform.infrastructure.persistence.factories import CredentialFactory
from dhruva.contexts.platform.infrastructure.persistence.mappers import to_credential_model_kwargs
from dhruva.contexts.platform.infrastructure.persistence.models import CredentialModel
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import ConflictError, SafetyError
from dhruva.shared.identity import AccountId, CredentialId

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# See `test_repository_correctness` for why the markers are declared here rather
# than synthesised at collection time.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

FACTORY: Final = CredentialFactory()
ACCOUNT: Final = AccountId.deterministic("primary")
OTHER_ACCOUNT: Final = AccountId.deterministic("secondary")
BROKER: Final = "zerodha"
ISSUED: Final = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)
PLAINTEXT: Final = "kite-api-secret-abc123"

#: Fixed rather than random, so a failure reproduces. It is still 32 bytes of
#: real key material -- nothing here is a placeholder the adapter would refuse.
MASTER_KEY: Final = SecretValue(
    base64.b64encode(bytes(range(MASTER_KEY_BYTES))).decode(), register=False
)


@pytest.fixture
def provider() -> MasterKeyProvider:
    """Return the key provider the tests seal and open with."""
    return MasterKeyProvider(MASTER_KEY)


def _credential(
    provider: MasterKeyProvider,
    *,
    credential_id: CredentialId,
    account_id: AccountId = ACCOUNT,
    broker: str = BROKER,
    plaintext: str = PLAINTEXT,
) -> Credential:
    """Seal a credential bound to the row it is about to occupy."""
    sealed = seal_credential(
        SecretValue(plaintext, register=False),
        provider,
        credential_id=credential_id,
        account_id=account_id,
        broker=broker,
    )
    return Credential(
        credential_id=credential_id,
        account_id=account_id,
        broker=broker,
        secret=sealed,
        created_at=ISSUED,
        updated_at=ISSUED,
    )


# --------------------------------------------------------------------------- #
# The ordinary path
# --------------------------------------------------------------------------- #


async def test_a_credential_survives_a_round_trip_through_postgresql(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """Sealed bytes through ``BYTEA`` and back, still openable.

    The assertion that matters is the last one. Equality of the byte fields would
    pass even if the driver returned a ``memoryview`` that compared equal and
    decrypted differently; opening the credential is what proves the row is
    intact all the way down.
    """
    repository = CredentialRepository(session, FACTORY)
    original = _credential(provider, credential_id=CredentialId.new())

    await repository.add(original)
    await session.flush()
    session.expunge_all()

    loaded = await repository.get(ACCOUNT, BROKER)
    assert loaded is not None
    assert loaded == original
    assert open_credential(loaded, provider).reveal() == PLAINTEXT


async def test_a_read_returns_ciphertext_and_nothing_else(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """ADR-070: the repository's read yields no plaintext by any route.

    Asserted over the whole loaded aggregate rather than a chosen field, because
    the failure this guards against is a plaintext appearing somewhere nobody
    thought to look -- a debug attribute, a cached property, a ``repr``.
    """
    repository = CredentialRepository(session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new()))
    await session.flush()
    session.expunge_all()

    loaded = await repository.get(ACCOUNT, BROKER)
    assert loaded is not None
    assert PLAINTEXT not in repr(loaded)
    assert PLAINTEXT.encode() not in loaded.secret.ciphertext + loaded.secret.wrapped_data_key


async def test_an_absent_credential_reads_as_none(session: AsyncSession) -> None:
    """A missing credential is an ordinary answer to a lookup, not an error."""
    repository = CredentialRepository(session, FACTORY)

    assert await repository.get(ACCOUNT, "never-configured") is None
    assert await repository.get_by_id(CredentialId.new()) is None


async def test_a_credential_is_findable_by_its_own_identity(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """The identity is a real domain identifier here, so it must resolve."""
    repository = CredentialRepository(session, FACTORY)
    credential_id = CredentialId.new()
    await repository.add(_credential(provider, credential_id=credential_id))
    await session.flush()
    session.expunge_all()

    assert await repository.get_by_id(credential_id) == await repository.get(ACCOUNT, BROKER)


async def test_one_credential_per_broker_per_account(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """The unique constraint, exercised rather than assumed.

    The duplicate is inserted with a Core statement rather than through a second
    ``add`` and flush, for two reasons.

    The route being defended against is a writer that bypasses this repository --
    a migration backfill, a support query, the next subsystem to store a
    credential -- so bypassing it is what the test should do. That is how the
    worked example's constraints are exercised too.

    The second reason is a trap worth naming, because the first version of this
    test fell into it. Provoking the violation inside ``session.flush()`` makes
    the Session roll back its own transaction on the way out. This session is
    bound to a connection whose transaction the fixture owns, so that rolls back
    the *fixture's* transaction as well; teardown then finds it deassociated and
    emits ``SAWarning: transaction already deassociated from connection``, which
    ``filterwarnings = ["error"]`` turns into a teardown error on a test whose
    own assertion passed. An error raised by ``session.execute`` does not
    trigger that rollback, which is why the sibling check-constraint test below
    was unaffected.
    """
    repository = CredentialRepository(session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new()))
    await session.flush()

    duplicate = FACTORY.deconstruct(_credential(provider, credential_id=CredentialId.new()))
    statement = insert(CredentialModel).values(**to_credential_model_kwargs(duplicate))

    with pytest.raises(IntegrityError, match="uq_credential_account_broker"):
        await session.execute(statement)


async def test_two_accounts_may_hold_a_credential_for_the_same_broker(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """ADR-004: the tenant column is part of the key, not decoration."""
    repository = CredentialRepository(session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new()))
    await repository.add(
        _credential(provider, credential_id=CredentialId.new(), account_id=OTHER_ACCOUNT)
    )
    await session.flush()
    session.expunge_all()

    assert await repository.get(ACCOUNT, BROKER) is not None
    assert await repository.get(OTHER_ACCOUNT, BROKER) is not None


# --------------------------------------------------------------------------- #
# Rotation and optimistic concurrency (ADR-057)
# --------------------------------------------------------------------------- #


async def test_a_rotated_credential_persists_and_opens_to_the_new_secret(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """The update path, end to end, including the columns rotation moves."""
    repository = CredentialRepository(session, FACTORY)
    credential_id = CredentialId.new()
    original = _credential(provider, credential_id=credential_id)
    await repository.add(original)
    await session.flush()

    rotated_at = ISSUED + timedelta(days=30)
    replacement = seal_credential(
        SecretValue("kite-api-secret-rotated", register=False),
        provider,
        credential_id=credential_id,
        account_id=ACCOUNT,
        broker=BROKER,
    )
    await repository.update(original.resealed(replacement, at=rotated_at, key_version=2))
    await session.flush()
    session.expunge_all()

    loaded = await repository.get(ACCOUNT, BROKER)
    assert loaded is not None
    assert loaded.version == 2
    assert loaded.key_version == 2
    assert loaded.rotated_at == rotated_at
    assert open_credential(loaded, provider).reveal() == "kite-api-secret-rotated"


async def test_a_lost_update_is_refused(session: AsyncSession, provider: MasterKeyProvider) -> None:
    """ADR-057, and the reason the ``version`` column exists on this table.

    Two rotations from the same loaded state. Without the check the second would
    overwrite the first, leaving a row wrapped under a data key whose plaintext
    nobody recorded -- silently, and discoverable only when the broker rejected
    it.
    """
    repository = CredentialRepository(session, FACTORY)
    credential_id = CredentialId.new()
    original = _credential(provider, credential_id=credential_id)
    await repository.add(original)
    await session.flush()

    replacement = seal_credential(
        SecretValue("kite-api-secret-rotated", register=False),
        provider,
        credential_id=credential_id,
        account_id=ACCOUNT,
        broker=BROKER,
    )
    first = original.resealed(replacement, at=ISSUED + timedelta(days=1))
    await repository.update(first)
    await session.flush()

    with pytest.raises(ConflictError):
        await repository.update(original.resealed(replacement, at=ISSUED + timedelta(days=2)))


async def test_the_check_constraint_refuses_a_version_below_one(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """The database re-asserts the invariant for writers that bypass the domain.

    The aggregate refuses a version below one as well. Both matter: the domain
    check protects the application, and this one protects the table from a
    migration script, a support query, or the next subsystem to write here.
    """
    repository = CredentialRepository(session, FACTORY)
    credential_id = CredentialId.new()
    await repository.add(_credential(provider, credential_id=credential_id))
    await session.flush()
    statement = text("UPDATE credential SET version = 0 WHERE id = :id")

    with pytest.raises(IntegrityError):
        await session.execute(statement, {"id": credential_id.value})


# --------------------------------------------------------------------------- #
# TD-S06-6: the binding, against real rows
# --------------------------------------------------------------------------- #


async def test_a_ciphertext_moved_between_rows_in_sql_no_longer_opens(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """The debt, closed against the attack it describes.

    Two real rows. The first row's ``ciphertext`` and ``wrapped_data_key`` are
    copied over the second's with an UPDATE that the application never issues --
    which is exactly the position someone with database access is in. The row is
    perfectly well-formed and the master key is the right one; it simply is not
    the record the ciphertext was sealed for, and so it refuses.

    Before this step the same UPDATE would have handed back the victim's secret
    under the impostor's name, and nothing would have logged.
    """
    repository = CredentialRepository(session, FACTORY)
    victim_id, impostor_id = CredentialId.new(), CredentialId.new()
    await repository.add(_credential(provider, credential_id=victim_id))
    await repository.add(
        _credential(
            provider,
            credential_id=impostor_id,
            account_id=OTHER_ACCOUNT,
            plaintext="kite-api-secret-other",
        )
    )
    await session.flush()

    await session.execute(
        text(
            "UPDATE credential AS impostor SET ciphertext = victim.ciphertext, "
            "wrapped_data_key = victim.wrapped_data_key "
            "FROM credential AS victim "
            "WHERE impostor.id = :impostor AND victim.id = :victim"
        ),
        {"impostor": impostor_id.value, "victim": victim_id.value},
    )
    session.expunge_all()

    impostor = await repository.get_by_id(impostor_id)
    assert impostor is not None
    with pytest.raises(SafetyError):
        open_credential(impostor, provider)


async def test_the_victim_row_still_opens_after_the_theft(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """The refusal must be the impostor's, not a vault-wide failure.

    Worth asserting separately: a binding that broke every read would also make
    the test above pass, and would be a far worse outcome than the bug.
    """
    repository = CredentialRepository(session, FACTORY)
    victim_id, impostor_id = CredentialId.new(), CredentialId.new()
    await repository.add(_credential(provider, credential_id=victim_id))
    await repository.add(_credential(provider, credential_id=impostor_id, account_id=OTHER_ACCOUNT))
    await session.flush()

    await session.execute(
        text(
            "UPDATE credential AS impostor SET ciphertext = victim.ciphertext, "
            "wrapped_data_key = victim.wrapped_data_key "
            "FROM credential AS victim "
            "WHERE impostor.id = :impostor AND victim.id = :victim"
        ),
        {"impostor": impostor_id.value, "victim": victim_id.value},
    )
    session.expunge_all()

    victim = await repository.get_by_id(victim_id)
    assert victim is not None
    assert open_credential(victim, provider).reveal() == PLAINTEXT


async def test_a_credential_re_parented_in_sql_no_longer_opens(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """Changing the tenant column alone is enough to invalidate the binding.

    This is the case the repository's UPDATE cannot express -- ``account_id`` is
    not among the columns it writes -- so the only way to reach it is the way an
    attacker would.
    """
    repository = CredentialRepository(session, FACTORY)
    credential_id = CredentialId.new()
    await repository.add(_credential(provider, credential_id=credential_id))
    await session.flush()

    await session.execute(
        text("UPDATE credential SET account_id = :account WHERE id = :id"),
        {"account": OTHER_ACCOUNT.value, "id": credential_id.value},
    )
    session.expunge_all()

    moved = await repository.get(OTHER_ACCOUNT, BROKER)
    assert moved is not None
    with pytest.raises(SafetyError):
        open_credential(moved, provider)


# --------------------------------------------------------------------------- #
# What the repository is not
# --------------------------------------------------------------------------- #


async def test_the_repository_stages_rather_than_writes(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """ADR-053. Transaction lifetime belongs to the Unit of Work.

    ``add`` leaves the model pending in the session. Nothing reaches the database
    until something else decides to flush, and nothing becomes durable until the
    Unit of Work commits -- neither of which this repository can do.
    """
    repository = CredentialRepository(session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new()))

    assert len(session.new) == 1, "add() must stage, not write"


async def test_the_repository_offers_no_way_to_decrypt() -> None:
    """ADR-070, asserted on the repository's surface.

    No method returns a plaintext and no constructor argument could supply the
    means to obtain one. The plaintext path is one named function elsewhere,
    which is what makes it greppable.
    """
    public = {name for name in dir(CredentialRepository) if not name.startswith("_")}

    assert public == {"add", "get", "get_by_id", "update"}
    assert "key_provider" not in CredentialRepository.__init__.__annotations__
