"""Identity Unit of Work repository exposure and delegated transaction semantics."""

from __future__ import annotations

from typing import Any, cast

import pytest

from dhruva.contexts.platform.infrastructure.audit import AuditRecorder
from dhruva.contexts.platform.infrastructure.database.identity_unit_of_work import (
    SqlAlchemyIdentityUnitOfWork,
)
from dhruva.contexts.platform.infrastructure.persistence.authorisation import (
    PostgresAuthorisationDirectory,
    RoleRepository,
)
from dhruva.contexts.platform.infrastructure.persistence.identity import (
    PrincipalRepository,
    RefreshTokenRepository,
)
from dhruva.shared.errors import InvariantViolation
from tests.unit.persistence.fakes import FakeSessionFactory

pytestmark = pytest.mark.unit


def _uow(factory: FakeSessionFactory) -> SqlAlchemyIdentityUnitOfWork:
    return SqlAlchemyIdentityUnitOfWork(cast("Any", factory))


@pytest.mark.asyncio
async def test_identity_stores_are_bound_to_the_one_active_session() -> None:
    """Every store exposed by the wrapper participates in the same transaction."""
    factory = FakeSessionFactory()
    uow = _uow(factory)

    with pytest.raises(InvariantViolation, match="not active"):
        _ = uow.session

    async with uow:
        assert cast("object", uow.session) is factory.latest
        assert isinstance(uow.principals, PrincipalRepository)
        assert isinstance(uow.refresh_tokens, RefreshTokenRepository)
        assert isinstance(uow.roles, RoleRepository)
        assert isinstance(uow.authorisation, PostgresAuthorisationDirectory)
        assert isinstance(uow.audit, AuditRecorder)

    assert factory.latest.calls == ["rollback", "close"]


@pytest.mark.asyncio
async def test_identity_commit_and_explicit_rollback_delegate_to_the_inner_uow() -> None:
    """The wrapper adds repositories without changing transaction ownership."""
    factory = FakeSessionFactory()
    uow = _uow(factory)

    async with uow:
        await uow.rollback()
        await uow.commit()

    assert factory.latest.calls == ["rollback", "commit", "close"]
