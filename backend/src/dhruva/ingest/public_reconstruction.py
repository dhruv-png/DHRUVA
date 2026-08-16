"""Deterministic public-history reconstruction, diagnostics, and planning."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from dhruva.ingest.public_exchange import (
    DisappearanceState,
    ObservationState,
    PublicBar,
    PublicFileFormat,
    PublicFileInspection,
    PublicSecurity,
    inspect_public_file,
)

__all__ = [
    "PUBLIC_LIQUID_NSE_UNIVERSE_ID",
    "PUBLIC_RECONSTRUCTION_REVISION",
    "AcquisitionPlan",
    "BiasFinding",
    "BiasSeverity",
    "LiquidityMembership",
    "LiquidityUniverseRule",
    "PublicPreflight",
    "SecurityObservation",
    "build_acquisition_plan",
    "build_bias_report",
    "canonical_json_bytes",
    "compute_liquidity_memberships",
    "inspect_drop",
    "public_preflight",
    "reconstruct_security_observations",
]

PUBLIC_LIQUID_NSE_UNIVERSE_ID: Final = "public-liquid-nse-v0"
PUBLIC_RECONSTRUCTION_REVISION: Final = "public_exchange_reconstruction_v0"
PUBLIC_PREFLIGHT_SCHEMA: Final = "dhruva.public-data-preflight.v1"
PUBLIC_BIAS_SCHEMA: Final = "dhruva.public-reconstruction-bias.v1"
PUBLIC_PLAN_SCHEMA: Final = "dhruva.public-acquisition-plan.v1"
_ALLOWED_SUFFIXES = (".csv", ".csv.gz", ".zip", ".gz")
_DAYS_IN_WORK_WEEK = 5
_LONG_DATE_DIGITS = 8
_SHORT_DATE_DIGITS = 6
_MIN_DUPLICATE_FILES = 2


@dataclass(frozen=True, slots=True)
class LiquidityUniverseRule:
    """Frozen v0 rules, evaluated after each cutoff close using no future row."""

    universe_id: str = PUBLIC_LIQUID_NSE_UNIVERSE_ID
    revision: str = "public_liquid_nse_universe_v0"
    eligible_series: tuple[str, ...] = ("BE", "EQ")
    lookback_sessions: int = 60
    warmup_sessions: int = 60
    minimum_traded_sessions: int = 48
    minimum_median_rupee_turnover: Decimal = Decimal("10000000")
    minimum_close: Decimal = Decimal("10")


@dataclass(frozen=True, slots=True)
class LiquidityMembership:
    """One cutoff decision and the exact trailing evidence."""

    source_security_key: str
    cutoff: date
    eligible: bool
    observed_sessions: int
    traded_sessions: int
    median_rupee_turnover: Decimal | None
    close: Decimal | None
    reason: str


@dataclass(frozen=True, slots=True)
class SecurityObservation:
    """Conservative instrument/date lifecycle observation."""

    source_security_key: str
    on: date
    state: ObservationState
    symbol: str
    series: str
    isin: str | None
    final_observed_date: date
    disappearance: DisappearanceState | None


@dataclass(frozen=True, slots=True)
class PublicPreflight:
    """Public-drop readiness; it never fills a missing archive date."""

    schema: str
    reconstruction_revision: str
    file_count: int
    formats: tuple[tuple[str, int], ...]
    coverage_start: str
    coverage_end: str
    duplicate_files: tuple[str, ...]
    revised_files: tuple[str, ...]
    unsupported_files: tuple[str, ...]
    archive_gap_weekdays: tuple[str, ...]
    corporate_actions_present: bool
    benchmark_price_present: bool
    benchmark_tri_present: bool
    security_snapshots_present: bool
    known_at_semantics: str
    evidence_class: str
    readiness: str
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    def export_bytes(self) -> bytes:
        """Return canonical JSON bytes."""
        return canonical_json_bytes(asdict(self)) + b"\n"


class BiasSeverity(StrEnum):
    """Ordinal limitation severity; no fake aggregate confidence exists."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BiasFinding:
    """One scientific limitation and why it received its severity."""

    risk: str
    severity: BiasSeverity
    basis: str


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    """Network-free manual acquisition inventory."""

    schema: str
    source: str
    from_date: str
    to_date: str
    expected_weekday_sessions: int
    expected_file_counts: tuple[tuple[str, int], ...]
    approximate_disk_bytes: int | None
    present_files: int
    missing_expected_names: tuple[str, ...]
    automation_status: str
    terms_review_required: bool
    instructions: tuple[str, ...]
    limitations: tuple[str, ...]

    def export_bytes(self) -> bytes:
        """Return canonical JSON bytes."""
        return canonical_json_bytes(asdict(self)) + b"\n"


def canonical_json_bytes(value: object) -> bytes:
    """Encode deterministic reports."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda item: item.value if isinstance(item, StrEnum) else str(item),
    ).encode()


def inspect_drop(root: Path) -> tuple[PublicFileInspection, ...]:
    """Recursively inspect only recognized local archive extensions."""
    resolved = root.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("drop root must be a regular directory")
    inspections: list[PublicFileInspection] = []
    for path in sorted(item for item in resolved.rglob("*") if item.is_file()):
        lower = path.name.lower()
        if not any(lower.endswith(suffix) for suffix in _ALLOWED_SUFFIXES):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(resolved):
            raise ValueError("drop contains a symlink or path escape")
        inspections.append(inspect_public_file(path))
    return tuple(inspections)


def _security_key(bar: PublicBar) -> str:
    if bar.isin:
        return f"isin:{bar.isin}"
    if bar.security_id:
        return f"nse:{bar.security_id}"
    return f"unresolved:{bar.symbol}:{bar.series}"


def compute_liquidity_memberships(  # noqa: PLR0912 - reasons remain explicit
    bars: tuple[PublicBar, ...],
    *,
    expected_sessions: tuple[date, ...],
    rule: LiquidityUniverseRule | None = None,
) -> tuple[LiquidityMembership, ...]:
    """Compute v0 membership from data observable no later than each cutoff."""
    rule = rule or LiquidityUniverseRule()
    sessions = tuple(sorted(set(expected_sessions)))
    if not sessions or sessions != expected_sessions:
        raise ValueError("expected sessions must be sorted and unique")
    session_set = set(sessions)
    grouped: dict[str, dict[date, PublicBar]] = defaultdict(dict)
    for input_bar in bars:
        if input_bar.trading_date not in session_set:
            raise ValueError("bar date is absent from expected sessions")
        key = _security_key(input_bar)
        if input_bar.trading_date in grouped[key]:
            raise ValueError(f"duplicate bar for {key} on {input_bar.trading_date}")
        grouped[key][input_bar.trading_date] = input_bar
    histories: dict[str, deque[tuple[date, Decimal | None, Decimal | None, str]]] = {
        key: deque(maxlen=rule.lookback_sessions) for key in grouped
    }
    results: list[LiquidityMembership] = []
    for cutoff in sessions:
        for key in sorted(grouped):
            day_bar = grouped[key].get(cutoff)
            turnover = None
            close = None
            series = ""
            if day_bar is not None:
                turnover = (
                    day_bar.turnover
                    if day_bar.turnover is not None
                    else day_bar.close * day_bar.volume
                )
                close = day_bar.close
                series = day_bar.series
            elif histories[key]:
                series = histories[key][-1][3]
            histories[key].append((cutoff, turnover, close, series))
            history = histories[key]
            traded = tuple(item for item in history if item[1] is not None)
            turnovers = tuple(item[1] for item in traded if item[1] is not None)
            median = None if not turnovers else Decimal(str(statistics.median(turnovers)))
            latest_close = next(
                (item[2] for item in reversed(history) if item[2] is not None), None
            )
            eligible = (
                len(history) >= rule.warmup_sessions
                and len(traded) >= rule.minimum_traded_sessions
                and median is not None
                and median >= rule.minimum_median_rupee_turnover
                and latest_close is not None
                and latest_close >= rule.minimum_close
                and series in rule.eligible_series
            )
            reasons: list[str] = []
            if len(history) < rule.warmup_sessions:
                reasons.append("WARMUP")
            if len(traded) < rule.minimum_traded_sessions:
                reasons.append("PARTICIPATION")
            if median is None or median < rule.minimum_median_rupee_turnover:
                reasons.append("TURNOVER")
            if latest_close is None or latest_close < rule.minimum_close:
                reasons.append("PRICE_FLOOR")
            if series not in rule.eligible_series:
                reasons.append("SERIES")
            results.append(
                LiquidityMembership(
                    key,
                    cutoff,
                    eligible,
                    len(history),
                    len(traded),
                    median,
                    latest_close,
                    "ELIGIBLE" if eligible else "+".join(reasons),
                )
            )
    return tuple(results)


def reconstruct_security_observations(  # noqa: PLR0912 - states remain explicit
    securities: tuple[PublicSecurity, ...],
    bars: tuple[PublicBar, ...],
    *,
    expected_sessions: tuple[date, ...],
) -> tuple[SecurityObservation, ...]:
    """Separate listed, traded, suspended, delisted, and absent observations."""
    snapshots: dict[date, dict[str, PublicSecurity]] = defaultdict(dict)
    for item in securities:
        key = f"isin:{item.isin}" if item.isin else f"nse:{item.security_id}"
        existing = snapshots[item.snapshot_date].get(key)
        if existing is not None and (existing.symbol, existing.series) != (
            item.symbol,
            item.series,
        ):
            raise ValueError(f"conflicting identity within snapshot: {key}")
        snapshots[item.snapshot_date][key] = item
    traded: dict[date, dict[str, PublicBar]] = defaultdict(dict)
    for input_bar in bars:
        traded[input_bar.trading_date][_security_key(input_bar)] = input_bar
    all_keys = sorted(
        set().union(
            *(items.keys() for items in snapshots.values()),
            *(items.keys() for items in traded.values()),
        )
    )
    final_observed = {
        key: max(
            (
                *[day for day, values in snapshots.items() if key in values],
                *[day for day, values in traded.items() if key in values],
            )
        )
        for key in all_keys
    }
    latest_security: dict[str, PublicSecurity] = {}
    output: list[SecurityObservation] = []
    for on in expected_sessions:
        for key in all_keys:
            security = snapshots.get(on, {}).get(key)
            day_bar = traded.get(on, {}).get(key)
            if security is not None:
                latest_security[key] = security
            reference = security or latest_security.get(key)
            status = "" if reference is None else reference.status.upper()
            if "DELIST" in status:
                state = ObservationState.DELISTED
            elif "SUSP" in status:
                state = ObservationState.SUSPENDED_IF_KNOWN
            elif day_bar is not None:
                state = ObservationState.OBSERVED_TRADED
            elif security is not None:
                state = ObservationState.OBSERVED_LISTED
            elif reference is not None and on <= final_observed[key]:
                state = ObservationState.INFERRED_LISTED
            elif reference is None:
                state = ObservationState.UNKNOWN
            else:
                state = ObservationState.NOT_OBSERVED
            disappearance = None
            if on == expected_sessions[-1] and on > final_observed[key]:
                disappearance = DisappearanceState.NO_LONGER_OBSERVED
            if "DELIST" in status:
                disappearance = DisappearanceState.CONFIRMED_DELISTED
            if key.startswith("unresolved:"):
                disappearance = DisappearanceState.MAPPING_UNRESOLVED
            symbol = (
                day_bar.symbol
                if day_bar is not None
                else (reference.symbol if reference is not None else "UNKNOWN")
            )
            series = (
                day_bar.series
                if day_bar is not None
                else (reference.series if reference is not None else "UNKNOWN")
            )
            isin = (
                day_bar.isin
                if day_bar is not None
                else (reference.isin if reference is not None else None)
            )
            output.append(
                SecurityObservation(
                    key,
                    on,
                    state,
                    symbol,
                    series,
                    isin,
                    final_observed[key],
                    disappearance,
                )
            )
    return tuple(output)


def _inspection_date(item: PublicFileInspection) -> date | None:
    name = item.original_filename
    patterns = (r"(?<!\d)(\d{8})(?!\d)", r"(?<!\d)(\d{6})(?!\d)")
    for pattern in patterns:
        match = re.search(pattern, name)
        if match is None:
            continue
        raw = match.group(1)
        candidates = (
            (
                (int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
                if len(raw) == _LONG_DATE_DIGITS
                else None
            ),
            (
                (int(raw[4:]), int(raw[2:4]), int(raw[:2]))
                if len(raw) == _LONG_DATE_DIGITS
                else None
            ),
            (
                (2000 + int(raw[4:6]), int(raw[2:4]), int(raw[:2]))
                if len(raw) == _SHORT_DATE_DIGITS
                else None
            ),
        )
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                return date(*candidate)
            except ValueError:
                continue
    return None


def public_preflight(inspections: tuple[PublicFileInspection, ...]) -> PublicPreflight:
    """Classify local coverage, corrections, and archive gaps deterministically."""
    unsupported = tuple(
        sorted(
            item.original_filename
            for item in inspections
            if item.format is PublicFileFormat.UNKNOWN
        )
    )
    by_logical: dict[tuple[PublicFileFormat, date | None, str], list[PublicFileInspection]] = (
        defaultdict(list)
    )
    formats: dict[str, int] = defaultdict(int)
    dates: set[date] = set()
    for item in inspections:
        formats[item.format.value] += 1
        on = _inspection_date(item)
        if on is not None and item.format in {
            PublicFileFormat.NSE_CM_UDIFF_BHAVCOPY_V1,
            PublicFileFormat.NSE_CM_LEGACY_BHAVCOPY_V1,
            PublicFileFormat.NSE_FULL_BHAVCOPY_DELIVERABLE_V1,
        }:
            dates.add(on)
        stem = (
            item.original_filename.lower()
            .replace(".csv.gz", "")
            .replace(".csv", "")
            .replace(".zip", "")
            .replace(".gz", "")
        )
        by_logical[(item.format, on, stem)].append(item)
    duplicates: list[str] = []
    revisions: list[str] = []
    for items in by_logical.values():
        if len(items) < _MIN_DUPLICATE_FILES:
            continue
        hashes = {item.sha256 for item in items}
        names = ",".join(sorted(item.original_filename for item in items))
        (duplicates if len(hashes) == 1 else revisions).append(names)
    gaps: list[str] = []
    if dates:
        current = min(dates)
        while current <= max(dates):
            if current.weekday() < _DAYS_IN_WORK_WEEK and current not in dates:
                gaps.append(current.isoformat())
            current += timedelta(days=1)
    has_security = any(
        item.format is PublicFileFormat.NSE_CM_MII_SECURITY_V1 for item in inspections
    )
    has_action = any(
        item.format is PublicFileFormat.NSE_CORPORATE_ACTIONS_V1 for item in inspections
    )
    has_price = any(
        item.format is PublicFileFormat.NSE_NIFTY_PRICE_INDEX_V1 for item in inspections
    )
    has_tri = any(item.format is PublicFileFormat.NSE_NIFTY_TRI_V1 for item in inspections)
    blockers = list(unsupported)
    if revisions:
        blockers.append("corrected/revised source files require explicit selection")
    if not dates:
        blockers.append("no supported cash-equity bhavcopy dates")
    warnings = (
        ["weekday gaps may be exchange holidays; configured holiday evidence is absent"]
        if gaps
        else []
    )
    if not has_security:
        warnings.append("historical listed-security snapshots are absent")
    if not has_action:
        warnings.append("corporate-action evidence is absent")
    if not has_price:
        warnings.append("NIFTY 50 price-index history is absent")
    readiness = (
        "REJECTED" if unsupported else ("QUARANTINED" if blockers else "PUBLIC_RECONSTRUCTED")
    )
    return PublicPreflight(
        PUBLIC_PREFLIGHT_SCHEMA,
        PUBLIC_RECONSTRUCTION_REVISION,
        len(inspections),
        tuple(sorted(formats.items())),
        "" if not dates else min(dates).isoformat(),
        "" if not dates else max(dates).isoformat(),
        tuple(sorted(duplicates)),
        tuple(sorted(revisions)),
        unsupported,
        tuple(gaps),
        has_action,
        has_price,
        has_tri,
        has_security,
        "RETRIEVED_LATER",
        "PUBLIC_RECONSTRUCTED",
        readiness,
        tuple(sorted(blockers)),
        tuple(sorted(warnings)),
    )


def build_bias_report(preflight: PublicPreflight) -> tuple[BiasFinding, ...]:
    """Assess named risks from explicit preflight evidence."""
    has_gaps = bool(preflight.archive_gap_weekdays)
    has_security = preflight.security_snapshots_present
    has_actions = preflight.corporate_actions_present
    findings = (
        BiasFinding(
            "survivorship risk",
            BiasSeverity.MEDIUM if has_security else BiasSeverity.HIGH,
            "public snapshots/removals are partial",
        ),
        BiasFinding(
            "universe-selection bias",
            BiasSeverity.MEDIUM,
            "frozen trailing liquidity replaces unavailable PIT index membership",
        ),
        BiasFinding(
            "delisting bias",
            BiasSeverity.MEDIUM if has_security else BiasSeverity.HIGH,
            "disappearance is retained but not treated as confirmed delisting",
        ),
        BiasFinding(
            "look-ahead risk",
            BiasSeverity.HIGH,
            "files were retrieved later; event-time is reconstructed",
        ),
        BiasFinding(
            "corporate-action risk",
            BiasSeverity.MEDIUM if has_actions else BiasSeverity.HIGH,
            "public action archive completeness is unproven",
        ),
        BiasFinding(
            "identifier continuity risk",
            BiasSeverity.MEDIUM if has_security else BiasSeverity.HIGH,
            "ISIN/security snapshots may not span the full archive",
        ),
        BiasFinding(
            "missing-data risk",
            BiasSeverity.HIGH if has_gaps else BiasSeverity.MEDIUM,
            "archive gaps are never silently filled",
        ),
        BiasFinding(
            "benchmark-return-basis risk",
            BiasSeverity.LOW if preflight.benchmark_tri_present else BiasSeverity.MEDIUM,
            "price index and TRI remain separate",
        ),
        BiasFinding(
            "archive-reconstruction risk",
            BiasSeverity.HIGH,
            "not prospectively archived by DHRUVA at event time",
        ),
        BiasFinding(
            "revision-history risk",
            BiasSeverity.UNKNOWN,
            "official revision history is not established",
        ),
    )
    return tuple(sorted(findings, key=lambda item: item.risk))


def build_acquisition_plan(
    *, source: str, from_date: date, to_date: date, local_root: Path | None = None
) -> AcquisitionPlan:
    """Plan manual files using weekday upper bounds; perform no network request."""
    if source.lower() != "nse":
        raise ValueError("only the reviewed NSE manual plan is supported")
    if to_date < from_date:
        raise ValueError("plan date range is reversed")
    sessions: list[date] = []
    current = from_date
    while current <= to_date:
        if current.weekday() < _DAYS_IN_WORK_WEEK:
            sessions.append(current)
        current += timedelta(days=1)
    present = 0
    present_names: set[str] = set()
    if local_root is not None and local_root.exists():
        present_names = {item.name.lower() for item in local_root.rglob("*") if item.is_file()}
        present = len(present_names)
    expected_names = tuple(f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip" for day in sessions)
    missing = tuple(name for name in expected_names if name.lower() not in present_names)
    return AcquisitionPlan(
        PUBLIC_PLAN_SCHEMA,
        "nse",
        from_date.isoformat(),
        to_date.isoformat(),
        len(sessions),
        (
            ("cash_bhavcopy", len(sessions)),
            ("security_master_snapshot", len(sessions)),
            ("corporate_action_exports", 1),
            ("nifty50_price_index", 1),
            ("nifty50_tri", 1),
        ),
        None,
        present,
        missing,
        "AUTOMATION_UNCLEAR",
        True,
        (
            "Use official NSE report pages in a normal browser and download files manually.",
            "Retain original filenames and bytes under an ignored local data-drop directory.",
            "Run inspect-drop and preflight before reconstruction or authoritative import.",
        ),
        (
            "weekday count is an upper bound because reviewed holiday/special-session "
            "files were not supplied",
            "historical public archive depth and endpoint automation permission are not assumed",
            "disk size is UNKNOWN until owner samples establish defensible averages",
        ),
    )


def reconstruction_fingerprint(inspections: tuple[PublicFileInspection, ...]) -> str:
    """Hash immutable source identities and mapper revision."""
    payload = (
        PUBLIC_RECONSTRUCTION_REVISION
        + "\n"
        + "\n".join(
            f"{item.format.value}:{item.original_filename}:{item.sha256}"
            for item in sorted(inspections, key=lambda value: value.original_filename)
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()
