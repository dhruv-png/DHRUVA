"""``dhruva-marketdata`` -- the surface, coverage classification, and bootstrap math.

Mirrors ``test_broker_cli.py``'s negative style for the parser (no secret can be
carried by this command either) and ``test_daily_history_application.py``'s
fake-store style for the coverage classification, since coverage is exercised
against the real :class:`GetDailyBarSeries` over a fake in-memory store rather
than a hand-rolled substitute for it -- the point-in-time rule stays proven in
exactly the one place the repository owns it, and these tests do not
re-implement that rule.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import TYPE_CHECKING, Self

import pytest

from dhruva.contexts.marketdata.api import (
    DEFAULT_MULTI_DAY_SESSIONS,
    DEFAULT_STALE_AFTER_DAYS,
    GetDailyBarSeries,
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
from dhruva.contexts.reference.domain.instrument_master import (
    ArchivedInstrumentDiscovery,
    ArchivedInstrumentMaster,
    FuturesAvailability,
    FuturesAvailabilityStatus,
    InstrumentResolution,
    ResolvedCashInstrument,
)
from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
    InstrumentKind,
    WatchlistInstrument,
    WatchlistMembershipRevision,
)
from dhruva.shared.errors import MissingDataError
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.workers import marketdata as cli

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit

ACCOUNT = AccountId.deterministic("owner-family")
AS_OF = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
RECORDED = datetime(2026, 8, 2, 6, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The command surface
# --------------------------------------------------------------------------- #

_FORBIDDEN_IN_OPTIONS = (
    "secret",
    "token",
    "password",
    "passwd",
    "pin",
    "totp",
    "otp",
    "key",
    "credential",
)


def _all_actions(parser: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    for action in parser._actions:
        yield action
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _all_actions(sub)


def test_no_option_anywhere_can_carry_a_secret() -> None:
    """The whole point of reusing dhruva-broker's session: nothing new to leak."""
    names = [
        option for action in _all_actions(cli.build_parser()) for option in action.option_strings
    ]

    assert names
    for name in names:
        lowered = name.lower()
        for forbidden in _FORBIDDEN_IN_OPTIONS:
            assert forbidden not in lowered, f"{name} could carry a secret"


def test_the_two_subcommands_are_coverage_and_refresh() -> None:
    """Stated so that a third verb is a deliberate act."""
    parser = cli.build_parser()
    subcommands: set[str] = set()
    for action in _all_actions(parser):
        if isinstance(action, argparse._SubParsersAction):
            subcommands |= set(action.choices)

    assert subcommands == {"coverage", "refresh"}


def test_both_subcommands_require_an_account() -> None:
    """An account-less default would read or write one operator's data as another's."""
    parser = cli.build_parser()

    for action in _all_actions(parser):
        if "--account" in action.option_strings:
            assert action.required is True


def test_coverage_takes_no_arguments_beyond_account_and_as_of() -> None:
    """Coverage is a read; there is nothing else for it to be configured with."""
    args = cli.build_parser().parse_args(["coverage", "--account", "owner-family"])

    assert args.account == "owner-family"
    assert args.as_of is None


def test_refresh_accepts_an_explicit_as_of() -> None:
    """The bootstrap window is computed from this instant, so it must be settable."""
    args = cli.build_parser().parse_args(
        ["refresh", "--account", "owner-family", "--as-of", "2026-08-10T12:00:00+00:00"]
    )

    assert args.as_of == "2026-08-10T12:00:00+00:00"


def test_the_safety_notice_states_what_refresh_will_not_do() -> None:
    """Printed on --help, before an operator with a live session runs it."""
    epilog = cli.build_parser().epilog or ""

    for promise in ("no order placement", "no provider call at all from 'coverage'"):
        assert promise in epilog


# --------------------------------------------------------------------------- #
# IST date arithmetic
# --------------------------------------------------------------------------- #


def test_ist_date_rolls_forward_past_the_utc_midnight_boundary() -> None:
    """18:30 UTC is already past midnight in IST; the trading date is the next day."""
    instant = datetime(2026, 8, 10, 18, 30, tzinfo=UTC)

    assert cli._ist_date(instant) == date(2026, 8, 11)


def test_ist_date_stays_on_the_same_day_before_the_boundary() -> None:
    """18:29 UTC is still the same IST calendar day."""
    instant = datetime(2026, 8, 10, 18, 29, tzinfo=UTC)

    assert cli._ist_date(instant) == date(2026, 8, 10)


# --------------------------------------------------------------------------- #
# Mapping lookup
# --------------------------------------------------------------------------- #


def _snapshot(market_date: date = date(2026, 8, 9)) -> ArchivedInstrumentMaster:
    raw = b"header\n"
    return ArchivedInstrumentMaster(
        provider="zerodha",
        market_date=market_date,
        fetched_at=RECORDED,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        raw_csv=raw,
        row_count=1,
    )


def _resolution(
    instrument_id: InstrumentId,
    symbol: str,
    *,
    mapped: bool,
    token: int = 100,
) -> InstrumentResolution:
    cash = (
        ResolvedCashInstrument(
            instrument_id=instrument_id,
            provider="zerodha",
            exchange="NSE",
            trading_symbol=symbol,
            instrument_token=token,
            exchange_token=token,
        )
        if mapped
        else None
    )
    return InstrumentResolution(
        instrument_id=instrument_id,
        canonical_symbol=symbol,
        cash=cash,
        cash_unavailable_reason=None if mapped else "No exact current NSE cash mapping.",
        futures=FuturesAvailability(
            status=FuturesAvailabilityStatus.CURRENTLY_UNAVAILABLE,
            reason="Futures research is not enabled for this underlying.",
            contracts=(),
        ),
        futures_observations=(),
    )


def test_mapped_cash_is_empty_before_any_archive_exists() -> None:
    """No archive yet means every instrument is reported unmapped, never guessed."""
    assert cli._mapped_cash(None) == {}


def test_mapped_cash_indexes_only_resolutions_that_actually_mapped() -> None:
    """An unresolved cash mapping must not appear as a false positive token."""
    mapped_id = InstrumentId.deterministic("reference", "nse-equity-mapped")
    unmapped_id = InstrumentId.deterministic("reference", "nse-equity-unmapped")
    discovery = ArchivedInstrumentDiscovery(
        snapshot=_snapshot(),
        resolutions=(
            _resolution(mapped_id, "MAPPED", mapped=True),
            _resolution(unmapped_id, "UNMAPPED", mapped=False),
        ),
        resolver_revision="instrument-discovery-v1",
    )

    result = cli._mapped_cash(discovery)

    assert set(result) == {mapped_id}
    assert result[mapped_id].instrument_token == 100


# --------------------------------------------------------------------------- #
# Coverage classification, against the real GetDailyBarSeries
# --------------------------------------------------------------------------- #


class FakeDailyBarStore:
    """An in-memory point-in-time store, filtered exactly as the repository is."""

    def __init__(self, bars: tuple[DailyBarRevision, ...] = ()) -> None:
        self._bars = bars

    async def add_series(self, series: DailyBarSeries) -> DailyBarArchiveWrite:
        """Fail because coverage reads must never write."""
        raise AssertionError("coverage must never write", series)

    async def list_series(
        self,
        instrument_id: InstrumentId,
        *,
        from_date: date,
        to_date: date,
        known_at: datetime,
        require_complete: bool,  # noqa: ARG002 - every fixture bar here is COMPLETE
    ) -> DailyBarSeries:
        """Resolve the same PIT filter the real repository applies."""
        matched = tuple(
            bar
            for bar in self._bars
            if bar.instrument_id == instrument_id
            and from_date <= bar.candle.trading_date <= to_date
            and bar.retrieved_at <= known_at
        )
        if not matched:
            raise MissingDataError("no bars", instrument_id=str(instrument_id))
        return DailyBarSeries(bars=matched)


class FakeMarketDataUnitOfWork:
    """Expose one fixed store; coverage reads never commit."""

    def __init__(self, store: FakeDailyBarStore) -> None:
        self.daily_bars = store

    async def __aenter__(self) -> Self:
        """Open the fake transaction."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """No-op: nothing here holds a real resource to release."""
        return

    async def commit(self) -> None:
        """Fail because a read must never commit."""
        raise AssertionError("coverage must never commit")

    async def rollback(self) -> None:
        """No-op: nothing is ever staged to discard."""
        return


def _watchlist_entry(symbol: str, identity_key: str) -> WatchlistInstrument:
    instrument_id = InstrumentId.deterministic("reference", identity_key)
    identity = InstrumentIdentityRevision(
        instrument_id=instrument_id,
        kind=InstrumentKind.EQUITY,
        canonical_symbol=symbol,
        company_name=f"{symbol} Limited",
        aliases=(),
        former_names=(),
        isin=None,
        sector="Test",
        concentration_groups=(),
        cash_mapping=CashInstrumentMapping(exchange="NSE", trading_symbol=symbol),
        futures_research_requested=False,
        valid_from=date(2026, 8, 2),
        valid_to=None,
        recorded_at=RECORDED,
        source="owner_configuration",
        source_revision="owner-watchlist-2026-08-02",
    )
    membership = WatchlistMembershipRevision(
        account_id=ACCOUNT,
        instrument_id=instrument_id,
        active_from=date(2026, 8, 2),
        active_to=None,
        recorded_at=RECORDED,
        source="owner_configuration",
        source_revision="owner-watchlist-2026-08-02",
    )
    return WatchlistInstrument(identity=identity, membership=membership)


def _bar(
    instrument_id: InstrumentId, trading_date: date, *, retrieved_at: datetime
) -> DailyBarRevision:
    price = Decimal("100")
    return DailyBarRevision(
        instrument_id=instrument_id,
        instrument_kind=MarketInstrumentKind.CASH_EQUITY,
        source="zerodha",
        source_instrument_id=1,
        candle=DailyCandle(
            trading_date=trading_date,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=1_000,
            open_interest=None,
        ),
        retrieved_at=retrieved_at,
        adjustment_status=AdjustmentStatus.UNKNOWN,
        completeness=BarCompleteness.COMPLETE,
        source_revision="a" * 64,
        batch_sha256="b" * 64,
        quality_revision="daily-history-quality-v1",
    )


async def _classify(
    *, symbol: str, mapped: bool, bars: tuple[DailyBarRevision, ...]
) -> cli.InstrumentCoverage:
    watchlist = (_watchlist_entry(symbol, f"nse-equity-{symbol.lower()}"),)
    instrument_id = watchlist[0].identity.instrument_id
    mapping = (
        {
            instrument_id: ResolvedCashInstrument(
                instrument_id=instrument_id,
                provider="zerodha",
                exchange="NSE",
                trading_symbol=symbol,
                instrument_token=1,
                exchange_token=1,
            )
        }
        if mapped
        else {}
    )
    read = GetDailyBarSeries(lambda _account: FakeMarketDataUnitOfWork(FakeDailyBarStore(bars)))
    entries = await cli._coverage_entries(
        watchlist=watchlist, mapped=mapping, read=read, account_id=ACCOUNT, as_of=AS_OF
    )
    assert len(entries) == 1
    return entries[0]


@pytest.mark.asyncio
async def test_an_unmapped_instrument_is_missing_mapping() -> None:
    """No archived cash mapping is reported explicitly, never guessed."""
    entry = await _classify(symbol="UNMAPPED", mapped=False, bars=())

    assert entry.status is cli.CoverageStatus.MISSING_MAPPING
    assert entry.mapped is False
    assert entry.bar_count == 0


@pytest.mark.asyncio
async def test_a_mapped_instrument_with_no_bars_is_no_data() -> None:
    """Mapped but never ingested is a different fact from unmapped."""
    entry = await _classify(symbol="NODATA", mapped=True, bars=())

    assert entry.status is cli.CoverageStatus.NO_DATA
    assert entry.mapped is True


@pytest.mark.asyncio
async def test_five_bars_is_insufficient_for_a_five_session_return() -> None:
    """Six bars are needed for DEFAULT_MULTI_DAY_SESSIONS; five is one short."""
    instrument_id = InstrumentId.deterministic("reference", "nse-equity-short")
    bars = tuple(
        _bar(instrument_id, AS_OF.date() - timedelta(days=n), retrieved_at=RECORDED)
        for n in range(DEFAULT_MULTI_DAY_SESSIONS - 1, -1, -1)
    )
    assert len(bars) == DEFAULT_MULTI_DAY_SESSIONS

    entry = await _classify(symbol="SHORT", mapped=True, bars=bars)

    assert entry.status is cli.CoverageStatus.INSUFFICIENT_HISTORY
    assert entry.enough_history is False


@pytest.mark.asyncio
async def test_six_fresh_bars_are_ready() -> None:
    """Enough history and fresh as of the cutoff is exactly READY."""
    instrument_id = InstrumentId.deterministic("reference", "nse-equity-ready")
    bars = tuple(
        _bar(instrument_id, AS_OF.date() - timedelta(days=n), retrieved_at=RECORDED)
        for n in range(DEFAULT_MULTI_DAY_SESSIONS, -1, -1)
    )
    assert len(bars) == DEFAULT_MULTI_DAY_SESSIONS + 1

    entry = await _classify(symbol="READY", mapped=True, bars=bars)

    assert entry.status is cli.CoverageStatus.READY
    assert entry.enough_history is True
    assert entry.fresh is True
    assert entry.latest_complete == AS_OF.date()
    assert entry.bar_count == DEFAULT_MULTI_DAY_SESSIONS + 1


@pytest.mark.asyncio
async def test_six_bars_older_than_the_staleness_bound_are_stale_not_insufficient() -> None:
    """Enough history and freshness are independent facts, exactly as market_context treats them."""
    instrument_id = InstrumentId.deterministic("reference", "nse-equity-stale")
    last = AS_OF.date() - timedelta(days=DEFAULT_STALE_AFTER_DAYS + 5)
    bars = tuple(
        _bar(instrument_id, last - timedelta(days=n), retrieved_at=RECORDED)
        for n in range(DEFAULT_MULTI_DAY_SESSIONS, -1, -1)
    )

    entry = await _classify(symbol="STALE", mapped=True, bars=bars)

    assert entry.status is cli.CoverageStatus.STALE
    assert entry.enough_history is True
    assert entry.fresh is False


@pytest.mark.asyncio
async def test_a_bar_retrieved_after_the_cutoff_is_invisible_to_coverage() -> None:
    """The same bitemporal rule digest and market_context already prove, exercised here too."""
    instrument_id = InstrumentId.deterministic("reference", "nse-equity-future")
    future_bar = _bar(instrument_id, AS_OF.date(), retrieved_at=AS_OF + timedelta(days=1))

    entry = await _classify(symbol="FUTURE", mapped=True, bars=(future_bar,))

    assert entry.status is cli.CoverageStatus.NO_DATA


@pytest.mark.asyncio
async def test_an_empty_watchlist_produces_no_entries() -> None:
    """An empty watchlist is a valid, complete, empty coverage report."""
    read = GetDailyBarSeries(lambda _account: FakeMarketDataUnitOfWork(FakeDailyBarStore(())))

    entries = await cli._coverage_entries(
        watchlist=(), mapped={}, read=read, account_id=ACCOUNT, as_of=AS_OF
    )

    assert entries == ()


@pytest.mark.asyncio
async def test_entries_are_ordered_by_canonical_symbol() -> None:
    """Stable ordering, independent of watchlist insertion order."""
    watchlist = (
        _watchlist_entry("ZZZ", "nse-equity-zzz"),
        _watchlist_entry("AAA", "nse-equity-aaa"),
    )
    read = GetDailyBarSeries(lambda _account: FakeMarketDataUnitOfWork(FakeDailyBarStore(())))

    entries = await cli._coverage_entries(
        watchlist=watchlist, mapped={}, read=read, account_id=ACCOUNT, as_of=AS_OF
    )

    assert [entry.canonical_symbol for entry in entries] == ["AAA", "ZZZ"]


# --------------------------------------------------------------------------- #
# Aggregate counts and rendering
# --------------------------------------------------------------------------- #


def _coverage(status: cli.CoverageStatus, *, symbol: str = "X") -> cli.InstrumentCoverage:
    return cli.InstrumentCoverage(
        instrument_id=InstrumentId.deterministic("reference", f"nse-equity-{symbol.lower()}"),
        canonical_symbol=symbol,
        company_name=f"{symbol} Limited",
        mapped=status is not cli.CoverageStatus.MISSING_MAPPING,
        source_instrument_id=None,
        earliest_complete=None,
        latest_complete=None,
        bar_count=0,
        enough_history=status in (cli.CoverageStatus.STALE, cli.CoverageStatus.READY),
        fresh=status is cli.CoverageStatus.READY,
        status=status,
    )


def test_aggregate_counts_are_exact() -> None:
    """Every count is exact, not a rounded or sampled estimate."""
    summary = cli.CoverageSummary(
        as_of=AS_OF,
        entries=(
            _coverage(cli.CoverageStatus.MISSING_MAPPING, symbol="A"),
            _coverage(cli.CoverageStatus.NO_DATA, symbol="B"),
            _coverage(cli.CoverageStatus.INSUFFICIENT_HISTORY, symbol="C"),
            _coverage(cli.CoverageStatus.STALE, symbol="D"),
            _coverage(cli.CoverageStatus.READY, symbol="E"),
        ),
    )

    counts = summary.counts

    assert counts == {
        "watchlist": 5,
        "mapped": 4,
        "unmapped": 1,
        "with_bars": 0,
        "no_data": 1,
        "enough_history": 2,
        "insufficient": 1,
        "stale": 1,
    }


def test_rendering_names_every_instrument_and_the_aggregate() -> None:
    """An operator reads the symbol, the status and the aggregate in one screen."""
    summary = cli.CoverageSummary(
        as_of=AS_OF,
        entries=(_coverage(cli.CoverageStatus.READY, symbol="READYCO"),),
    )

    rendered = cli._render_coverage(summary)

    assert "READYCO" in rendered
    assert "READY" in rendered
    assert "watchlist=1" in rendered
    assert AS_OF.isoformat() in rendered


def test_missing_mappings_are_reported_by_name_not_silently_dropped() -> None:
    """A missing instrument stays a distinct, separately testable reporting step."""
    entries = (_coverage(cli.CoverageStatus.MISSING_MAPPING, symbol="GHOST"),)

    text = cli._missing_mappings_text(entries)

    assert "MISSING_MAPPING" in text
    assert "GHOST" in text


def test_no_missing_mappings_renders_no_text() -> None:
    """An empty mapping gap list contributes nothing to the printed report."""
    assert cli._missing_mappings_text(()) == ""
