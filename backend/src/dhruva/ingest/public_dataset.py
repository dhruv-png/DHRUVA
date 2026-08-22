"""Build an importable canonical dataset from immutable public exchange files."""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from dhruva.ingest.historical_dataset import CANONICAL_SCHEMAS, MANIFEST_SCHEMA
from dhruva.ingest.historical_dataset import preflight_dataset as canonical_preflight
from dhruva.ingest.historical_mapper import MappingResult
from dhruva.ingest.public_exchange import (
    EvidenceClass,
    PublicBar,
    PublicFileFormat,
    PublicFileInspection,
    iter_public_actions,
    iter_public_bars,
    iter_public_benchmarks,
    iter_public_securities,
)
from dhruva.ingest.public_reconstruction import (
    PUBLIC_LIQUID_NSE_UNIVERSE_ID,
    PUBLIC_RECONSTRUCTION_REVISION,
    LiquidityUniverseRule,
    canonical_json_bytes,
    inspect_drop,
    public_preflight,
    reconstruction_fingerprint,
)

__all__ = ["NsePublicDropMapper", "PublicBuildResult", "build_public_dataset"]

PUBLIC_MANIFEST_SCHEMA: Final = "dhruva.public-reconstruction-manifest.v1"
_BAR_FORMATS = {
    PublicFileFormat.NSE_CM_UDIFF_BHAVCOPY_V1,
    PublicFileFormat.NSE_CM_LEGACY_BHAVCOPY_V1,
    PublicFileFormat.NSE_FULL_BHAVCOPY_DELIVERABLE_V1,
    PublicFileFormat.NSE_EXCHANGE_MONTHLY_TRANSACTION_V1,
}
_BAR_BATCH_SIZE = 5000
_DEFAULT_LIQUIDITY_RULE = LiquidityUniverseRule()


@dataclass(frozen=True, slots=True)
class PublicBuildResult:
    """Paths and stable identity of one completed reconstruction."""

    canonical_manifest: Path
    reconstruction_manifest: Path
    dataset_fingerprint: str
    counts: tuple[tuple[str, int], ...]
    warnings: tuple[str, ...]


class NsePublicDropMapper:
    """Versioned adapter from one local NSE drop to the canonical contract."""

    mapper_id = "nse_public_drop"
    mapping_revision = "nse_public_offline_mapper_v2"

    def __init__(
        self,
        *,
        retrieved_at: datetime,
        benchmark_basis: str = "PRICE_INDEX",
    ) -> None:
        self._retrieved_at = retrieved_at
        self._benchmark_basis = benchmark_basis

    def map(self, source: Path, *, destination: Path | None = None) -> MappingResult:
        """Build a canonical pack without network access or source mutation."""
        if destination is None:
            raise ValueError("NSE public mapping requires an explicit destination")
        result = build_public_dataset(
            inspect_drop(source),
            destination=destination,
            retrieved_at=self._retrieved_at,
            benchmark_basis=self._benchmark_basis,
        )
        report = canonical_preflight(result.canonical_manifest)
        return MappingResult(
            mapper_id=self.mapper_id,
            mapping_revision=self.mapping_revision,
            manifest_path=result.canonical_manifest,
            import_decision=report.import_decision,
            dataset_fingerprint=report.dataset_fingerprint,
        )


@dataclass(slots=True)
class _IdentityRevision:
    key: str
    symbol: str
    company_name: str
    isin: str | None
    security_id: str | None
    first: date
    last: date


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source_key(*, isin: str | None, security_id: str | None, symbol: str, series: str) -> str:
    if isin:
        return f"isin-{isin}"
    if security_id:
        return f"nse-{security_id}"
    return f"unresolved-{symbol.lower()}-{series.lower()}"


def _row(**values: object) -> dict[str, str]:
    return {key: "" if value is None else str(value) for key, value in values.items()}


def _write_csv(path: Path, role: str, rows: list[dict[str, str]]) -> tuple[int, str, int]:
    _schema, columns = CANONICAL_SCHEMAS[role]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    content = path.read_bytes()
    return len(rows), hashlib.sha256(content).hexdigest(), len(content)


def _empty_destination(destination: Path) -> Path:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("public reconstruction destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("public reconstruction destination cannot be a symlink")
    return destination.resolve()


def _create_bar_store(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """CREATE TABLE bars (
        source_key TEXT NOT NULL, source_row_id TEXT NOT NULL, trading_date TEXT NOT NULL,
        symbol TEXT NOT NULL, series TEXT NOT NULL, isin TEXT, security_id TEXT,
        open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, close TEXT NOT NULL,
        volume INTEGER NOT NULL, turnover TEXT, source_format TEXT NOT NULL,
        PRIMARY KEY (source_key, trading_date))"""
    )
    connection.execute("CREATE INDEX ix_public_bars_date ON bars(trading_date, source_key)")
    connection.execute(
        """CREATE TABLE listed (
        source_key TEXT NOT NULL, snapshot_date TEXT NOT NULL, symbol TEXT NOT NULL,
        series TEXT NOT NULL, isin TEXT, security_id TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY (source_key, snapshot_date))"""
    )
    connection.execute("CREATE INDEX ix_public_listed_date ON listed(snapshot_date, source_key)")
    return connection


def _identity_index(
    inspections: tuple[PublicFileInspection, ...],
) -> tuple[dict[tuple[str, str], set[str]], dict[str, list[_IdentityRevision]]]:
    by_symbol: dict[tuple[str, str], set[str]] = defaultdict(set)
    revisions: dict[str, list[_IdentityRevision]] = defaultdict(list)
    security_files = sorted(
        (item for item in inspections if item.format is PublicFileFormat.NSE_CM_MII_SECURITY_V1),
        key=lambda item: item.original_filename,
    )
    for inspection in security_files:
        for security in iter_public_securities(inspection):
            key = _source_key(
                isin=security.isin,
                security_id=security.security_id,
                symbol=security.symbol,
                series=security.series,
            )
            by_symbol[(security.symbol, security.series)].add(key)
            candidates = revisions[key]
            if candidates and candidates[-1].symbol == security.symbol:
                candidates[-1].last = security.snapshot_date
            else:
                candidates.append(
                    _IdentityRevision(
                        key,
                        security.symbol,
                        security.company_name,
                        security.isin,
                        security.security_id,
                        security.listing_date or security.snapshot_date,
                        security.snapshot_date,
                    )
                )
    return by_symbol, revisions


def _resolved_bar_key(
    bar: PublicBar, by_symbol: dict[tuple[str, str], set[str]]
) -> tuple[str, str | None]:
    if bar.isin or bar.security_id:
        return (
            _source_key(
                isin=bar.isin,
                security_id=bar.security_id,
                symbol=bar.symbol,
                series=bar.series,
            ),
            None,
        )
    matches = by_symbol.get((bar.symbol, bar.series), set())
    if len(matches) == 1:
        return next(iter(matches)), None
    if not matches:
        return _source_key(isin=None, security_id=None, symbol=bar.symbol, series=bar.series), (
            f"identity unresolved for {bar.symbol}:{bar.series}"
        )
    return _source_key(isin=None, security_id=None, symbol=bar.symbol, series=bar.series), (
        f"identity ambiguous for {bar.symbol}:{bar.series}"
    )


def _load_bars(
    connection: sqlite3.Connection,
    inspections: tuple[PublicFileInspection, ...],
    by_symbol: dict[tuple[str, str], set[str]],
    revisions: dict[str, list[_IdentityRevision]],
) -> tuple[tuple[date, ...], tuple[str, ...], int]:
    dates: set[date] = set()
    warnings: set[str] = set()
    count = 0
    for inspection in sorted(
        (item for item in inspections if item.format in _BAR_FORMATS),
        key=lambda item: item.original_filename,
    ):
        batch: list[tuple[object, ...]] = []
        for bar in iter_public_bars(inspection):
            key, warning = _resolved_bar_key(bar, by_symbol)
            if warning:
                warnings.add(warning)
            dates.add(bar.trading_date)
            existing = revisions[key]
            if existing and existing[-1].symbol == bar.symbol:
                existing[-1].first = min(existing[-1].first, bar.trading_date)
                existing[-1].last = max(existing[-1].last, bar.trading_date)
            else:
                existing.append(
                    _IdentityRevision(
                        key,
                        bar.symbol,
                        bar.company_name or bar.symbol,
                        bar.isin,
                        bar.security_id,
                        bar.trading_date,
                        bar.trading_date,
                    )
                )
            batch.append(
                (
                    key,
                    bar.source_row_id,
                    bar.trading_date.isoformat(),
                    bar.symbol,
                    bar.series,
                    bar.isin,
                    bar.security_id,
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    bar.volume,
                    None if bar.turnover is None else str(bar.turnover),
                    bar.source_format.value,
                )
            )
            if len(batch) >= _BAR_BATCH_SIZE:
                _insert_bar_batch(connection, batch)
                count += len(batch)
                batch.clear()
        _insert_bar_batch(connection, batch)
        count += len(batch)
        connection.commit()
    return tuple(sorted(dates)), tuple(sorted(warnings)), count


def _load_listed_store(
    connection: sqlite3.Connection, inspections: tuple[PublicFileInspection, ...]
) -> int:
    count = 0
    for inspection in sorted(
        (item for item in inspections if item.format is PublicFileFormat.NSE_CM_MII_SECURITY_V1),
        key=lambda item: item.original_filename,
    ):
        batch: list[tuple[object, ...]] = []
        for security in iter_public_securities(inspection):
            key = _source_key(
                isin=security.isin,
                security_id=security.security_id,
                symbol=security.symbol,
                series=security.series,
            )
            batch.append(
                (
                    key,
                    security.snapshot_date.isoformat(),
                    security.symbol,
                    security.series,
                    security.isin,
                    security.security_id,
                    security.status,
                )
            )
            if len(batch) >= _BAR_BATCH_SIZE:
                connection.executemany("INSERT INTO listed VALUES (?,?,?,?,?,?,?)", batch)
                count += len(batch)
                batch.clear()
        try:
            connection.executemany("INSERT INTO listed VALUES (?,?,?,?,?,?,?)", batch)
        except sqlite3.IntegrityError as error:
            raise ValueError("conflicting listed-security snapshot rows") from error
        count += len(batch)
        connection.commit()
    return count


def _insert_bar_batch(connection: sqlite3.Connection, batch: list[tuple[object, ...]]) -> None:
    try:
        connection.executemany(
            "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch,
        )
    except sqlite3.IntegrityError as error:
        raise ValueError(
            "duplicate or revised bhavcopy date requires explicit source selection"
        ) from error


def _identity_rows(
    revisions: dict[str, list[_IdentityRevision]], retrieved: str, revision: str
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for key, items in sorted(revisions.items()):
        ordered = sorted(items, key=lambda item: (item.first, item.symbol))
        compact: list[_IdentityRevision] = []
        for item in ordered:
            if compact and compact[-1].symbol == item.symbol and compact[-1].isin == item.isin:
                compact[-1].first = min(compact[-1].first, item.first)
                compact[-1].last = max(compact[-1].last, item.last)
            else:
                compact.append(item)
        for index, item in enumerate(compact):
            end = compact[index + 1].first - timedelta(days=1) if index + 1 < len(compact) else None
            token = item.security_id if item.security_id and item.security_id.isdigit() else None
            rows.append(
                _row(
                    source_row_id=f"identity-{hashlib.sha256(f'{key}:{item.symbol}:{item.first}'.encode()).hexdigest()[:24]}",
                    source_security_id=key,
                    instrument_kind="EQUITY",
                    canonical_symbol=item.symbol,
                    company_name=item.company_name,
                    isin=item.isin,
                    exchange="NSE",
                    provider_instrument_token=token,
                    exchange_token=token,
                    valid_from=item.first,
                    valid_to=end,
                    known_at=retrieved,
                    source_revision=revision,
                )
            )
    return rows


def _daily_bar_rows(
    connection: sqlite3.Connection, retrieved: str, revision: str, path: Path
) -> tuple[int, str, int]:
    _schema, columns = CANONICAL_SCHEMAS["daily_bars"]
    count = 0
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        cursor = connection.execute(
            "SELECT source_key,source_row_id,trading_date,open,high,low,close,volume "
            "FROM bars ORDER BY source_key,trading_date"
        )
        for key, row_id, on, open_, high, low, close, volume in cursor:
            writer.writerow(
                _row(
                    source_row_id=row_id,
                    source_security_id=key,
                    instrument_kind="CASH_EQUITY",
                    trading_date=on,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    currency="INR",
                    exchange="NSE",
                    known_at=retrieved,
                    adjustment_status="RAW",
                    source_revision=revision,
                )
            )
            count += 1
    content = path.read_bytes()
    return count, hashlib.sha256(content).hexdigest(), len(content)


def _security_observation_rows(
    connection: sqlite3.Connection, sessions: tuple[date, ...], path: Path
) -> int:
    columns = (
        "source_security_id",
        "observation_date",
        "state",
        "symbol",
        "series",
        "isin",
        "final_observed_date",
        "disappearance_state",
    )
    keys = tuple(
        row[0]
        for row in connection.execute(
            "SELECT source_key FROM bars UNION SELECT source_key FROM listed ORDER BY source_key"
        )
    )
    final = dict(
        connection.execute(
            "SELECT source_key,MAX(observed) FROM ("
            "SELECT source_key,trading_date AS observed FROM bars UNION ALL "
            "SELECT source_key,snapshot_date AS observed FROM listed) GROUP BY source_key"
        )
    )
    latest: dict[str, tuple[str, str, str | None, str]] = {}
    count = 0
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for session in sessions:
            on = session.isoformat()
            listed = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT source_key,symbol,series,isin,status FROM listed "
                    "WHERE snapshot_date=? ORDER BY source_key",
                    (on,),
                )
            }
            traded = {
                row[0]: row[1:]
                for row in connection.execute(
                    "SELECT source_key,symbol,series,isin FROM bars "
                    "WHERE trading_date=? ORDER BY source_key",
                    (on,),
                )
            }
            latest.update(listed)
            for key in keys:
                listed_row = listed.get(key)
                traded_row = traded.get(key)
                reference = listed_row or latest.get(key)
                status = "" if reference is None else str(reference[3]).upper()
                if "DELIST" in status:
                    state = "DELISTED"
                elif "SUSP" in status:
                    state = "SUSPENDED_IF_KNOWN"
                elif traded_row is not None:
                    state = "OBSERVED_TRADED"
                elif listed_row is not None:
                    state = "OBSERVED_LISTED"
                elif reference is not None and on <= final[key]:
                    state = "INFERRED_LISTED"
                else:
                    state = "NOT_OBSERVED"
                identity = traded_row or reference
                disappearance = ""
                if session == sessions[-1] and on > final[key]:
                    disappearance = "NO_LONGER_OBSERVED"
                if "DELIST" in status:
                    disappearance = "CONFIRMED_DELISTED"
                writer.writerow(
                    _row(
                        source_security_id=key,
                        observation_date=on,
                        state=state,
                        symbol="UNKNOWN" if identity is None else identity[0],
                        series="UNKNOWN" if identity is None else identity[1],
                        isin=None if identity is None else identity[2],
                        final_observed_date=final[key],
                        disappearance_state=disappearance,
                    )
                )
                count += 1
    return count


def _membership_rows(
    connection: sqlite3.Connection,
    sessions: tuple[date, ...],
    retrieved: str,
    revision: str,
    rule: LiquidityUniverseRule,
) -> list[dict[str, str]]:
    histories: dict[str, deque[tuple[Decimal | None, Decimal | None, str]]] = defaultdict(
        lambda: deque(maxlen=rule.lookback_sessions)
    )
    starts: dict[str, date] = {}
    periods: list[tuple[str, date, date]] = []
    for cutoff in sessions:
        day_rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT source_key,turnover,close,series,volume FROM bars "
                "WHERE trading_date=? ORDER BY source_key",
                (cutoff.isoformat(),),
            )
        }
        keys = sorted(set(histories) | set(day_rows))
        for key in keys:
            current = day_rows.get(key)
            turnover: Decimal | None = None
            close: Decimal | None = None
            series = histories[key][-1][2] if histories[key] else ""
            if current is not None:
                turnover_raw, close_raw, series, volume = current
                close = Decimal(close_raw)
                turnover = Decimal(turnover_raw) if turnover_raw else close * int(volume)
            histories[key].append((turnover, close, series))
            history = histories[key]
            turnovers = sorted(item[0] for item in history if item[0] is not None)
            median = turnovers[len(turnovers) // 2] if turnovers else None
            latest_close = next(
                (item[1] for item in reversed(history) if item[1] is not None), None
            )
            eligible = (
                len(history) >= rule.warmup_sessions
                and len(turnovers) >= rule.minimum_traded_sessions
                and median is not None
                and median >= rule.minimum_median_rupee_turnover
                and latest_close is not None
                and latest_close >= rule.minimum_close
                and series in rule.eligible_series
            )
            if eligible and key not in starts:
                starts[key] = cutoff
            elif not eligible and key in starts:
                periods.append((key, starts.pop(key), cutoff - timedelta(days=1)))
    if sessions:
        periods.extend((key, start, sessions[-1]) for key, start in starts.items())
    periods.sort()
    return [
        _row(
            source_row_id=f"membership-{index:08d}",
            universe_id=PUBLIC_LIQUID_NSE_UNIVERSE_ID,
            source_security_id=key,
            effective_from=start,
            effective_to=end,
            known_at=retrieved,
            source_revision=revision,
            membership_reason="LIQUIDITY_ELIGIBLE",
            is_delisted="false",
        )
        for index, (key, start, end) in enumerate(periods, 1)
    ]


def _action_rows(
    inspections: tuple[PublicFileInspection, ...],
    by_symbol: dict[tuple[str, str], set[str]],
    retrieved: str,
    revision: str,
) -> tuple[list[dict[str, str]], list[dict[str, object]], tuple[str, ...]]:
    canonical: list[dict[str, str]] = []
    raw: list[dict[str, object]] = []
    warnings: list[str] = []
    for inspection in sorted(
        (item for item in inspections if item.format is PublicFileFormat.NSE_CORPORATE_ACTIONS_V1),
        key=lambda item: item.original_filename,
    ):
        for action in iter_public_actions(inspection):
            raw.append({**asdict(action), "source_file_sha256": inspection.sha256})
            matches = by_symbol.get((action.symbol, action.series), set())
            if len(matches) != 1:
                warnings.append(f"action identity unresolved: {action.symbol}:{action.series}")
                continue
            if action.event_type in {"SPLIT", "BONUS"} and action.ratio_numerator is None:
                warnings.append(f"action ratio unresolved: {action.symbol}:{action.ex_date}")
                continue
            key = next(iter(matches))
            canonical.append(
                _row(
                    source_row_id=action.source_row_id,
                    source_security_id=key,
                    event_type=action.event_type,
                    effective_date=action.ex_date,
                    ex_date=action.ex_date,
                    record_date=action.record_date,
                    known_at=retrieved,
                    ratio_numerator=action.ratio_numerator,
                    ratio_denominator=action.ratio_denominator,
                    cash_value=action.cash_value,
                    currency="INR" if action.cash_value is not None else None,
                    related_source_security_id=None,
                    verification="SOURCE_REPORTED",
                    source_revision=revision,
                )
            )
    canonical.sort(
        key=lambda item: (item["source_security_id"], item["effective_date"], item["source_row_id"])
    )
    raw.sort(key=lambda item: str(item["source_row_id"]))
    return canonical, raw, tuple(sorted(set(warnings)))


def _benchmark_rows(
    inspections: tuple[PublicFileInspection, ...],
    *,
    basis: str,
    retrieved: str,
    revision: str,
) -> list[dict[str, str]]:
    desired = (
        PublicFileFormat.NSE_NIFTY_PRICE_INDEX_V1
        if basis == "PRICE_INDEX"
        else PublicFileFormat.NSE_NIFTY_TRI_V1
    )
    rows: list[dict[str, str]] = []
    for inspection in sorted(
        (item for item in inspections if item.format is desired),
        key=lambda item: item.original_filename,
    ):
        for bar in iter_public_benchmarks(inspection):
            rows.append(
                _row(
                    source_row_id=bar.source_row_id,
                    benchmark_id=bar.benchmark_id,
                    canonical_symbol="NIFTY 50" if basis == "PRICE_INDEX" else "NIFTY 50 TRI",
                    trading_date=bar.trading_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    currency="INR",
                    exchange="NSE",
                    known_at=retrieved,
                    benchmark_basis=basis,
                    adjustment_status="RAW",
                    source_revision=revision,
                )
            )
    rows.sort(key=lambda item: (item["benchmark_id"], item["trading_date"]))
    if not rows:
        raise ValueError(f"selected benchmark basis {basis} has no supported source file")
    return rows


def _file_entry(role: str, path: Path, count: int, digest: str, size: int) -> dict[str, object]:
    return {
        "role": role,
        "path": path.name,
        "sha256": digest,
        "size_bytes": size,
        "row_count": count,
        "schema": CANONICAL_SCHEMAS[role][0],
        "media_type": "text/csv",
    }


def build_public_dataset(  # noqa: PLR0915 - one atomic file-to-file reconstruction
    inspections: tuple[PublicFileInspection, ...],
    *,
    destination: Path,
    retrieved_at: datetime,
    benchmark_basis: str = "PRICE_INDEX",
    rule: LiquidityUniverseRule = _DEFAULT_LIQUIDITY_RULE,
) -> PublicBuildResult:
    """Reconstruct and map public files into the existing transactional contract."""
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() != timedelta(0):
        raise ValueError("retrieved_at must be a UTC instant")
    if benchmark_basis not in {"PRICE_INDEX", "TOTAL_RETURN_INDEX"}:
        raise ValueError("benchmark basis must be PRICE_INDEX or TOTAL_RETURN_INDEX")
    preflight = public_preflight(inspections)
    if preflight.unsupported_files or preflight.revised_files or not inspections:
        raise ValueError("public drop is not buildable; inspect deterministic preflight blockers")
    root = _empty_destination(destination)
    fingerprint = reconstruction_fingerprint(inspections)
    revision = f"public-{fingerprint[:24]}"
    retrieved = _iso(retrieved_at)
    by_symbol, identities = _identity_index(inspections)
    store_path = root / ".public-reconstruction.sqlite3"
    connection = _create_bar_store(store_path)
    try:
        sessions, identity_warnings, bar_count = _load_bars(
            connection, inspections, by_symbol, identities
        )
        listed_count = _load_listed_store(connection, inspections)
        if not sessions:
            raise ValueError("no supported cash-equity bars were reconstructed")
        identity_rows = _identity_rows(identities, retrieved, revision)
        if any(item["source_security_id"].startswith("unresolved-") for item in identity_rows):
            raise ValueError("unresolved/ambiguous bar identities fail closed")
        membership_rows = _membership_rows(connection, sessions, retrieved, revision, rule)
        action_rows, raw_actions, action_warnings = _action_rows(
            inspections, by_symbol, retrieved, revision
        )
        benchmark_rows = _benchmark_rows(
            inspections,
            basis=benchmark_basis,
            retrieved=retrieved,
            revision=revision,
        )
        benchmark_rows = [
            item
            for item in benchmark_rows
            if sessions[0].isoformat() <= item["trading_date"] <= sessions[-1].isoformat()
        ]
        if not benchmark_rows:
            raise ValueError("selected benchmark has no rows inside cash-equity coverage")
        definition_rows = [
            _row(
                source_row_id="public-liquid-nse-universe-v0",
                universe_id=PUBLIC_LIQUID_NSE_UNIVERSE_ID,
                label="PUBLIC RECONSTRUCTED HISTORICAL UNIVERSE - Liquid NSE v0",
                kind="LIQUIDITY_FILTERED_UNIVERSE",
                known_at=retrieved,
                source_revision=revision,
                historical_membership_available="true",
                removals_included="true",
                delistings_included="false",
                pit_known_at_available="false",
                instrument_lifecycle_available=str(preflight.security_snapshots_present).lower(),
                corporate_action_coverage_available=str(
                    preflight.corporate_actions_present
                ).lower(),
                licensing_confirmed="true",
                return_basis="RAW_PRICE",
                benchmark_basis=benchmark_basis,
                benchmark_history_available="true",
            )
        ]
        roles = {
            "instrument_identities": identity_rows,
            "universe_definitions": definition_rows,
            "universe_memberships": membership_rows,
            "corporate_actions": action_rows,
            "benchmark_bars": benchmark_rows,
        }
        file_entries: list[dict[str, object]] = []
        counts: dict[str, int] = {"daily_bars": bar_count}
        for role, rows in roles.items():
            path = root / f"{role}.csv"
            count, digest, size = _write_csv(path, role, rows)
            file_entries.append(_file_entry(role, path, count, digest, size))
            counts[role] = count
        daily_path = root / "daily_bars.csv"
        daily_count, daily_digest, daily_size = _daily_bar_rows(
            connection, retrieved, revision, daily_path
        )
        file_entries.append(
            _file_entry("daily_bars", daily_path, daily_count, daily_digest, daily_size)
        )
        raw_actions_path = root / "public-action-evidence.jsonl"
        raw_actions_path.write_bytes(
            b"".join(canonical_json_bytes(item) + b"\n" for item in raw_actions)
        )
        observations_path = root / "security-observations.csv"
        observation_count = _security_observation_rows(connection, sessions, observations_path)
        counts["listed_security_snapshot_rows"] = listed_count
        counts["security_observations"] = observation_count
        file_entries.sort(key=lambda item: str(item["role"]))
        source_files = [
            {
                "original_filename": item.original_filename,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "source_schema_revision": item.source_schema_revision,
                "format": item.format.value,
                "acquisition": "MANUAL_DROP",
                "retrieved_at": retrieved,
                "source_url_identifier": (
                    "https://www.nseindia.com/static/regulations/segment-wise-historical-reports"
                    if item.format is PublicFileFormat.NSE_EXCHANGE_MONTHLY_TRANSACTION_V1
                    else "https://www.nseindia.com/all-reports"
                ),
            }
            for item in sorted(inspections, key=lambda value: value.original_filename)
        ]
        reconstruction = {
            "schema": PUBLIC_MANIFEST_SCHEMA,
            "dataset_id": f"public-nse-{fingerprint[:16]}",
            "evidence_class": EvidenceClass.PUBLIC_RECONSTRUCTED.value,
            "source_family": "NSE_PUBLIC_REPORTS",
            "reconstruction_revision": PUBLIC_RECONSTRUCTION_REVISION,
            "mapper_revision": "nse_public_offline_mapper_v2",
            "identity_mapping_revision": revision,
            "universe_reconstruction_revision": rule.revision,
            "event_time_semantics": "SOURCE_FILE_DATE",
            "known_at_semantics": "RETRIEVED_LATER",
            "return_basis": "RAW_PRICE",
            "benchmark_basis": benchmark_basis,
            "source_files": source_files,
            "liquidity_rule": asdict(rule),
            "raw_action_evidence": raw_actions_path.name,
            "security_observations": {
                "path": observations_path.name,
                "row_count": observation_count,
                "sha256": hashlib.sha256(observations_path.read_bytes()).hexdigest(),
                "states": [
                    "OBSERVED_TRADED",
                    "OBSERVED_LISTED",
                    "INFERRED_LISTED",
                    "NOT_OBSERVED",
                    "DELISTED",
                    "SUSPENDED_IF_KNOWN",
                    "UNKNOWN",
                ],
            },
            "limitations": [
                "PUBLIC RECONSTRUCTED HISTORICAL UNIVERSE",
                "event-time reconstruction is not prospective known-at evidence",
                "not institutional-grade PIT or survivorship-safe",
                "raw bars are not adjusted and TRI is never fabricated",
                "disappearance is not treated as confirmed delisting",
            ],
        }
        reconstruction_path = root / "reconstruction-manifest.json"
        reconstruction_path.write_bytes(canonical_json_bytes(reconstruction) + b"\n")
        has_dividend = any(item.get("event_type") == "DIVIDEND" for item in action_rows)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "dataset_id": f"public-nse-{fingerprint[:16]}",
            "evidence_class": EvidenceClass.PUBLIC_RECONSTRUCTED.value,
            "provider": {
                "id": "nse_public",
                "name": "National Stock Exchange of India",
                "product": "Owner-retained public report reconstruction",
            },
            "license": {
                "reference": "SEBI-research-data-policy-and-NSE-public-report-pages",
                "status": "PUBLIC_RESEARCH_LOCAL_USE",
            },
            "acquired_at": retrieved,
            "coverage": {"start": sessions[0], "end": sessions[-1]},
            "source_revision": revision,
            "market": {
                "currency": "INR",
                "exchange": "NSE",
                "timezone": "Asia/Kolkata",
                "price_basis": "OHLCV",
                "adjustment_basis": "RAW",
                "return_basis": "RAW_PRICE",
                "benchmark_basis": benchmark_basis,
            },
            "known_at_semantics": "RETRIEVED_LATER",
            "import_policy": "APPEND_ONLY",
            "capabilities": {
                "historical_membership": True,
                "removals": True,
                "delistings": False,
                "inactive_securities": True,
                "instrument_lifecycle": preflight.security_snapshots_present,
                "corporate_actions": preflight.corporate_actions_present,
                "dividends": has_dividend,
                "publication_timestamps": False,
                "revision_history": False,
                "pit_known_at": False,
            },
            "files": file_entries,
            "notes": (
                "PUBLIC_RECONSTRUCTED; PARTIAL_PIT; DIAGNOSTIC_ONLY; "
                "raw files retained outside authoritative DB state"
            ),
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        warning_set = set(preflight.warnings) | set(identity_warnings) | set(action_warnings)
        warnings = tuple(sorted(warning_set))
        return PublicBuildResult(
            manifest_path,
            reconstruction_path,
            fingerprint,
            tuple(sorted(counts.items())),
            warnings,
        )
    finally:
        connection.close()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{store_path}{suffix}")
            if candidate.exists():
                candidate.unlink()
