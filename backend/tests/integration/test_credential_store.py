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

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import insert, text
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from dhruva.contexts.platform.domain.identity.credentials import Credential, CredentialPurpose
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
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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
ENROLMENT: Final = CredentialPurpose.ENROLMENT
SESSION: Final = CredentialPurpose.SESSION
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


def _credential(  # noqa: PLR0913 - one keyword per bound fact, all defaulted
    provider: MasterKeyProvider,
    *,
    credential_id: CredentialId,
    account_id: AccountId = ACCOUNT,
    broker: str = BROKER,
    purpose: CredentialPurpose = ENROLMENT,
    plaintext: str = PLAINTEXT,
) -> Credential:
    """Seal a credential bound to the row it is about to occupy."""
    sealed = seal_credential(
        SecretValue(plaintext, register=False),
        provider,
        credential_id=credential_id,
        account_id=account_id,
        broker=broker,
        purpose=purpose,
    )
    return Credential(
        credential_id=credential_id,
        account_id=account_id,
        broker=broker,
        purpose=purpose,
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

    loaded = await repository.get(ACCOUNT, BROKER, ENROLMENT)
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

    loaded = await repository.get(ACCOUNT, BROKER, ENROLMENT)
    assert loaded is not None
    assert PLAINTEXT not in repr(loaded)
    assert PLAINTEXT.encode() not in loaded.secret.ciphertext + loaded.secret.wrapped_data_key


async def test_an_absent_credential_reads_as_none(session: AsyncSession) -> None:
    """A missing credential is an ordinary answer to a lookup, not an error."""
    repository = CredentialRepository(session, FACTORY)

    assert await repository.get(ACCOUNT, "never-configured", ENROLMENT) is None
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

    assert await repository.get_by_id(credential_id) == await repository.get(
        ACCOUNT, BROKER, ENROLMENT
    )


async def test_one_credential_per_broker_per_purpose_per_account(
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

    with pytest.raises(IntegrityError, match="uq_credential_account_broker_purpose"):
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

    assert await repository.get(ACCOUNT, BROKER, ENROLMENT) is not None
    assert await repository.get(OTHER_ACCOUNT, BROKER, ENROLMENT) is not None


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
        purpose=ENROLMENT,
    )
    await repository.update(original.resealed(replacement, at=rotated_at, key_version=2))
    await session.flush()
    session.expunge_all()

    loaded = await repository.get(ACCOUNT, BROKER, ENROLMENT)
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
        purpose=ENROLMENT,
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

    moved = await repository.get(OTHER_ACCOUNT, BROKER, ENROLMENT)
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


# --------------------------------------------------------------------------- #
# ADR-077: two lifecycles under one broker
# --------------------------------------------------------------------------- #


async def test_one_broker_holds_an_enrolment_and_a_session_at_once(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """The change's whole reason for existing, against a real unique constraint.

    Same account, same broker, two rows. Before ADR-077 the second insert was an
    integrity error, which is why a broker session had nowhere to live except on
    top of the owner's enrolment material.
    """
    repository = CredentialRepository(session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new(), purpose=ENROLMENT))
    await repository.add(
        _credential(
            provider,
            credential_id=CredentialId.new(),
            purpose=SESSION,
            plaintext="kite-access-token-xyz789",
        )
    )
    await session.flush()
    session.expunge_all()

    enrolment = await repository.get(ACCOUNT, BROKER, ENROLMENT)
    broker_session = await repository.get(ACCOUNT, BROKER, SESSION)
    assert enrolment is not None
    assert broker_session is not None
    assert open_credential(enrolment, provider).reveal() == PLAINTEXT
    assert open_credential(broker_session, provider).reveal() == "kite-access-token-xyz789"


async def test_a_lookup_never_returns_the_other_purpose(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """A missing session is a missing row, not somebody else's secret.

    The failure this rules out is the quiet one: a purpose-agnostic query
    returning whichever row the database reached first, so a login command would
    find the enrolment credential and use an API secret as an access token.
    """
    repository = CredentialRepository(session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new(), purpose=ENROLMENT))
    await session.flush()
    session.expunge_all()

    assert await repository.get(ACCOUNT, BROKER, SESSION) is None
    assert await repository.get(ACCOUNT, BROKER, ENROLMENT) is not None


async def test_replacing_a_session_leaves_the_enrolment_row_untouched(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """Independent rotation, which is the operational point of the separation.

    A session is replaced every morning. If that write moved the enrolment row's
    ``rotated_at`` or ``version``, "when did I last rotate my API secret" would
    silently become "when did I last log in", and ADR-057's conflict detection
    would fire on routine logins.
    """
    repository = CredentialRepository(session, FACTORY)
    enrolment_id, session_id = CredentialId.new(), CredentialId.new()
    await repository.add(_credential(provider, credential_id=enrolment_id, purpose=ENROLMENT))
    original_session = _credential(
        provider,
        credential_id=session_id,
        purpose=SESSION,
        plaintext="kite-access-token-day-one",
    )
    await repository.add(original_session)
    await session.flush()

    replacement = seal_credential(
        SecretValue("kite-access-token-day-two", register=False),
        provider,
        credential_id=session_id,
        account_id=ACCOUNT,
        broker=BROKER,
        purpose=SESSION,
    )
    await repository.update(original_session.resealed(replacement, at=ISSUED + timedelta(days=1)))
    await session.flush()
    session.expunge_all()

    enrolment = await repository.get(ACCOUNT, BROKER, ENROLMENT)
    rotated_session = await repository.get(ACCOUNT, BROKER, SESSION)
    assert enrolment is not None
    assert enrolment.version == 1
    assert enrolment.rotated_at is None
    assert open_credential(enrolment, provider).reveal() == PLAINTEXT
    assert rotated_session is not None
    assert rotated_session.version == 2
    assert open_credential(rotated_session, provider).reveal() == "kite-access-token-day-two"


async def test_a_session_ciphertext_copied_into_the_enrolment_row_no_longer_opens(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """Cross-purpose theft, by the same SQL that proved cross-row theft fails.

    Same account, same broker, same master key. Only the purpose differs, and
    that alone must be enough -- otherwise a token that expires tomorrow opens as
    the owner's long-lived API secret, and the system has no way to tell.
    """
    repository = CredentialRepository(session, FACTORY)
    enrolment_id, session_id = CredentialId.new(), CredentialId.new()
    await repository.add(_credential(provider, credential_id=enrolment_id, purpose=ENROLMENT))
    await repository.add(
        _credential(
            provider,
            credential_id=session_id,
            purpose=SESSION,
            plaintext="kite-access-token-xyz789",
        )
    )
    await session.flush()

    await session.execute(
        text(
            "UPDATE credential AS target SET ciphertext = source.ciphertext, "
            "wrapped_data_key = source.wrapped_data_key "
            "FROM credential AS source "
            "WHERE target.id = :target AND source.id = :source"
        ),
        {"target": enrolment_id.value, "source": session_id.value},
    )
    session.expunge_all()

    tampered = await repository.get(ACCOUNT, BROKER, ENROLMENT)
    assert tampered is not None
    with pytest.raises(SafetyError):
        open_credential(tampered, provider)

    intact = await repository.get(ACCOUNT, BROKER, SESSION)
    assert intact is not None
    assert open_credential(intact, provider).reveal() == "kite-access-token-xyz789"


async def test_a_row_relabelled_to_the_other_purpose_no_longer_opens(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """Relabelling is the cheaper attack, and is refused for the same reason.

    No ciphertext is copied here at all. The row is simply told it is a session
    now, which the repository's UPDATE cannot express -- ``purpose`` is not among
    the columns it writes -- so the only route is the one an attacker with
    database access would take.
    """
    repository = CredentialRepository(session, FACTORY)
    credential_id = CredentialId.new()
    await repository.add(_credential(provider, credential_id=credential_id, purpose=ENROLMENT))
    await session.flush()

    await session.execute(
        text("UPDATE credential SET purpose = 'SESSION' WHERE id = :id"),
        {"id": credential_id.value},
    )
    session.expunge_all()

    relabelled = await repository.get(ACCOUNT, BROKER, SESSION)
    assert relabelled is not None
    with pytest.raises(SafetyError):
        open_credential(relabelled, provider)


async def test_the_check_constraint_refuses_an_unrecognised_purpose(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """The database re-asserts the closed enum for writers that bypass the domain.

    The domain refuses a non-member already. This one protects the table from a
    migration script, a support query, or the next subsystem to write here --
    the same writers the version check constraint exists for.
    """
    repository = CredentialRepository(session, FACTORY)
    credential_id = CredentialId.new()
    await repository.add(_credential(provider, credential_id=credential_id))
    await session.flush()

    with pytest.raises(IntegrityError, match="ck_credential_purpose"):
        await session.execute(
            text("UPDATE credential SET purpose = 'BOTH' WHERE id = :id"),
            {"id": credential_id.value},
        )


async def test_two_brokers_hold_their_purposes_independently(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """Adding purpose to the key must not have collapsed the broker dimension.

    A widened unique constraint is easy to get subtly wrong, and the wrong
    version -- one that keyed on account and purpose alone -- would pass every
    test above while making a second broker impossible to enrol.
    """
    repository = CredentialRepository(session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new()))
    await repository.add(
        _credential(
            provider,
            credential_id=CredentialId.new(),
            broker="another-broker",
            plaintext="kite-api-secret-elsewhere",
        )
    )
    await session.flush()
    session.expunge_all()

    here = await repository.get(ACCOUNT, BROKER, ENROLMENT)
    elsewhere = await repository.get(ACCOUNT, "another-broker", ENROLMENT)
    assert here is not None
    assert elsewhere is not None
    assert open_credential(here, provider).reveal() == PLAINTEXT
    assert open_credential(elsewhere, provider).reveal() == "kite-api-secret-elsewhere"


async def test_a_lost_update_on_one_purpose_is_refused(
    session: AsyncSession, provider: MasterKeyProvider
) -> None:
    """ADR-057 still applies per credential, not per broker.

    Two rotations of the session from the same loaded state. The concurrency
    token is on the row, so widening the key must not have widened the conflict:
    the second session write conflicts, and the enrolment credential -- which
    neither writer touched -- is unaffected.
    """
    repository = CredentialRepository(session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new(), purpose=ENROLMENT))
    session_id = CredentialId.new()
    original = _credential(
        provider,
        credential_id=session_id,
        purpose=SESSION,
        plaintext="kite-access-token-xyz789",
    )
    await repository.add(original)
    await session.flush()

    replacement = seal_credential(
        SecretValue("kite-access-token-replaced", register=False),
        provider,
        credential_id=session_id,
        account_id=ACCOUNT,
        broker=BROKER,
        purpose=SESSION,
    )
    await repository.update(original.resealed(replacement, at=ISSUED + timedelta(days=1)))
    await session.flush()

    with pytest.raises(ConflictError):
        await repository.update(original.resealed(replacement, at=ISSUED + timedelta(days=2)))


# --------------------------------------------------------------------------- #
# Migration 0017
# --------------------------------------------------------------------------- #


def _run_alembic(command: str, revision: str) -> None:
    """Run one migration command in Alembic's required worker thread."""
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper

    getattr(alembic_command, command)(_alembic_config(), revision)


def _alembic_config() -> Config:
    """Build the Alembic config these tests share with the fixtures."""
    from alembic.config import Config as AlembicConfig  # noqa: PLC0415 - test helper

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test helper

    root = find_repo_root()
    config = AlembicConfig(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    return config


def _check_alembic_drift() -> None:
    """Require the live schema and the model metadata to agree."""
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper

    alembic_command.check(_alembic_config())


async def test_the_purpose_migration_downgrades_reapplies_and_has_no_drift(
    migrated: AsyncEngine,
    sole_alembic_head: str,
) -> None:
    """Revision 0017 is reversible on an empty table and leaves no drift.

    Reversibility is asserted through the schema rather than through the
    revision marker alone: after the downgrade the column is gone and the narrow
    uniqueness is back, which is what a rollback has to actually deliver.
    """
    await asyncio.to_thread(_run_alembic, "downgrade", "0016_news_archive")
    try:
        async with migrated.connect() as connection:
            columns = await connection.scalar(
                sql_text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'credential' AND column_name = 'purpose'"
                )
            )
            constraints = await connection.scalar(
                sql_text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'uq_credential_account_broker'"
                )
            )
        assert columns == 0
        assert constraints == 1
    finally:
        await asyncio.to_thread(_run_alembic, "upgrade", "head")

    async with migrated.connect() as connection:
        current = await connection.scalar(sql_text("SELECT version_num FROM alembic_version"))
        purpose_constraints = await connection.scalar(
            sql_text(
                "SELECT count(*) FROM pg_constraint WHERE conname IN "
                "('uq_credential_account_broker_purpose', 'ck_credential_purpose')"
            )
        )
    assert current == sole_alembic_head
    assert purpose_constraints == 2
    await asyncio.to_thread(_check_alembic_drift)


async def test_the_downgrade_refuses_to_discard_a_second_purpose(
    migrated: AsyncEngine,
    committed_session: AsyncSession,
    provider: MasterKeyProvider,
    sole_alembic_head: str,
) -> None:
    """A rollback that would delete sealed material fails instead.

    Two committed credentials for one broker cannot both survive the narrower
    uniqueness, and the migration has no way to regenerate whichever one it
    dropped -- the plaintext is not recoverable from anything it can reach. So
    it refuses, names the conflict, and leaves the schema where it was. A
    recoverable failure beats a silent loss of the owner's API secret.
    """
    repository = CredentialRepository(committed_session, FACTORY)
    await repository.add(_credential(provider, credential_id=CredentialId.new(), purpose=ENROLMENT))
    await repository.add(
        _credential(
            provider,
            credential_id=CredentialId.new(),
            purpose=SESSION,
            plaintext="kite-access-token-xyz789",
        )
    )
    await committed_session.commit()

    with pytest.raises(RuntimeError, match="more than one credential purpose"):
        await asyncio.to_thread(_run_alembic, "downgrade", "0016_news_archive")

    async with migrated.connect() as connection:
        current = await connection.scalar(sql_text("SELECT version_num FROM alembic_version"))
        surviving = await connection.scalar(sql_text("SELECT count(*) FROM credential"))
    assert current == sole_alembic_head, "a refused downgrade must not move the head"
    assert surviving == 2, "no sealed material may be discarded by a refusal"
