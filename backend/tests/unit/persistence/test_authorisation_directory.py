"""Transaction lock and authority-query orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self, cast

import pytest

from dhruva.contexts.platform.infrastructure.persistence.authorisation import (
    PostgresAuthorisationDirectory,
)
from dhruva.contexts.platform.infrastructure.persistence.factories import PrincipalFactory
from dhruva.contexts.platform.infrastructure.persistence.models import PrincipalModel
from dhruva.shared.identity import AccountId

pytestmark = pytest.mark.unit

ACCOUNT = AccountId.deterministic("authorisation-directory")
NOW = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)


class _Result:
    def __init__(self, *, rows: tuple[object, ...] = (), scalar: int = 0) -> None:
        self.rows = rows
        self.scalar = scalar

    def scalars(self) -> Self:
        """Expose the queued model rows."""
        return self

    def all(self) -> list[object]:
        """Return all queued model rows."""
        return list(self.rows)

    def scalar_one(self) -> int:
        """Return the queued count."""
        return self.scalar


class _Session:
    def __init__(self, *results: _Result) -> None:
        self.results = list(results)
        self.calls: list[tuple[object, object | None]] = []

    async def execute(self, statement: object, parameters: object | None = None) -> _Result:
        """Retain statements and return queued results."""
        self.calls.append((statement, parameters))
        return self.results.pop(0) if self.results else _Result()


def _directory(session: _Session) -> PostgresAuthorisationDirectory:
    return PostgresAuthorisationDirectory(cast("Any", session), PrincipalFactory())


@pytest.mark.asyncio
async def test_tenant_lock_key_is_stable_and_account_specific() -> None:
    """The same tenant maps to one signed key; another tenant maps elsewhere."""
    session = _Session()
    directory = _directory(session)

    await directory.serialise(ACCOUNT)
    await directory.serialise(ACCOUNT)
    await directory.serialise(AccountId.deterministic("different-tenant"))

    parameters = [cast("dict[str, int]", call[1])["lock_key"] for call in session.calls]
    assert parameters[0] == parameters[1]
    assert parameters[0] != parameters[2]
    assert all("pg_advisory_xact_lock" in str(statement) for statement, _ in session.calls)


@pytest.mark.asyncio
async def test_holders_are_reconstructed_as_domain_principals() -> None:
    """The directory returns domain values, including enrolled and disabled state."""
    model = PrincipalModel(
        id=ACCOUNT.value,
        account_id=ACCOUNT.value,
        subject="holder@dhruva.local",
        password_hash="hashed-password",  # noqa: S106 - stored digest-shaped test value
        totp_secret=b"sealed",
        totp_wrapped_key=b"wrapped",
        disabled_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )
    directory = _directory(_Session(_Result(rows=(model,))))

    holders = await directory.holders(ACCOUNT, "operator")

    assert len(holders) == 1
    assert holders[0].subject == "holder@dhruva.local"
    assert holders[0].is_two_factor_enrolled
    assert not holders[0].is_active


@pytest.mark.asyncio
async def test_manager_count_is_returned_without_application_sql() -> None:
    """The inward-facing port reduces eligibility to a domain-sized count."""
    directory = _directory(_Session(_Result(scalar=3)))

    assert (
        await directory.count_active_managers(ACCOUNT, excluding_role_name="current-manager") == 3
    )
