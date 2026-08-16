"""Primitive persistence for accepted historical-dataset manifests and provenance."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid5

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert

from dhruva.contexts.reference.infrastructure.persistence.models import (
    HistoricalDatasetFactProvenanceModel,
    HistoricalDatasetFileModel,
    HistoricalDatasetModel,
    HistoricalImportRunModel,
)
from dhruva.shared.errors import ConflictError, MissingDataError

if TYPE_CHECKING:
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DatasetFileRecord",
    "DatasetRevisionRecord",
    "DatasetStatusRecord",
    "FactProvenanceRecord",
    "HistoricalDatasetRepository",
    "ImportRunRecord",
]

_DATASET_NAMESPACE = UUID("6272dd6d-0ecb-447a-9316-b15153e124a3")
_MAX_PROVENANCE_ROWS = 10_000


def _id(kind: str, *parts: str) -> UUID:
    return uuid5(_DATASET_NAMESPACE, "\x1f".join((kind, *parts)))


@dataclass(frozen=True, slots=True)
class DatasetRevisionRecord:
    """Primitive accepted-manifest revision."""

    account_id: UUID
    dataset_id: str
    provider_id: str
    provider_name: str
    provider_product: str
    license_reference: str
    licensing_status: str
    evidence_class: str
    acquired_at: datetime
    imported_at: datetime
    coverage_start: date
    coverage_end: date
    source_revision: str
    manifest_schema: str
    manifest_sha256: str
    dataset_fingerprint: str
    integrity_status: str
    import_policy: str
    currency: str
    exchange: str
    timezone: str
    price_basis: str
    adjustment_basis: str
    return_basis: str
    benchmark_basis: str
    known_at_semantics: str
    capability_claims: dict[str, bool]
    notes: str

    @property
    def id(self) -> UUID:
        """Derive one stable identity per account/provider revision."""
        return _id("dataset", str(self.account_id), self.dataset_id, self.source_revision)


@dataclass(frozen=True, slots=True)
class DatasetFileRecord:
    """Primitive hash-addressed payload metadata."""

    dataset_revision_id: UUID
    account_id: UUID
    role: str
    relative_path: str
    sha256: str
    size_bytes: int
    row_count: int
    schema_revision: str
    media_type: str

    @property
    def id(self) -> UUID:
        """Derive a stable file identity from dataset and role."""
        return _id("file", str(self.dataset_revision_id), self.role)


@dataclass(frozen=True, slots=True)
class FactProvenanceRecord:
    """Exact source-row dependency for one imported fact."""

    dataset_revision_id: UUID
    dataset_file_id: UUID
    account_id: UUID
    fact_role: str
    fact_key: str
    source_row_id: str
    provider_id: str
    provider_revision: str
    source_known_at: datetime
    imported_at: datetime
    file_sha256: str
    schema_revision: str
    mapping_revision: str
    fact_revision: str

    @property
    def id(self) -> UUID:
        """Derive a stable row identity that exposes conflicting re-use."""
        return _id(
            "fact",
            str(self.dataset_revision_id),
            self.fact_role,
            self.source_row_id,
        )


@dataclass(frozen=True, slots=True)
class ImportRunRecord:
    """One explicit apply attempt."""

    id: UUID
    dataset_revision_id: UUID
    account_id: UUID
    started_at: datetime
    finished_at: datetime
    mode: str
    status: str
    preflight_sha256: str
    result_sha256: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class DatasetStatusRecord:
    """Owner-visible persisted dataset status."""

    dataset_id: str
    source_revision: str
    provider_id: str
    licensing_status: str
    evidence_class: str
    integrity_status: str
    dataset_fingerprint: str
    coverage_start: date
    coverage_end: date
    imported_at: datetime


class HistoricalDatasetRepository:
    """Append manifests, files, provenance and import runs without committing."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register_dataset(
        self,
        record: DatasetRevisionRecord,
        files: tuple[DatasetFileRecord, ...],
    ) -> tuple[bool, int]:
        """Append a manifest and all file records, detecting revision conflicts."""
        dataset_values = {"id": record.id, **asdict(record)}
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                insert(HistoricalDatasetModel)
                .values(**dataset_values)
                .on_conflict_do_nothing(index_elements=[HistoricalDatasetModel.id])
            ),
        )
        added = result.rowcount == 1
        if not added:
            existing = await self._session.get(HistoricalDatasetModel, record.id)
            if existing is None or _dataset_values(existing) != dataset_values:
                raise ConflictError(
                    "historical dataset source revision was reused with different content",
                    dataset_id=record.dataset_id,
                    source_revision=record.source_revision,
                )
        file_added = 0
        for file in files:
            values = {"id": file.id, **asdict(file)}
            file_result = cast(
                "CursorResult[Any]",
                await self._session.execute(
                    insert(HistoricalDatasetFileModel)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[HistoricalDatasetFileModel.id])
                ),
            )
            if file_result.rowcount == 1:
                file_added += 1
                continue
            existing_file = await self._session.get(HistoricalDatasetFileModel, file.id)
            if existing_file is None or _file_values(existing_file) != values:
                raise ConflictError(
                    "historical dataset file role was reused with different content"
                )
        return added, file_added

    async def append_provenance(self, records: tuple[FactProvenanceRecord, ...]) -> tuple[int, int]:
        """Batch append source-row links and verify all idempotent collisions."""
        if not records:
            return 0, 0
        values = tuple({"id": item.id, **asdict(item)} for item in records)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                insert(HistoricalDatasetFactProvenanceModel)
                .values(values)
                .on_conflict_do_nothing(index_elements=[HistoricalDatasetFactProvenanceModel.id])
            ),
        )
        inserted = result.rowcount
        if inserted != len(records):
            models = (
                (
                    await self._session.execute(
                        select(HistoricalDatasetFactProvenanceModel).where(
                            HistoricalDatasetFactProvenanceModel.id.in_(
                                tuple(item.id for item in records)
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_id = {model.id: _provenance_values(model) for model in models}
            for candidate in values:
                if by_id.get(cast("UUID", candidate["id"])) != candidate:
                    raise ConflictError(
                        "historical source row was reused with different provenance"
                    )
        return inserted, len(records) - inserted

    async def add_import_run(self, record: ImportRunRecord) -> None:
        """Append one successful apply result idempotently."""
        values = asdict(record)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                insert(HistoricalImportRunModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[HistoricalImportRunModel.id])
            ),
        )
        if result.rowcount == 1:
            return
        existing = await self._session.get(HistoricalImportRunModel, record.id)
        if (
            existing is None
            or {field: getattr(existing, field) for field in ImportRunRecord.__dataclass_fields__}
            != values
        ):
            raise ConflictError("historical import run identity was reused")

    async def get_status(self, *, account_id: UUID, dataset_id: str) -> DatasetStatusRecord:
        """Return the most recently imported revision visible to an account."""
        model = (
            (
                await self._session.execute(
                    select(HistoricalDatasetModel)
                    .where(
                        HistoricalDatasetModel.account_id == account_id,
                        HistoricalDatasetModel.dataset_id == dataset_id,
                    )
                    .order_by(
                        HistoricalDatasetModel.imported_at.desc(),
                        HistoricalDatasetModel.id.desc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )
        if model is None:
            raise MissingDataError("historical dataset is not imported", dataset_id=dataset_id)
        return DatasetStatusRecord(
            dataset_id=model.dataset_id,
            source_revision=model.source_revision,
            provider_id=model.provider_id,
            licensing_status=model.licensing_status,
            evidence_class=model.evidence_class,
            integrity_status=model.integrity_status,
            dataset_fingerprint=model.dataset_fingerprint,
            coverage_start=model.coverage_start,
            coverage_end=model.coverage_end,
            imported_at=model.imported_at,
        )

    async def list_provenance(
        self,
        *,
        account_id: UUID,
        dataset_id: str,
        limit: int = 1000,
    ) -> tuple[FactProvenanceRecord, ...]:
        """Return a bounded deterministic source-row audit view."""
        if limit < 1 or limit > _MAX_PROVENANCE_ROWS:
            raise ValueError("provenance limit must be between 1 and 10000")
        models = (
            (
                await self._session.execute(
                    select(HistoricalDatasetFactProvenanceModel)
                    .join(
                        HistoricalDatasetModel,
                        HistoricalDatasetModel.id
                        == HistoricalDatasetFactProvenanceModel.dataset_revision_id,
                    )
                    .where(
                        HistoricalDatasetFactProvenanceModel.account_id == account_id,
                        HistoricalDatasetModel.dataset_id == dataset_id,
                    )
                    .order_by(
                        HistoricalDatasetFactProvenanceModel.fact_role,
                        HistoricalDatasetFactProvenanceModel.source_row_id,
                        HistoricalDatasetFactProvenanceModel.id,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            FactProvenanceRecord(
                **{
                    field: getattr(model, field)
                    for field in FactProvenanceRecord.__dataclass_fields__
                }
            )
            for model in models
        )


def _dataset_values(model: HistoricalDatasetModel) -> dict[str, object]:
    return {
        "id": model.id,
        **{field: getattr(model, field) for field in DatasetRevisionRecord.__dataclass_fields__},
    }


def _file_values(model: HistoricalDatasetFileModel) -> dict[str, object]:
    return {
        "id": model.id,
        **{field: getattr(model, field) for field in DatasetFileRecord.__dataclass_fields__},
    }


def _provenance_values(model: HistoricalDatasetFactProvenanceModel) -> dict[str, object]:
    return {
        "id": model.id,
        **{field: getattr(model, field) for field in FactProvenanceRecord.__dataclass_fields__},
    }
