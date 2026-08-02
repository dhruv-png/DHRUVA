"""The SQLAlchemy repository for stored broker credentials (ADR-070, ADR-053).

Persistence and nothing else. Four properties are load-bearing, and the fourth
is the one this repository has that the worked example does not:

* **It never commits.** Transaction lifetime belongs to the Unit of Work
  (ADR-053). Nothing here calls ``commit``, ``rollback`` or ``begin``.
* **Updates are explicit and version-checked.** There is no dirty tracking to
  fall back on (ADR-057), and a lost update raises rather than being
  overwritten -- which for a credential means two concurrent rotations cannot
  leave a row wrapped under a key nobody recorded.
* **No SQLAlchemy type escapes.** Callers receive domain objects; models and
  records stay inside this layer (ADR-052).
* **It cannot decrypt, and holds nothing that could.** This module imports no
  cipher and takes no ``KeyProvider``. A read returns ciphertext, exactly as
  ADR-070 requires; obtaining a plaintext is a separate call in
  ``infrastructure.crypto.credentials`` that takes the provider explicitly.

Why the binding columns are never updated
-----------------------------------------
:meth:`CredentialRepository.update` writes the sealed material, the key version
and the timestamps. It deliberately does **not** write ``id``, ``account_id`` or
``broker``, and the omission is a control rather than an oversight: those three
are what the ciphertext's associated data is computed from (TD-S06-6), so
changing one would leave a row whose own contents can no longer open it. The
UPDATE has no way to express that mistake because the statement never mentions
those columns.

Moving a credential between accounts is therefore not an update. It is a new
credential, sealed afresh under the new binding, which is the honest shape for
an operation that has to re-encrypt anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, select, update

from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_credential_model_kwargs,
    to_credential_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import CredentialModel
from dhruva.shared.errors import ConflictError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.platform.domain.identity.credentials import Credential
    from dhruva.contexts.platform.infrastructure.persistence.factories import CredentialFactory
    from dhruva.shared.identity import AccountId, CredentialId

__all__ = ["CredentialRepository"]


class CredentialRepository:
    """Loads and stores :class:`Credential` aggregates."""

    __slots__ = ("_factory", "_session")

    def __init__(self, session: AsyncSession, factory: CredentialFactory) -> None:
        """Bind the repository to a session and a reconstruction factory.

        Both are injected. The repository constructs neither -- and notably it is
        not given a ``KeyProvider`` either, because there is nothing here that
        could use one.
        """
        self._session = session
        self._factory = factory

    async def get(self, account_id: AccountId, broker: str) -> Credential | None:
        """Return the credential for an account and broker, or ``None``.

        The natural key, and the one the table enforces as unique. ``None``
        rather than raising: "this account has no credential for that broker" is
        an ordinary answer, and a caller that requires one raises its own
        :class:`~dhruva.shared.errors.NotFoundError` with context this layer does
        not have.
        """
        statement = select(CredentialModel).where(
            CredentialModel.account_id == account_id.value,
            CredentialModel.broker == broker,
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            return None
        return self._factory.reconstruct(to_credential_record(model))

    async def get_by_id(self, credential_id: CredentialId) -> Credential | None:
        """Return the credential with this identity, or ``None``.

        Present alongside the natural-key lookup because the identity is a real
        domain identifier here rather than a hidden surrogate: an audit record
        naming a credential names it by this, and following that reference should
        not require knowing which account it belonged to.
        """
        model = await self._session.get(CredentialModel, credential_id.value)
        if model is None:
            return None
        return self._factory.reconstruct(to_credential_record(model))

    async def add(self, aggregate: Credential) -> None:
        """Stage a new credential for insertion.

        Staged, not written: nothing reaches the database until the Unit of Work
        commits. No identifier is minted here, unlike the worked example -- the
        aggregate arrives carrying the identity its ciphertext was sealed against,
        and inventing a different one at this point would store a row that can
        never be opened.
        """
        record = self._factory.deconstruct(aggregate)
        self._session.add(CredentialModel(**to_credential_model_kwargs(record)))

    async def update(self, aggregate: Credential) -> None:
        """Stage changes, refusing the write if another writer got there first.

        The aggregate carries the version it was loaded at, incremented by
        whatever domain operation produced it. The UPDATE matches on the previous
        value and writes the new one; zero rows affected means someone else
        committed in between.

        Raises
        ------
        ConflictError
            If the stored version no longer matches. Retry policy belongs to the
            caller -- and for a credential rotation it is very much not
            automatic, since the plaintext that was sealed may no longer be the
            one the broker will accept.
        """
        previous_version = aggregate.version - 1
        record = self._factory.deconstruct(aggregate)
        statement = (
            update(CredentialModel)
            .where(
                CredentialModel.id == record.id,
                CredentialModel.version == previous_version,
            )
            .values(
                # The binding columns -- id, account_id, broker -- are absent by
                # design. See the module docstring.
                ciphertext=record.ciphertext,
                wrapped_data_key=record.wrapped_data_key,
                key_version=record.key_version,
                rotated_at=record.rotated_at,
                updated_at=record.updated_at,
                version=record.version,
            )
        )
        # `rowcount` lives on CursorResult; `execute` is typed as returning the
        # broader Result. The cast is a typing accommodation, not a behavioural
        # assumption -- an UPDATE always yields a cursor result.
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        if result.rowcount == 0:
            msg = "credential was modified by another writer"
            raise ConflictError(
                msg,
                credential_id=str(aggregate.credential_id),
                broker=aggregate.broker,
                expected_version=previous_version,
            )
