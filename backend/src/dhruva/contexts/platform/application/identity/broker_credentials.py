"""Store and fetch sealed broker credentials, one purpose at a time.

Generic on purpose. Nothing here knows what a Kite API secret is or what an
access token is for -- it knows that a broker has secrets, that secrets belong to
lifecycles, and that a lifecycle is replaced without disturbing its neighbour.
The Zerodha specifics belong to the enrolment and login commands that will use
this, and putting them here would put a vendor into the shared identity context.

No plaintext crosses this boundary in either direction except at the two points
where it must: a caller hands in the secret it wants sealed, and asks explicitly
for one to be opened. There is no accessor that returns a plaintext as a side
effect of a lookup, and :class:`StoredCredential` deliberately holds none.

Sealing arrives as a port rather than an import. The application layer may not
reach into ``infrastructure`` (ADR-001), and a use case that imported a cipher
directly would be the exact violation the layer contract exists to fail -- so the
composition root injects the sealer alongside the ``KeyProvider`` ADR-070
requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.platform.domain.identity.credentials import Credential
from dhruva.shared.errors import NotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from dhruva.contexts.platform.domain.identity.credentials import CredentialPurpose
    from dhruva.contexts.platform.domain.identity.keys import KeyProvider
    from dhruva.contexts.platform.domain.identity.ports import (
        CredentialSealer,
        IdentityUnitOfWork,
    )
    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.identity import AccountId, CredentialId

__all__ = [
    "StoreBrokerCredential",
    "StoreBrokerCredentialCommand",
    "StoredCredential",
]


@dataclass(frozen=True, slots=True)
class StoreBrokerCredentialCommand:
    """One secret to seal for one account, broker and purpose."""

    account_id: AccountId
    broker: str
    purpose: CredentialPurpose
    secret: SecretValue
    at: datetime
    #: Identity for a *new* credential. Supplied by the caller because the
    #: ciphertext is bound to it before the row exists, so it cannot be minted
    #: by the repository afterwards. Ignored when a credential already exists:
    #: rotation keeps the original identity, or the binding would stop matching.
    credential_id: CredentialId


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """What a caller may learn about a stored credential without opening it.

    No ciphertext, no wrapped key and no plaintext. A caller wanting the secret
    calls the explicit open; a caller wanting to know whether one exists, when
    it was rotated, or which version it is at, gets that here without touching
    key material at all.
    """

    credential_id: CredentialId
    account_id: AccountId
    broker: str
    purpose: CredentialPurpose
    created_at: datetime
    updated_at: datetime
    rotated_at: datetime | None
    key_version: int
    version: int

    @classmethod
    def of(cls, credential: Credential) -> StoredCredential:
        """Summarise a credential, dropping everything sealed."""
        return cls(
            credential_id=credential.credential_id,
            account_id=credential.account_id,
            broker=credential.broker,
            purpose=credential.purpose,
            created_at=credential.created_at,
            updated_at=credential.updated_at,
            rotated_at=credential.rotated_at,
            key_version=credential.key_version,
            version=credential.version,
        )


class StoreBrokerCredential:
    """Enrol a broker secret, or rotate the one already held for that purpose."""

    __slots__ = ("_key_provider", "_seal", "_unit_of_work_factory")

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], IdentityUnitOfWork],
        key_provider: KeyProvider,
        seal: CredentialSealer,
    ) -> None:
        """Bind the use case to a transaction factory, a key provider and a sealer."""
        self._unit_of_work_factory = unit_of_work_factory
        self._key_provider = key_provider
        self._seal = seal

    async def execute(self, command: StoreBrokerCredentialCommand) -> StoredCredential:
        """Seal the secret and write it, replacing only this purpose.

        First write and rotation are the same call deliberately. An operator
        enrolling a secret and an operator replacing one are doing the same
        thing, and a separate "rotate" entry point would differ only in which
        error it raised when its assumption about existence was wrong.

        The other purpose for this broker is never read or written. That is the
        whole point of ADR-077: replacing a day's session must not touch the
        owner's long-lived material, and the way to guarantee it is to give the
        write no way to reach it.
        """
        async with self._unit_of_work_factory(command.account_id) as unit_of_work:
            existing = await unit_of_work.credentials.get(
                command.account_id, command.broker, command.purpose
            )
            # Rotation reuses the stored identity. Minting a fresh one would
            # seal under a binding the old row cannot present, so the row would
            # be updated with ciphertext it could never open.
            credential_id = command.credential_id if existing is None else existing.credential_id
            sealed = self._seal(
                command.secret,
                self._key_provider,
                credential_id=credential_id,
                account_id=command.account_id,
                broker=command.broker,
                purpose=command.purpose,
            )

            if existing is None:
                stored = Credential(
                    credential_id=credential_id,
                    account_id=command.account_id,
                    broker=command.broker,
                    purpose=command.purpose,
                    secret=sealed,
                    created_at=command.at,
                    updated_at=command.at,
                )
                await unit_of_work.credentials.add(stored)
            else:
                stored = existing.resealed(sealed, at=command.at)
                await unit_of_work.credentials.update(stored)

            await unit_of_work.commit()

        return StoredCredential.of(stored)


class GetBrokerCredential:
    """Read one sealed credential, or say plainly that there is none."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], IdentityUnitOfWork],
    ) -> None:
        """Bind the query to a transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def summary(
        self,
        account_id: AccountId,
        broker: str,
        purpose: CredentialPurpose,
    ) -> StoredCredential | None:
        """Return what is known about the credential without opening it.

        ``None`` is an ordinary answer with two ordinary causes: nothing has been
        enrolled, or no session has been established. A caller distinguishes them
        by which purpose it asked about.
        """
        async with self._unit_of_work_factory(account_id) as unit_of_work:
            credential = await unit_of_work.credentials.get(account_id, broker, purpose)
        return None if credential is None else StoredCredential.of(credential)

    async def sealed(
        self,
        account_id: AccountId,
        broker: str,
        purpose: CredentialPurpose,
    ) -> Credential:
        """Return the sealed credential itself, for a caller that will open it.

        Separate from :meth:`summary` so that obtaining something openable is a
        distinct, greppable act rather than a field somebody happened to read.
        Still no plaintext: opening requires a ``KeyProvider`` the caller must
        hold, and this returns only what ADR-070 says a row contains.

        Raises
        ------
        NotFoundError
            When no credential exists for that purpose. Raising rather than
            returning ``None`` because a caller reaching for openable material
            has already decided it needs one.
        """
        async with self._unit_of_work_factory(account_id) as unit_of_work:
            credential = await unit_of_work.credentials.get(account_id, broker, purpose)
        if credential is None:
            raise NotFoundError(
                "no credential is enrolled for this broker and purpose",
                broker=broker,
                purpose=purpose.value,
            )
        return credential
