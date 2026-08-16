"""Network-free historical-dataset manifest and streaming CSV preflight.

The intake boundary deliberately lives in the ``ingest`` composition root: a
single licensed delivery spans Reference and Market Data, while neither bounded
context is allowed to depend on the other.  This module performs no database
writes and executes no embedded content.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = [
    "CANONICAL_SCHEMAS",
    "MANIFEST_SCHEMA",
    "CapabilityCoverage",
    "DatasetFile",
    "DatasetManifest",
    "ImportDecision",
    "LicensingStatus",
    "PreflightFinding",
    "PreflightReport",
    "canonical_json_bytes",
    "iter_dataset_rows",
    "load_manifest",
    "preflight_dataset",
]

MANIFEST_SCHEMA: Final = "dhruva.historical-dataset-manifest.v1"
PREFLIGHT_SCHEMA: Final = "dhruva.dataset-preflight.v1"
_MAX_MANIFEST_BYTES = 1_000_000
_MAX_FILE_BYTES = 20_000_000_000
_MAX_ROWS = 20_000_000
_MAX_FIELD_CHARS = 65_536
_MAX_TEXT_CHARS = 4_096
_CONTROL_CHARACTER_LIMIT = 32
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{1,127}\Z")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_EXCHANGE = frozenset({"NSE", "BSE"})
_FORMULA_PREFIXES = ("=", "+", "-", "@")


class LicensingStatus(StrEnum):
    """Owner-controlled evidence; provider identity never elevates this value."""

    UNVERIFIED = "UNVERIFIED"
    EVALUATION_ONLY = "EVALUATION_ONLY"
    PERSONAL_USE_CONFIRMED = "PERSONAL_USE_CONFIRMED"
    LOCAL_RETENTION_CONFIRMED = "LOCAL_RETENTION_CONFIRMED"
    AUTOMATED_ANALYSIS_CONFIRMED = "AUTOMATED_ANALYSIS_CONFIRMED"
    RESTRICTED = "RESTRICTED"
    REJECTED = "REJECTED"

    @property
    def permits_import(self) -> bool:
        """Return whether the owner asserted rights sufficient for local persistence."""
        return self in {
            LicensingStatus.LOCAL_RETENTION_CONFIRMED,
            LicensingStatus.AUTOMATED_ANALYSIS_CONFIRMED,
        }


class ImportDecision(StrEnum):
    """Preflight disposition; only READY may reach an authoritative transaction."""

    READY = "READY"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"


class IntegrityStatus(StrEnum):
    """Deterministic stage reached by the local delivery."""

    RECEIVED = "RECEIVED"
    HASH_VERIFIED = "HASH_VERIFIED"
    SCHEMA_VALID = "SCHEMA_VALID"
    SEMANTIC_VALID = "SEMANTIC_VALID"
    QUARANTINED = "QUARANTINED"
    READY_FOR_IMPORT = "READY_FOR_IMPORT"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class CapabilityCoverage:
    """Claims declared by the owner-reviewed manifest, never inferred from rows."""

    historical_membership: bool
    removals: bool
    delistings: bool
    inactive_securities: bool
    instrument_lifecycle: bool
    corporate_actions: bool
    dividends: bool
    publication_timestamps: bool
    revision_history: bool
    pit_known_at: bool


@dataclass(frozen=True, slots=True)
class DatasetFile:
    """One immutable payload named by a safe relative path."""

    role: str
    path: str
    sha256: str
    size_bytes: int
    row_count: int
    schema: str
    media_type: str


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """Validated, versioned dataset envelope."""

    schema: str
    dataset_id: str
    provider_id: str
    provider_name: str
    provider_product: str
    license_reference: str
    licensing_status: LicensingStatus
    acquired_at: datetime
    coverage_start: date
    coverage_end: date
    source_revision: str
    currency: str
    exchange: str
    timezone: str
    price_basis: str
    adjustment_basis: str
    return_basis: str
    benchmark_basis: str
    known_at_semantics: str
    import_policy: str
    capabilities: CapabilityCoverage
    files: tuple[DatasetFile, ...]
    notes: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True, order=True)
class PreflightFinding:
    """Stable, row-addressable validation evidence."""

    code: str
    role: str
    row: int
    message: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Machine- and owner-readable outcome with no volatile generated time."""

    schema: str
    dataset_id: str
    provider_id: str
    manifest_sha256: str
    dataset_fingerprint: str
    coverage_start: str
    coverage_end: str
    counts: tuple[tuple[str, int], ...]
    failures: tuple[PreflightFinding, ...]
    warnings: tuple[PreflightFinding, ...]
    blockers: tuple[str, ...]
    integrity_status: IntegrityStatus
    import_decision: ImportDecision

    def export_bytes(self) -> bytes:
        """Return byte-deterministic canonical JSON."""
        return canonical_json_bytes(asdict(self)) + b"\n"


CANONICAL_SCHEMAS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "instrument_identities": (
        "dhruva.instrument-lifecycle.csv.v1",
        (
            "source_row_id",
            "source_security_id",
            "instrument_kind",
            "canonical_symbol",
            "company_name",
            "isin",
            "exchange",
            "provider_instrument_token",
            "exchange_token",
            "valid_from",
            "valid_to",
            "known_at",
            "source_revision",
        ),
    ),
    "universe_definitions": (
        "dhruva.historical-universe-definition.csv.v1",
        (
            "source_row_id",
            "universe_id",
            "label",
            "kind",
            "known_at",
            "source_revision",
            "historical_membership_available",
            "removals_included",
            "delistings_included",
            "pit_known_at_available",
            "instrument_lifecycle_available",
            "corporate_action_coverage_available",
            "licensing_confirmed",
            "return_basis",
            "benchmark_basis",
            "benchmark_history_available",
        ),
    ),
    "universe_memberships": (
        "dhruva.historical-universe-membership.csv.v1",
        (
            "source_row_id",
            "universe_id",
            "source_security_id",
            "effective_from",
            "effective_to",
            "known_at",
            "source_revision",
            "membership_reason",
            "is_delisted",
        ),
    ),
    "corporate_actions": (
        "dhruva.corporate-action.csv.v1",
        (
            "source_row_id",
            "source_security_id",
            "event_type",
            "effective_date",
            "ex_date",
            "record_date",
            "known_at",
            "ratio_numerator",
            "ratio_denominator",
            "cash_value",
            "currency",
            "related_source_security_id",
            "verification",
            "source_revision",
        ),
    ),
    "daily_bars": (
        "dhruva.daily-market-bar.csv.v1",
        (
            "source_row_id",
            "source_security_id",
            "instrument_kind",
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "currency",
            "exchange",
            "known_at",
            "adjustment_status",
            "source_revision",
        ),
    ),
    "benchmark_bars": (
        "dhruva.benchmark-history.csv.v1",
        (
            "source_row_id",
            "benchmark_id",
            "canonical_symbol",
            "trading_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "currency",
            "exchange",
            "known_at",
            "benchmark_basis",
            "adjustment_status",
            "source_revision",
        ),
    ),
}
_ROLE_ORDER = tuple(CANONICAL_SCHEMAS)


class DatasetContractError(ValueError):
    """Manifest or path failure safe to expose in a preflight report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json_bytes(value: object) -> bytes:
    """Encode JSON deterministically without accepting non-JSON values."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    ).encode()


def _json_default(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def load_manifest(manifest_path: Path) -> tuple[Path, DatasetManifest]:
    """Load a local manifest and reject paths outside its selected dataset root."""
    supplied = manifest_path.absolute()
    if supplied.is_symlink():
        raise DatasetContractError("MANIFEST_SYMLINK", "manifest must not be a symlink")
    resolved = supplied.resolve(strict=True)
    if not resolved.is_file():
        raise DatasetContractError("MANIFEST_NOT_FILE", "manifest path is not a regular file")
    size = resolved.stat().st_size
    if size <= 0 or size > _MAX_MANIFEST_BYTES:
        raise DatasetContractError("MANIFEST_SIZE", "manifest size is outside the safe bound")
    raw = resolved.read_bytes()
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DatasetContractError("MANIFEST_JSON", "manifest is not valid UTF-8 JSON") from error
    mapping = _mapping(decoded, field="manifest")
    manifest_sha = hashlib.sha256(canonical_json_bytes(mapping)).hexdigest()
    manifest = _manifest(mapping, manifest_sha=manifest_sha)
    root = resolved.parent.resolve(strict=True)
    _validate_manifest_contract(manifest, root=root)
    return root, manifest


def _manifest(value: Mapping[str, object], *, manifest_sha: str) -> DatasetManifest:
    provider = _mapping(value.get("provider"), field="provider")
    license_data = _mapping(value.get("license"), field="license")
    coverage = _mapping(value.get("coverage"), field="coverage")
    market = _mapping(value.get("market"), field="market")
    capabilities = _mapping(value.get("capabilities"), field="capabilities")
    files_value = value.get("files")
    if not isinstance(files_value, list):
        raise DatasetContractError("MANIFEST_FILES", "files must be a JSON array")
    return DatasetManifest(
        schema=_text(value.get("schema"), field="schema"),
        dataset_id=_text(value.get("dataset_id"), field="dataset_id"),
        provider_id=_text(provider.get("id"), field="provider.id"),
        provider_name=_text(provider.get("name"), field="provider.name"),
        provider_product=_text(provider.get("product"), field="provider.product"),
        license_reference=_text(license_data.get("reference"), field="license.reference"),
        licensing_status=_enum(LicensingStatus, license_data.get("status"), field="license.status"),
        acquired_at=_instant(value.get("acquired_at"), field="acquired_at"),
        coverage_start=_date(coverage.get("start"), field="coverage.start"),
        coverage_end=_date(coverage.get("end"), field="coverage.end"),
        source_revision=_text(value.get("source_revision"), field="source_revision"),
        currency=_text(market.get("currency"), field="market.currency"),
        exchange=_text(market.get("exchange"), field="market.exchange"),
        timezone=_text(market.get("timezone"), field="market.timezone"),
        price_basis=_text(market.get("price_basis"), field="market.price_basis"),
        adjustment_basis=_text(market.get("adjustment_basis"), field="market.adjustment_basis"),
        return_basis=_text(market.get("return_basis"), field="market.return_basis"),
        benchmark_basis=_text(market.get("benchmark_basis"), field="market.benchmark_basis"),
        known_at_semantics=_text(value.get("known_at_semantics"), field="known_at_semantics"),
        import_policy=_text(value.get("import_policy"), field="import_policy"),
        capabilities=CapabilityCoverage(
            **{
                field: _boolean(capabilities.get(field), field=f"capabilities.{field}")
                for field in CapabilityCoverage.__dataclass_fields__
            }
        ),
        files=tuple(_manifest_file(_mapping(item, field="files[]")) for item in files_value),
        notes=_text(value.get("notes", ""), field="notes", allow_empty=True),
        manifest_sha256=manifest_sha,
    )


def _manifest_file(value: Mapping[str, object]) -> DatasetFile:
    return DatasetFile(
        role=_text(value.get("role"), field="files.role"),
        path=_text(value.get("path"), field="files.path"),
        sha256=_text(value.get("sha256"), field="files.sha256"),
        size_bytes=_integer(value.get("size_bytes"), field="files.size_bytes"),
        row_count=_integer(value.get("row_count"), field="files.row_count"),
        schema=_text(value.get("schema"), field="files.schema"),
        media_type=_text(value.get("media_type"), field="files.media_type"),
    )


def _validate_manifest_contract(  # noqa: PLR0912, PLR0915 - fail-closed contract gates
    manifest: DatasetManifest, *, root: Path
) -> None:
    if manifest.schema != MANIFEST_SCHEMA:
        raise DatasetContractError("UNSUPPORTED_MANIFEST_SCHEMA", manifest.schema)
    if not _ID.fullmatch(manifest.dataset_id):
        raise DatasetContractError("INVALID_DATASET_ID", "dataset_id is not canonical")
    if not _PROVIDER.fullmatch(manifest.provider_id):
        raise DatasetContractError("INVALID_PROVIDER_ID", "provider id is not canonical")
    if not _ID.fullmatch(manifest.source_revision):
        raise DatasetContractError("INVALID_SOURCE_REVISION", "source revision is not canonical")
    if manifest.coverage_end < manifest.coverage_start:
        raise DatasetContractError("COVERAGE_REVERSED", "coverage date range is reversed")
    if manifest.coverage_end > manifest.acquired_at.date():
        raise DatasetContractError("FUTURE_COVERAGE", "coverage ends after acquisition")
    if not _CURRENCY.fullmatch(manifest.currency):
        raise DatasetContractError("INVALID_CURRENCY", "manifest currency must be ISO uppercase")
    if manifest.exchange not in _EXCHANGE:
        raise DatasetContractError("UNSUPPORTED_EXCHANGE", "only NSE or BSE is accepted")
    if manifest.timezone != "Asia/Kolkata":
        raise DatasetContractError("UNSUPPORTED_TIMEZONE", "Indian data must use Asia/Kolkata")
    if manifest.price_basis not in {"CLOSE", "OHLCV"}:
        raise DatasetContractError("UNSUPPORTED_PRICE_BASIS", manifest.price_basis)
    if manifest.adjustment_basis not in {"RAW", "PRICE_ADJUSTED_VERIFIED", "UNKNOWN"}:
        raise DatasetContractError("UNSUPPORTED_ADJUSTMENT_BASIS", manifest.adjustment_basis)
    if manifest.return_basis not in {"RAW_PRICE", "PRICE_ADJUSTED", "TOTAL_RETURN", "UNKNOWN"}:
        raise DatasetContractError("UNSUPPORTED_RETURN_BASIS", manifest.return_basis)
    if manifest.benchmark_basis not in {"PRICE_INDEX", "TOTAL_RETURN_INDEX"}:
        raise DatasetContractError("UNSUPPORTED_BENCHMARK_BASIS", manifest.benchmark_basis)
    if manifest.known_at_semantics != "SOURCE_OBSERVED_AT":
        raise DatasetContractError(
            "UNSUPPORTED_KNOWN_AT", "known_at_semantics must be SOURCE_OBSERVED_AT"
        )
    if manifest.import_policy != "APPEND_ONLY":
        raise DatasetContractError("UNSUPPORTED_IMPORT_POLICY", "only APPEND_ONLY is accepted")
    roles = tuple(item.role for item in manifest.files)
    if set(roles) != set(_ROLE_ORDER) or len(roles) != len(_ROLE_ORDER):
        raise DatasetContractError("FILE_ROLE_SET", "manifest must name each canonical role once")
    paths = tuple(item.path for item in manifest.files)
    if len(paths) != len(set(paths)):
        raise DatasetContractError("DUPLICATE_FILE_PATH", "manifest file paths repeat")
    for item in manifest.files:
        expected_schema, _fields = CANONICAL_SCHEMAS[item.role]
        if item.schema != expected_schema:
            raise DatasetContractError("UNSUPPORTED_FILE_SCHEMA", f"{item.role}: {item.schema}")
        if item.media_type != "text/csv":
            raise DatasetContractError("UNSUPPORTED_MEDIA_TYPE", f"{item.role}: {item.media_type}")
        if not _SHA256.fullmatch(item.sha256):
            raise DatasetContractError("INVALID_FILE_SHA256", item.role)
        if item.size_bytes <= 0 or item.size_bytes > _MAX_FILE_BYTES:
            raise DatasetContractError("UNSAFE_FILE_SIZE", item.role)
        if item.row_count < 0 or item.row_count > _MAX_ROWS:
            raise DatasetContractError("UNSAFE_ROW_COUNT", item.role)
        _payload_path(root, item)
    if manifest.return_basis == "TOTAL_RETURN" and not (
        manifest.adjustment_basis == "PRICE_ADJUSTED_VERIFIED"
        and manifest.capabilities.corporate_actions
        and manifest.capabilities.dividends
    ):
        raise DatasetContractError(
            "UNSUPPORTED_TOTAL_RETURN_CLAIM",
            "TOTAL_RETURN requires verified adjustment, actions, and dividends",
        )
    if manifest.return_basis == "PRICE_ADJUSTED" and (
        manifest.adjustment_basis != "PRICE_ADJUSTED_VERIFIED"
    ):
        raise DatasetContractError(
            "UNSUPPORTED_ADJUSTED_RETURN_CLAIM",
            "PRICE_ADJUSTED requires verified adjustment semantics",
        )


def _payload_path(root: Path, item: DatasetFile) -> Path:
    pure = PurePosixPath(item.path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in item.path or not pure.parts:
        raise DatasetContractError("PATH_TRAVERSAL", f"unsafe path for {item.role}")
    candidate = root.joinpath(*pure.parts)
    if candidate.is_symlink():
        raise DatasetContractError("PAYLOAD_SYMLINK", f"symlink rejected for {item.role}")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise DatasetContractError("MISSING_FILE", f"missing payload for {item.role}") from error
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise DatasetContractError("PATH_ESCAPE", f"payload escapes dataset root: {item.role}")
    return resolved


def preflight_dataset(  # noqa: PLR0915 - report assembles every validation stage
    manifest_path: Path,
) -> PreflightReport:
    """Verify hashes, schemas and semantics without opening a database connection."""
    try:
        root, manifest = load_manifest(manifest_path)
    except (DatasetContractError, FileNotFoundError) as error:
        code = error.code if isinstance(error, DatasetContractError) else "MISSING_MANIFEST"
        finding = PreflightFinding(code, "manifest", 0, str(error))
        return PreflightReport(
            schema=PREFLIGHT_SCHEMA,
            dataset_id="UNKNOWN",
            provider_id="UNKNOWN",
            manifest_sha256="",
            dataset_fingerprint="",
            coverage_start="",
            coverage_end="",
            counts=(),
            failures=(finding,),
            warnings=(),
            blockers=(code,),
            integrity_status=IntegrityStatus.REJECTED,
            import_decision=ImportDecision.REJECTED,
        )
    failures: list[PreflightFinding] = []
    warnings: list[PreflightFinding] = []
    counts: dict[str, int] = {}
    identities: dict[str, list[tuple[date, date | None]]] = {}
    universes: dict[str, str] = {}
    memberships: dict[tuple[str, str], list[tuple[date, date | None]]] = {}
    action_dates: set[tuple[str, date]] = set()
    previous_bars: dict[str, tuple[date, Decimal, datetime, str]] = {}
    file_digests: list[str] = []
    hashes_valid = True
    schemas_valid = True
    for role in _ROLE_ORDER:
        item = next(entry for entry in manifest.files if entry.role == role)
        path = _payload_path(root, item)
        actual_size, actual_sha = _hash_file(path)
        file_digests.append(f"{role}:{actual_sha}")
        if actual_size != item.size_bytes:
            hashes_valid = False
            failures.append(
                PreflightFinding("FILE_SIZE_MISMATCH", role, 0, "declared size differs")
            )
        if actual_sha != item.sha256:
            hashes_valid = False
            failures.append(PreflightFinding("FILE_HASH_MISMATCH", role, 0, "SHA-256 differs"))
        observed = 0
        try:
            for row_number, row in iter_dataset_rows(root, item):
                observed += 1
                try:
                    _validate_row(
                        manifest,
                        role=role,
                        row_number=row_number,
                        row=row,
                        identities=identities,
                        universes=universes,
                        memberships=memberships,
                        action_dates=action_dates,
                        previous_bars=previous_bars,
                        warnings=warnings,
                    )
                except (ValueError, InvalidOperation) as error:
                    failures.append(
                        PreflightFinding("ROW_SEMANTIC_INVALID", role, row_number, str(error))
                    )
        except DatasetContractError as error:
            schemas_valid = False
            failures.append(PreflightFinding(error.code, role, observed + 2, str(error)))
        counts[role] = observed
        if observed != item.row_count:
            failures.append(
                PreflightFinding(
                    "ROW_COUNT_MISMATCH",
                    role,
                    0,
                    f"declared {item.row_count}, observed {observed}",
                )
            )
    blockers = _readiness_blockers(manifest)
    if not manifest.licensing_status.permits_import:
        blockers.append("OWNER_LOCAL_RETENTION_NOT_CONFIRMED")
    if failures:
        decision = (
            ImportDecision.REJECTED
            if not hashes_valid or not schemas_valid
            else ImportDecision.QUARANTINED
        )
        integrity = (
            IntegrityStatus.REJECTED
            if decision is ImportDecision.REJECTED
            else IntegrityStatus.QUARANTINED
        )
    elif blockers:
        decision = ImportDecision.QUARANTINED
        integrity = IntegrityStatus.QUARANTINED
    else:
        decision = ImportDecision.READY
        integrity = IntegrityStatus.READY_FOR_IMPORT
    fingerprint = hashlib.sha256(
        canonical_json_bytes(
            {
                "manifest": manifest.manifest_sha256,
                "files": sorted(file_digests),
                "counts": sorted(counts.items()),
            }
        )
    ).hexdigest()
    return PreflightReport(
        schema=PREFLIGHT_SCHEMA,
        dataset_id=manifest.dataset_id,
        provider_id=manifest.provider_id,
        manifest_sha256=manifest.manifest_sha256,
        dataset_fingerprint=fingerprint,
        coverage_start=manifest.coverage_start.isoformat(),
        coverage_end=manifest.coverage_end.isoformat(),
        counts=tuple(sorted(counts.items())),
        failures=tuple(sorted(failures)),
        warnings=tuple(sorted(warnings)),
        blockers=tuple(sorted(set(blockers))),
        integrity_status=integrity,
        import_decision=decision,
    )


def iter_dataset_rows(root: Path, item: DatasetFile) -> Iterator[tuple[int, dict[str, str]]]:
    """Stream canonical CSV rows with bounded fields and exact headers."""
    path = _payload_path(root, item)
    csv.field_size_limit(_MAX_FIELD_CHARS)
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            expected = list(CANONICAL_SCHEMAS[item.role][1])
            if reader.fieldnames != expected:
                raise DatasetContractError(
                    "CSV_HEADER_MISMATCH", f"expected {expected}, received {reader.fieldnames}"
                )
            for row_number, raw in enumerate(reader, start=2):
                if None in raw or any(value is None for value in raw.values()):
                    raise DatasetContractError("MALFORMED_CSV", f"malformed row {row_number}")
                row = {key: cast("str", value).strip() for key, value in raw.items()}
                if any(len(value) > _MAX_FIELD_CHARS for value in row.values()):
                    raise DatasetContractError("FIELD_TOO_LARGE", f"oversized row {row_number}")
                yield row_number, row
    except (UnicodeDecodeError, csv.Error) as error:
        raise DatasetContractError("MALFORMED_CSV", str(error)) from error


def _validate_row(  # noqa: PLR0912, PLR0913, PLR0915 - role validators are explicit
    manifest: DatasetManifest,
    *,
    role: str,
    row_number: int,
    row: dict[str, str],
    identities: dict[str, list[tuple[date, date | None]]],
    universes: dict[str, str],
    memberships: dict[tuple[str, str], list[tuple[date, date | None]]],
    action_dates: set[tuple[str, date]],
    previous_bars: dict[str, tuple[date, Decimal, datetime, str]],
    warnings: list[PreflightFinding],
) -> None:
    _safe_row_id(row["source_row_id"])
    _safe_text(row["source_revision"], field="source_revision")
    if role == "instrument_identities":
        security = _safe_text(row["source_security_id"], field="source_security_id")
        _enum_text(row["instrument_kind"], {"EQUITY", "INDEX"}, field="instrument_kind")
        _safe_text(row["canonical_symbol"], field="canonical_symbol")
        _safe_text(row["company_name"], field="company_name")
        if row["isin"] and not re.fullmatch(r"IN[A-Z0-9]{10}", row["isin"]):
            raise ValueError("invalid Indian ISIN")
        _exchange(row["exchange"], manifest)
        _token_pair(row["provider_instrument_token"], row["exchange_token"])
        start = _date(row["valid_from"], field="valid_from")
        end = _optional_date(row["valid_to"], field="valid_to")
        _interval(start, end, field="identity")
        _known_at(row["known_at"], manifest)
        periods = identities.setdefault(security, [])
        _no_overlap(periods, start, end, field="identity")
        periods.append((start, end))
    elif role == "universe_definitions":
        universe = _safe_text(row["universe_id"], field="universe_id")
        _safe_text(row["label"], field="label")
        _enum_text(
            row["kind"],
            {
                "HISTORICAL_PIT_OWNER_WATCHLIST",
                "HISTORICAL_MARKET_UNIVERSE",
                "INDEX_CONSTITUENT_UNIVERSE",
                "LIQUIDITY_FILTERED_UNIVERSE",
            },
            field="kind",
        )
        _known_at(row["known_at"], manifest)
        for field in (
            "historical_membership_available",
            "removals_included",
            "delistings_included",
            "pit_known_at_available",
            "instrument_lifecycle_available",
            "corporate_action_coverage_available",
            "licensing_confirmed",
            "benchmark_history_available",
        ):
            _bool_text(row[field], field=field)
        coverage_claims = {
            "historical_membership_available": manifest.capabilities.historical_membership,
            "removals_included": manifest.capabilities.removals,
            "delistings_included": manifest.capabilities.delistings,
            "pit_known_at_available": manifest.capabilities.pit_known_at,
            "instrument_lifecycle_available": manifest.capabilities.instrument_lifecycle,
            "corporate_action_coverage_available": manifest.capabilities.corporate_actions,
        }
        for field, expected_claim in coverage_claims.items():
            if _bool_text(row[field], field=field) != expected_claim:
                raise ValueError(f"definition {field} differs from manifest capability")
        if _bool_text(row["licensing_confirmed"], field="licensing_confirmed") != (
            manifest.licensing_status.permits_import
        ):
            raise ValueError("definition licensing claim differs from owner manifest")
        if row["return_basis"] != manifest.return_basis:
            raise ValueError("definition return basis differs from manifest")
        if row["benchmark_basis"] != manifest.benchmark_basis:
            raise ValueError("definition benchmark basis differs from manifest")
        if not _bool_text(row["benchmark_history_available"], field="benchmark_history_available"):
            raise ValueError("evaluation-ready definition requires benchmark history")
        if universe in universes:
            raise ValueError("universe definition repeats")
        universes[universe] = row["source_revision"]
    elif role == "universe_memberships":
        universe = _safe_text(row["universe_id"], field="universe_id")
        security = _safe_text(row["source_security_id"], field="source_security_id")
        if universe not in universes or security not in identities:
            raise ValueError("membership references unknown universe or security")
        if row["source_revision"] != universes[universe]:
            raise ValueError("membership source revision differs from its definition")
        start = _date(row["effective_from"], field="effective_from")
        end = _optional_date(row["effective_to"], field="effective_to")
        _interval(start, end, field="membership")
        _known_at(row["known_at"], manifest)
        _enum_text(
            row["membership_reason"],
            {
                "OWNER_SELECTION",
                "INDEX_CONSTITUENT",
                "MARKET_ELIGIBLE",
                "LIQUIDITY_ELIGIBLE",
                "SOURCE_REPORTED",
            },
            field="membership_reason",
        )
        _bool_text(row["is_delisted"], field="is_delisted")
        periods = memberships.setdefault((universe, security), [])
        _no_overlap(periods, start, end, field="membership")
        periods.append((start, end))
    elif role == "corporate_actions":
        security = _safe_text(row["source_security_id"], field="source_security_id")
        if security not in identities:
            raise ValueError("corporate action references unknown security")
        event = _enum_text(
            row["event_type"],
            {
                "SPLIT",
                "BONUS",
                "DIVIDEND",
                "RIGHTS_ISSUE",
                "MERGER",
                "DEMERGER",
                "SYMBOL_CHANGE",
                "DELISTING",
                "OTHER",
            },
            field="event_type",
        )
        effective = _date(row["effective_date"], field="effective_date")
        ex_date = _optional_date(row["ex_date"], field="ex_date")
        record_date = _optional_date(row["record_date"], field="record_date")
        if ex_date is not None and record_date is not None and record_date < ex_date:
            raise ValueError("corporate-action record date precedes ex-date")
        _known_at(row["known_at"], manifest)
        numerator = _optional_decimal(row["ratio_numerator"], field="ratio_numerator")
        denominator = _optional_decimal(row["ratio_denominator"], field="ratio_denominator")
        if (numerator is None) != (denominator is None):
            raise ValueError("corporate-action ratio is partial")
        if event in {"SPLIT", "BONUS"} and (
            numerator is None or denominator is None or numerator <= 0 or denominator <= 0
        ):
            raise ValueError("split/bonus requires a positive ratio")
        cash = _optional_decimal(row["cash_value"], field="cash_value")
        if cash is not None and cash < 0:
            raise ValueError("corporate-action cash value is negative")
        if (cash is None) != (row["currency"] == ""):
            raise ValueError("corporate-action cash value and currency must be paired")
        if row["currency"] and not _CURRENCY.fullmatch(row["currency"]):
            raise ValueError("invalid corporate-action currency")
        related = row["related_source_security_id"]
        if related and related not in identities:
            raise ValueError("corporate action references unknown related security")
        _enum_text(
            row["verification"],
            {"UNVERIFIED", "SOURCE_REPORTED", "VERIFIED", "CONFLICTED"},
            field="verification",
        )
        action_dates.add((security, ex_date or effective))
    elif role in {"daily_bars", "benchmark_bars"}:
        security_field = "source_security_id" if role == "daily_bars" else "benchmark_id"
        security = _safe_text(row[security_field], field=security_field)
        if role == "daily_bars" and security not in identities:
            raise ValueError("bar references unknown security")
        if role == "daily_bars":
            _enum_text(row["instrument_kind"], {"CASH_EQUITY"}, field="instrument_kind")
        else:
            _safe_text(row["canonical_symbol"], field="canonical_symbol")
            if row["benchmark_basis"] != manifest.benchmark_basis:
                raise ValueError("benchmark row basis differs from manifest")
        trading_date = _date(row["trading_date"], field="trading_date")
        if trading_date > manifest.coverage_end or trading_date < manifest.coverage_start:
            raise ValueError("bar date is outside manifest coverage")
        prices = tuple(
            _decimal(row[field], field=field) for field in ("open", "high", "low", "close")
        )
        open_price, high, low, close = prices
        if any(value <= 0 for value in prices):
            raise ValueError("bar price must be positive")
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise ValueError("bar OHLC relationship is invalid")
        volume = _integer(row["volume"], field="volume")
        if volume < 0:
            raise ValueError("bar volume is negative")
        _currency_exchange(row, manifest)
        known_at = _known_at(row["known_at"], manifest, not_before=trading_date)
        adjustment = _enum_text(
            row["adjustment_status"],
            {"RAW", "ADJUSTED", "VERIFIED", "UNKNOWN"},
            field="adjustment_status",
        )
        expected = {
            "RAW": "RAW",
            "PRICE_ADJUSTED_VERIFIED": "VERIFIED",
            "UNKNOWN": "UNKNOWN",
        }[manifest.adjustment_basis]
        if adjustment != expected:
            raise ValueError("bar adjustment status differs from manifest")
        prior = previous_bars.get(security)
        if prior is not None:
            prior_date, prior_close, prior_known_at, prior_revision = prior
            if trading_date < prior_date:
                raise ValueError("bars must be ordered by security/date")
            if trading_date == prior_date:
                if known_at <= prior_known_at or row["source_revision"] == prior_revision:
                    raise ValueError(
                        "bar corrections require later known_at and a new source revision"
                    )
                previous_bars[security] = (
                    trading_date,
                    close,
                    known_at,
                    row["source_revision"],
                )
                return
            discontinuity = abs(open_price / prior_close - Decimal(1)) >= Decimal("0.40")
            if (
                discontinuity
                and manifest.adjustment_basis == "PRICE_ADJUSTED_VERIFIED"
                and (
                    security,
                    trading_date,
                )
                not in action_dates
            ):
                raise ValueError("verified adjusted series has unexplained discontinuity")
            if discontinuity and manifest.adjustment_basis != "PRICE_ADJUSTED_VERIFIED":
                warnings.append(
                    PreflightFinding(
                        "SUSPICIOUS_PRICE_DISCONTINUITY",
                        role,
                        row_number,
                        f"{security} opens >=40% from prior close; no repair was attempted",
                    )
                )
        previous_bars[security] = (
            trading_date,
            close,
            known_at,
            row["source_revision"],
        )
    else:  # pragma: no cover - role set validated before dispatch
        raise ValueError(f"unsupported role: {role}")
    if row_number > _MAX_ROWS + 1:
        raise ValueError("payload exceeds safe row bound")


def _readiness_blockers(manifest: DatasetManifest) -> list[str]:
    claims = (
        (manifest.capabilities.historical_membership, "HISTORICAL_UNIVERSE_UNAVAILABLE"),
        (manifest.capabilities.removals, "UNIVERSE_MEMBERSHIP_UNKNOWN"),
        (manifest.capabilities.delistings, "DELISTING_COVERAGE_UNKNOWN"),
        (manifest.capabilities.inactive_securities, "INACTIVE_SECURITIES_UNAVAILABLE"),
        (manifest.capabilities.instrument_lifecycle, "INSTRUMENT_MAPPING_UNRESOLVED"),
        (manifest.capabilities.corporate_actions, "CORPORATE_ACTION_UNVERIFIED"),
        (manifest.capabilities.dividends, "DIVIDEND_COVERAGE_UNAVAILABLE"),
        (
            manifest.capabilities.publication_timestamps,
            "PUBLICATION_TIMESTAMPS_UNAVAILABLE",
        ),
        (manifest.capabilities.pit_known_at, "PIT_KNOWN_AT_UNAVAILABLE"),
        (manifest.capabilities.revision_history, "REVISION_HISTORY_UNAVAILABLE"),
        (manifest.return_basis in {"PRICE_ADJUSTED", "TOTAL_RETURN"}, "ADJUSTMENT_UNKNOWN"),
    )
    return [reason for supported, reason in claims if not supported]


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DatasetContractError("MANIFEST_TYPE", f"{field} must be an object")
    return cast("Mapping[str, object]", value)


def _text(value: object, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise DatasetContractError("MANIFEST_TYPE", f"{field} must be non-empty text")
    if len(value) > _MAX_TEXT_CHARS:
        raise DatasetContractError("MANIFEST_FIELD_TOO_LARGE", field)
    return value


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise DatasetContractError("MANIFEST_TYPE", f"{field} must be an integer")
    try:
        return int(cast("str | int", value))
    except (TypeError, ValueError) as error:
        raise DatasetContractError("INVALID_INTEGER", field) from error


def _boolean(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise DatasetContractError("MANIFEST_TYPE", f"{field} must be boolean")
    return value


def _enum(enum_type: type[LicensingStatus], value: object, *, field: str) -> LicensingStatus:
    try:
        return enum_type(_text(value, field=field))
    except ValueError as error:
        raise DatasetContractError("MANIFEST_ENUM", field) from error


def _date(value: object, *, field: str) -> date:
    if not isinstance(value, str):
        raise DatasetContractError("INVALID_DATE", field)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise DatasetContractError("INVALID_DATE", field) from error


def _optional_date(value: str, *, field: str) -> date | None:
    return None if value == "" else _date(value, field=field)


def _instant(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise DatasetContractError("INVALID_TIMESTAMP", field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DatasetContractError("INVALID_TIMESTAMP", field) from error
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise DatasetContractError("INVALID_TIMESTAMP", f"{field} must be UTC")
    return parsed.astimezone(UTC)


def _known_at(raw: str, manifest: DatasetManifest, *, not_before: date | None = None) -> datetime:
    value = _instant(raw, field="known_at")
    if value > manifest.acquired_at:
        raise ValueError("known_at is after dataset acquisition")
    if not_before is not None and value.date() < not_before:
        raise ValueError("bar known_at precedes its trading date")
    return value


def _decimal(value: str, *, field: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field} is not decimal") from error
    if not parsed.is_finite():
        raise ValueError(f"{field} is not finite")
    return parsed


def _optional_decimal(value: str, *, field: str) -> Decimal | None:
    return None if value == "" else _decimal(value, field=field)


def _safe_text(value: str, *, field: str) -> str:
    if not value or len(value) > _MAX_TEXT_CHARS or value.startswith(_FORMULA_PREFIXES):
        raise ValueError(f"unsafe or empty text in {field}")
    if any(
        ord(character) < _CONTROL_CHARACTER_LIMIT and character not in "\t" for character in value
    ):
        raise ValueError(f"control character in {field}")
    return value


def _safe_row_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError("source_row_id is not canonical")
    return value


def _enum_text(value: str, allowed: set[str], *, field: str) -> str:
    if value not in allowed:
        raise ValueError(f"unsupported {field}: {value}")
    return value


def _bool_text(value: str, *, field: str) -> bool:
    if value not in {"true", "false"}:
        raise ValueError(f"{field} must be true or false")
    return value == "true"


def _exchange(value: str, manifest: DatasetManifest) -> None:
    if value not in _EXCHANGE or value != manifest.exchange:
        raise ValueError("row exchange differs from manifest or is unsupported")


def _currency_exchange(row: dict[str, str], manifest: DatasetManifest) -> None:
    if row["currency"] != manifest.currency or not _CURRENCY.fullmatch(row["currency"]):
        raise ValueError("row currency differs from manifest")
    _exchange(row["exchange"], manifest)


def _token_pair(left: str, right: str) -> None:
    if (left == "") != (right == ""):
        raise ValueError("provider and exchange tokens must be paired")
    if left and (
        _integer(left, field="provider_instrument_token") <= 0
        or _integer(right, field="exchange_token") <= 0
    ):
        raise ValueError("instrument tokens must be positive")


def _interval(start: date, end: date | None, *, field: str) -> None:
    if end is not None and end < start:
        raise ValueError(f"{field} interval is reversed")


def _no_overlap(
    periods: list[tuple[date, date | None]], start: date, end: date | None, *, field: str
) -> None:
    candidate_end = end or date.max
    if any(
        start <= (existing_end or date.max) and existing_start <= candidate_end
        for existing_start, existing_end in periods
    ):
        raise ValueError(f"contradictory overlapping {field} interval")
