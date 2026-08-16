"""Persist and resolve licensed historical-universe and action evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date, datetime

    from dhruva.contexts.reference.domain.corporate_actions import CorporateActionEvidence
    from dhruva.contexts.reference.domain.historical_universe import (
        HistoricalUniverseDefinition,
        HistoricalUniverseMembershipRevision,
        ResolvedHistoricalUniverse,
    )
    from dhruva.contexts.reference.domain.ports import HistoricalReferenceUnitOfWork
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "GetCorporateActions",
    "GetHistoricalUniverse",
    "RegisterCorporateActions",
    "RegisterCorporateActionsResult",
    "RegisterHistoricalUniverse",
    "RegisterHistoricalUniverseCommand",
    "RegisterHistoricalUniverseResult",
]


@dataclass(frozen=True, slots=True)
class RegisterHistoricalUniverseCommand:
    """One atomic definition revision and all membership facts it supplies."""

    definition: HistoricalUniverseDefinition
    memberships: tuple[HistoricalUniverseMembershipRevision, ...]


@dataclass(frozen=True, slots=True)
class RegisterHistoricalUniverseResult:
    """Idempotent append counts."""

    definition_added: bool
    memberships_added: int
    memberships_unchanged: int


class RegisterHistoricalUniverse:
    """Append source evidence without rewriting an earlier source fact."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(self, factory: Callable[[AccountId], HistoricalReferenceUnitOfWork]) -> None:
        self._unit_of_work_factory = factory

    async def execute(
        self, command: RegisterHistoricalUniverseCommand
    ) -> RegisterHistoricalUniverseResult:
        """Validate provenance coherence and commit once."""
        definition = command.definition
        for member in command.memberships:
            if (
                member.account_id != definition.account_id
                or member.universe_id != definition.universe_id
                or member.source != definition.source
                or member.source_revision != definition.source_revision
            ):
                raise ValidationError("historical membership provenance differs from definition")
        logical = tuple(
            (item.instrument_id, item.effective_from, item.source_member_key)
            for item in command.memberships
        )
        if len(logical) != len(set(logical)):
            raise ValidationError("historical universe membership is duplicated")
        async with self._unit_of_work_factory(definition.account_id) as unit_of_work:
            definition_added = await unit_of_work.reference.add_historical_universe_definition(
                definition
            )
            added = 0
            for member in command.memberships:
                added += await unit_of_work.reference.add_historical_universe_membership(member)
            await unit_of_work.commit()
        return RegisterHistoricalUniverseResult(
            definition_added=definition_added,
            memberships_added=added,
            memberships_unchanged=len(command.memberships) - added,
        )


class GetHistoricalUniverse:
    """Resolve a historical universe at effective and knowledge cutoffs."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(self, factory: Callable[[AccountId], HistoricalReferenceUnitOfWork]) -> None:
        self._unit_of_work_factory = factory

    async def execute(
        self,
        *,
        account_id: AccountId,
        universe_id: str,
        effective_on: date,
        known_at: datetime,
    ) -> ResolvedHistoricalUniverse:
        """Return only revisions observable by ``known_at``."""
        async with self._unit_of_work_factory(account_id) as unit_of_work:
            return await unit_of_work.reference.get_historical_universe(
                account_id,
                universe_id=universe_id,
                effective_on=effective_on,
                known_at=known_at,
            )


@dataclass(frozen=True, slots=True)
class RegisterCorporateActionsResult:
    """Idempotent action append counts."""

    added: int
    unchanged: int


class RegisterCorporateActions:
    """Append provider action evidence under an explicit account transaction."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(self, factory: Callable[[AccountId], HistoricalReferenceUnitOfWork]) -> None:
        self._unit_of_work_factory = factory

    async def execute(
        self, *, account_id: AccountId, actions: tuple[CorporateActionEvidence, ...]
    ) -> RegisterCorporateActionsResult:
        """Append unique source revisions atomically."""
        async with self._unit_of_work_factory(account_id) as unit_of_work:
            added = 0
            for action in actions:
                added += await unit_of_work.reference.add_corporate_action(action)
            await unit_of_work.commit()
        return RegisterCorporateActionsResult(added=added, unchanged=len(actions) - added)


class GetCorporateActions:
    """Read corporate actions without future-publication leakage."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(self, factory: Callable[[AccountId], HistoricalReferenceUnitOfWork]) -> None:
        self._unit_of_work_factory = factory

    async def execute(
        self,
        *,
        account_id: AccountId,
        instrument_id: InstrumentId,
        effective_from: date,
        effective_to: date,
        known_at: datetime,
    ) -> tuple[CorporateActionEvidence, ...]:
        """Return effective action observations known by the supplied instant."""
        async with self._unit_of_work_factory(account_id) as unit_of_work:
            return await unit_of_work.reference.list_corporate_actions(
                instrument_id,
                effective_from=effective_from,
                effective_to=effective_to,
                known_at=known_at,
            )
