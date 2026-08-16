"""``dhruva-marketdata`` -- routine refresh and explicit historical acquisition.

``coverage`` reads full locally visible history and makes **no external network
call of any kind**. ``refresh`` resolves the owner watchlist and fetches only a
twenty-calendar-day routine bootstrap when current context needs it.  Routine
freshness ends at the latest configured trading session whose close is at or
before the refresh cutoff; a weekend is never itself demanded as a bar.
``backfill-plan`` is a separate network-free historical dry run; ``backfill``
executes its bounded deterministic chunks. Only the two execution commands need
a live logged-in session, and neither accepts a secret or exposes an order API.

**Routine whole-run refusal is deliberate, not a bug.** ``IngestDailyHistory`` already
fetches every requested instrument before opening a transaction and validates
the whole batch against one shared benchmark calendar before writing anything,
so one incompatible instrument -- a stale session, a provider timeout, a
malformed payload -- refuses the entire attempted refresh and commits zero
rows. This module adds no partial-success path on top of that; it reports the
refusal, names the offender where the error carries one, and leaves every
previously committed bar exactly where it was.

**The bootstrap window is twenty calendar days, and this module never asks for
more.** That bound lives in configuration (``settings.marketdata``), is
enforced there, and is repeated here only as the arithmetic that turns it into
a date range. A watchlist instrument still short of the six complete sessions a
current market-context calculation needs after that window is reported
``INSUFFICIENT_HISTORY`` -- the window is never silently extended further back
to manufacture a longer answer.

**The broker session is reused, never re-invented.** Authentication is
``dhruva-broker``'s job; this module opens exactly the ``ENROLMENT`` and
``SESSION`` credentials that command already established, through the same
:class:`~dhruva.contexts.platform.application.identity.broker_credentials.GetBrokerCredential`
and :class:`~dhruva.contexts.platform.application.broker.DescribeBrokerAuthentication`
it uses, and reports ``SESSION_MISSING`` or ``SESSION_EXPIRED`` explicitly
rather than failing on a confusing provider error three calls later. No option
here accepts a secret of any kind.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx2

from dhruva.contexts.marketdata.api import (
    DEFAULT_MULTI_DAY_SESSIONS,
    AdjustmentStatus,
    DailyHistoryRequest,
    GetDailyBarSeries,
    GetDailyBarSeriesQuery,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.application.daily_history import (
    IngestDailyHistory,
    IngestDailyHistoryCommand,
    IngestHistoricalDailyHistory,
    IngestHistoricalDailyHistoryCommand,
)
from dhruva.contexts.marketdata.application.historical_backfill import (
    EVALUATION_TARGET_SESSIONS,
    FEATURE_SESSION_THRESHOLDS,
    OPERATIONAL_TARGET_SESSIONS,
    BackfillInstrument,
    BackfillUniverseRole,
    BenchmarkReturnBasis,
    HistoricalBackfillPlan,
    plan_historical_backfill,
)
from dhruva.contexts.marketdata.infrastructure.persistence.unit_of_work import (
    SqlAlchemyMarketDataUnitOfWork,
)
from dhruva.contexts.marketdata.infrastructure.zerodha_history import KiteDailyHistoryAdapter
from dhruva.contexts.platform.application.broker import DescribeBrokerAuthentication
from dhruva.contexts.platform.application.identity.broker_credentials import GetBrokerCredential
from dhruva.contexts.platform.domain.broker.session import (
    BrokerAuthState,
    parse_broker_application,
    parse_broker_session,
)
from dhruva.contexts.platform.domain.identity.credentials import CredentialPurpose
from dhruva.contexts.platform.infrastructure.crypto import MasterKeyProvider
from dhruva.contexts.platform.infrastructure.crypto.credentials import open_credential
from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.contexts.platform.infrastructure.database.identity_unit_of_work import (
    SqlAlchemyIdentityUnitOfWork,
)
from dhruva.contexts.platform.infrastructure.zerodha import KITE_API_BASE
from dhruva.contexts.reference.api import (
    ArchiveOwnerInstrumentMaster,
    GetArchivedInstrumentDiscovery,
    GetLatestArchivedInstrumentDiscovery,
    GetSharedWatchlist,
)
from dhruva.contexts.reference.application.instrument_archive import (
    ArchiveOwnerInstrumentMasterCommand,
)
from dhruva.contexts.reference.infrastructure import (
    ConfiguredNseCashCalendar,
    KiteInstrumentMasterAdapter,
    SqlAlchemyReferenceUnitOfWork,
    load_owner_universe,
)
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, MissingDataError, ValidationError
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.shared.time import DateRange, TradingCalendar
from dhruva.shared.time.clock import SystemClock
from dhruva.workers.cli_arguments import parse_account, parse_cutoff

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.contexts.marketdata.application.daily_history import IngestDailyHistoryResult
    from dhruva.contexts.marketdata.domain.daily_bars import DailyBarRevision
    from dhruva.contexts.marketdata.domain.ports import DailyHistorySource, MarketDataUnitOfWork
    from dhruva.contexts.platform.domain.broker.session import BrokerApplication, BrokerSession
    from dhruva.contexts.reference.domain.instrument_master import (
        ArchivedInstrumentDiscovery,
        ResolvedCashInstrument,
    )
    from dhruva.contexts.reference.domain.ports import InstrumentMasterSource
    from dhruva.contexts.reference.domain.watchlist import WatchlistInstrument
    from dhruva.shared.config.settings import Settings
    from dhruva.shared.time.clock import Clock

__all__ = [
    "CoverageStatus",
    "CoverageSummary",
    "InstrumentCoverage",
    "MarketDataRefreshOutcome",
    "MarketDataRefreshStatus",
    "build_parser",
    "main",
    "read_coverage",
    "refresh_market_data",
]

_EXIT_OK = 0
_EXIT_REFUSED = 2
_EXIT_NOT_AUTHENTICATED = 3

_BROKER = "zerodha"
_PROVIDER = "zerodha"
#: Matches ``InstrumentDiscovery.resolver_revision``'s default. Repeated as a
#: literal here rather than imported because nothing in ``reference`` exports
#: it as a named constant -- the existing archive tests hard-code the same
#: string for the same reason.
_RESOLVER_REVISION = "instrument-discovery-v1"
#: The Nifty 50 benchmark's stable identity, derived exactly as
#: ``ConfigureReferenceUniverse`` derives every other watchlist instrument's,
#: from the fixture's own identity key. It is never on the shared watchlist
#: (``included_in_watchlist: false``), so it never appears in a coverage row --
#: but ``IngestDailyHistory`` requires exactly one benchmark in every
#: synchronized batch, and this is that instrument.
_BENCHMARK_IDENTITY_KEY = "nse-index-nifty-50"
_BENCHMARK_SYMBOL = "NIFTY 50"
_BENCHMARK_NAME = "Nifty 50 price index"

#: Complete daily bars a current five-session return needs: one more than the
#: sessions it spans (``domain.market_context._multi_day``).
_NEEDED_BARS = DEFAULT_MULTI_DAY_SESSIONS + 1

#: Full supported market-date floor for a network-free PIT coverage query.
_COVERAGE_FROM = date(1900, 1, 1)

_IST_OFFSET = timedelta(hours=5, minutes=30)
_HTTP_TIMEOUT_SECONDS = 30.0
_MAX_BACKFILL_YEARS = 10
#: A calendar returning no completed session across this range is unusable for
#: routine freshness.  The bound is only a query span handed to the calendar;
#: session selection itself remains calendar-owned.
_COMPLETED_SESSION_SEARCH_DAYS = 32

_SAFETY = (
    "Reads and writes daily cash/index history only: no order placement, no "
    "positions or holdings, no options, futures or intraday data, and no "
    "provider call at all from 'coverage' or 'backfill-plan'. 'refresh' and explicit "
    "'backfill' use only the session dhruva-broker already established -- no "
    "option here accepts a secret."
)


class CoverageStatus(StrEnum):
    """What a network-free read can say about one watchlist instrument."""

    #: No current Zerodha mapping is on record for this instrument.
    MISSING_MAPPING = "MISSING_MAPPING"
    #: Mapped, but nothing has ever been ingested.
    NO_DATA = "NO_DATA"
    #: Mapped and has bars, but fewer than a market-context read needs.
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    #: Enough bars, but the latest one is older than the staleness bound.
    STALE = "STALE"
    #: Enough bars and fresh as of the cutoff.
    READY = "READY"


@dataclass(frozen=True, slots=True)
class InstrumentCoverage:
    """Locally knowable coverage state for one watchlist instrument."""

    instrument_id: InstrumentId
    canonical_symbol: str
    company_name: str
    mapped: bool
    source_instrument_id: int | None
    earliest_complete: date | None
    latest_complete: date | None
    bar_count: int
    enough_history: bool
    fresh: bool
    status: CoverageStatus
    history_span_years: float = 0.0
    feature_20_ready: bool = False
    feature_60_ready: bool = False
    feature_120_ready: bool = False
    feature_200_ready: bool = False
    feature_252_ready: bool = False
    operational_gap_sessions: int = OPERATIONAL_TARGET_SESSIONS
    evaluation_depth_gap_sessions: int = EVALUATION_TARGET_SESSIONS
    adjustment_status: AdjustmentStatus | None = None
    universe_role: BackfillUniverseRole = BackfillUniverseRole.OWNER_WATCHLIST


@dataclass(frozen=True, slots=True)
class _HistoricalCoverage:
    """Derived acquisition-depth fields for one compatible stored series."""

    history_span_years: float
    feature_20_ready: bool
    feature_60_ready: bool
    feature_120_ready: bool
    feature_200_ready: bool
    feature_252_ready: bool
    operational_gap_sessions: int
    evaluation_depth_gap_sessions: int
    adjustment_status: AdjustmentStatus


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Per-instrument coverage plus the aggregate an operator scans first."""

    as_of: datetime
    entries: tuple[InstrumentCoverage, ...]
    benchmark: InstrumentCoverage | None = None

    @property
    def counts(self) -> dict[str, int]:
        """Return the aggregate an operator reads before any single row."""
        entries = self.entries
        return {
            "watchlist": len(entries),
            "mapped": sum(item.mapped for item in entries),
            "unmapped": sum(not item.mapped for item in entries),
            "with_bars": sum(item.bar_count > 0 for item in entries),
            "no_data": sum(item.status is CoverageStatus.NO_DATA for item in entries),
            "enough_history": sum(item.enough_history for item in entries),
            "insufficient": sum(
                item.status is CoverageStatus.INSUFFICIENT_HISTORY for item in entries
            ),
            "stale": sum(item.status is CoverageStatus.STALE for item in entries),
        }


def _coverage_is_ready(summary: CoverageSummary) -> bool:
    """Return whether owner symbols and the available benchmark are current."""
    benchmark_ready = summary.benchmark is None or summary.benchmark.status is CoverageStatus.READY
    return benchmark_ready and all(
        entry.status is CoverageStatus.READY for entry in summary.entries
    )


class MarketDataRefreshStatus(StrEnum):
    """Every terminal state one refresh attempt can reach, independent of text.

    Exists so a caller other than this CLI -- ``dhruva-refresh``'s
    orchestration is the first one -- can decide what a refresh attempt did
    without parsing the printed report. Every member here corresponds to
    exactly one ``return`` inside :func:`refresh_market_data`.
    """

    #: Local coverage already met every instrument's need; no provider call.
    ALREADY_SUFFICIENT = "ALREADY_SUFFICIENT"
    #: The broker session was missing, expired, or nothing is enrolled.
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    #: Instrument-master resolution raised a ``DhruvaError``.
    RESOLUTION_REFUSED = "RESOLUTION_REFUSED"
    #: The Nifty 50 benchmark has no current Zerodha mapping.
    BENCHMARK_UNMAPPED = "BENCHMARK_UNMAPPED"
    #: Mapping was refreshed but no instrument actually needed new bars.
    MAPPING_REFRESHED = "MAPPING_REFRESHED"
    #: ``IngestDailyHistory`` raised a ``DhruvaError``; nothing was persisted.
    INGEST_REFUSED = "INGEST_REFUSED"
    #: The bounded synchronized batch was fetched and committed.
    INGESTED = "INGESTED"


#: The statuses under which a provider call was never even attempted, so no
#: benchmark, resolution or ingest information exists to report.
_NO_PROVIDER_CALL = frozenset(
    {MarketDataRefreshStatus.ALREADY_SUFFICIENT, MarketDataRefreshStatus.NOT_AUTHENTICATED}
)


@dataclass(frozen=True, slots=True)
class MarketDataRefreshOutcome:
    """The terminal result of one refresh attempt, before any text is rendered.

    Carries exactly what :func:`refresh_market_data` decided and nothing about
    how to print it -- ``stdout``/``stderr`` are this CLI's own rendering of
    that decision, byte-identical to what it printed before this type existed,
    kept here only so ``main`` stays a thin ``sys.stdout.write(outcome.stdout)``.
    A caller that wants its own rendering, such as ``dhruva-refresh``, reads
    ``status``/``coverage``/``auth_state`` and ignores both text fields.
    """

    status: MarketDataRefreshStatus
    exit_code: int
    #: Coverage as best known when this outcome was decided: the initial read
    #: for every early-exit status, the post-ingest read for
    #: ``MAPPING_REFRESHED``/``INGESTED``.
    coverage: CoverageSummary
    auth_state: BrokerAuthState | None = None
    ingest_result: IngestDailyHistoryResult | None = None
    still_missing: tuple[InstrumentCoverage, ...] = ()
    stdout: str = ""
    stderr: str = ""

    @property
    def attempted_provider_call(self) -> bool:
        """Return whether this outcome reached (or tried to reach) Zerodha."""
        return self.status not in _NO_PROVIDER_CALL


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dhruva-marketdata",
        description=(
            "Resolve the owner watchlist to Zerodha instrument identities and "
            "maintain bounded daily cash/index history. 'coverage' reads stored "
            "state only. 'refresh' fetches only the bounded range local "
            "coverage says is missing."
        ),
        epilog=_SAFETY,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    coverage = sub.add_parser(
        "coverage", help="report stored daily-bar coverage for the watchlist; no network call"
    )
    coverage.add_argument("--account", required=True, help="account the read is attributed to")
    coverage.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 knowledge cutoff; nothing first retrieved after it is used (default: now)",
    )

    plan = sub.add_parser(
        "backfill-plan",
        help="plan bounded historical chunks from local coverage; never calls a provider",
    )
    _add_backfill_arguments(plan)

    backfill = sub.add_parser(
        "backfill",
        help="execute an explicit bounded historical plan sequentially",
    )
    _add_backfill_arguments(backfill)

    refresh = sub.add_parser(
        "refresh",
        help="fetch only the missing bounded daily history and ingest it synchronously",
    )
    refresh.add_argument("--account", required=True, help="account the refresh is attributed to")
    refresh.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 instant the refresh is run at; bounds the bootstrap window (default: now)",
    )
    return parser


def _add_backfill_arguments(parser: argparse.ArgumentParser) -> None:
    """Add one explicitly bounded target shape to a historical command."""
    parser.add_argument("--account", required=True, help="account the operation is attributed to")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 knowledge/operation cutoff (default: now)",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--years", type=int, help="whole years to acquire (1 through 10)")
    target.add_argument("--from", dest="from_date", help="earliest market date (YYYY-MM-DD)")


async def run(
    argv: Sequence[str] | None = None,
    *,
    instrument_source: InstrumentMasterSource | None = None,
    history_source: DailyHistorySource | None = None,
    clock: Clock | None = None,
    trading_calendar: TradingCalendar | None = None,
) -> int:
    """Dispatch one subcommand against a freshly built engine.

    ``instrument_source`` and ``history_source`` let a caller substitute a
    synthetic Kite provider without touching a network -- the same seam
    ``dhruva-broker`` leaves for its hidden-prompt injection, used here so the
    whole refresh flow, including the SESSION credential check, is testable
    against a real (test) database with no HTTP call ever attempted.
    """
    args = build_parser().parse_args(argv)
    account_id = parse_account(args.account)
    as_of = parse_cutoff(args.as_of)
    settings = load_settings()
    active_clock: Clock = clock if clock is not None else SystemClock()
    active_calendar = trading_calendar or ConfiguredNseCashCalendar()

    engine = build_engine(settings.db)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)

    def reference_uow(account: AccountId) -> SqlAlchemyReferenceUnitOfWork:
        return SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)

    def marketdata_uow(account: AccountId) -> SqlAlchemyMarketDataUnitOfWork:
        return SqlAlchemyMarketDataUnitOfWork(session_factory, account_id=account)

    def identity_uow(account: AccountId) -> SqlAlchemyIdentityUnitOfWork:
        return SqlAlchemyIdentityUnitOfWork(session_factory, account_id=account)

    try:
        if args.command == "coverage":
            summary = await read_coverage(
                account_id=account_id,
                as_of=as_of,
                reference_uow_factory=reference_uow,
                marketdata_uow_factory=marketdata_uow,
                trading_calendar=active_calendar,
            )
            sys.stdout.write(_render_coverage(summary) + "\n")
            return _EXIT_OK

        if args.command in {"backfill-plan", "backfill"}:
            return await _backfill_command(
                args=args,
                account_id=account_id,
                as_of=as_of,
                reference_uow_factory=reference_uow,
                marketdata_uow_factory=marketdata_uow,
                identity_uow_factory=identity_uow,
                key_provider=MasterKeyProvider(settings.crypto.master_key),
                clock=active_clock,
                history_source=history_source,
            )

        return await _refresh(
            account_id=account_id,
            as_of=as_of,
            settings=settings,
            reference_uow_factory=reference_uow,
            marketdata_uow_factory=marketdata_uow,
            identity_uow_factory=identity_uow,
            key_provider=MasterKeyProvider(settings.crypto.master_key),
            clock=active_clock,
            instrument_source=instrument_source,
            history_source=history_source,
            trading_calendar=active_calendar,
        )
    finally:
        await engine.dispose()


async def read_coverage(
    *,
    account_id: AccountId,
    as_of: datetime,
    reference_uow_factory: Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    marketdata_uow_factory: Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
    trading_calendar: TradingCalendar | None = None,
) -> CoverageSummary:
    """Read watchlist mapping and stored-bar coverage; no network call."""
    active_calendar = trading_calendar or ConfiguredNseCashCalendar()
    required_through = _required_through(as_of, active_calendar)
    watchlist = await GetSharedWatchlist(reference_uow_factory).execute(
        account_id=account_id, effective_on=as_of.date(), known_at=as_of
    )
    discovery = await GetLatestArchivedInstrumentDiscovery(reference_uow_factory).execute(
        account_id=account_id, provider=_PROVIDER, resolver_revision=_RESOLVER_REVISION
    )
    mapped = _mapped_cash(discovery)
    read = GetDailyBarSeries(marketdata_uow_factory)
    entries = await _coverage_entries(
        watchlist=watchlist,
        mapped=mapped,
        read=read,
        account_id=account_id,
        as_of=as_of,
        required_through=required_through,
    )
    benchmark = await _benchmark_coverage(
        mapped=mapped,
        read=read,
        account_id=account_id,
        as_of=as_of,
        required_through=required_through,
    )
    return CoverageSummary(as_of=as_of, entries=entries, benchmark=benchmark)


async def _benchmark_coverage(
    *,
    mapped: dict[InstrumentId, ResolvedCashInstrument],
    read: GetDailyBarSeries,
    account_id: AccountId,
    as_of: datetime,
    required_through: date,
) -> InstrumentCoverage:
    benchmark_id = InstrumentId.deterministic("reference", _BENCHMARK_IDENTITY_KEY)
    cash = mapped.get(benchmark_id)
    if cash is None:
        return InstrumentCoverage(
            instrument_id=benchmark_id,
            canonical_symbol=_BENCHMARK_SYMBOL,
            company_name=_BENCHMARK_NAME,
            mapped=False,
            source_instrument_id=None,
            earliest_complete=None,
            latest_complete=None,
            bar_count=0,
            enough_history=False,
            fresh=False,
            status=CoverageStatus.MISSING_MAPPING,
            universe_role=BackfillUniverseRole.BENCHMARK,
        )
    try:
        series = await read.execute(
            GetDailyBarSeriesQuery(
                account_id=account_id,
                instrument_id=benchmark_id,
                from_date=_COVERAGE_FROM,
                to_date=as_of.date(),
                known_at=as_of,
                require_complete=True,
            )
        )
    except MissingDataError:
        return InstrumentCoverage(
            instrument_id=benchmark_id,
            canonical_symbol=_BENCHMARK_SYMBOL,
            company_name=_BENCHMARK_NAME,
            mapped=True,
            source_instrument_id=cash.instrument_token,
            earliest_complete=None,
            latest_complete=None,
            bar_count=0,
            enough_history=False,
            fresh=False,
            status=CoverageStatus.NO_DATA,
            universe_role=BackfillUniverseRole.BENCHMARK,
        )
    bars = series.bars
    latest = bars[-1].candle.trading_date
    enough = len(bars) >= _NEEDED_BARS
    fresh = latest >= required_through
    status = (
        CoverageStatus.INSUFFICIENT_HISTORY
        if not enough
        else CoverageStatus.READY
        if fresh
        else CoverageStatus.STALE
    )
    history = _historical_coverage_fields(bars)
    return InstrumentCoverage(
        instrument_id=benchmark_id,
        canonical_symbol=_BENCHMARK_SYMBOL,
        company_name=_BENCHMARK_NAME,
        mapped=True,
        source_instrument_id=cash.instrument_token,
        earliest_complete=bars[0].candle.trading_date,
        latest_complete=latest,
        bar_count=len(bars),
        enough_history=enough,
        fresh=fresh,
        status=status,
        universe_role=BackfillUniverseRole.BENCHMARK,
        history_span_years=history.history_span_years,
        feature_20_ready=history.feature_20_ready,
        feature_60_ready=history.feature_60_ready,
        feature_120_ready=history.feature_120_ready,
        feature_200_ready=history.feature_200_ready,
        feature_252_ready=history.feature_252_ready,
        operational_gap_sessions=history.operational_gap_sessions,
        evaluation_depth_gap_sessions=history.evaluation_depth_gap_sessions,
        adjustment_status=history.adjustment_status,
    )


def _historical_coverage_fields(
    bars: tuple[DailyBarRevision, ...],
) -> _HistoricalCoverage:
    """Derive feature depth without claiming that depth validates a model."""
    count = len(bars)
    span_days = (bars[-1].candle.trading_date - bars[0].candle.trading_date).days
    ready = {threshold: count >= threshold for threshold in FEATURE_SESSION_THRESHOLDS}
    return _HistoricalCoverage(
        history_span_years=span_days / 365.2425,
        feature_20_ready=ready[20],
        feature_60_ready=ready[60],
        feature_120_ready=ready[120],
        feature_200_ready=ready[200],
        feature_252_ready=ready[252],
        operational_gap_sessions=max(0, OPERATIONAL_TARGET_SESSIONS - count),
        evaluation_depth_gap_sessions=max(0, EVALUATION_TARGET_SESSIONS - count),
        adjustment_status=bars[0].adjustment_status,
    )


def _yn(value: bool) -> str:
    return "yes" if value else "no"


async def _coverage_entries(  # noqa: PLR0913 - one field per coverage input
    *,
    watchlist: tuple[WatchlistInstrument, ...],
    mapped: dict[InstrumentId, ResolvedCashInstrument],
    read: GetDailyBarSeries,
    account_id: AccountId,
    as_of: datetime,
    required_through: date,
) -> tuple[InstrumentCoverage, ...]:
    as_of_date = as_of.date()
    from_date = _COVERAGE_FROM
    entries: list[InstrumentCoverage] = []
    for item in sorted(watchlist, key=lambda entry: entry.identity.canonical_symbol):
        identity = item.identity
        cash = mapped.get(identity.instrument_id)
        if cash is None:
            entries.append(
                InstrumentCoverage(
                    instrument_id=identity.instrument_id,
                    canonical_symbol=identity.canonical_symbol,
                    company_name=identity.company_name,
                    mapped=False,
                    source_instrument_id=None,
                    earliest_complete=None,
                    latest_complete=None,
                    bar_count=0,
                    enough_history=False,
                    fresh=False,
                    status=CoverageStatus.MISSING_MAPPING,
                )
            )
            continue
        try:
            series = await read.execute(
                GetDailyBarSeriesQuery(
                    account_id=account_id,
                    instrument_id=identity.instrument_id,
                    from_date=from_date,
                    to_date=as_of_date,
                    known_at=as_of,
                    require_complete=True,
                )
            )
        except MissingDataError:
            entries.append(
                InstrumentCoverage(
                    instrument_id=identity.instrument_id,
                    canonical_symbol=identity.canonical_symbol,
                    company_name=identity.company_name,
                    mapped=True,
                    source_instrument_id=cash.instrument_token,
                    earliest_complete=None,
                    latest_complete=None,
                    bar_count=0,
                    enough_history=False,
                    fresh=False,
                    status=CoverageStatus.NO_DATA,
                )
            )
            continue
        bars = series.bars
        latest = bars[-1].candle.trading_date
        enough = len(bars) >= _NEEDED_BARS
        fresh = latest >= required_through
        if not enough:
            status = CoverageStatus.INSUFFICIENT_HISTORY
        elif not fresh:
            status = CoverageStatus.STALE
        else:
            status = CoverageStatus.READY
        history = _historical_coverage_fields(bars)
        entries.append(
            InstrumentCoverage(
                instrument_id=identity.instrument_id,
                canonical_symbol=identity.canonical_symbol,
                company_name=identity.company_name,
                mapped=True,
                source_instrument_id=cash.instrument_token,
                earliest_complete=bars[0].candle.trading_date,
                latest_complete=latest,
                bar_count=len(bars),
                enough_history=enough,
                fresh=fresh,
                status=status,
                history_span_years=history.history_span_years,
                feature_20_ready=history.feature_20_ready,
                feature_60_ready=history.feature_60_ready,
                feature_120_ready=history.feature_120_ready,
                feature_200_ready=history.feature_200_ready,
                feature_252_ready=history.feature_252_ready,
                operational_gap_sessions=history.operational_gap_sessions,
                evaluation_depth_gap_sessions=history.evaluation_depth_gap_sessions,
                adjustment_status=history.adjustment_status,
            )
        )
    return tuple(entries)


def _mapped_cash(
    discovery: ArchivedInstrumentDiscovery | None,
) -> dict[InstrumentId, ResolvedCashInstrument]:
    """Index the archive's resolved cash mappings by stable instrument identity."""
    if discovery is None:
        return {}
    return {
        resolution.instrument_id: resolution.cash
        for resolution in discovery.resolutions
        if resolution.cash is not None
    }


def _render_coverage(summary: CoverageSummary) -> str:
    """Render one line per instrument, then the aggregate an operator scans first."""
    lines = [f"coverage as of {summary.as_of.isoformat()}", ""]
    rendered_entries = list(summary.entries)
    if summary.benchmark is not None:
        rendered_entries.append(summary.benchmark)
    for entry in rendered_entries:
        earliest = entry.earliest_complete.isoformat() if entry.earliest_complete else "-"
        latest = entry.latest_complete.isoformat() if entry.latest_complete else "-"
        lines.append(
            f"{entry.canonical_symbol:<12} {entry.status.value:<20} "
            f"mapped={'yes' if entry.mapped else 'no':<3} bars={entry.bar_count:<4} "
            f"earliest={earliest:<12} latest={latest:<12}"
        )
        lines.append(
            f"  role={entry.universe_role.value} span_years={entry.history_span_years:.2f} "
            f"features=20:{_yn(entry.feature_20_ready)} 60:{_yn(entry.feature_60_ready)} "
            f"120:{_yn(entry.feature_120_ready)} 200:{_yn(entry.feature_200_ready)} "
            f"252:{_yn(entry.feature_252_ready)} operational_gap={entry.operational_gap_sessions} "
            f"evaluation_depth_gap={entry.evaluation_depth_gap_sessions} "
            "adjustment="
            f"{entry.adjustment_status.value if entry.adjustment_status else 'UNAVAILABLE'}"
        )
    counts = summary.counts
    lines.append("")
    lines.append(
        f"watchlist={counts['watchlist']} mapped={counts['mapped']} "
        f"unmapped={counts['unmapped']} with_bars={counts['with_bars']} "
        f"no_data={counts['no_data']} enough_history={counts['enough_history']} "
        f"insufficient={counts['insufficient']} stale={counts['stale']}"
    )
    lines.append(
        "evaluation note: 2000 sessions is acquisition-depth readiness only; the current "
        "owner watchlist is not an unbiased historical evaluation universe."
    )
    lines.append("benchmark note: NIFTY 50 is PRICE_INDEX; TRI/total-return data is unavailable.")
    return "\n".join(lines)


async def _build_backfill_plan(
    *,
    args: argparse.Namespace,
    account_id: AccountId,
    as_of: datetime,
    reference_uow_factory: Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    marketdata_uow_factory: Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
) -> tuple[HistoricalBackfillPlan, CoverageSummary]:
    """Build a plan from local PIT state only; this function owns no provider port."""
    coverage = await read_coverage(
        account_id=account_id,
        as_of=as_of,
        reference_uow_factory=reference_uow_factory,
        marketdata_uow_factory=marketdata_uow_factory,
    )
    completed_through = _ist_date(as_of) - timedelta(days=1)
    target_from = _parse_historical_target(args, completed_through)
    if coverage.benchmark is None or not coverage.benchmark.mapped:
        raise ValidationError("historical backfill requires an archived NIFTY 50 provider mapping")
    benchmark = _backfill_instrument(coverage.benchmark, MarketInstrumentKind.INDEX)
    mapped_entries = tuple(entry for entry in coverage.entries if entry.mapped)
    unresolved = tuple(entry.canonical_symbol for entry in coverage.entries if not entry.mapped)
    plan = plan_historical_backfill(
        target_from=target_from,
        completed_through=completed_through,
        benchmark=benchmark,
        benchmark_basis=BenchmarkReturnBasis.PRICE_INDEX,
        instruments=tuple(
            _backfill_instrument(entry, MarketInstrumentKind.CASH_EQUITY)
            for entry in mapped_entries
        ),
        unresolved_symbols=unresolved,
    )
    return plan, coverage


def _parse_historical_target(args: argparse.Namespace, completed_through: date) -> date:
    if args.years is not None:
        if not 1 <= args.years <= _MAX_BACKFILL_YEARS:
            raise ValidationError("--years must be between 1 and 10")
        try:
            return completed_through.replace(year=completed_through.year - args.years)
        except ValueError:
            return completed_through.replace(year=completed_through.year - args.years, day=28)
    if args.from_date is None:
        raise ValidationError("historical backfill requires --years or --from")
    try:
        return date.fromisoformat(args.from_date)
    except ValueError as error:
        raise ValidationError("--from must be a valid YYYY-MM-DD date") from error


def _backfill_instrument(
    entry: InstrumentCoverage, kind: MarketInstrumentKind
) -> BackfillInstrument:
    if entry.source_instrument_id is None:
        raise ValidationError(
            "historical target has no provider token", instrument=entry.canonical_symbol
        )
    return BackfillInstrument(
        instrument_id=entry.instrument_id,
        canonical_symbol=entry.canonical_symbol,
        instrument_kind=kind,
        source_instrument_id=entry.source_instrument_id,
        role=entry.universe_role,
        earliest_stored=entry.earliest_complete,
    )


def _render_backfill_plan(plan: HistoricalBackfillPlan) -> str:
    target_years = (plan.completed_through - plan.target_from).days / 365.2425
    expected_sessions = round(target_years * 252)
    planned_instruments = len({chunk.target_instrument_id for chunk in plan.chunks})
    lines = [
        "historical backfill plan (network-free)",
        f"target={plan.target_from.isoformat()}..{plan.completed_through.isoformat()} ",
        f"target_years={target_years:.2f} expected_sessions_approx={expected_sessions} "
        f"evaluation_target_sessions=2000 instruments_planned={planned_instruments}",
        f"benchmark={_BENCHMARK_SYMBOL} basis={plan.benchmark_basis.value} "
        f"chunks={len(plan.chunks)} provider_requests={plan.provider_requests}",
        "atomicity=one target instrument plus benchmark per chunk; execution is sequential",
    ]
    for chunk in plan.chunks:
        lines.append(
            f"{chunk.ordinal:04d} {chunk.target_symbol:<12} role={chunk.role.value:<15} "
            f"core={chunk.core_from.isoformat()}..{chunk.core_to.isoformat()} "
            f"request={chunk.request_from.isoformat()}..{chunk.request_to.isoformat()} "
            f"calls={len(chunk.requests)}"
        )
    for symbol in plan.unresolved_symbols:
        lines.append(f"BLOCKER MISSING_MAPPING {symbol}")
    lines.append(
        "adjustment=UNKNOWN for Zerodha history; raw observations are never relabelled as adjusted"
    )
    lines.append(
        "universe=OWNER_WATCHLIST only; this is not an unbiased historical evaluation universe"
    )
    lines.append(
        "readiness blockers=HISTORICAL_UNIVERSE_UNAVAILABLE, DELISTING_COVERAGE_UNKNOWN, "
        "CORPORATE_ACTION_UNVERIFIED, ADJUSTMENT_UNKNOWN, SOURCE_LICENSING_UNRESOLVED"
    )
    return "\n".join(lines)


async def _backfill_command(  # noqa: PLR0913 - composition root collaborators
    *,
    args: argparse.Namespace,
    account_id: AccountId,
    as_of: datetime,
    reference_uow_factory: Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    marketdata_uow_factory: Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
    identity_uow_factory: Callable[[AccountId], SqlAlchemyIdentityUnitOfWork],
    key_provider: MasterKeyProvider,
    clock: Clock,
    history_source: DailyHistorySource | None,
) -> int:
    plan, _coverage = await _build_backfill_plan(
        args=args,
        account_id=account_id,
        as_of=as_of,
        reference_uow_factory=reference_uow_factory,
        marketdata_uow_factory=marketdata_uow_factory,
    )
    rendered = _render_backfill_plan(plan)
    if args.command == "backfill-plan":
        sys.stdout.write(rendered + "\n")
        return _EXIT_OK
    if plan.unresolved_symbols:
        sys.stderr.write(rendered + "\nbackfill: REFUSED -- resolve every mapping first.\n")
        return _EXIT_REFUSED
    if not plan.chunks:
        sys.stdout.write(rendered + "\nbackfill: target already covered; no provider call.\n")
        return _EXIT_OK

    source = history_source
    if source is None:
        status = await DescribeBrokerAuthentication(
            identity_uow_factory, key_provider, open_credential, clock
        ).execute(account_id, _BROKER)
        if status.state is not BrokerAuthState.SUCCESS:
            sys.stderr.write(_SESSION_REMEDY.get(status.state, status.state.value) + "\n")
            return _EXIT_NOT_AUTHENTICATED
        application, session = await _open_zerodha_credentials(
            identity_uow_factory, key_provider, account_id
        )
        async with httpx2.AsyncClient(
            base_url=KITE_API_BASE, timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            source = KiteDailyHistoryAdapter(
                client=client,
                api_key=application.identifier,
                access_token=session.token,
                clock=clock,
            )
            return await _execute_backfill(plan, account_id, source, marketdata_uow_factory)
    return await _execute_backfill(plan, account_id, source, marketdata_uow_factory)


async def _execute_backfill(
    plan: HistoricalBackfillPlan,
    account_id: AccountId,
    source: DailyHistorySource,
    marketdata_uow_factory: Callable[[AccountId], MarketDataUnitOfWork],
) -> int:
    ingest = IngestHistoricalDailyHistory(source, marketdata_uow_factory)
    completed = 0
    added = 0
    unchanged = 0
    for chunk in plan.chunks:
        try:
            result = await ingest.execute(
                IngestHistoricalDailyHistoryCommand(
                    account_id=account_id,
                    requests=chunk.requests,
                    benchmark_id=plan.benchmark_id,
                    completed_through=plan.completed_through,
                )
            )
        except DhruvaError as error:
            sys.stderr.write(
                f"backfill: REFUSED at chunk {chunk.ordinal} ({chunk.target_symbol}) -- {error}\n"
                f"partial progress: {completed}/{len(plan.chunks)} chunks committed, "
                f"{added} bars added, {unchanged} unchanged; rerun the plan to resume.\n"
            )
            return _EXIT_REFUSED
        completed += 1
        added += result.bars_added
        unchanged += result.bars_unchanged
    sys.stdout.write(
        f"backfill: completed {completed} chunks / {plan.provider_requests} provider requests; "
        f"{added} bars added, {unchanged} unchanged.\n"
    )
    return _EXIT_OK


_SESSION_REMEDY: dict[BrokerAuthState, str] = {
    BrokerAuthState.ENROLMENT_MISSING: (
        "refresh: REFUSED -- no Zerodha application credential is enrolled for "
        "this account. Run 'dhruva-broker zerodha enrol --account …' first."
    ),
    BrokerAuthState.SESSION_MISSING: (
        "refresh: REFUSED -- SESSION_MISSING: this account has never logged in. "
        "Run 'dhruva-broker zerodha login --account …'."
    ),
    BrokerAuthState.SESSION_EXPIRED: (
        "refresh: REFUSED -- SESSION_EXPIRED: the Kite session ended at 06:00 "
        "IST. Run 'dhruva-broker zerodha login --account …' again."
    ),
}


async def _refresh(  # noqa: PLR0913 - one collaborator per capability, matching broker.py's login
    *,
    account_id: AccountId,
    as_of: datetime,
    settings: Settings,
    reference_uow_factory: Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    marketdata_uow_factory: Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
    identity_uow_factory: Callable[[AccountId], SqlAlchemyIdentityUnitOfWork],
    key_provider: MasterKeyProvider,
    clock: Clock,
    instrument_source: InstrumentMasterSource | None,
    history_source: DailyHistorySource | None,
    trading_calendar: TradingCalendar,
) -> int:
    """Print exactly what ``refresh_market_data`` decided, and return its exit code."""
    outcome = await refresh_market_data(
        account_id=account_id,
        as_of=as_of,
        settings=settings,
        reference_uow_factory=reference_uow_factory,
        marketdata_uow_factory=marketdata_uow_factory,
        identity_uow_factory=identity_uow_factory,
        key_provider=key_provider,
        clock=clock,
        instrument_source=instrument_source,
        history_source=history_source,
        trading_calendar=trading_calendar,
    )
    if outcome.stdout:
        sys.stdout.write(outcome.stdout)
    if outcome.stderr:
        sys.stderr.write(outcome.stderr)
    return outcome.exit_code


async def refresh_market_data(  # noqa: PLR0913 - one collaborator per capability, matching broker.py's login
    *,
    account_id: AccountId,
    as_of: datetime,
    settings: Settings,
    reference_uow_factory: Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    marketdata_uow_factory: Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
    identity_uow_factory: Callable[[AccountId], SqlAlchemyIdentityUnitOfWork],
    key_provider: MasterKeyProvider,
    clock: Clock,
    instrument_source: InstrumentMasterSource | None,
    history_source: DailyHistorySource | None,
    trading_calendar: TradingCalendar | None = None,
) -> MarketDataRefreshOutcome:
    """Inspect coverage first, then fetch only what it says is missing.

    Prints nothing and raises nothing ``IngestDailyHistory``/
    ``ArchiveOwnerInstrumentMaster`` did not already raise as a
    :class:`~dhruva.shared.errors.DhruvaError` -- every terminal state is a
    ``return``, captured in the result's :class:`MarketDataRefreshStatus` so a
    caller can act on it without parsing text. ``dhruva-marketdata refresh``
    and ``dhruva-refresh`` are both thin renderings of this one function; ADR
    fail-closed semantics, the atomic whole-batch refusal, the bounded
    bootstrap window and the SESSION-only credential read are unchanged from
    before this function existed -- extracted, not rewritten.
    """
    active_calendar = trading_calendar or ConfiguredNseCashCalendar()
    coverage = await read_coverage(
        account_id=account_id,
        as_of=as_of,
        reference_uow_factory=reference_uow_factory,
        marketdata_uow_factory=marketdata_uow_factory,
        trading_calendar=active_calendar,
    )
    if _coverage_is_ready(coverage):
        return MarketDataRefreshOutcome(
            status=MarketDataRefreshStatus.ALREADY_SUFFICIENT,
            exit_code=_EXIT_OK,
            coverage=coverage,
            stdout=(
                _render_coverage(coverage)
                + "\n\nrefresh: local coverage is already sufficient; no provider call was made.\n"
            ),
        )

    auth_state: BrokerAuthState | None = None
    if instrument_source is None or history_source is None:
        status = await DescribeBrokerAuthentication(
            identity_uow_factory,
            key_provider,
            open_credential,
            clock,
        ).execute(account_id, _BROKER)
        auth_state = status.state
        if status.state is not BrokerAuthState.SUCCESS:
            return MarketDataRefreshOutcome(
                status=MarketDataRefreshStatus.NOT_AUTHENTICATED,
                exit_code=_EXIT_NOT_AUTHENTICATED,
                coverage=coverage,
                auth_state=auth_state,
                stderr=(
                    _SESSION_REMEDY.get(status.state, f"refresh: REFUSED -- {status.state.value}")
                    + "\n"
                ),
            )

    async with httpx2.AsyncClient(
        base_url=KITE_API_BASE, timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        return await _resolve_and_ingest(
            account_id=account_id,
            as_of=as_of,
            settings=settings,
            client=client,
            initial_coverage=coverage,
            auth_state=auth_state,
            reference_uow_factory=reference_uow_factory,
            marketdata_uow_factory=marketdata_uow_factory,
            identity_uow_factory=identity_uow_factory,
            key_provider=key_provider,
            clock=clock,
            instrument_source=instrument_source,
            history_source=history_source,
            trading_calendar=active_calendar,
        )


async def _resolve_and_ingest(  # noqa: PLR0913 - one collaborator per capability
    *,
    account_id: AccountId,
    as_of: datetime,
    settings: Settings,
    client: httpx2.AsyncClient,
    initial_coverage: CoverageSummary,
    auth_state: BrokerAuthState | None,
    reference_uow_factory: Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    marketdata_uow_factory: Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
    identity_uow_factory: Callable[[AccountId], SqlAlchemyIdentityUnitOfWork],
    key_provider: MasterKeyProvider,
    clock: Clock,
    instrument_source: InstrumentMasterSource | None,
    history_source: DailyHistorySource | None,
    trading_calendar: TradingCalendar,
) -> MarketDataRefreshOutcome:
    """Resolve instrument mapping, then fetch and ingest the bounded missing range."""
    instr_source: InstrumentMasterSource
    hist_source: DailyHistorySource
    if instrument_source is not None and history_source is not None:
        instr_source = instrument_source
        hist_source = history_source
    else:
        application, session = await _open_zerodha_credentials(
            identity_uow_factory, key_provider, account_id
        )
        instr_source = KiteInstrumentMasterAdapter(
            client=client,
            api_key=application.identifier,
            access_token=session.token,
            clock=clock,
        )
        hist_source = KiteDailyHistoryAdapter(
            client=client,
            api_key=application.identifier,
            access_token=session.token,
            clock=clock,
        )

    try:
        archive_result = await ArchiveOwnerInstrumentMaster(
            instr_source, reference_uow_factory
        ).execute(
            ArchiveOwnerInstrumentMasterCommand(
                account_id=account_id,
                definitions=load_owner_universe(account_id, recorded_at=as_of).definitions,
                market_date=_ist_date(as_of),
            )
        )
        discovery = await GetArchivedInstrumentDiscovery(reference_uow_factory).execute(
            account_id=account_id,
            provider=archive_result.provider,
            market_date=archive_result.market_date,
            resolver_revision=archive_result.resolver_revision,
        )
    except DhruvaError as error:
        return MarketDataRefreshOutcome(
            status=MarketDataRefreshStatus.RESOLUTION_REFUSED,
            exit_code=_EXIT_REFUSED,
            coverage=initial_coverage,
            auth_state=auth_state,
            stderr=_refusal_text("instrument resolution", error),
        )

    mapped = _mapped_cash(discovery)
    benchmark_id = InstrumentId.deterministic("reference", _BENCHMARK_IDENTITY_KEY)
    benchmark_cash = mapped.get(benchmark_id)
    if benchmark_cash is None:
        return MarketDataRefreshOutcome(
            status=MarketDataRefreshStatus.BENCHMARK_UNMAPPED,
            exit_code=_EXIT_REFUSED,
            coverage=initial_coverage,
            auth_state=auth_state,
            stderr=(
                "refresh: REFUSED -- the Nifty 50 benchmark has no current Zerodha "
                "mapping; no synchronized session calendar is available.\n"
            ),
        )

    coverage = await read_coverage(
        account_id=account_id,
        as_of=as_of,
        reference_uow_factory=reference_uow_factory,
        marketdata_uow_factory=marketdata_uow_factory,
        trading_calendar=trading_calendar,
    )
    not_ready = tuple(
        entry for entry in coverage.entries if entry.status is not CoverageStatus.READY
    )
    still_missing = tuple(
        entry for entry in not_ready if entry.status is CoverageStatus.MISSING_MAPPING
    )
    needs_history = tuple(
        entry for entry in not_ready if entry.status is not CoverageStatus.MISSING_MAPPING
    )
    benchmark_needs_history = (
        coverage.benchmark is not None and coverage.benchmark.status is not CoverageStatus.READY
    )

    if not needs_history and not benchmark_needs_history:
        return MarketDataRefreshOutcome(
            status=MarketDataRefreshStatus.MAPPING_REFRESHED,
            exit_code=_EXIT_OK,
            coverage=coverage,
            auth_state=auth_state,
            still_missing=still_missing,
            stdout=(
                _render_coverage(coverage)
                + "\n\nrefresh: instrument mapping refreshed; no instrument needed new bars.\n"
                + _missing_mappings_text(still_missing)
            ),
        )

    required_through = _required_through(as_of, trading_calendar)
    from_date = required_through - timedelta(days=settings.marketdata.bootstrap_lookback_days - 1)
    requests = [
        DailyHistoryRequest(
            instrument_id=benchmark_id,
            instrument_kind=MarketInstrumentKind.INDEX,
            source_instrument_id=benchmark_cash.instrument_token,
            from_date=from_date,
            to_date=required_through,
        ),
        *(
            DailyHistoryRequest(
                instrument_id=entry.instrument_id,
                instrument_kind=MarketInstrumentKind.CASH_EQUITY,
                source_instrument_id=mapped[entry.instrument_id].instrument_token,
                from_date=from_date,
                to_date=required_through,
            )
            for entry in needs_history
        ),
    ]

    try:
        result = await IngestDailyHistory(hist_source, marketdata_uow_factory).execute(
            IngestDailyHistoryCommand(
                account_id=account_id,
                requests=tuple(requests),
                benchmark_id=benchmark_id,
                completed_through=required_through,
                required_through=required_through,
            )
        )
    except DhruvaError as error:
        return MarketDataRefreshOutcome(
            status=MarketDataRefreshStatus.INGEST_REFUSED,
            exit_code=_EXIT_REFUSED,
            coverage=coverage,
            auth_state=auth_state,
            stderr=_refusal_text("daily history ingest", error),
        )

    final_coverage = await read_coverage(
        account_id=account_id,
        as_of=as_of,
        reference_uow_factory=reference_uow_factory,
        marketdata_uow_factory=marketdata_uow_factory,
        trading_calendar=trading_calendar,
    )
    return MarketDataRefreshOutcome(
        status=MarketDataRefreshStatus.INGESTED,
        exit_code=_EXIT_OK,
        coverage=final_coverage,
        auth_state=auth_state,
        ingest_result=result,
        still_missing=still_missing,
        stdout=(
            _render_coverage(final_coverage)
            + f"\n\nrefresh: ingested {result.instruments} instruments "
            f"({from_date.isoformat()} to {required_through.isoformat()}), "
            f"{result.bars_added} bars added, {result.bars_unchanged} unchanged.\n"
            + _missing_mappings_text(still_missing)
        ),
    )


def _missing_mappings_text(entries: tuple[InstrumentCoverage, ...]) -> str:
    """State every unresolved mapping by name; a missing instrument is never quiet."""
    return "".join(
        f"  MISSING_MAPPING: {entry.canonical_symbol} has no current Zerodha mapping.\n"
        for entry in entries
    )


def _refusal_text(stage: str, error: DhruvaError) -> str:
    """Render the atomic-refusal banner: what refused, why, and what changed."""
    lines = [f"refresh: REFUSED during {stage} -- {error}"]
    lines.extend(f"    {key}: {value}" for key, value in sorted(error.context.items()))
    lines.append(
        "the attempted synchronized ingest was not persisted; "
        "previously committed data is unchanged."
    )
    return "\n".join(lines) + "\n"


async def _open_zerodha_credentials(
    identity_uow_factory: Callable[[AccountId], SqlAlchemyIdentityUnitOfWork],
    key_provider: MasterKeyProvider,
    account_id: AccountId,
) -> tuple[BrokerApplication, BrokerSession]:
    """Open exactly the ENROLMENT and SESSION credentials dhruva-broker sealed."""
    read = GetBrokerCredential(identity_uow_factory)
    enrolment = await read.sealed(account_id, _BROKER, CredentialPurpose.ENROLMENT)
    application = parse_broker_application(open_credential(enrolment, key_provider))
    session_credential = await read.sealed(account_id, _BROKER, CredentialPurpose.SESSION)
    session = parse_broker_session(open_credential(session_credential, key_provider))
    return application, session


def _ist_date(instant: datetime) -> date:
    """Return the IST calendar date for a UTC-aware instant.

    A fixed offset, not a market calendar: this decides which date a refresh
    treats as "today" for the bootstrap window, not which dates the exchange
    was open. IST has carried no daylight saving since 1945, so the fixed
    offset used for the broker's own session-expiry cutoff is exact here too.
    """
    return (instant.astimezone(UTC) + _IST_OFFSET).date()


def _required_through(instant: datetime, calendar: TradingCalendar) -> date:
    """Return the latest calendar-declared session completed by ``instant``.

    The calendar owns both session membership and close times.  The date range
    here merely bounds the calendar query; selecting ``yesterday`` or manually
    skipping weekend dates would repeat the bug this function prevents.
    """
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValidationError("daily-history cutoff must be timezone-aware")
    utc_instant = instant.astimezone(UTC)
    span = DateRange(
        (utc_instant - timedelta(days=_COMPLETED_SESSION_SEARCH_DAYS)).date(),
        (utc_instant + timedelta(days=2)).date(),
    )
    completed = tuple(
        session
        for day in calendar.sessions_between(span)
        if (session := calendar.session(day)).closes_at <= utc_instant
    )
    if not completed:
        raise MissingDataError(
            "trading calendar has no completed session near the refresh cutoff",
            cutoff=utc_instant.isoformat(),
            searched_from=span.start.isoformat(),
            searched_to=span.end.isoformat(),
        )
    return max(completed, key=lambda session: session.closes_at).day.on


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    try:
        return asyncio.run(run(argv))
    except ValidationError as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_REFUSED
    except DhruvaError as error:
        sys.stderr.write(f"{error}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
