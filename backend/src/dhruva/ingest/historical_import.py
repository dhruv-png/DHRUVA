"""Transactional, network-free apply path for a preflighted historical delivery."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid5

from sqlalchemy import text

from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.infrastructure.persistence.repository import (
    DailyBarRepository,
)
from dhruva.contexts.reference.domain.corporate_actions import (
    CorporateActionEvidence,
    CorporateActionType,
    EvidenceVerification,
)
from dhruva.contexts.reference.domain.historical_universe import (
    HistoricalUniverseDefinition,
    HistoricalUniverseMembershipRevision,
    MembershipReason,
    SourceDiligenceStatus,
    UniverseKind,
)
from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
    InstrumentKind,
)
from dhruva.contexts.reference.infrastructure.persistence.historical_dataset import (
    DatasetFileRecord,
    DatasetRevisionRecord,
    FactProvenanceRecord,
    HistoricalDatasetRepository,
    ImportRunRecord,
)
from dhruva.contexts.reference.infrastructure.persistence.repository import (
    ReferenceRepository,
)
from dhruva.ingest.historical_dataset import (
    ImportDecision,
    canonical_json_bytes,
    iter_dataset_rows,
    load_manifest,
    preflight_dataset,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.ingest.historical_dataset import DatasetFile, DatasetManifest

__all__ = ["IMPORT_RESULT_SCHEMA", "HistoricalImportResult", "import_historical_dataset"]

IMPORT_RESULT_SCHEMA: Final = "dhruva.historical-import-result.v1"
_IMPORT_NAMESPACE = UUID("5d45bead-4acd-463e-8be4-8e81afecaa21")
_MAPPING_REVISION = "historical_import_v1"
_PROVENANCE_BATCH_SIZE = 1000
_BAR_BATCH_SIZE = 1000


@dataclass(frozen=True, slots=True)
class HistoricalImportResult:
    """Deterministic result; retries of identical bytes serialize identically."""

    schema: str
    dataset_id: str
    source_revision: str
    dataset_fingerprint: str
    preflight_sha256: str
    counts: tuple[tuple[str, int], ...]
    status: str

    def export_bytes(self) -> bytes:
        """Return canonical JSON suitable for an immutable local report."""
        return canonical_json_bytes(asdict(self)) + b"\n"


def _stable_uuid(kind: str, *parts: str) -> UUID:
    return uuid5(_IMPORT_NAMESPACE, "\x1f".join((kind, *parts)))


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _day(value: str) -> date:
    return date.fromisoformat(value)


def _optional_day(value: str) -> date | None:
    return None if value == "" else _day(value)


def _optional_decimal(value: str) -> Decimal | None:
    return None if value == "" else Decimal(value)


def _flag(value: str) -> bool:
    return value == "true"


def _source_instrument_id(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big") % (2**63 - 1) + 1


def _bar_revision(provider: str, revision: str) -> str:
    return hashlib.sha256(f"{provider}\x1f{revision}".encode()).hexdigest()


def _mapping_revision(provider: str, security: str) -> str:
    return hashlib.sha256(f"{_MAPPING_REVISION}\x1f{provider}\x1f{security}".encode()).hexdigest()


def _file_records(
    account_id: AccountId,
    dataset: DatasetRevisionRecord,
    files: tuple[DatasetFile, ...],
) -> tuple[DatasetFileRecord, ...]:
    return tuple(
        DatasetFileRecord(
            dataset_revision_id=dataset.id,
            account_id=account_id.value,
            role=item.role,
            relative_path=item.path,
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            row_count=item.row_count,
            schema_revision=item.schema,
            media_type=item.media_type,
        )
        for item in files
    )


def _dataset_record(
    account_id: AccountId,
    manifest: DatasetManifest,
    *,
    fingerprint: str,
) -> DatasetRevisionRecord:
    return DatasetRevisionRecord(
        account_id=account_id.value,
        dataset_id=manifest.dataset_id,
        provider_id=manifest.provider_id,
        provider_name=manifest.provider_name,
        provider_product=manifest.provider_product,
        license_reference=manifest.license_reference,
        licensing_status=manifest.licensing_status.value,
        acquired_at=manifest.acquired_at,
        imported_at=manifest.acquired_at,
        coverage_start=manifest.coverage_start,
        coverage_end=manifest.coverage_end,
        source_revision=manifest.source_revision,
        manifest_schema=manifest.schema,
        manifest_sha256=manifest.manifest_sha256,
        dataset_fingerprint=fingerprint,
        integrity_status="READY_FOR_IMPORT",
        import_policy=manifest.import_policy,
        currency=manifest.currency,
        exchange=manifest.exchange,
        timezone=manifest.timezone,
        price_basis=manifest.price_basis,
        adjustment_basis=manifest.adjustment_basis,
        return_basis=manifest.return_basis,
        benchmark_basis=manifest.benchmark_basis,
        known_at_semantics=manifest.known_at_semantics,
        capability_claims=asdict(manifest.capabilities),
        notes=manifest.notes,
    )


class _ProvenanceBuffer:
    __slots__ = ("_file", "_items", "_manifest", "_repository", "_revision")

    def __init__(
        self,
        repository: HistoricalDatasetRepository,
        manifest: DatasetManifest,
        revision: DatasetRevisionRecord,
        file: DatasetFileRecord,
    ) -> None:
        self._repository = repository
        self._manifest = manifest
        self._revision = revision
        self._file = file
        self._items: list[FactProvenanceRecord] = []

    async def add(
        self,
        row: Mapping[str, str],
        *,
        fact_key: str,
        source_security_id: str,
    ) -> tuple[int, int]:
        self._items.append(
            FactProvenanceRecord(
                dataset_revision_id=self._revision.id,
                dataset_file_id=self._file.id,
                account_id=self._revision.account_id,
                fact_role=self._file.role,
                fact_key=fact_key,
                source_row_id=row["source_row_id"],
                provider_id=self._manifest.provider_id,
                provider_revision=self._manifest.source_revision,
                source_known_at=_instant(row["known_at"]),
                imported_at=self._manifest.acquired_at,
                file_sha256=self._file.sha256,
                schema_revision=self._file.schema_revision,
                mapping_revision=_mapping_revision(self._manifest.provider_id, source_security_id),
                fact_revision=row["source_revision"],
            )
        )
        return await self.flush() if len(self._items) >= _PROVENANCE_BATCH_SIZE else (0, 0)

    async def flush(self) -> tuple[int, int]:
        items = tuple(self._items)
        self._items.clear()
        return await self._repository.append_provenance(items)


async def import_historical_dataset(  # noqa: PLR0912, PLR0915
    manifest_path: Path,
    *,
    account_id: AccountId,
    session_factory: async_sessionmaker[AsyncSession],
) -> HistoricalImportResult:
    """Apply one accepted NSE delivery atomically; never performs network I/O."""
    report = preflight_dataset(manifest_path)
    if report.import_decision is not ImportDecision.READY:
        raise ValidationError(
            "historical dataset is not ready for import",
            decision=report.import_decision.value,
            blockers=report.blockers,
        )
    root, manifest = load_manifest(manifest_path)
    if manifest.exchange != "NSE":
        raise ValidationError(
            "authoritative BSE identity import is not supported by the personal MVP"
        )
    preflight_sha = hashlib.sha256(report.export_bytes()).hexdigest()
    revision = _dataset_record(account_id, manifest, fingerprint=report.dataset_fingerprint)
    files = _file_records(account_id, revision, manifest.files)
    file_by_role = {item.role: item for item in files}
    counts: dict[str, int] = {"dataset_added": 0, "dataset_unchanged": 0}
    instruments: dict[str, InstrumentId] = {}

    async with session_factory() as session:
        try:
            await session.execute(
                text("SELECT set_config(:setting, :account_id, true)"),
                {
                    "setting": "dhruva.current_account_id",
                    "account_id": str(account_id.value),
                },
            )
            reference = ReferenceRepository(session)
            bars = DailyBarRepository(session)
            ledger = HistoricalDatasetRepository(session)
            dataset_added, files_added = await ledger.register_dataset(revision, files)
            counts["dataset_added" if dataset_added else "dataset_unchanged"] += 1
            counts["files_added"] = files_added
            counts["files_unchanged"] = len(files) - files_added

            for item in manifest.files:
                buffer = _ProvenanceBuffer(ledger, manifest, revision, file_by_role[item.role])
                added = unchanged = provenance_added = provenance_unchanged = 0
                bar_batch: list[DailyBarRevision] = []
                for _, row in iter_dataset_rows(root, item):
                    security = row.get("source_security_id", row.get("benchmark_id", ""))
                    fact_key = row["source_row_id"]
                    was_added = False
                    if item.role == "instrument_identities":
                        instrument_id = instruments.setdefault(
                            security,
                            InstrumentId.deterministic(
                                "historical-dataset", manifest.provider_id, security
                            ),
                        )
                        provider_token = row["provider_instrument_token"]
                        mapping = CashInstrumentMapping(
                            exchange="NSE",
                            trading_symbol=row["canonical_symbol"],
                            provider=manifest.provider_id if provider_token else None,
                            instrument_token=int(provider_token) if provider_token else None,
                            exchange_token=(int(row["exchange_token"]) if provider_token else None),
                        )
                        was_added = await reference.add_identity(
                            InstrumentIdentityRevision(
                                instrument_id=instrument_id,
                                kind=InstrumentKind(row["instrument_kind"].lower()),
                                canonical_symbol=row["canonical_symbol"],
                                company_name=row["company_name"],
                                aliases=(),
                                former_names=(),
                                isin=row["isin"] or None,
                                sector="UNKNOWN",
                                concentration_groups=(),
                                cash_mapping=mapping,
                                futures_research_requested=False,
                                valid_from=_day(row["valid_from"]),
                                valid_to=_optional_day(row["valid_to"]),
                                recorded_at=_instant(row["known_at"]),
                                source=manifest.provider_id,
                                source_revision=row["source_revision"],
                            )
                        )
                        fact_key = str(instrument_id)
                    elif item.role == "universe_definitions":
                        was_added = await reference.add_historical_universe_definition(
                            HistoricalUniverseDefinition(
                                account_id=account_id,
                                universe_id=row["universe_id"],
                                label=row["label"],
                                kind=UniverseKind(row["kind"]),
                                known_at=_instant(row["known_at"]),
                                source=manifest.provider_id,
                                source_revision=row["source_revision"],
                                source_status=SourceDiligenceStatus.TECHNICALLY_SUITABLE,
                                historical_membership_available=_flag(
                                    row["historical_membership_available"]
                                ),
                                removals_included=_flag(row["removals_included"]),
                                delistings_included=_flag(row["delistings_included"]),
                                pit_known_at_available=_flag(row["pit_known_at_available"]),
                                instrument_lifecycle_available=_flag(
                                    row["instrument_lifecycle_available"]
                                ),
                                licensing_confirmed=_flag(row["licensing_confirmed"]),
                                corporate_action_coverage_available=_flag(
                                    row["corporate_action_coverage_available"]
                                ),
                                return_basis=row["return_basis"],
                                benchmark_basis=row["benchmark_basis"],
                                benchmark_history_available=_flag(
                                    row["benchmark_history_available"]
                                ),
                            )
                        )
                        security = row["universe_id"]
                        fact_key = row["universe_id"]
                    elif item.role == "universe_memberships":
                        instrument_id = instruments[security]
                        was_added = await reference.add_historical_universe_membership(
                            HistoricalUniverseMembershipRevision(
                                account_id=account_id,
                                universe_id=row["universe_id"],
                                instrument_id=instrument_id,
                                effective_from=_day(row["effective_from"]),
                                effective_to=_optional_day(row["effective_to"]),
                                known_at=_instant(row["known_at"]),
                                source=manifest.provider_id,
                                source_revision=row["source_revision"],
                                reason=MembershipReason(row["membership_reason"]),
                                source_member_key=row["source_row_id"],
                                is_delisted=_flag(row["is_delisted"]),
                            )
                        )
                        fact_key = f"{row['universe_id']}:{instrument_id}"
                    elif item.role == "corporate_actions":
                        instrument_id = instruments[security]
                        related = row["related_source_security_id"]
                        was_added = await reference.add_corporate_action(
                            CorporateActionEvidence(
                                instrument_id=instrument_id,
                                event_type=CorporateActionType(row["event_type"]),
                                effective_date=_day(row["effective_date"]),
                                ex_date=_optional_day(row["ex_date"]),
                                record_date=_optional_day(row["record_date"]),
                                known_at=_instant(row["known_at"]),
                                source=manifest.provider_id,
                                source_revision=row["source_revision"],
                                verification=EvidenceVerification(row["verification"]),
                                ratio_numerator=_optional_decimal(row["ratio_numerator"]),
                                ratio_denominator=_optional_decimal(row["ratio_denominator"]),
                                cash_value=_optional_decimal(row["cash_value"]),
                                currency=row["currency"] or None,
                                related_instrument_id=(instruments[related] if related else None),
                            )
                        )
                        fact_key = f"{instrument_id}:{row['event_type']}:{row['effective_date']}"
                    elif item.role in {"daily_bars", "benchmark_bars"}:
                        if item.role == "benchmark_bars" and security not in instruments:
                            instrument_id = InstrumentId.deterministic(
                                "historical-dataset", manifest.provider_id, security
                            )
                            instruments[security] = instrument_id
                            await reference.add_identity(
                                InstrumentIdentityRevision(
                                    instrument_id=instrument_id,
                                    kind=InstrumentKind.INDEX,
                                    canonical_symbol=row["canonical_symbol"],
                                    company_name=row["canonical_symbol"],
                                    aliases=(),
                                    former_names=(),
                                    isin=None,
                                    sector="INDEX",
                                    concentration_groups=(),
                                    cash_mapping=CashInstrumentMapping(
                                        exchange="NSE", trading_symbol=row["canonical_symbol"]
                                    ),
                                    futures_research_requested=False,
                                    valid_from=manifest.coverage_start,
                                    valid_to=None,
                                    recorded_at=_instant(row["known_at"]),
                                    source=manifest.provider_id,
                                    source_revision=manifest.source_revision,
                                )
                            )
                        instrument_id = instruments[security]
                        bar = DailyBarRevision(
                            instrument_id=instrument_id,
                            instrument_kind=(
                                MarketInstrumentKind.CASH_EQUITY
                                if item.role == "daily_bars"
                                else MarketInstrumentKind.INDEX
                            ),
                            source=manifest.provider_id,
                            source_instrument_id=_source_instrument_id(security),
                            candle=DailyCandle(
                                trading_date=_day(row["trading_date"]),
                                open=Decimal(row["open"]),
                                high=Decimal(row["high"]),
                                low=Decimal(row["low"]),
                                close=Decimal(row["close"]),
                                volume=int(row["volume"]),
                                open_interest=None,
                            ),
                            retrieved_at=_instant(row["known_at"]),
                            adjustment_status=AdjustmentStatus(row["adjustment_status"]),
                            completeness=BarCompleteness.COMPLETE,
                            source_revision=_bar_revision(
                                manifest.provider_id, row["source_revision"]
                            ),
                            batch_sha256=item.sha256,
                            quality_revision=_MAPPING_REVISION,
                        )
                        if bar_batch and (
                            len(bar_batch) >= _BAR_BATCH_SIZE
                            or bar.instrument_id != bar_batch[-1].instrument_id
                            or bar.candle.trading_date <= bar_batch[-1].candle.trading_date
                        ):
                            archive = await bars.add_series(DailyBarSeries(bars=tuple(bar_batch)))
                            added += archive.added
                            unchanged += archive.unchanged
                            bar_batch.clear()
                        bar_batch.append(bar)
                        fact_key = f"{instrument_id}:{row['trading_date']}"
                    else:  # pragma: no cover - manifest contract is closed
                        raise AssertionError(item.role)  # noqa: TRY301
                    if item.role not in {"daily_bars", "benchmark_bars"}:
                        added += int(was_added)
                        unchanged += int(not was_added)
                    pa, pu = await buffer.add(row, fact_key=fact_key, source_security_id=security)
                    provenance_added += pa
                    provenance_unchanged += pu
                if bar_batch:
                    archive = await bars.add_series(DailyBarSeries(bars=tuple(bar_batch)))
                    added += archive.added
                    unchanged += archive.unchanged
                pa, pu = await buffer.flush()
                provenance_added += pa
                provenance_unchanged += pu
                counts[f"{item.role}_added"] = added
                counts[f"{item.role}_unchanged"] = unchanged
                counts[f"{item.role}_provenance_added"] = provenance_added
                counts[f"{item.role}_provenance_unchanged"] = provenance_unchanged

            logical_counts = {
                "files": len(manifest.files),
                **{item.role: item.row_count for item in manifest.files},
            }
            result = HistoricalImportResult(
                schema=IMPORT_RESULT_SCHEMA,
                dataset_id=manifest.dataset_id,
                source_revision=manifest.source_revision,
                dataset_fingerprint=report.dataset_fingerprint,
                preflight_sha256=preflight_sha,
                counts=tuple(sorted(logical_counts.items())),
                status="IMPORTED_OR_ALREADY_PRESENT",
            )
            result_sha = hashlib.sha256(result.export_bytes()).hexdigest()
            await ledger.add_import_run(
                ImportRunRecord(
                    id=_stable_uuid(
                        "run", str(account_id.value), revision.dataset_id, revision.source_revision
                    ),
                    dataset_revision_id=revision.id,
                    account_id=account_id.value,
                    started_at=manifest.acquired_at,
                    finished_at=manifest.acquired_at,
                    mode="APPLY",
                    status="IMPORTED_OR_ALREADY_PRESENT",
                    preflight_sha256=preflight_sha,
                    result_sha256=result_sha,
                    counts=dict(result.counts),
                )
            )
            await session.commit()
            return result  # noqa: TRY300
        except BaseException:
            await session.rollback()
            raise
