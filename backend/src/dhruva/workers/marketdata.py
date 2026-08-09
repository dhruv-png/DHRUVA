"""``dhruva-marketdata`` -- resolve the watchlist, then fetch only what is missing.

Two subcommands. ``coverage`` reads what is already stored -- watchlist mapping
state, the earliest and latest visible complete daily bar, and whether that is
enough for the current market-context calculation -- and makes **no external
network call of any kind**. ``refresh`` is the only thing here that talks to a
provider, and it only does so when local coverage says it must: it resolves the
owner watchlist to Zerodha instrument identities through the existing archive
use cases, fetches only the bounded range still missing inside a
twenty-calendar-day bootstrap window, and ingests through the existing
synchronized daily-history path. Neither command places an order, reads a
position, or requires the paid Kite historical-data plan to be active -- only
``refresh`` needs a live, logged-in session at all, and only when something is
actually missing.

**Whole-run refusal is deliberate, not a bug.** ``IngestDailyHistory`` already
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
from datetime import UTC, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx2

from dhruva.contexts.marketdata.api import (
    DEFAULT_MULTI_DAY_SESSIONS,
    DEFAULT_STALE_AFTER_DAYS,
    DailyHistoryRequest,
    GetDailyBarSeries,
    GetDailyBarSeriesQuery,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.application.daily_history import (
    IngestDailyHistory,
    IngestDailyHistoryCommand,
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
    KiteInstrumentMasterAdapter,
    SqlAlchemyReferenceUnitOfWork,
    load_owner_universe,
)
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, MissingDataError, ValidationError
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.shared.time.clock import SystemClock
from dhruva.workers.cli_arguments import parse_account, parse_cutoff

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.contexts.marketdata.application.daily_history import IngestDailyHistoryResult
    from dhruva.contexts.marketdata.domain.ports import DailyHistorySource
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

#: Complete daily bars a current five-session return needs: one more than the
#: sessions it spans (``domain.market_context._multi_day``).
_NEEDED_BARS = DEFAULT_MULTI_DAY_SESSIONS + 1

#: Calendar days of local history a coverage read inspects. Generous margin
#: over the twenty-day bootstrap window so coverage shows the true earliest and
#: latest stored bar rather than an artifact of its own read window; this MVP
#: never backfills further than bootstrap reaches, so the margin is ample.
_COVERAGE_LOOKBACK_DAYS = 60

_IST_OFFSET = timedelta(hours=5, minutes=30)
_HTTP_TIMEOUT_SECONDS = 30.0

_SAFETY = (
    "Reads and writes daily cash/index history only: no order placement, no "
    "positions or holdings, no options, futures or intraday data, and no "
    "provider call at all from 'coverage'. 'refresh' talks to Zerodha only "
    "when local coverage is insufficient, using the session dhruva-broker "
    "already established -- no option here accepts a secret."
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


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Per-instrument coverage plus the aggregate an operator scans first."""

    as_of: datetime
    entries: tuple[InstrumentCoverage, ...]

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


async def run(
    argv: Sequence[str] | None = None,
    *,
    instrument_source: InstrumentMasterSource | None = None,
    history_source: DailyHistorySource | None = None,
    clock: Clock | None = None,
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
            )
            sys.stdout.write(_render_coverage(summary) + "\n")
            return _EXIT_OK

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
        )
    finally:
        await engine.dispose()


async def read_coverage(
    *,
    account_id: AccountId,
    as_of: datetime,
    reference_uow_factory: Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    marketdata_uow_factory: Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
) -> CoverageSummary:
    """Read watchlist mapping and stored-bar coverage; no network call."""
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
    )
    return CoverageSummary(as_of=as_of, entries=entries)


async def _coverage_entries(
    *,
    watchlist: tuple[WatchlistInstrument, ...],
    mapped: dict[InstrumentId, ResolvedCashInstrument],
    read: GetDailyBarSeries,
    account_id: AccountId,
    as_of: datetime,
) -> tuple[InstrumentCoverage, ...]:
    as_of_date = as_of.date()
    from_date = as_of_date - timedelta(days=_COVERAGE_LOOKBACK_DAYS)
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
        fresh = (as_of_date - latest).days <= DEFAULT_STALE_AFTER_DAYS
        if not enough:
            status = CoverageStatus.INSUFFICIENT_HISTORY
        elif not fresh:
            status = CoverageStatus.STALE
        else:
            status = CoverageStatus.READY
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
    for entry in summary.entries:
        earliest = entry.earliest_complete.isoformat() if entry.earliest_complete else "-"
        latest = entry.latest_complete.isoformat() if entry.latest_complete else "-"
        lines.append(
            f"{entry.canonical_symbol:<12} {entry.status.value:<20} "
            f"mapped={'yes' if entry.mapped else 'no':<3} bars={entry.bar_count:<4} "
            f"earliest={earliest:<12} latest={latest:<12}"
        )
    counts = summary.counts
    lines.append("")
    lines.append(
        f"watchlist={counts['watchlist']} mapped={counts['mapped']} "
        f"unmapped={counts['unmapped']} with_bars={counts['with_bars']} "
        f"no_data={counts['no_data']} enough_history={counts['enough_history']} "
        f"insufficient={counts['insufficient']} stale={counts['stale']}"
    )
    return "\n".join(lines)


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
    coverage = await read_coverage(
        account_id=account_id,
        as_of=as_of,
        reference_uow_factory=reference_uow_factory,
        marketdata_uow_factory=marketdata_uow_factory,
    )
    if all(entry.status is CoverageStatus.READY for entry in coverage.entries):
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

    if not needs_history:
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

    required_through = _ist_date(as_of) - timedelta(days=1)
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
