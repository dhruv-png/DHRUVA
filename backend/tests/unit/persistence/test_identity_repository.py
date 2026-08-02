"""Principal and refresh-token repository orchestration without a database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID

import pytest

from dhruva.contexts.platform.domain.identity import PasswordHash, Principal, RefreshToken
from dhruva.contexts.platform.infrastructure.persistence.factories import (
    PrincipalFactory,
    RefreshTokenFactory,
)
from dhruva.contexts.platform.infrastructure.persistence.identity import (
    PrincipalRepository,
    RefreshTokenRepository,
)
from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_principal_model_kwargs,
    to_refresh_token_model_kwargs,
)
from dhruva.contexts.platform.infrastructure.persistence.models import (
    PrincipalModel,
    RefreshTokenModel,
)
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId, PrincipalId, RefreshTokenId

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("identity-repository-unit")
PRINCIPAL_ID: Final = PrincipalId.deterministic("identity-repository-unit")
TOKEN_ID: Final = RefreshTokenId.deterministic("identity-repository-unit")
CREATED: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
ENCODED: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
PRINCIPALS: Final = PrincipalFactory()
TOKENS: Final = RefreshTokenFactory()


class _Result:
    """The scalar and row-count result surface these repositories consume."""

    def __init__(self, *, one: object | None = None, rowcount: int = 1) -> None:
        self.one = one
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> object | None:
        """Return the configured scalar."""
        return self.one


class _Session:
    """Queue statement results and retain staged rows."""

    def __init__(self, *results: _Result, got: object | None = None) -> None:
        self.results = list(results)
        self.got = got
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement: object) -> _Result:
        """Return the next configured result."""
        self.statements.append(statement)
        return self.results.pop(0)

    async def get(self, model: type[object], key: object) -> object | None:
        """Return the configured primary-key lookup."""
        del model, key
        return self.got

    def add(self, instance: object) -> None:
        """Retain a staged row."""
        self.added.append(instance)


def _principal(*, version: int = 1) -> Principal:
    return Principal(
        principal_id=PRINCIPAL_ID,
        account_id=ACCOUNT,
        subject="operator@dhruva.local",
        password_hash=PasswordHash(ENCODED),
        created_at=CREATED,
        updated_at=CREATED,
        version=version,
    )


def _principal_model(principal: Principal) -> PrincipalModel:
    return PrincipalModel(**to_principal_model_kwargs(PRINCIPALS.deconstruct(principal)))


def _token() -> RefreshToken:
    return RefreshToken(
        token_id=TOKEN_ID,
        account_id=ACCOUNT,
        principal_id=PRINCIPAL_ID,
        lineage_id=TOKEN_ID.value,
        token_hash=b"identity-repository-digest",
        issued_at=CREATED,
        expires_at=CREATED + timedelta(days=30),
    )


def _token_model(token: RefreshToken) -> RefreshTokenModel:
    return RefreshTokenModel(**to_refresh_token_model_kwargs(TOKENS.deconstruct(token)))


def _principals(session: _Session) -> PrincipalRepository:
    return PrincipalRepository(cast("Any", session), PRINCIPALS)


def _tokens(session: _Session) -> RefreshTokenRepository:
    return RefreshTokenRepository(cast("Any", session), TOKENS)


@pytest.mark.asyncio
async def test_principal_lookups_distinguish_missing_rows_from_reconstruction() -> None:
    """Authentication and refresh paths both fail closed on absence."""
    principal = _principal()
    model = _principal_model(principal)

    assert await _principals(_Session(_Result(one=None))).get_by_subject(principal.subject) is None
    assert (
        await _principals(_Session(_Result(one=model))).get_by_subject(principal.subject)
        == principal
    )
    assert await _principals(_Session(got=None)).get(principal.principal_id) is None
    assert await _principals(_Session(got=model)).get(principal.principal_id) == principal


@pytest.mark.asyncio
async def test_principal_add_stages_a_complete_row_without_committing() -> None:
    """Transaction ownership remains with the Unit of Work."""
    session = _Session()
    await _principals(session).add(_principal())

    assert len(session.added) == 1
    assert isinstance(session.added[0], PrincipalModel)


@pytest.mark.asyncio
async def test_principal_update_reports_compare_and_swap_loss() -> None:
    """A stale password or TOTP update is never silently accepted."""
    changed = _principal(version=2)
    await _principals(_Session(_Result(rowcount=1))).update(changed)

    with pytest.raises(ConflictError, match="modified by another writer"):
        await _principals(_Session(_Result(rowcount=0))).update(changed)


@pytest.mark.asyncio
async def test_refresh_lookup_and_add_preserve_missing_and_present_states() -> None:
    """Unknown hashes deny while issued tokens reconstruct exactly."""
    token = _token()
    model = _token_model(token)

    assert await _tokens(_Session(_Result(one=None))).get_by_hash(token.token_hash) is None
    assert await _tokens(_Session(_Result(one=model))).get_by_hash(token.token_hash) == token

    session = _Session()
    await _tokens(session).add(token)
    assert len(session.added) == 1
    assert isinstance(session.added[0], RefreshTokenModel)


@pytest.mark.asyncio
@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
async def test_mark_replaced_reports_exactly_whether_rotation_won(
    rowcount: int, *, expected: bool
) -> None:
    """The conditional update result is the concurrency verdict."""
    original = _token()
    replaced = RefreshToken(
        token_id=original.token_id,
        account_id=original.account_id,
        principal_id=original.principal_id,
        lineage_id=original.lineage_id,
        token_hash=original.token_hash,
        issued_at=original.issued_at,
        expires_at=original.expires_at,
        replaced_by=RefreshTokenId.new(),
    )

    assert await _tokens(_Session(_Result(rowcount=rowcount))).mark_replaced(replaced) is expected


@pytest.mark.asyncio
async def test_revoke_lineage_returns_the_database_affected_row_count() -> None:
    """Repeated revocation can distinguish a real change from an idempotent no-op."""
    repository = _tokens(_Session(_Result(rowcount=3), _Result(rowcount=0)))
    lineage = UUID("55555555-5555-5555-5555-555555555555")

    assert await repository.revoke_lineage(lineage, at=CREATED) == 3
    assert await repository.revoke_lineage(lineage, at=CREATED) == 0
