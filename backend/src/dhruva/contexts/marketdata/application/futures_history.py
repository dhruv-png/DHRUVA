"""Ingest actual futures-contract OHLCV/OI from an archived instrument master.

Only *actual* contracts are fetched and persisted here. A continuous research
series is a derived, versioned artefact built from these facts; it is never a
directly tradable instrument and is therefore never requested from the provider
(ADR-076, personal MVP plan section 5, MVP 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.marketdata.application.daily_history import (
    DAILY_HISTORY_QUALITY_REVISION,
    append_daily_series,
    build_daily_series,
    fetch_history_batches,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    BarCompleteness,
    DailyBarSeries,
    DailyHistoryRequest,
    MarketInstrumentKind,
)
from dhruva.contexts.reference.api import FuturesContractStatus
from dhruva.shared.errors import DataQualityError, MissingDataError, StaleDataError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from dhruva.contexts.marketdata.domain.ports import DailyHistorySource, MarketDataUnitOfWork
    from dhruva.contexts.reference.api import (
        ArchivedInstrumentDiscovery,
        FuturesContractObservation,
    )
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "IngestActualFuturesHistory",
    "IngestActualFuturesHistoryCommand",
    "IngestActualFuturesHistoryResult",
]

_NIFTY_CANONICAL_SYMBOL = "NIFTY 50"


@dataclass(frozen=True, slots=True)
class IngestActualFuturesHistoryCommand:
    """Archived identities and synchronized benchmark range for one refresh."""

    account_id: AccountId
    archive: ArchivedInstrumentDiscovery
    benchmark_request: DailyHistoryRequest
    completed_through: date
    required_through: date


@dataclass(frozen=True, slots=True)
class IngestActualFuturesHistoryResult:
    """Actual-contract counts and append evidence after an atomic refresh."""

    provider: str
    archive_market_date: date
    contracts: int
    active_contracts: int
    expired_contracts: int
    bars_added: int
    bars_unchanged: int
    incomplete_bars: int
    required_through: date
    quality_revision: str


@dataclass(frozen=True, slots=True)
class _FuturesTarget:
    """One archived actual contract paired with its bounded provider request."""

    observation: FuturesContractObservation
    request: DailyHistoryRequest


class IngestActualFuturesHistory:
    """Fetch actual contracts only; continuous research history is a separate concern."""

    __slots__ = ("_source", "_unit_of_work_factory")

    def __init__(
        self,
        source: DailyHistorySource,
        unit_of_work_factory: Callable[[AccountId], MarketDataUnitOfWork],
    ) -> None:
        """Bind one read-only history source and transaction factory."""
        self._source = source
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self,
        command: IngestActualFuturesHistoryCommand,
    ) -> IngestActualFuturesHistoryResult:
        """Resolve archived contracts, validate session coverage, then append."""
        self._validate_command(command)
        targets = _targets(command)
        batches = await fetch_history_batches(
            self._source,
            (command.benchmark_request, *(target.request for target in targets)),
        )
        if batches[0].provider != command.archive.snapshot.provider:
            raise DataQualityError(
                "futures history provider differs from the instrument archive",
                archive_provider=command.archive.snapshot.provider,
                history_provider=batches[0].provider,
            )

        benchmark = build_daily_series(
            batches[0],
            completed_through=command.completed_through,
        )
        _validate_benchmark(benchmark, required_through=command.required_through)
        futures_series = tuple(
            build_daily_series(batch, completed_through=command.completed_through)
            for batch in batches[1:]
        )
        for target, series in zip(targets, futures_series, strict=True):
            _validate_contract_sessions(
                target,
                series,
                benchmark=benchmark,
                required_through=command.required_through,
            )

        added, unchanged = await append_daily_series(
            account_id=command.account_id,
            series=futures_series,
            unit_of_work_factory=self._unit_of_work_factory,
        )
        return IngestActualFuturesHistoryResult(
            provider=command.archive.snapshot.provider,
            archive_market_date=command.archive.snapshot.market_date,
            contracts=len(targets),
            active_contracts=sum(
                target.observation.status is FuturesContractStatus.ACTIVE for target in targets
            ),
            expired_contracts=sum(
                target.observation.status is FuturesContractStatus.EXPIRED for target in targets
            ),
            bars_added=added,
            bars_unchanged=unchanged,
            incomplete_bars=sum(
                bar.completeness is BarCompleteness.INCOMPLETE
                for series in futures_series
                for bar in series.bars
            ),
            required_through=command.required_through,
            quality_revision=DAILY_HISTORY_QUALITY_REVISION,
        )

    @staticmethod
    def _validate_command(command: IngestActualFuturesHistoryCommand) -> None:
        """Require a fresh archive and its exact archived Nifty cash mapping."""
        request = command.benchmark_request
        if request.instrument_kind is not MarketInstrumentKind.INDEX:
            raise ValidationError("futures history benchmark must be an index")
        if not (
            request.from_date
            <= command.required_through
            <= command.completed_through
            <= request.to_date
        ):
            raise ValidationError("futures history cutoffs fall outside the benchmark range")
        if command.archive.snapshot.market_date < command.required_through:
            raise StaleDataError(
                "instrument archive is older than the required futures session",
                archive_market_date=command.archive.snapshot.market_date.isoformat(),
                required_through=command.required_through.isoformat(),
            )
        nifty = tuple(
            item
            for item in command.archive.resolutions
            if item.canonical_symbol == _NIFTY_CANONICAL_SYMBOL
        )
        if len(nifty) != 1 or nifty[0].cash is None:
            raise MissingDataError("archived Nifty benchmark mapping is unavailable")
        mapping = nifty[0].cash
        if (
            request.instrument_id != nifty[0].instrument_id
            or request.source_instrument_id != mapping.instrument_token
            or mapping.provider != command.archive.snapshot.provider
        ):
            raise DataQualityError(
                "futures history benchmark does not match the archived Nifty mapping"
            )


def _targets(command: IngestActualFuturesHistoryCommand) -> tuple[_FuturesTarget, ...]:
    """Create only non-continuous, OI-enabled requests from archived contracts."""
    targets: list[_FuturesTarget] = []
    seen: set[InstrumentId] = set()
    for resolution in command.archive.resolutions:
        for observation in resolution.futures_observations:
            contract = observation.contract
            if contract.expiry < command.benchmark_request.from_date:
                continue
            if contract.contract_id in seen:
                raise DataQualityError(
                    "instrument archive repeats a futures contract identity",
                    contract_id=str(contract.contract_id),
                )
            seen.add(contract.contract_id)
            targets.append(
                _FuturesTarget(
                    observation=observation,
                    request=DailyHistoryRequest(
                        instrument_id=contract.contract_id,
                        instrument_kind=MarketInstrumentKind.FUTURES_CONTRACT,
                        source_instrument_id=contract.instrument_token,
                        from_date=command.benchmark_request.from_date,
                        to_date=min(command.benchmark_request.to_date, contract.expiry),
                        continuous=False,
                        include_open_interest=True,
                    ),
                )
            )
    if not targets:
        raise MissingDataError("archive contains no futures contracts in the requested range")
    return tuple(targets)


def _validate_benchmark(series: DailyBarSeries, *, required_through: date) -> None:
    """Require the explicit Nifty calendar to be complete through the cutoff."""
    required = {
        bar.candle.trading_date
        for bar in series.bars
        if bar.candle.trading_date <= required_through
        and bar.completeness is BarCompleteness.COMPLETE
    }
    if required_through not in required:
        raise StaleDataError(
            "benchmark futures calendar is stale or incomplete",
            required_through=required_through.isoformat(),
        )


def _validate_contract_sessions(
    target: _FuturesTarget,
    series: DailyBarSeries,
    *,
    benchmark: DailyBarSeries,
    required_through: date,
) -> None:
    """Reject synthetic sessions and missing in-life sessions for one contract.

    Contract identity, instrument kind, provider token and the presence of open
    interest are already guaranteed before this runs: ``fetch_history_batches``
    refuses a batch answering a different request, and ``DailyHistoryBatch``
    refuses a response whose candles omit requested open interest.
    """
    contract = target.observation.contract
    benchmark_dates = {bar.candle.trading_date for bar in benchmark.bars}
    extra = {bar.candle.trading_date for bar in series.bars} - benchmark_dates
    if extra:
        raise DataQualityError(
            "actual futures history contains non-benchmark sessions",
            contract_id=str(contract.contract_id),
            extra_sessions=len(extra),
        )

    effective_end = min(required_through, target.request.to_date)
    required_dates = tuple(
        sorted(
            bar.candle.trading_date
            for bar in benchmark.bars
            if series.bars[0].candle.trading_date <= bar.candle.trading_date <= effective_end
            and bar.completeness is BarCompleteness.COMPLETE
        )
    )
    complete_dates = {
        bar.candle.trading_date
        for bar in series.bars
        if bar.completeness is BarCompleteness.COMPLETE
    }
    if not required_dates or required_dates[-1] not in complete_dates:
        raise StaleDataError(
            "actual futures contract history is stale or incomplete",
            contract_id=str(contract.contract_id),
            required_through=effective_end.isoformat(),
        )
    missing = set(required_dates) - complete_dates
    if missing:
        raise DataQualityError(
            "actual futures contract history has missing in-life sessions",
            contract_id=str(contract.contract_id),
            missing_sessions=len(missing),
        )
