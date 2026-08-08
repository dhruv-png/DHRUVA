"""In-memory doubles for the identity ports.

Deliberately **fakes rather than mocks**: each one is a working implementation
over a dict, so the tests assert what ended up stored rather than which methods
were called. A test that asserts on calls passes when the calls are right and the
logic is wrong, which on an authentication path is the wrong way round.

Two behaviours are copied from the real adapters on purpose, because a fake that
is easier than the thing it stands for will pass tests the real code fails:

* :meth:`FakeRefreshTokenStore.mark_replaced` returns ``False`` for an
  already-closed token, exactly as the ``WHERE replaced_by IS NULL`` predicate
  does. A fake returning ``True`` twice would let both halves of a double-click
  succeed and nothing would notice.
* :meth:`FakeRefreshTokenStore.revoke_lineage` skips already-revoked tokens and
  returns a count, so the idempotence the real ``WHERE revoked_at IS NULL``
  provides is testable here too.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Self
from uuid import UUID, uuid4

from dhruva.contexts.platform.domain.identity.authorisation import Permission, is_permitted
from dhruva.contexts.platform.domain.identity.metrics import (
    AuthenticationOperation,
    AuthorisationOperation,
    SecurityOutcome,
)
from dhruva.contexts.platform.domain.identity.passwords import PasswordHash
from dhruva.contexts.platform.domain.identity.ports import MintedRefreshToken
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import ConflictError

if TYPE_CHECKING:
    from datetime import datetime
    from types import TracebackType

    from dhruva.contexts.platform.domain.audit import AuditRecord
    from dhruva.contexts.platform.domain.identity.authorisation import Role
    from dhruva.contexts.platform.domain.identity.credentials import (
        Credential,
        CredentialPurpose,
    )
    from dhruva.contexts.platform.domain.identity.principals import Principal
    from dhruva.contexts.platform.domain.identity.refresh import RefreshToken
    from dhruva.shared.identity import AccountId, CredentialId, PrincipalId

__all__ = [
    "FakeAuditSink",
    "FakeAuthorisationDirectory",
    "FakeCredentialStore",
    "FakeIdentityMetrics",
    "FakePasswordHasher",
    "FakePrincipalStore",
    "FakeRefreshTokenMinter",
    "FakeRefreshTokenStore",
    "FakeRoleStore",
    "FakeUnitOfWork",
]


class FakeIdentityMetrics:
    """Collects only the closed operation/outcome pairs accepted by the port."""

    def __init__(self) -> None:
        self.authentications: list[tuple[AuthenticationOperation, SecurityOutcome]] = []
        self.authorisations: list[tuple[AuthorisationOperation, SecurityOutcome]] = []

    def authentication(
        self,
        operation: AuthenticationOperation,
        outcome: SecurityOutcome,
    ) -> None:
        """Collect one bounded authentication outcome."""
        self.authentications.append((operation, outcome))

    def authorisation(
        self,
        operation: AuthorisationOperation,
        outcome: SecurityOutcome,
    ) -> None:
        """Collect one bounded authorisation outcome."""
        self.authorisations.append((operation, outcome))


class FakePasswordHasher:
    """Hashes by prefixing, so a "hash" is readable in a failing assertion.

    Records every call to :meth:`verify_nothing`, because ADR-072's timing
    equalisation is a real requirement and "the unknown-subject path did the
    work" is only assertable if the fake counts it.
    """

    def __init__(self) -> None:
        self.wasted_verifications = 0

    def hash(self, password: SecretValue) -> PasswordHash:
        """Return a readable stand-in for a hash."""
        return PasswordHash(f"hashed::{password.reveal()}")

    def verify(self, password: SecretValue, expected: PasswordHash) -> bool:
        """Report whether the password produced the stored hash."""
        return expected.encoded == f"hashed::{password.reveal()}"

    def verify_nothing(self, password: SecretValue) -> None:
        """Count the wasted verification the real hasher would have performed."""
        _ = password.reveal()
        self.wasted_verifications += 1


class FakeRefreshTokenMinter:
    """Mints predictable tokens, so a test can present one it knows.

    The real minter draws from the CSPRNG, which is correct and untestable: "the
    token was random" is not something an assertion can make about a value it
    cannot predict. That is exactly why the minter is a port.
    """

    def __init__(self) -> None:
        self.minted: list[str] = []

    def mint(self) -> MintedRefreshToken:
        """Return the next token in a predictable sequence."""
        token = f"refresh-{len(self.minted)}-{uuid4().hex[:8]}"
        self.minted.append(token)
        return MintedRefreshToken(secret=SecretValue(token, register=False), digest=_digest(token))

    def digest(self, presented: SecretValue) -> bytes:
        """Digest a presented token the same way :meth:`mint` did."""
        return _digest(presented.reveal())


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class FakePrincipalStore:
    """A principal store over two dicts, keyed by identity and by subject."""

    def __init__(self) -> None:
        self.by_id: dict[UUID, Principal] = {}

    def seed(self, principal: Principal) -> Principal:
        """Insert a principal directly, bypassing the transaction."""
        self.by_id[principal.principal_id.value] = principal
        return principal

    async def get_by_subject(self, subject: str) -> Principal | None:
        """Return the principal with this login subject, or ``None``."""
        return next((p for p in self.by_id.values() if p.subject == subject), None)

    async def get(self, principal_id: PrincipalId) -> Principal | None:
        """Return the principal with this identity, or ``None``."""
        return self.by_id.get(principal_id.value)

    async def add(self, principal: Principal) -> None:
        """Stage a new principal."""
        self.by_id[principal.principal_id.value] = principal

    async def update(self, principal: Principal) -> None:
        """Stage changes to an existing principal."""
        self.by_id[principal.principal_id.value] = principal


class FakeRefreshTokenStore:
    """A refresh-token store that reproduces the real one's conditional updates."""

    def __init__(self) -> None:
        self.by_id: dict[UUID, RefreshToken] = {}
        self.lose_next_rotation = False

    def seed(self, token: RefreshToken) -> RefreshToken:
        """Insert a token directly, bypassing the transaction."""
        self.by_id[token.token_id.value] = token
        return token

    async def get_by_hash(self, token_hash: bytes) -> RefreshToken | None:
        """Return the token with this digest, or ``None``."""
        return next((t for t in self.by_id.values() if t.token_hash == token_hash), None)

    async def add(self, token: RefreshToken) -> None:
        """Stage a newly issued token."""
        self.by_id[token.token_id.value] = token

    async def mark_replaced(self, token: RefreshToken) -> bool:
        """Close a token if it is still open, reporting whether this call won.

        Mirrors ``WHERE replaced_by IS NULL``. See the module docstring for why
        this fake must be able to say no.
        """
        if self.lose_next_rotation:
            self.lose_next_rotation = False
            return False

        stored = self.by_id.get(token.token_id.value)
        if stored is None or stored.is_spent:
            return False
        self.by_id[token.token_id.value] = replace(stored, replaced_by=token.replaced_by)
        return True

    async def revoke_lineage(self, lineage_id: UUID, *, at: datetime) -> int:
        """Revoke every unrevoked token in a chain, returning how many."""
        revoked = 0
        for key, token in list(self.by_id.items()):
            if token.lineage_id == lineage_id and not token.is_revoked:
                self.by_id[key] = replace(token, revoked_at=at)
                revoked += 1
        return revoked


class FakeRoleStore:
    """A role store keyed by tenant and name, with explicit principal assignments."""

    def __init__(self) -> None:
        self.by_key: dict[tuple[UUID, str], Role] = {}
        self.assignments: dict[UUID, tuple[UUID, str]] = {}
        self.lose_next_update = False

    def seed(self, role: Role) -> Role:
        """Insert a role directly, bypassing the transaction."""
        self.by_key[(role.account_id.value, role.name)] = role
        return role

    def assign(self, principal_id: PrincipalId, role: Role) -> None:
        """Bind a principal to a seeded role for a use-case test."""
        self.assignments[principal_id.value] = (role.account_id.value, role.name)

    async def get(self, account_id: AccountId, name: str) -> Role | None:
        """Return the named tenant role, or ``None``."""
        return self.by_key.get((account_id.value, name))

    async def get_for_principal(self, principal_id: PrincipalId) -> Role | None:
        """Return the assigned role, or ``None`` for deny by default."""
        key = self.assignments.get(principal_id.value)
        return self.by_key.get(key) if key is not None else None

    async def add(self, role: Role) -> None:
        """Stage a new role."""
        self.by_key[(role.account_id.value, role.name)] = role

    async def update(self, role: Role) -> None:
        """Stage a changed role."""
        if self.lose_next_update:
            self.lose_next_update = False
            raise ConflictError("role was modified by another writer")
        self.by_key[(role.account_id.value, role.name)] = role


class FakeCredentialStore:
    """A credential store keyed the way the table is: account, broker, purpose.

    Two behaviours are copied from the real repository rather than simplified,
    for the reason the module docstring gives.

    The key includes the purpose, so a fake lookup cannot find a credential the
    real ``WHERE purpose = :purpose`` would not. A fake keyed on account and
    broker alone would let a use case that forgot the purpose pass here and
    return the wrong lifecycle's secret in production.

    :meth:`update` raises ``ConflictError`` on a stale version, mirroring
    ADR-057's conditional UPDATE, so a lost update is detectable without a
    database.
    """

    def __init__(self) -> None:
        self.by_key: dict[tuple[UUID, str, str], Credential] = {}
        self.lose_next_update = False

    @staticmethod
    def _key(
        account_id: AccountId, broker: str, purpose: CredentialPurpose
    ) -> tuple[UUID, str, str]:
        return (account_id.value, broker, purpose.value)

    def seed(self, credential: Credential) -> Credential:
        """Insert a credential directly, bypassing the transaction."""
        self.by_key[self._key(credential.account_id, credential.broker, credential.purpose)] = (
            credential
        )
        return credential

    async def get(
        self,
        account_id: AccountId,
        broker: str,
        purpose: CredentialPurpose,
    ) -> Credential | None:
        """Return the credential for this account, broker and purpose."""
        return self.by_key.get(self._key(account_id, broker, purpose))

    async def get_by_id(self, credential_id: CredentialId) -> Credential | None:
        """Return the credential with this identity, or ``None``."""
        return next(
            (c for c in self.by_key.values() if c.credential_id == credential_id),
            None,
        )

    async def add(self, credential: Credential) -> None:
        """Stage a new credential."""
        self.seed(credential)

    async def update(self, credential: Credential) -> None:
        """Stage a rotation, refusing it if another writer got there first."""
        if self.lose_next_update:
            self.lose_next_update = False
            raise ConflictError("credential was modified by another writer")
        self.seed(credential)


class FakeAuditSink:
    """Collects audit records instead of writing them."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def record(self, record: AuditRecord) -> UUID:
        """Collect one record and return the identity it would have been given."""
        self.records.append(record)
        return uuid4()


class FakeAuthorisationDirectory:
    """Authority queries over the same principal, role and assignment fakes."""

    def __init__(self, principals: FakePrincipalStore, roles: FakeRoleStore) -> None:
        self._principals = principals
        self._roles = roles
        self.serialised: list[AccountId] = []

    async def serialise(self, account_id: AccountId) -> None:
        """Remember which tenant would have been transaction-locked."""
        self.serialised.append(account_id)

    async def holders(self, account_id: AccountId, role_name: str) -> tuple[Principal, ...]:
        """Return all principals assigned to the named tenant role."""
        return tuple(
            principal
            for principal_id, assignment in self._roles.assignments.items()
            if assignment == (account_id.value, role_name)
            and (principal := self._principals.by_id.get(principal_id)) is not None
        )

    async def count_active_managers(
        self,
        account_id: AccountId,
        *,
        excluding_role_name: str | None = None,
    ) -> int:
        """Count eligible managers outside an optional role."""
        count = 0
        for principal_id, assignment in self._roles.assignments.items():
            assigned_account, role_name = assignment
            if assigned_account != account_id.value or role_name == excluding_role_name:
                continue
            principal = self._principals.by_id.get(principal_id)
            role = self._roles.by_key.get(assignment)
            if (
                principal is not None
                and role is not None
                and principal.is_active
                and principal.is_two_factor_enrolled
                and is_permitted(role, Permission.MANAGE_AUTHORISATION)
            ):
                count += 1
        return count


@dataclass
class FakeUnitOfWork:
    """A transaction that remembers whether it was committed.

    ``commits`` is the assertion the failed-authentication tests turn on: ADR-072
    and plan §15.1 require a failed attempt to be *recorded*, which means the
    transaction that holds its audit row must commit before the error is raised.
    A fake that could not tell committed from rolled back would make that
    untestable without a database.
    """

    principals: FakePrincipalStore = field(default_factory=FakePrincipalStore)
    refresh_tokens: FakeRefreshTokenStore = field(default_factory=FakeRefreshTokenStore)
    roles: FakeRoleStore = field(default_factory=FakeRoleStore)
    audit: FakeAuditSink = field(default_factory=FakeAuditSink)
    credentials: FakeCredentialStore = field(default_factory=FakeCredentialStore)
    authorisation: FakeAuthorisationDirectory = field(init=False)
    commits: int = 0
    rollbacks: int = 0
    entered: int = 0
    enter_error: Exception | None = None

    def __post_init__(self) -> None:
        """Bind authority queries to this unit of work's stores."""
        self.authorisation = FakeAuthorisationDirectory(self.principals, self.roles)

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        self.entered += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless committed, matching the real semantics."""
        if self.commits == 0:
            self.rollbacks += 1

    async def commit(self) -> None:
        """Record that the transaction committed."""
        self.commits += 1

    async def rollback(self) -> None:
        """Record an explicit rollback."""
        self.rollbacks += 1
