"""Actual futures history is fetched per contract and validated before persistence."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from dhruva.contexts.marketdata.application.futures_history import (
    IngestActualFuturesHistory,
    IngestActualFuturesHistoryCommand,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    DailyBarArchiveWrite,
    DailyBarSeries,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
    MarketInstrumentKind,
)
from dhruva.contexts.reference.domain.instrument_master import (
    ArchivedInstrumentDiscovery,
    ArchivedInstrumentMaster,
    FuturesAvailability,
    FuturesAvailabilityStatus,
    FuturesContract,
    FuturesContractObservation,
    FuturesContractStatus,
    InstrumentResolution,
    ResolvedCashInstrument,
)
from dhruva.shared.errors import (
    DataQualityError,
    MissingDataError,
    StaleDataError,
    ValidationError,
)
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

PROVIDER = "zerodha"
ACCOUNT = AccountId.deterministic("owner-family")
RETRIEVED = datetime(2026, 7, 31, 12, 30, tzinfo=UTC)
DATES = (date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31))
MARKET_DATE = DATES[-1]
NEAR_EXPIRY = DATES[1]
FAR_EXPIRY = date(2026, 8, 27)

NIFTY = InstrumentId.deterministic("reference", "nse-index-nifty-50")
SBIN = InstrumentId.deterministic("reference", "nse-equity-sbin")
NIFTY_FUT = InstrumentId.deterministic("reference", "nfo-fut-nifty-50-2026-08-27")
SBIN_FAR_FUT = InstrumentId.deterministic("reference", "nfo-fut-sbin-2026-08-27")
SBIN_NEAR_FUT = InstrumentId.deterministic("reference", "nfo-fut-sbin-2026-07-30")

NIFTY_TOKEN = 256265
SBIN_TOKEN = 779521


def _contract(  # noqa: PLR0913 - a contract identity is exactly these six exchange facts
    contract_id: InstrumentId,
    underlying_id: InstrumentId,
    *,
    token: int,
    trading_symbol: str,
    expiry: date,
    lot_size: int,
) -> FuturesContract:
    return FuturesContract(
        contract_id=contract_id,
        underlying_id=underlying_id,
        provider=PROVIDER,
        instrument_token=token,
        exchange_token=token // 256,
        trading_symbol=trading_symbol,
        expiry=expiry,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
        instrument_type="FUT",
        segment="NFO-FUT",
        exchange="NFO",
    )


NIFTY_FUT_CONTRACT = _contract(
    NIFTY_FUT,
    NIFTY,
    token=12345678,
    trading_symbol="NIFTY26AUGFUT",
    expiry=FAR_EXPIRY,
    lot_size=75,
)
SBIN_FAR_CONTRACT = _contract(
    SBIN_FAR_FUT,
    SBIN,
    token=22334455,
    trading_symbol="SBIN26AUGFUT",
    expiry=FAR_EXPIRY,
    lot_size=750,
)
SBIN_NEAR_CONTRACT = _contract(
    SBIN_NEAR_FUT,
    SBIN,
    token=22334400,
    trading_symbol="SBIN26JULFUT",
    expiry=NEAR_EXPIRY,
    lot_size=750,
)


def _cash(instrument_id: InstrumentId, *, symbol: str, token: int) -> ResolvedCashInstrument:
    return ResolvedCashInstrument(
        instrument_id=instrument_id,
        provider=PROVIDER,
        exchange="NSE",
        trading_symbol=symbol,
        instrument_token=token,
        exchange_token=token // 256 or 1,
    )


def _resolution(
    instrument_id: InstrumentId,
    *,
    symbol: str,
    cash_token: int,
    contracts: tuple[FuturesContract, ...],
    market_date: date,
) -> InstrumentResolution:
    """Derive lifecycle state and availability the way the resolver does."""
    ordered = tuple(sorted(contracts, key=lambda item: item.expiry))
    observations = tuple(
        FuturesContractObservation(
            contract=item,
            status=(
                FuturesContractStatus.ACTIVE
                if item.expiry >= market_date
                else FuturesContractStatus.EXPIRED
            ),
            selected_for_availability=item.expiry >= market_date,
        )
        for item in ordered
    )
    selected = tuple(item.contract for item in observations if item.selected_for_availability)
    return InstrumentResolution(
        instrument_id=instrument_id,
        canonical_symbol=symbol,
        cash=_cash(instrument_id, symbol=symbol, token=cash_token),
        cash_unavailable_reason=None,
        futures=FuturesAvailability(
            status=(
                FuturesAvailabilityStatus.AVAILABLE
                if selected
                else FuturesAvailabilityStatus.CURRENTLY_UNAVAILABLE
            ),
            reason=("active contract selected" if selected else "no active contract"),
            contracts=selected,
        ),
        futures_observations=observations,
    )


def _archive(
    *,
    market_date: date = MARKET_DATE,
    resolutions: tuple[InstrumentResolution, ...] | None = None,
) -> ArchivedInstrumentDiscovery:
    raw = f"instrument-master-{market_date.isoformat()}".encode()
    if resolutions is None:
        resolutions = (
            _resolution(
                NIFTY,
                symbol="NIFTY 50",
                cash_token=NIFTY_TOKEN,
                contracts=(NIFTY_FUT_CONTRACT,),
                market_date=market_date,
            ),
            _resolution(
                SBIN,
                symbol="SBIN",
                cash_token=SBIN_TOKEN,
                contracts=(SBIN_NEAR_CONTRACT, SBIN_FAR_CONTRACT),
                market_date=market_date,
            ),
        )
    return ArchivedInstrumentDiscovery(
        snapshot=ArchivedInstrumentMaster(
            provider=PROVIDER,
            market_date=market_date,
            fetched_at=RETRIEVED,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            raw_csv=raw,
            row_count=4,
        ),
        resolutions=resolutions,
        resolver_revision="instrument-discovery-v1",
    )


BENCHMARK_REQUEST = DailyHistoryRequest(
    instrument_id=NIFTY,
    instrument_kind=MarketInstrumentKind.INDEX,
    source_instrument_id=NIFTY_TOKEN,
    from_date=DATES[0],
    to_date=DATES[-1],
)


def _candle(trading_date: date, *, open_interest: int | None) -> DailyCandle:
    return DailyCandle(
        trading_date=trading_date,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("103"),
        volume=1000,
        open_interest=open_interest,
    )


class FakeSource:
    """Answer each request exactly, with per-instrument session overrides."""

    def __init__(
        self,
        *,
        dates: dict[InstrumentId, tuple[date, ...]] | None = None,
        provider: str = PROVIDER,
    ) -> None:
        self.dates = dates or {}
        self.provider = provider
        self.requests: list[DailyHistoryRequest] = []

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        """Return a replayable batch answering exactly this request."""
        self.requests.append(request)
        arranged = self.dates.get(request.instrument_id, DATES)
        sessions = tuple(item for item in arranged if request.from_date <= item <= request.to_date)
        raw = f"{self.provider}-{request.source_instrument_id}-{sessions}".encode()
        return DailyHistoryBatch(
            provider=self.provider,
            request=request,
            retrieved_at=RETRIEVED,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            raw_response=raw,
            adjustment_status=AdjustmentStatus.UNKNOWN,
            candles=tuple(
                _candle(item, open_interest=5000 if request.include_open_interest else None)
                for item in sessions
            ),
        )


class FakeStore:
    """Capture series writes and implement source-revision idempotency."""

    def __init__(self) -> None:
        self.series: list[DailyBarSeries] = []
        self.revisions: set[tuple[InstrumentId, str]] = set()

    async def add_series(self, series: DailyBarSeries) -> DailyBarArchiveWrite:
        """Record new bar revisions without a database."""
        self.series.append(series)
        added = 0
        for bar in series.bars:
            key = (bar.instrument_id, bar.source_revision)
            if key not in self.revisions:
                self.revisions.add(key)
                added += 1
        return DailyBarArchiveWrite(added=added, unchanged=len(series.bars) - added)

    async def list_series(
        self,
        instrument_id: InstrumentId,
        *,
        from_date: date,
        to_date: date,
        known_at: datetime,
        require_complete: bool,
    ) -> DailyBarSeries:
        """Fail because ingestion tests must never execute a query."""
        raise AssertionError((instrument_id, from_date, to_date, known_at, require_complete))


class FakeUnitOfWork:
    """Expose one in-memory store and transaction counters."""

    def __init__(self, store: FakeStore) -> None:
        self.daily_bars = store
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        """Open the fake transaction."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record rollback-by-default behavior."""
        if not self.commits:
            self.rollbacks += 1

    async def commit(self) -> None:
        """Record a commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Record an explicit rollback."""
        self.rollbacks += 1


class Factory:
    """Retain created transactions so validation-before-write is observable."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.created: list[FakeUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeUnitOfWork:
        """Create a transaction for the arranged account."""
        assert account_id == ACCOUNT
        unit_of_work = FakeUnitOfWork(self.store)
        self.created.append(unit_of_work)
        return unit_of_work


def _command(
    *,
    archive: ArchivedInstrumentDiscovery | None = None,
    benchmark_request: DailyHistoryRequest | None = None,
    completed_through: date = DATES[-1],
    required_through: date = DATES[-1],
) -> IngestActualFuturesHistoryCommand:
    return IngestActualFuturesHistoryCommand(
        account_id=ACCOUNT,
        archive=_archive() if archive is None else archive,
        benchmark_request=BENCHMARK_REQUEST if benchmark_request is None else benchmark_request,
        completed_through=completed_through,
        required_through=required_through,
    )


async def test_actual_contracts_are_persisted_with_open_interest_and_lifecycle_counts() -> None:
    """Active and unambiguous expired contracts both become research history."""
    store = FakeStore()
    factory = Factory(store)
    source = FakeSource()

    result = await IngestActualFuturesHistory(source, factory).execute(_command())

    assert result.provider == PROVIDER
    assert result.archive_market_date == MARKET_DATE
    assert result.contracts == 3
    assert result.active_contracts == 2
    assert result.expired_contracts == 1
    assert result.bars_added == 8
    assert result.bars_unchanged == 0
    assert result.incomplete_bars == 0
    assert result.quality_revision == "daily-history-quality-v1"
    assert factory.created[0].commits == 1
    assert len(store.series) == 3
    assert {bar.instrument_kind for item in store.series for bar in item.bars} == {
        MarketInstrumentKind.FUTURES_CONTRACT
    }
    assert all(bar.candle.open_interest == 5000 for item in store.series for bar in item.bars)


async def test_the_benchmark_is_read_but_never_written_as_futures_history() -> None:
    """Nifty supplies the session calendar; only contracts are appended here."""
    store = FakeStore()
    source = FakeSource()

    await IngestActualFuturesHistory(source, Factory(store)).execute(_command())

    assert NIFTY not in {item.bars[0].instrument_id for item in store.series}


async def test_no_continuous_series_is_ever_requested_from_the_provider() -> None:
    """A continuous series is derived research output, not a tradable instrument."""
    source = FakeSource()

    await IngestActualFuturesHistory(source, Factory(FakeStore())).execute(_command())

    assert not any(item.continuous for item in source.requests)
    futures = [
        item
        for item in source.requests
        if item.instrument_kind is MarketInstrumentKind.FUTURES_CONTRACT
    ]
    assert len(futures) == 3
    assert all(item.include_open_interest for item in futures)
    assert {item.to_date for item in futures} == {DATES[-1], NEAR_EXPIRY}


async def test_incomplete_sessions_stay_explicit() -> None:
    """A session after the completion cutoff cannot masquerade as settled."""
    source = FakeSource()

    result = await IngestActualFuturesHistory(source, Factory(FakeStore())).execute(
        _command(completed_through=DATES[1], required_through=DATES[1])
    )

    assert result.incomplete_bars == 2
    assert result.bars_added == 8


async def test_repeating_the_refresh_appends_nothing() -> None:
    """Identical provider content is an idempotent retry, not a new revision."""
    store = FakeStore()
    use_case = IngestActualFuturesHistory(FakeSource(), Factory(store))

    first = await use_case.execute(_command())
    second = await use_case.execute(_command())

    assert first.bars_added == 8
    assert second.bars_added == 0
    assert second.bars_unchanged == 8


async def test_a_non_index_benchmark_is_refused() -> None:
    """The futures session calendar must be the index, not another equity."""
    factory = Factory(FakeStore())

    with pytest.raises(ValidationError, match="benchmark must be an index"):
        await IngestActualFuturesHistory(FakeSource(), factory).execute(
            _command(
                benchmark_request=replace(
                    BENCHMARK_REQUEST,
                    instrument_kind=MarketInstrumentKind.CASH_EQUITY,
                )
            )
        )

    assert factory.created == []


async def test_cutoffs_outside_the_benchmark_range_are_refused() -> None:
    """Freshness cutoffs cannot silently exceed the fetched range."""
    factory = Factory(FakeStore())

    with pytest.raises(ValidationError, match="outside the benchmark range"):
        await IngestActualFuturesHistory(FakeSource(), factory).execute(
            _command(required_through=DATES[-1], completed_through=DATES[0])
        )

    assert factory.created == []


async def test_an_archive_older_than_the_required_session_is_stale() -> None:
    """Yesterday's instrument master cannot describe today's contracts."""
    factory = Factory(FakeStore())

    with pytest.raises(StaleDataError, match="archive is older"):
        await IngestActualFuturesHistory(FakeSource(), factory).execute(
            _command(archive=_archive(market_date=DATES[0]))
        )

    assert factory.created == []


async def test_a_benchmark_request_that_is_not_the_archived_nifty_mapping_is_refused() -> None:
    """The calendar must come from the same archived provider token."""
    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="archived Nifty mapping"):
        await IngestActualFuturesHistory(FakeSource(), factory).execute(
            _command(benchmark_request=replace(BENCHMARK_REQUEST, source_instrument_id=999999))
        )

    assert factory.created == []


async def test_an_archive_without_a_nifty_mapping_is_missing_data() -> None:
    """A futures refresh without its benchmark is refused, not approximated."""
    factory = Factory(FakeStore())
    archive = _archive(
        resolutions=(
            _resolution(
                SBIN,
                symbol="SBIN",
                cash_token=SBIN_TOKEN,
                contracts=(SBIN_FAR_CONTRACT,),
                market_date=MARKET_DATE,
            ),
        )
    )

    with pytest.raises(MissingDataError, match="Nifty benchmark mapping"):
        await IngestActualFuturesHistory(FakeSource(), factory).execute(_command(archive=archive))

    assert factory.created == []


async def test_an_archive_without_contracts_in_range_is_missing_data() -> None:
    """Cash analysis survives elsewhere; this refresh has nothing to fetch."""
    long_expired = _contract(
        SBIN_NEAR_FUT,
        SBIN,
        token=22334400,
        trading_symbol="SBIN26JUNFUT",
        expiry=date(2026, 6, 25),
        lot_size=750,
    )
    archive = _archive(
        resolutions=(
            _resolution(
                NIFTY,
                symbol="NIFTY 50",
                cash_token=NIFTY_TOKEN,
                contracts=(),
                market_date=MARKET_DATE,
            ),
            _resolution(
                SBIN,
                symbol="SBIN",
                cash_token=SBIN_TOKEN,
                contracts=(long_expired,),
                market_date=MARKET_DATE,
            ),
        )
    )
    factory = Factory(FakeStore())

    with pytest.raises(MissingDataError, match="no futures contracts in the requested range"):
        await IngestActualFuturesHistory(FakeSource(), factory).execute(_command(archive=archive))

    assert factory.created == []


async def test_a_repeated_contract_identity_in_one_archive_is_refused() -> None:
    """One contract identity cannot belong to two underlyings on one day."""
    collided = _contract(
        NIFTY_FUT,
        SBIN,
        token=22334455,
        trading_symbol="SBIN26AUGFUT",
        expiry=FAR_EXPIRY,
        lot_size=750,
    )
    archive = _archive(
        resolutions=(
            _resolution(
                NIFTY,
                symbol="NIFTY 50",
                cash_token=NIFTY_TOKEN,
                contracts=(NIFTY_FUT_CONTRACT,),
                market_date=MARKET_DATE,
            ),
            _resolution(
                SBIN,
                symbol="SBIN",
                cash_token=SBIN_TOKEN,
                contracts=(collided,),
                market_date=MARKET_DATE,
            ),
        )
    )
    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="repeats a futures contract identity"):
        await IngestActualFuturesHistory(FakeSource(), factory).execute(_command(archive=archive))

    assert factory.created == []


async def test_history_from_a_different_provider_than_the_archive_is_refused() -> None:
    """Contract metadata and prices must come from one provider."""
    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="differs from the instrument archive"):
        await IngestActualFuturesHistory(FakeSource(provider="other"), factory).execute(_command())

    assert factory.created == []


async def test_a_stale_benchmark_calendar_refuses_every_write() -> None:
    """Without a settled Nifty session there is no calendar to validate against."""
    factory = Factory(FakeStore())
    source = FakeSource(dates={NIFTY: DATES[:2]})

    with pytest.raises(StaleDataError, match="benchmark futures calendar"):
        await IngestActualFuturesHistory(source, factory).execute(_command())

    assert factory.created == []


async def test_a_contract_session_outside_the_benchmark_calendar_is_refused() -> None:
    """A synthetic or rolled session cannot enter actual-contract history."""
    factory = Factory(FakeStore())
    source = FakeSource(dates={NIFTY: (DATES[0], DATES[2])})

    with pytest.raises(DataQualityError, match="non-benchmark sessions"):
        await IngestActualFuturesHistory(source, factory).execute(_command())

    assert factory.created == []


async def test_a_missing_in_life_session_refuses_every_write() -> None:
    """A gap inside a contract's life is a defect, not a silent hole."""
    factory = Factory(FakeStore())
    source = FakeSource(dates={SBIN_FAR_FUT: (DATES[0], DATES[2])})

    with pytest.raises(DataQualityError, match="missing in-life sessions"):
        await IngestActualFuturesHistory(source, factory).execute(_command())

    assert factory.created == []


async def test_a_contract_missing_the_required_session_is_stale() -> None:
    """An active contract must reach the freshness cutoff to be usable."""
    factory = Factory(FakeStore())
    source = FakeSource(dates={SBIN_FAR_FUT: DATES[:2]})

    with pytest.raises(StaleDataError, match="contract history is stale"):
        await IngestActualFuturesHistory(source, factory).execute(_command())

    assert factory.created == []
