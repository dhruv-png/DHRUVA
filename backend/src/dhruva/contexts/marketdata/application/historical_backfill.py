"""Deterministic, bounded plans for explicit historical daily-bar acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.contexts.marketdata.domain.daily_bars import DailyHistoryRequest, MarketInstrumentKind
from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from datetime import date

    from dhruva.shared.identity import InstrumentId

FEATURE_SESSION_THRESHOLDS = (20, 60, 120, 200, 252)
OPERATIONAL_TARGET_SESSIONS = 252
EVALUATION_TARGET_SESSIONS = 2000
BACKFILL_CHUNK_CALENDAR_DAYS = 365
BACKFILL_OVERLAP_CALENDAR_DAYS = 7
MAX_BACKFILL_CALENDAR_DAYS = 3660


class BackfillUniverseRole(StrEnum):
    """Why an instrument is present in a historical acquisition plan."""

    OWNER_WATCHLIST = "OWNER_WATCHLIST"
    BENCHMARK = "BENCHMARK"


class BenchmarkReturnBasis(StrEnum):
    """Truthful return semantics for an index observation."""

    PRICE_INDEX = "PRICE_INDEX"
    TOTAL_RETURN_INDEX = "TOTAL_RETURN_INDEX"


@dataclass(frozen=True, slots=True)
class BackfillInstrument:
    """One mapped provider-neutral target with its current coverage frontier."""

    instrument_id: InstrumentId
    canonical_symbol: str
    instrument_kind: MarketInstrumentKind
    source_instrument_id: int
    role: BackfillUniverseRole
    earliest_stored: date | None


@dataclass(frozen=True, slots=True)
class HistoricalBackfillChunk:
    """One independently atomic, sequential provider operation."""

    ordinal: int
    target_instrument_id: InstrumentId
    target_symbol: str
    role: BackfillUniverseRole
    core_from: date
    core_to: date
    request_from: date
    request_to: date
    requests: tuple[DailyHistoryRequest, ...]


@dataclass(frozen=True, slots=True)
class HistoricalBackfillPlan:
    """A stable ordered plan derived entirely from local coverage."""

    target_from: date
    completed_through: date
    benchmark_id: InstrumentId
    benchmark_basis: BenchmarkReturnBasis
    chunks: tuple[HistoricalBackfillChunk, ...]
    unresolved_symbols: tuple[str, ...] = ()

    @property
    def provider_requests(self) -> int:
        """Return the exact number of sequential provider fetches."""
        return sum(len(chunk.requests) for chunk in self.chunks)


def plan_historical_backfill(  # noqa: PLR0913 - each keyword is a distinct plan axis
    *,
    target_from: date,
    completed_through: date,
    benchmark: BackfillInstrument,
    benchmark_basis: BenchmarkReturnBasis,
    instruments: tuple[BackfillInstrument, ...],
    unresolved_symbols: tuple[str, ...] = (),
) -> HistoricalBackfillPlan:
    """Plan newest-to-oldest chunks; identical inputs produce identical order."""
    span = (completed_through - target_from).days
    if span < 0:
        raise ValidationError("historical target starts after the completed cutoff")
    if span > MAX_BACKFILL_CALENDAR_DAYS:
        raise ValidationError("historical target exceeds the ten-year safety bound")
    if benchmark.role is not BackfillUniverseRole.BENCHMARK:
        raise ValidationError("historical plan benchmark has the wrong universe role")
    if benchmark.instrument_kind is not MarketInstrumentKind.INDEX:
        raise ValidationError("historical plan benchmark must be an index")

    targets = (benchmark, *sorted(instruments, key=lambda item: item.canonical_symbol))
    chunks: list[HistoricalBackfillChunk] = []
    for target in targets:
        missing_to = (
            completed_through
            if target.earliest_stored is None
            else min(completed_through, target.earliest_stored - timedelta(days=1))
        )
        while missing_to >= target_from:
            core_from = max(
                target_from,
                missing_to - timedelta(days=BACKFILL_CHUNK_CALENDAR_DAYS - 1),
            )
            request_to = min(
                completed_through,
                missing_to + timedelta(days=BACKFILL_OVERLAP_CALENDAR_DAYS),
            )
            benchmark_request = _request(benchmark, core_from, request_to)
            requests = (
                (benchmark_request,)
                if target.instrument_id == benchmark.instrument_id
                else (benchmark_request, _request(target, core_from, request_to))
            )
            chunks.append(
                HistoricalBackfillChunk(
                    ordinal=len(chunks) + 1,
                    target_instrument_id=target.instrument_id,
                    target_symbol=target.canonical_symbol,
                    role=target.role,
                    core_from=core_from,
                    core_to=missing_to,
                    request_from=core_from,
                    request_to=request_to,
                    requests=requests,
                )
            )
            missing_to = core_from - timedelta(days=1)

    return HistoricalBackfillPlan(
        target_from=target_from,
        completed_through=completed_through,
        benchmark_id=benchmark.instrument_id,
        benchmark_basis=benchmark_basis,
        chunks=tuple(chunks),
        unresolved_symbols=tuple(sorted(unresolved_symbols)),
    )


def _request(instrument: BackfillInstrument, from_date: date, to_date: date) -> DailyHistoryRequest:
    return DailyHistoryRequest(
        instrument_id=instrument.instrument_id,
        instrument_kind=instrument.instrument_kind,
        source_instrument_id=instrument.source_instrument_id,
        from_date=from_date,
        to_date=to_date,
    )
