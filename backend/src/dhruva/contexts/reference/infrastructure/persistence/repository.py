"""PostgreSQL reference repository with idempotent point-in-time revisions."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid5

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert

from dhruva.contexts.reference.domain.corporate_actions import (
    CorporateActionEvidence,
    CorporateActionType,
    EvidenceVerification,
)
from dhruva.contexts.reference.domain.historical_universe import (
    HistoricalUniverseDefinition,
    HistoricalUniverseMember,
    HistoricalUniverseMembershipRevision,
    MembershipReason,
    ResolvedHistoricalUniverse,
    SourceDiligenceStatus,
    UniverseKind,
    assess_survivorship,
    historical_universe_fingerprint,
)
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
    CorporateActionRevisionModel,
    HistoricalUniverseDefinitionRevisionModel,
    HistoricalUniverseMembershipRevisionModel,
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

    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.reference.domain.watchlist import (
        InstrumentIdentityRevision,
        WatchlistMembershipRevision,
    )
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = ["ReferenceRepository"]

_HISTORICAL_REVISION_NAMESPACE = UUID("b5f6f20d-7bdb-4b42-875d-c062766e7d85")


def _historical_id(kind: str, *parts: str) -> UUID:
    return uuid5(_HISTORICAL_REVISION_NAMESPACE, "\x1f".join((kind, *parts)))


def _definition_id(definition: HistoricalUniverseDefinition) -> UUID:
    return _historical_id(
        "universe-definition",
        str(definition.account_id.value),
        definition.universe_id,
        definition.source,
        definition.source_revision,
    )


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

    async def add_historical_universe_definition(
        self, definition: HistoricalUniverseDefinition
    ) -> bool:
        """Append a coverage/source revision with content-conflict detection."""
        values = _definition_values(definition)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                insert(HistoricalUniverseDefinitionRevisionModel)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[HistoricalUniverseDefinitionRevisionModel.id]
                )
            ),
        )
        if result.rowcount == 1:
            return True
        existing = await self._session.get(HistoricalUniverseDefinitionRevisionModel, values["id"])
        if existing is None or _definition_from_model(existing) != definition:
            raise ConflictError(
                "historical universe source revision was reused with different content",
                universe_id=definition.universe_id,
                source_revision=definition.source_revision,
            )
        return False

    async def add_historical_universe_membership(
        self, revision: HistoricalUniverseMembershipRevision
    ) -> bool:
        """Append one effective membership fact idempotently."""
        definition_stub = HistoricalUniverseDefinition(
            account_id=revision.account_id,
            universe_id=revision.universe_id,
            label="membership definition lookup",
            kind=UniverseKind.HISTORICAL_MARKET_UNIVERSE,
            known_at=revision.known_at,
            source=revision.source,
            source_revision=revision.source_revision,
            source_status=SourceDiligenceStatus.UNREVIEWED,
            historical_membership_available=True,
            removals_included=False,
            delistings_included=False,
            pit_known_at_available=False,
            instrument_lifecycle_available=False,
            licensing_confirmed=False,
        )
        definition_id = _definition_id(definition_stub)
        definition_row = await self._session.get(
            HistoricalUniverseDefinitionRevisionModel, definition_id
        )
        if definition_row is None:
            raise MissingDataError("historical membership has no definition source revision")
        row_id = _historical_id(
            "universe-membership",
            str(revision.account_id.value),
            revision.universe_id,
            str(revision.instrument_id.value),
            revision.effective_from.isoformat(),
            revision.source,
            revision.source_revision,
            revision.source_member_key,
        )
        values = {
            "id": row_id,
            "definition_revision_id": definition_id,
            "account_id": revision.account_id.value,
            "universe_id": revision.universe_id,
            "instrument_id": revision.instrument_id.value,
            "effective_from": revision.effective_from,
            "effective_to": revision.effective_to,
            "known_at": revision.known_at,
            "source": revision.source,
            "source_revision": revision.source_revision,
            "reason": revision.reason.value,
            "source_member_key": revision.source_member_key,
            "is_delisted": revision.is_delisted,
        }
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                insert(HistoricalUniverseMembershipRevisionModel)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[HistoricalUniverseMembershipRevisionModel.id]
                )
            ),
        )
        if result.rowcount == 1:
            return True
        existing = await self._session.get(HistoricalUniverseMembershipRevisionModel, row_id)
        if existing is None or _membership_from_model(existing) != revision:
            raise ConflictError(
                "historical membership source revision was reused with different content",
                universe_id=revision.universe_id,
                source_member_key=revision.source_member_key,
            )
        return False

    async def get_historical_universe(
        self,
        account_id: AccountId,
        *,
        universe_id: str,
        effective_on: date,
        known_at: datetime,
    ) -> ResolvedHistoricalUniverse:
        """Resolve definition, memberships, and identity revisions bitemporally."""
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ValidationError("known_at must be timezone-aware")
        definition_model = (
            (
                await self._session.execute(
                    select(HistoricalUniverseDefinitionRevisionModel)
                    .where(
                        HistoricalUniverseDefinitionRevisionModel.account_id == account_id.value,
                        HistoricalUniverseDefinitionRevisionModel.universe_id == universe_id,
                        HistoricalUniverseDefinitionRevisionModel.known_at <= known_at,
                    )
                    .order_by(
                        HistoricalUniverseDefinitionRevisionModel.known_at.desc(),
                        HistoricalUniverseDefinitionRevisionModel.id.desc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )
        if definition_model is None:
            raise MissingDataError(
                "historical universe is unavailable at the knowledge cutoff",
                universe_id=universe_id,
            )
        definition = _definition_from_model(definition_model)
        membership_models = (
            (
                await self._session.execute(
                    select(HistoricalUniverseMembershipRevisionModel).where(
                        HistoricalUniverseMembershipRevisionModel.definition_revision_id
                        == definition_model.id,
                        HistoricalUniverseMembershipRevisionModel.known_at <= known_at,
                        HistoricalUniverseMembershipRevisionModel.effective_from <= effective_on,
                        (
                            HistoricalUniverseMembershipRevisionModel.effective_to.is_(None)
                            | (
                                HistoricalUniverseMembershipRevisionModel.effective_to
                                >= effective_on
                            )
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        memberships = tuple(_membership_from_model(model) for model in membership_models)
        by_instrument: dict[UUID, HistoricalUniverseMembershipRevision] = {}
        for member in memberships:
            if member.instrument_id.value in by_instrument:
                raise DataQualityError(
                    "historical universe membership periods overlap",
                    instrument_id=str(member.instrument_id),
                )
            by_instrument[member.instrument_id.value] = member
        identities = await self._effective_identities(
            tuple(by_instrument), effective_on=effective_on, known_at=known_at
        )
        missing = set(by_instrument) - set(identities)
        if missing:
            raise MissingDataError(
                "historical universe member has no PIT-effective identity mapping",
                missing_count=len(missing),
            )
        members = tuple(
            sorted(
                (
                    HistoricalUniverseMember(
                        membership=member,
                        canonical_symbol=identities[key].canonical_symbol,
                        company_name=identities[key].company_name,
                        identity_source_revision=identities[key].source_revision,
                    )
                    for key, member in by_instrument.items()
                ),
                key=lambda item: item.canonical_symbol,
            )
        )
        assessment = assess_survivorship(definition)
        fingerprint = historical_universe_fingerprint(definition, effective_on, known_at, members)
        return ResolvedHistoricalUniverse(
            definition=definition,
            effective_on=effective_on,
            known_at=known_at,
            members=members,
            survivorship=assessment,
            fingerprint=fingerprint,
        )

    async def _effective_identities(
        self,
        instrument_ids: tuple[UUID, ...],
        *,
        effective_on: date,
        known_at: datetime,
    ) -> dict[UUID, InstrumentIdentityRevision]:
        if not instrument_ids:
            return {}
        models = (
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
        latest = _latest_identity_periods(tuple(identity_record(model) for model in models))
        active = tuple(
            record
            for record in latest
            if record.valid_from <= effective_on
            and (record.valid_to is None or effective_on <= record.valid_to)
        )
        records = _one_identity_per_instrument(active)
        return {key: self._identity_factory.reconstruct(value) for key, value in records.items()}

    async def add_corporate_action(self, action: CorporateActionEvidence) -> bool:
        """Append one action observation, refusing source-revision reuse."""
        row_id = _historical_id(
            "corporate-action",
            str(action.instrument_id.value),
            action.source,
            action.source_revision,
        )
        values = _corporate_action_values(action, row_id=row_id)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                insert(CorporateActionRevisionModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[CorporateActionRevisionModel.id])
            ),
        )
        if result.rowcount == 1:
            return True
        existing = await self._session.get(CorporateActionRevisionModel, row_id)
        if existing is None or _corporate_action_from_model(existing) != action:
            raise ConflictError("corporate-action source revision was reused")
        return False

    async def list_corporate_actions(
        self,
        instrument_id: InstrumentId,
        *,
        effective_from: date,
        effective_to: date,
        known_at: datetime,
    ) -> tuple[CorporateActionEvidence, ...]:
        """Return actions by effective date without future-publication leakage."""
        if effective_to < effective_from:
            raise ValidationError("corporate-action date range is reversed")
        models = (
            (
                await self._session.execute(
                    select(CorporateActionRevisionModel)
                    .where(
                        CorporateActionRevisionModel.instrument_id == instrument_id.value,
                        CorporateActionRevisionModel.effective_date >= effective_from,
                        CorporateActionRevisionModel.effective_date <= effective_to,
                        CorporateActionRevisionModel.known_at <= known_at,
                    )
                    .order_by(
                        CorporateActionRevisionModel.effective_date,
                        CorporateActionRevisionModel.known_at,
                        CorporateActionRevisionModel.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_corporate_action_from_model(model) for model in models)

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


def _definition_values(definition: HistoricalUniverseDefinition) -> dict[str, Any]:
    return {
        "id": _definition_id(definition),
        "account_id": definition.account_id.value,
        "universe_id": definition.universe_id,
        "label": definition.label,
        "kind": definition.kind.value,
        "known_at": definition.known_at,
        "source": definition.source,
        "source_revision": definition.source_revision,
        "source_status": definition.source_status.value,
        "historical_membership_available": definition.historical_membership_available,
        "removals_included": definition.removals_included,
        "delistings_included": definition.delistings_included,
        "pit_known_at_available": definition.pit_known_at_available,
        "instrument_lifecycle_available": definition.instrument_lifecycle_available,
        "licensing_confirmed": definition.licensing_confirmed,
    }


def _definition_from_model(
    model: HistoricalUniverseDefinitionRevisionModel,
) -> HistoricalUniverseDefinition:
    from dhruva.shared.identity import AccountId  # noqa: PLC0415 - infrastructure mapper

    return HistoricalUniverseDefinition(
        account_id=AccountId(model.account_id),
        universe_id=model.universe_id,
        label=model.label,
        kind=UniverseKind(model.kind),
        known_at=model.known_at,
        source=model.source,
        source_revision=model.source_revision,
        source_status=SourceDiligenceStatus(model.source_status),
        historical_membership_available=model.historical_membership_available,
        removals_included=model.removals_included,
        delistings_included=model.delistings_included,
        pit_known_at_available=model.pit_known_at_available,
        instrument_lifecycle_available=model.instrument_lifecycle_available,
        licensing_confirmed=model.licensing_confirmed,
    )


def _membership_from_model(
    model: HistoricalUniverseMembershipRevisionModel,
) -> HistoricalUniverseMembershipRevision:
    from dhruva.shared.identity import AccountId, InstrumentId  # noqa: PLC0415

    return HistoricalUniverseMembershipRevision(
        account_id=AccountId(model.account_id),
        universe_id=model.universe_id,
        instrument_id=InstrumentId(model.instrument_id),
        effective_from=model.effective_from,
        effective_to=model.effective_to,
        known_at=model.known_at,
        source=model.source,
        source_revision=model.source_revision,
        reason=MembershipReason(model.reason),
        source_member_key=model.source_member_key,
        is_delisted=model.is_delisted,
    )


def _corporate_action_values(action: CorporateActionEvidence, *, row_id: UUID) -> dict[str, Any]:
    return {
        "id": row_id,
        "instrument_id": action.instrument_id.value,
        "event_type": action.event_type.value,
        "effective_date": action.effective_date,
        "ex_date": action.ex_date,
        "record_date": action.record_date,
        "known_at": action.known_at,
        "source": action.source,
        "source_revision": action.source_revision,
        "verification": action.verification.value,
        "ratio_numerator": action.ratio_numerator,
        "ratio_denominator": action.ratio_denominator,
        "cash_value": action.cash_value,
        "currency": action.currency,
        "related_instrument_id": (
            None if action.related_instrument_id is None else action.related_instrument_id.value
        ),
    }


def _corporate_action_from_model(model: CorporateActionRevisionModel) -> CorporateActionEvidence:
    from dhruva.shared.identity import InstrumentId  # noqa: PLC0415

    return CorporateActionEvidence(
        instrument_id=InstrumentId(model.instrument_id),
        event_type=CorporateActionType(model.event_type),
        effective_date=model.effective_date,
        ex_date=model.ex_date,
        record_date=model.record_date,
        known_at=model.known_at,
        source=model.source,
        source_revision=model.source_revision,
        verification=EvidenceVerification(model.verification),
        ratio_numerator=model.ratio_numerator,
        ratio_denominator=model.ratio_denominator,
        cash_value=model.cash_value,
        currency=model.currency,
        related_instrument_id=(
            None
            if model.related_instrument_id is None
            else InstrumentId(model.related_instrument_id)
        ),
    )
