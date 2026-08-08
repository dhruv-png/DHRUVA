"""Append-only daily instrument-master archive backed by PostgreSQL."""

from __future__ import annotations

import gzip
import zlib
from collections import defaultdict
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid5

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert

from dhruva.contexts.reference.domain.instrument_master import (
    ArchivedInstrumentDiscovery,
    ArchivedInstrumentMaster,
    FuturesAvailability,
    FuturesAvailabilityStatus,
    FuturesContract,
    FuturesContractObservation,
    FuturesContractStatus,
    InstrumentArchiveWrite,
    InstrumentResolution,
    ResolvedCashInstrument,
)
from dhruva.contexts.reference.infrastructure.persistence.models import (
    CashInstrumentMappingRevisionModel,
    FuturesContractModel,
    FuturesContractRevisionModel,
    InstrumentMasterSnapshotModel,
    InstrumentResolutionRevisionModel,
)
from dhruva.shared.errors import ConflictError, DataQualityError, MissingDataError
from dhruva.shared.identity import InstrumentId

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.reference.domain.instrument_master import InstrumentDiscovery

__all__ = ["InstrumentArchiveRepository"]

_ARCHIVE_NAMESPACE = UUID("bc7e1172-3176-4451-a53e-d4a9ab1eb56f")
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def _archive_id(kind: str, *parts: object) -> UUID:
    """Derive a stable key from provider evidence and resolver identity."""
    return uuid5(_ARCHIVE_NAMESPACE, "\x1f".join((kind, *(str(part) for part in parts))))


def _inserted(result: object) -> bool:
    """Read the PostgreSQL insert outcome without widening repository types."""
    return cast("CursorResult[Any]", result).rowcount == 1


class InstrumentArchiveRepository:
    """Persist and reconstruct versioned daily provider discoveries."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        """Bind one transaction-scoped SQLAlchemy session."""
        self._session = session

    async def archive(self, discovery: InstrumentDiscovery) -> InstrumentArchiveWrite:
        """Stage raw evidence, resolution rows and actual-contract revisions."""
        snapshot = discovery.snapshot
        snapshot_id = _archive_id("snapshot", snapshot.provider, snapshot.market_date)
        compressed = gzip.compress(snapshot.raw_csv, mtime=0)
        snapshot_added = int(
            _inserted(
                await self._session.execute(
                    insert(InstrumentMasterSnapshotModel)
                    .values(
                        id=snapshot_id,
                        provider=snapshot.provider,
                        market_date=snapshot.market_date,
                        fetched_at=snapshot.fetched_at,
                        content_sha256=snapshot.content_sha256,
                        compression="gzip",
                        compressed_csv=compressed,
                        row_count=len(snapshot.entries),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            InstrumentMasterSnapshotModel.provider,
                            InstrumentMasterSnapshotModel.market_date,
                        ]
                    )
                )
            )
        )
        if not snapshot_added:
            await self._verify_snapshot(snapshot_id, discovery)

        resolutions_added = 0
        cash_mappings_added = 0
        contracts_added = 0
        contract_revisions_added = 0
        for resolution in discovery.resolutions:
            resolutions_added += int(await self._add_resolution(snapshot_id, discovery, resolution))
            if resolution.cash is not None:
                cash_mappings_added += int(
                    await self._add_cash_mapping(snapshot_id, discovery, resolution)
                )
            for observation in resolution.futures_observations:
                stable_added, revision_added = await self._add_contract(
                    snapshot_id,
                    discovery,
                    observation,
                )
                contracts_added += int(stable_added)
                contract_revisions_added += int(revision_added)

        return InstrumentArchiveWrite(
            snapshot_added=snapshot_added,
            resolutions_added=resolutions_added,
            cash_mappings_added=cash_mappings_added,
            contracts_added=contracts_added,
            contract_revisions_added=contract_revisions_added,
        )

    async def get(
        self,
        *,
        provider: str,
        market_date: date,
        resolver_revision: str,
    ) -> ArchivedInstrumentDiscovery:
        """Reconstruct a validated archive result from normalized mapped facts."""
        snapshot_model = await self._session.scalar(
            select(InstrumentMasterSnapshotModel).where(
                InstrumentMasterSnapshotModel.provider == provider,
                InstrumentMasterSnapshotModel.market_date == market_date,
            )
        )
        if snapshot_model is None:
            raise MissingDataError(
                "instrument master archive was not found",
                provider=provider,
                market_date=market_date.isoformat(),
            )
        raw_csv = _decompress(snapshot_model.compressed_csv)
        archived_snapshot = ArchivedInstrumentMaster(
            provider=snapshot_model.provider,
            market_date=snapshot_model.market_date,
            fetched_at=snapshot_model.fetched_at,
            content_sha256=snapshot_model.content_sha256,
            raw_csv=raw_csv,
            row_count=snapshot_model.row_count,
        )

        resolution_models = (
            (
                await self._session.execute(
                    select(InstrumentResolutionRevisionModel)
                    .where(
                        InstrumentResolutionRevisionModel.snapshot_id == snapshot_model.id,
                        InstrumentResolutionRevisionModel.resolver_revision == resolver_revision,
                    )
                    .order_by(InstrumentResolutionRevisionModel.canonical_symbol)
                )
            )
            .scalars()
            .all()
        )
        if not resolution_models:
            raise MissingDataError(
                "instrument resolution archive was not found",
                provider=provider,
                market_date=market_date.isoformat(),
                resolver_revision=resolver_revision,
            )
        cash_models = (
            (
                await self._session.execute(
                    select(CashInstrumentMappingRevisionModel).where(
                        CashInstrumentMappingRevisionModel.snapshot_id == snapshot_model.id,
                        CashInstrumentMappingRevisionModel.resolver_revision == resolver_revision,
                    )
                )
            )
            .scalars()
            .all()
        )
        cash_by_instrument = {model.instrument_id: model for model in cash_models}

        futures_rows = (
            await self._session.execute(
                select(FuturesContractRevisionModel, FuturesContractModel)
                .join(
                    FuturesContractModel,
                    FuturesContractModel.id == FuturesContractRevisionModel.contract_id,
                )
                .where(
                    FuturesContractRevisionModel.snapshot_id == snapshot_model.id,
                    FuturesContractRevisionModel.resolver_revision == resolver_revision,
                )
            )
        ).all()
        observations_by_underlying: dict[UUID, list[FuturesContractObservation]] = defaultdict(list)
        for revision, stable in futures_rows:
            observations_by_underlying[stable.underlying_id].append(
                FuturesContractObservation(
                    contract=FuturesContract(
                        contract_id=InstrumentId(stable.id),
                        underlying_id=InstrumentId(stable.underlying_id),
                        provider=revision.provider,
                        instrument_token=revision.instrument_token,
                        exchange_token=revision.exchange_token,
                        trading_symbol=revision.trading_symbol,
                        expiry=stable.expiry,
                        lot_size=revision.lot_size,
                        tick_size=revision.tick_size,
                        instrument_type=revision.instrument_type,
                        segment=revision.segment,
                        exchange=revision.exchange,
                    ),
                    status=FuturesContractStatus(revision.contract_status),
                    selected_for_availability=revision.selected_for_availability,
                )
            )

        resolutions = tuple(
            self._resolution(
                model,
                cash_by_instrument.get(model.instrument_id),
                observations=tuple(
                    sorted(
                        observations_by_underlying.get(model.instrument_id, []),
                        key=lambda item: item.contract.expiry,
                    )
                ),
            )
            for model in resolution_models
        )
        return ArchivedInstrumentDiscovery(
            snapshot=archived_snapshot,
            resolutions=resolutions,
            resolver_revision=resolver_revision,
        )

    async def get_latest(
        self,
        *,
        provider: str,
        resolver_revision: str,
    ) -> ArchivedInstrumentDiscovery | None:
        """Replay the most recently dated archive, or ``None`` before the first one.

        Used by a network-free coverage read, which must never guess a market
        date to ask :meth:`get` for -- it wants "whatever was last resolved",
        not evidence for a date it would have to already know.
        """
        snapshot_model = await self._session.scalar(
            select(InstrumentMasterSnapshotModel)
            .where(InstrumentMasterSnapshotModel.provider == provider)
            .order_by(InstrumentMasterSnapshotModel.market_date.desc())
            .limit(1)
        )
        if snapshot_model is None:
            return None
        try:
            return await self.get(
                provider=provider,
                market_date=snapshot_model.market_date,
                resolver_revision=resolver_revision,
            )
        except MissingDataError:
            return None

    async def _verify_snapshot(
        self,
        snapshot_id: UUID,
        discovery: InstrumentDiscovery,
    ) -> None:
        existing = await self._session.scalar(
            select(InstrumentMasterSnapshotModel).where(
                InstrumentMasterSnapshotModel.provider == discovery.snapshot.provider,
                InstrumentMasterSnapshotModel.market_date == discovery.snapshot.market_date,
            )
        )
        if (
            existing is None
            or existing.id != snapshot_id
            or existing.content_sha256 != discovery.snapshot.content_sha256
            or existing.row_count != len(discovery.snapshot.entries)
            or existing.compression != "gzip"
            or _decompress(existing.compressed_csv) != discovery.snapshot.raw_csv
        ):
            raise ConflictError(
                "daily instrument master key was reused with different content",
                provider=discovery.snapshot.provider,
                market_date=discovery.snapshot.market_date.isoformat(),
            )

    async def _add_resolution(
        self,
        snapshot_id: UUID,
        discovery: InstrumentDiscovery,
        resolution: InstrumentResolution,
    ) -> bool:
        row_id = _archive_id(
            "resolution",
            snapshot_id,
            resolution.instrument_id.value,
            discovery.resolver_revision,
        )
        values = {
            "id": row_id,
            "snapshot_id": snapshot_id,
            "instrument_id": resolution.instrument_id.value,
            "resolver_revision": discovery.resolver_revision,
            "canonical_symbol": resolution.canonical_symbol,
            "cash_available": resolution.cash is not None,
            "cash_unavailable_reason": resolution.cash_unavailable_reason,
            "futures_status": resolution.futures.status.value,
            "futures_reason": resolution.futures.reason,
            "futures_contract_count": len(resolution.futures.contracts),
        }
        added = _inserted(
            await self._session.execute(
                insert(InstrumentResolutionRevisionModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[InstrumentResolutionRevisionModel.id])
            )
        )
        if not added:
            existing = await self._session.get(InstrumentResolutionRevisionModel, row_id)
            if existing is None or any(
                getattr(existing, key) != value for key, value in values.items()
            ):
                raise ConflictError(
                    "instrument resolution revision was reused with different content",
                    instrument_id=str(resolution.instrument_id),
                    resolver_revision=discovery.resolver_revision,
                )
        return added

    async def _add_cash_mapping(
        self,
        snapshot_id: UUID,
        discovery: InstrumentDiscovery,
        resolution: InstrumentResolution,
    ) -> bool:
        cash = resolution.cash
        if cash is None:  # pragma: no cover - caller narrows this branch
            return False
        row_id = _archive_id(
            "cash",
            snapshot_id,
            resolution.instrument_id.value,
            discovery.resolver_revision,
        )
        values = {
            "id": row_id,
            "snapshot_id": snapshot_id,
            "instrument_id": resolution.instrument_id.value,
            "resolver_revision": discovery.resolver_revision,
            "provider": cash.provider,
            "exchange": cash.exchange,
            "trading_symbol": cash.trading_symbol,
            "instrument_token": cash.instrument_token,
            "exchange_token": cash.exchange_token,
        }
        added = _inserted(
            await self._session.execute(
                insert(CashInstrumentMappingRevisionModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[CashInstrumentMappingRevisionModel.id])
            )
        )
        if not added:
            existing = await self._session.get(CashInstrumentMappingRevisionModel, row_id)
            if existing is None or any(
                getattr(existing, key) != value for key, value in values.items()
            ):
                raise ConflictError(
                    "cash mapping revision was reused with different content",
                    instrument_id=str(resolution.instrument_id),
                    resolver_revision=discovery.resolver_revision,
                )
        return added

    async def _add_contract(
        self,
        snapshot_id: UUID,
        discovery: InstrumentDiscovery,
        observation: FuturesContractObservation,
    ) -> tuple[bool, bool]:
        contract = observation.contract
        stable_values = {
            "id": contract.contract_id.value,
            "underlying_id": contract.underlying_id.value,
            "exchange": contract.exchange,
            "expiry": contract.expiry,
            "created_at": discovery.snapshot.fetched_at,
        }
        stable_added = _inserted(
            await self._session.execute(
                insert(FuturesContractModel)
                .values(**stable_values)
                .on_conflict_do_nothing(index_elements=[FuturesContractModel.id])
            )
        )
        if not stable_added:
            existing = await self._session.get(FuturesContractModel, contract.contract_id.value)
            stable_identity = {
                key: value for key, value in stable_values.items() if key != "created_at"
            }
            if existing is None or any(
                getattr(existing, key) != value for key, value in stable_identity.items()
            ):
                raise ConflictError(
                    "futures contract identity was reused with different content",
                    contract_id=str(contract.contract_id),
                )

        revision_id = _archive_id(
            "future-revision",
            snapshot_id,
            contract.contract_id.value,
            discovery.resolver_revision,
        )
        revision_values = {
            "id": revision_id,
            "snapshot_id": snapshot_id,
            "contract_id": contract.contract_id.value,
            "resolver_revision": discovery.resolver_revision,
            "provider": contract.provider,
            "instrument_token": contract.instrument_token,
            "exchange_token": contract.exchange_token,
            "trading_symbol": contract.trading_symbol,
            "lot_size": contract.lot_size,
            "tick_size": contract.tick_size,
            "instrument_type": contract.instrument_type,
            "segment": contract.segment,
            "exchange": contract.exchange,
            "contract_status": observation.status.value,
            "selected_for_availability": observation.selected_for_availability,
        }
        revision_added = _inserted(
            await self._session.execute(
                insert(FuturesContractRevisionModel)
                .values(**revision_values)
                .on_conflict_do_nothing(index_elements=[FuturesContractRevisionModel.id])
            )
        )
        if not revision_added:
            existing_revision = await self._session.get(
                FuturesContractRevisionModel,
                revision_id,
            )
            if existing_revision is None or any(
                getattr(existing_revision, key) != value for key, value in revision_values.items()
            ):
                raise ConflictError(
                    "futures contract revision was reused with different content",
                    contract_id=str(contract.contract_id),
                    resolver_revision=discovery.resolver_revision,
                )
        return stable_added, revision_added

    @staticmethod
    def _resolution(
        model: InstrumentResolutionRevisionModel,
        cash_model: CashInstrumentMappingRevisionModel | None,
        observations: tuple[FuturesContractObservation, ...],
    ) -> InstrumentResolution:
        if model.cash_available != (cash_model is not None):
            raise DataQualityError(
                "archived cash availability does not match its mapping",
                instrument_id=str(model.instrument_id),
            )
        selected_contracts = tuple(
            observation.contract
            for observation in observations
            if observation.selected_for_availability
        )
        if model.futures_contract_count != len(selected_contracts):
            raise DataQualityError(
                "archived futures availability does not match its contracts",
                instrument_id=str(model.instrument_id),
            )
        cash = (
            ResolvedCashInstrument(
                instrument_id=InstrumentId(cash_model.instrument_id),
                provider=cash_model.provider,
                exchange=cash_model.exchange,
                trading_symbol=cash_model.trading_symbol,
                instrument_token=cash_model.instrument_token,
                exchange_token=cash_model.exchange_token,
            )
            if cash_model is not None
            else None
        )
        return InstrumentResolution(
            instrument_id=InstrumentId(model.instrument_id),
            canonical_symbol=model.canonical_symbol,
            cash=cash,
            cash_unavailable_reason=model.cash_unavailable_reason,
            futures=FuturesAvailability(
                status=FuturesAvailabilityStatus(model.futures_status),
                reason=model.futures_reason,
                contracts=selected_contracts,
            ),
            futures_observations=observations,
        )


def _decompress(payload: bytes) -> bytes:
    """Decompress a gzip archive with the same bound enforced at ingestion."""
    if not payload or len(payload) > _MAX_ARCHIVE_BYTES:
        raise DataQualityError("instrument master archive payload has an invalid size")
    decompressor = zlib.decompressobj(wbits=31)
    try:
        raw = decompressor.decompress(payload, _MAX_ARCHIVE_BYTES + 1)
        if decompressor.unconsumed_tail or len(raw) > _MAX_ARCHIVE_BYTES:
            raise DataQualityError("instrument master archive exceeds the decompression limit")
        raw += decompressor.flush(_MAX_ARCHIVE_BYTES + 1 - len(raw))
    except zlib.error as error:
        raise DataQualityError("instrument master archive is not valid gzip") from error
    if len(raw) > _MAX_ARCHIVE_BYTES or not decompressor.eof or decompressor.unused_data:
        raise DataQualityError("instrument master archive has invalid gzip framing")
    return raw
