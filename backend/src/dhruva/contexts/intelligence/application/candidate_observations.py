"""Freeze an already-resolved candidate ranking without another data read."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.candidate_observation import (
    CandidateObservation,
    CandidateObservationAppendResult,
    candidate_observation_fingerprint,
    candidate_universe_fingerprint,
)
from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.candidates import CandidateRanking
    from dhruva.contexts.intelligence.domain.ports import CandidateObservationUnitOfWork
    from dhruva.shared.identity import AccountId

__all__ = ["FreezeCandidateObservation", "FreezeCandidateObservationCommand"]


@dataclass(frozen=True, slots=True)
class FreezeCandidateObservationCommand:
    """One resolved ranking and the explicit instant at which it is recorded."""

    account_id: AccountId
    ranking: CandidateRanking
    recorded_at: datetime


class FreezeCandidateObservation:
    """Build and append one official candidate freeze idempotently."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], CandidateObservationUnitOfWork],
    ) -> None:
        """Bind an account-scoped transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self, command: FreezeCandidateObservationCommand
    ) -> CandidateObservationAppendResult:
        """Freeze supplied facts without recomputation or provider access."""
        if command.recorded_at < command.ranking.cutoff:
            raise ValidationError("candidate freeze cannot be recorded before its cutoff")
        universe_sha256 = candidate_universe_fingerprint(command.ranking)
        observation = CandidateObservation(
            account_id=command.account_id,
            ranking=command.ranking,
            recorded_at=command.recorded_at,
            universe_sha256=universe_sha256,
            observation_sha256=candidate_observation_fingerprint(
                account_id=command.account_id,
                ranking=command.ranking,
                universe_sha256=universe_sha256,
            ),
        )
        async with self._unit_of_work_factory(command.account_id) as unit_of_work:
            result = await unit_of_work.candidate_observations.append(observation)
            await unit_of_work.commit()
        return result
