"""The continuous series is rebuilt from persisted facts at an explicit as-of time."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from dhruva.contexts.marketdata.application.continuous_futures import (
    GetContinuousFuturesSeries,
    GetContinuousFuturesSeriesQuery,
    contract_terms,
)
from dhruva.contexts.marketdata.domain.continuous_futures import (
    FuturesContractTerms,
    RollPolicy,
    RollReason,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarArchiveWrite,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.contexts.reference.domain.instrument_master import FuturesContract
from dhruva.shared.errors import MissingDataError, ValidationError
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACCOUNT = AccountId.deterministic("owner-family")
SESSIONS = tuple(date(2026, 7, 1) + timedelta(days=item) for item in range(12))
RETRIEVED = datetime(2026, 7, 13, 6, tzinfo=UTC)
KNOWN_AT = datetime(2026, 7, 13, 12, tzinfo=UTC)

BENCHMARK = InstrumentId.deterministic("reference", "nse-index-nifty-50")
UNDERLYING = InstrumentId.deterministic("reference", "nse-equity-sbin")
FRONT = InstrumentId.deterministic("reference", "nfo-fut-sbin-front")
NEXT = InstrumentId.deterministic("reference", "nfo-fut-sbin-next")

FRONT_TERMS = FuturesContractTerms(
    contract_id=FRONT, instrument_token=111, expiry=SESSIONS[9], lot_size=750
)
NEXT_TERMS = FuturesContractTerms(
    contract_id=NEXT, instrument_token=222, expiry=SESSIONS[11], lot_size=1500
)
POLICY = RollPolicy(expiry_buffer_sessions=2)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _series(  # noqa: PLR0913 - each argument varies one axis these tests exercise
    instrument_id: InstrumentId,
    kind: MarketInstrumentKind,
    sessions: tuple[date, ...],
    *,
    token: int,
    volume: int = 1000,
    open_interest: int | None = 5000,
    incomplete_from: date | None = None,
) -> DailyBarSeries:
    return DailyBarSeries(
        bars=tuple(
            DailyBarRevision(
                instrument_id=instrument_id,
                instrument_kind=kind,
                source="zerodha",
                source_instrument_id=token,
                candle=DailyCandle(
                    trading_date=item,
                    open=Decimal("99"),
                    high=Decimal("105"),
                    low=Decimal("98"),
                    close=Decimal("100"),
                    volume=volume,
                    open_interest=open_interest,
                ),
                retrieved_at=RETRIEVED,
                adjustment_status=AdjustmentStatus.UNKNOWN,
                completeness=(
                    BarCompleteness.INCOMPLETE
                    if incomplete_from is not None and item >= incomplete_from
                    else BarCompleteness.COMPLETE
                ),
                source_revision=_digest(str(instrument_id), item.isoformat()),
                batch_sha256=_digest(str(instrument_id), "batch"),
                quality_revision="daily-history-quality-v1",
            )
            for item in sessions
        )
    )


def _benchmark(
    sessions: tuple[date, ...] = SESSIONS,
    *,
    incomplete_from: date | None = None,
) -> DailyBarSeries:
    return _series(
        BENCHMARK,
        MarketInstrumentKind.INDEX,
        sessions,
        token=9,
        volume=1,
        open_interest=None,
        incomplete_from=incomplete_from,
    )


def _arranged() -> dict[InstrumentId, DailyBarSeries]:
    """Arrange a back month more liquid from the first session, so the roll is early."""
    return {
        BENCHMARK: _benchmark(),
        FRONT: _series(FRONT, MarketInstrumentKind.FUTURES_CONTRACT, SESSIONS[:10], token=111),
        NEXT: _series(
            NEXT,
            MarketInstrumentKind.FUTURES_CONTRACT,
            SESSIONS,
            token=222,
            volume=5000,
            open_interest=9000,
        ),
    }


class FakeStore:
    """Serve arranged series and record exactly how each read was asked for."""

    def __init__(self, arranged: dict[InstrumentId, DailyBarSeries]) -> None:
        self.arranged = arranged
        self.reads: list[tuple[InstrumentId, date, date, datetime, bool]] = []

    async def add_series(self, series: DailyBarSeries) -> DailyBarArchiveWrite:
        """Fail because a query must never write."""
        raise AssertionError(series)

    async def list_series(
        self,
        instrument_id: InstrumentId,
        *,
        from_date: date,
        to_date: date,
        known_at: datetime,
        require_complete: bool,
    ) -> DailyBarSeries:
        """Return the arranged series, or the repository's own absence error."""
        self.reads.append((instrument_id, from_date, to_date, known_at, require_complete))
        if instrument_id not in self.arranged:
            raise MissingDataError(
                "daily bar series was not found",
                instrument_id=str(instrument_id),
            )
        return self.arranged[instrument_id]


class FakeUnitOfWork:
    """Expose one in-memory store and record that a query commits nothing."""

    def __init__(self, store: FakeStore) -> None:
        self.daily_bars = store
        self.commits = 0

    async def __aenter__(self) -> Self:
        """Open the fake transaction."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close without committing."""

    async def commit(self) -> None:
        """Record a commit a read path must never perform."""
        self.commits += 1

    async def rollback(self) -> None:
        """Discard nothing; a query stages nothing."""


class Factory:
    """Retain created transactions so read-only behaviour is observable."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.created: list[FakeUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeUnitOfWork:
        """Create a transaction for the arranged account."""
        assert account_id == ACCOUNT
        unit_of_work = FakeUnitOfWork(self.store)
        self.created.append(unit_of_work)
        return unit_of_work


def _query(
    *,
    terms: tuple[FuturesContractTerms, ...] = (FRONT_TERMS, NEXT_TERMS),
    from_date: date = SESSIONS[0],
    to_date: date = SESSIONS[-1],
    known_at: datetime = KNOWN_AT,
) -> GetContinuousFuturesSeriesQuery:
    return GetContinuousFuturesSeriesQuery(
        account_id=ACCOUNT,
        underlying_id=UNDERLYING,
        benchmark_id=BENCHMARK,
        terms=terms,
        from_date=from_date,
        to_date=to_date,
        known_at=known_at,
        policy=POLICY,
    )


async def test_the_series_is_stitched_from_the_persisted_actual_contracts() -> None:
    """Every price in the research series came from a contract that traded."""
    store = FakeStore(_arranged())
    factory = Factory(store)

    series = await GetContinuousFuturesSeries(factory).execute(_query())

    assert series.underlying_id == UNDERLYING
    assert series.policy_revision == POLICY.revision
    assert [roll.reason for roll in series.rolls] == [RollReason.LIQUIDITY_CROSSOVER]
    assert series.rolls[0].roll_date == SESSIONS[1]
    assert {bar.source_contract_id for bar in series.bars} == {FRONT, NEXT}
    assert factory.created[0].commits == 0


async def test_the_read_is_point_in_time_and_tolerates_unsettled_edges() -> None:
    """Corrections after ``known_at`` stay invisible; today's bar is still readable."""
    store = FakeStore(_arranged())

    await GetContinuousFuturesSeries(Factory(store)).execute(_query())

    assert store.reads
    for _, from_date, to_date, known_at, require_complete in store.reads:
        assert from_date == SESSIONS[0]
        assert to_date == SESSIONS[-1]
        assert known_at == KNOWN_AT
        assert require_complete is False


async def test_the_benchmark_supplies_the_calendar_and_never_a_bar() -> None:
    """Nifty decides which sessions exist; it contributes no futures price."""
    store = FakeStore(_arranged())

    series = await GetContinuousFuturesSeries(Factory(store)).execute(_query())

    assert BENCHMARK in {item[0] for item in store.reads}
    assert BENCHMARK not in {bar.source_contract_id for bar in series.bars}


async def test_only_settled_benchmark_sessions_form_the_calendar() -> None:
    """An unsettled index bar is not yet a session anything can be counted in."""
    arranged = _arranged()
    arranged[BENCHMARK] = _benchmark(incomplete_from=SESSIONS[10])
    store = FakeStore(arranged)

    series = await GetContinuousFuturesSeries(Factory(store)).execute(_query())

    assert tuple(bar.trading_date for bar in series.bars) == SESSIONS[:10]


async def test_a_contract_with_no_history_is_dropped_rather_than_fatal() -> None:
    """A far month that has never printed is absent, not a failure."""
    arranged = _arranged()
    del arranged[NEXT]
    store = FakeStore(arranged)

    series = await GetContinuousFuturesSeries(Factory(store)).execute(_query())

    assert {bar.source_contract_id for bar in series.bars} == {FRONT}
    assert series.rolls == ()


async def test_a_missing_benchmark_refuses_the_whole_series() -> None:
    """Without a calendar the expiry buffer has nothing to count in."""
    arranged = _arranged()
    del arranged[BENCHMARK]

    with pytest.raises(MissingDataError, match="benchmark session calendar"):
        await GetContinuousFuturesSeries(Factory(FakeStore(arranged))).execute(_query())


async def test_no_contract_with_history_refuses_the_series() -> None:
    """An empty research series would be indistinguishable from a flat market."""
    arranged = {BENCHMARK: _benchmark()}

    with pytest.raises(MissingDataError, match="no requested futures contract has history"):
        await GetContinuousFuturesSeries(Factory(FakeStore(arranged))).execute(_query())


async def test_a_query_without_contracts_is_refused() -> None:
    """Nothing to stitch is a caller error, caught before any read."""
    store = FakeStore(_arranged())
    factory = Factory(store)

    with pytest.raises(ValidationError, match="at least one contract"):
        await GetContinuousFuturesSeries(factory).execute(_query(terms=()))

    assert factory.created == []
    assert store.reads == []


async def test_a_reversed_date_range_is_refused() -> None:
    """A reversed range would silently return nothing at all."""
    with pytest.raises(ValidationError, match="range is reversed"):
        await GetContinuousFuturesSeries(Factory(FakeStore(_arranged()))).execute(
            _query(from_date=SESSIONS[-1], to_date=SESSIONS[0])
        )


async def test_a_naive_knowledge_time_is_refused() -> None:
    """A point-in-time read without a timezone is not a point in time (ADR-006)."""
    with pytest.raises(ValidationError, match="known_at must be timezone-aware"):
        await GetContinuousFuturesSeries(Factory(FakeStore(_arranged()))).execute(
            _query(known_at=datetime(2026, 7, 13, 12))  # noqa: DTZ001 - the defect under test
        )


async def test_a_repeated_contract_in_one_query_is_refused() -> None:
    """One contract twice would give the same expiry two places in the roll order."""
    with pytest.raises(ValidationError, match="contracts must be unique"):
        await GetContinuousFuturesSeries(Factory(FakeStore(_arranged()))).execute(
            _query(terms=(FRONT_TERMS, FRONT_TERMS))
        )


async def test_the_benchmark_cannot_also_be_one_of_the_contracts() -> None:
    """An index is not a contract, and a calendar cannot be its own source."""
    collision = FuturesContractTerms(
        contract_id=BENCHMARK, instrument_token=9, expiry=SESSIONS[9], lot_size=75
    )

    with pytest.raises(ValidationError, match="benchmark cannot also be a futures contract"):
        await GetContinuousFuturesSeries(Factory(FakeStore(_arranged()))).execute(
            _query(terms=(collision, NEXT_TERMS))
        )


async def test_reference_contracts_translate_into_roll_terms() -> None:
    """The roll rule needs expiry, lot and token, and takes nothing else across."""
    contract = FuturesContract(
        contract_id=FRONT,
        underlying_id=UNDERLYING,
        provider="zerodha",
        instrument_token=111,
        exchange_token=433,
        trading_symbol="SBIN26JULFUT",
        expiry=SESSIONS[9],
        lot_size=750,
        tick_size=Decimal("0.05"),
        instrument_type="FUT",
        segment="NFO-FUT",
        exchange="NFO",
    )

    assert contract_terms((contract,)) == (FRONT_TERMS,)
