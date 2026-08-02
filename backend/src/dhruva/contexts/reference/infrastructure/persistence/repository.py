"""PostgreSQL reference repository with idempotent point-in-time revisions."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert

from dhruva.contexts.reference.domain.watchlist import WatchlistInstrument
from dhruva.contexts.reference.infrastructure.persistence.factories import (
    IdentityRevisionFactory,
    MembershipRevisionFactory,
)
from dhruva.contexts.reference.infrastructure.persistence.mappers import (
    identity_model_kwargs,
    identity_record,
    membership_model_kwargs,
    membership_record,
)
from dhruva.contexts.reference.infrastructure.persistence.models import (
    InstrumentIdentityRevisionModel,
    ReferenceInstrumentModel,
    WatchlistMembershipRevisionModel,
)
from dhruva.contexts.reference.infrastructure.persistence.records import (
    IdentityRevisionRecord,
    MembershipRevisionRecord,
)
from dhruva.shared.errors import ConflictError, DataQualityError, MissingDataError, ValidationError

if TYPE_CHECKING:
    from datetime import date, datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.reference.domain.watchlist import (
        InstrumentIdentityRevision,
        WatchlistMembershipRevision,
    )
    from dhruva.shared.identity import AccountId

__all__ = ["ReferenceRepository"]


class ReferenceRepository:
    """Store and resolve effective reference revisions without committing."""

    __slots__ = ("_identity_factory", "_membership_factory", "_session")

    def __init__(
        self,
        session: AsyncSession,
        *,
        identity_factory: IdentityRevisionFactory | None = None,
        membership_factory: MembershipRevisionFactory | None = None,
    ) -> None:
        """Bind factories to one transaction-scoped session."""
        self._session = session
        self._identity_factory = identity_factory or IdentityRevisionFactory()
        self._membership_factory = membership_factory or MembershipRevisionFactory()

    async def add_identity(self, revision: InstrumentIdentityRevision) -> bool:
        """Stage an identity revision, making an identical provider retry a no-op."""
        record = self._identity_factory.deconstruct(revision)
        await self._session.execute(
            insert(ReferenceInstrumentModel)
            .values(id=record.instrument_id, created_at=record.recorded_at)
            .on_conflict_do_nothing(index_elements=[ReferenceInstrumentModel.id])
        )
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                insert(InstrumentIdentityRevisionModel)
                .values(**identity_model_kwargs(record))
                .on_conflict_do_nothing(index_elements=[InstrumentIdentityRevisionModel.id])
            ),
        )
        if result.rowcount == 1:
            return True
        existing = await self._session.get(InstrumentIdentityRevisionModel, record.id)
        if existing is None or not _same_identity(identity_record(existing), record):
            raise ConflictError(
                "reference source revision was reused with different identity content",
                instrument_id=str(revision.instrument_id),
                source=revision.source,
                source_revision=revision.source_revision,
            )
        return False

    async def add_membership(self, revision: WatchlistMembershipRevision) -> bool:
        """Stage a membership revision, making an identical retry a no-op."""
        record = self._membership_factory.deconstruct(revision)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                insert(WatchlistMembershipRevisionModel)
                .values(**membership_model_kwargs(record))
                .on_conflict_do_nothing(index_elements=[WatchlistMembershipRevisionModel.id])
            ),
        )
        if result.rowcount == 1:
            return True
        existing = await self._session.get(WatchlistMembershipRevisionModel, record.id)
        if existing is None or not _same_membership(membership_record(existing), record):
            raise ConflictError(
                "reference source revision was reused with different membership content",
                instrument_id=str(revision.instrument_id),
                source=revision.source,
                source_revision=revision.source_revision,
            )
        return False

    async def list_watchlist(
        self,
        account_id: AccountId,
        *,
        effective_on: date,
        known_at: datetime,
    ) -> tuple[WatchlistInstrument, ...]:
        """Resolve bitemporal membership and identity without future knowledge."""
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ValidationError("known_at must be timezone-aware")
        membership_models = (
            (
                await self._session.execute(
                    select(WatchlistMembershipRevisionModel).where(
                        WatchlistMembershipRevisionModel.account_id == account_id.value,
                        WatchlistMembershipRevisionModel.recorded_at <= known_at,
                    )
                )
            )
            .scalars()
            .all()
        )
        latest_memberships = _latest_membership_periods(
            tuple(membership_record(model) for model in membership_models)
        )
        active_memberships = tuple(
            record
            for record in latest_memberships
            if record.active_from <= effective_on
            and (record.active_to is None or effective_on <= record.active_to)
        )
        membership_by_instrument = _one_membership_per_instrument(active_memberships)
        if not membership_by_instrument:
            return ()

        instrument_ids = tuple(membership_by_instrument)
        identity_models = (
            (
                await self._session.execute(
                    select(InstrumentIdentityRevisionModel).where(
                        InstrumentIdentityRevisionModel.instrument_id.in_(instrument_ids),
                        InstrumentIdentityRevisionModel.recorded_at <= known_at,
                    )
                )
            )
            .scalars()
            .all()
        )
        latest_identities = _latest_identity_periods(
            tuple(identity_record(model) for model in identity_models)
        )
        effective_identities = tuple(
            record
            for record in latest_identities
            if record.valid_from <= effective_on
            and (record.valid_to is None or effective_on <= record.valid_to)
        )
        identity_by_instrument = _one_identity_per_instrument(effective_identities)

        missing = set(instrument_ids) - set(identity_by_instrument)
        if missing:
            raise MissingDataError(
                "active watchlist membership has no effective instrument identity",
                missing_count=len(missing),
            )

        items = tuple(
            WatchlistInstrument(
                identity=self._identity_factory.reconstruct(identity_by_instrument[instrument_id]),
                membership=self._membership_factory.reconstruct(
                    membership_by_instrument[instrument_id]
                ),
            )
            for instrument_id in instrument_ids
        )
        return tuple(sorted(items, key=lambda item: item.identity.canonical_symbol))


def _same_identity(existing: IdentityRevisionRecord, candidate: IdentityRevisionRecord) -> bool:
    """Ignore only first-observed time when evaluating an idempotent retry."""
    return replace(existing, recorded_at=candidate.recorded_at) == candidate


def _same_membership(
    existing: MembershipRevisionRecord,
    candidate: MembershipRevisionRecord,
) -> bool:
    """Ignore only first-observed time when evaluating an idempotent retry."""
    return replace(existing, recorded_at=candidate.recorded_at) == candidate


def _latest_membership_periods(
    records: tuple[MembershipRevisionRecord, ...],
) -> tuple[MembershipRevisionRecord, ...]:
    """Select the latest known correction for each membership effective period."""
    latest: dict[tuple[UUID, date], MembershipRevisionRecord] = {}
    for record in records:
        key = (record.instrument_id, record.active_from)
        if key not in latest or (record.recorded_at, record.id.int) > (
            latest[key].recorded_at,
            latest[key].id.int,
        ):
            latest[key] = record
    return tuple(latest.values())


def _latest_identity_periods(
    records: tuple[IdentityRevisionRecord, ...],
) -> tuple[IdentityRevisionRecord, ...]:
    """Select the latest known correction for each identity effective period."""
    latest: dict[tuple[UUID, date], IdentityRevisionRecord] = {}
    for record in records:
        key = (record.instrument_id, record.valid_from)
        if key not in latest or (record.recorded_at, record.id.int) > (
            latest[key].recorded_at,
            latest[key].id.int,
        ):
            latest[key] = record
    return tuple(latest.values())


def _one_membership_per_instrument(
    records: tuple[MembershipRevisionRecord, ...],
) -> dict[UUID, MembershipRevisionRecord]:
    """Reject overlapping active membership periods rather than guessing."""
    result: dict[UUID, MembershipRevisionRecord] = {}
    for record in records:
        if record.instrument_id in result:
            raise DataQualityError(
                "watchlist membership periods overlap",
                instrument_id=str(record.instrument_id),
            )
        result[record.instrument_id] = record
    return result


def _one_identity_per_instrument(
    records: tuple[IdentityRevisionRecord, ...],
) -> dict[UUID, IdentityRevisionRecord]:
    """Reject overlapping effective identities rather than guessing."""
    result: dict[UUID, IdentityRevisionRecord] = {}
    for record in records:
        if record.instrument_id in result:
            raise DataQualityError(
                "instrument identity periods overlap",
                instrument_id=str(record.instrument_id),
            )
        result[record.instrument_id] = record
    return result
