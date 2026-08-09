"""``dhruva-refresh`` -- the owner's normal research refresh, in one explicit command.

Three phases, composed through the exact application interfaces their own
commands already use -- never a subprocess, never a second copy of any
ingestion rule:

1. **Market data.** :func:`~dhruva.workers.marketdata.refresh_market_data`,
   extracted unchanged from ``dhruva-marketdata refresh`` for this reuse: the
   same coverage check, the same twenty-day bootstrap window, the same
   SESSION-only credential read, the same atomic whole-batch refusal.
2. **News.** The same composition ``dhruva-news poll`` performs --
   :func:`~dhruva.contexts.intelligence.domain.search.plan_search_phrases`,
   the GDELT feed adapters with their shared pacing gate, and
   :class:`~dhruva.contexts.intelligence.application.news_polling.PollNewsFeeds`,
   which already returns a structured result rather than raising for a rate
   limit -- this module reads that result, it does not reinvent it.
3. **Brief.** The exact read path ``dhruva-digest --brief`` renders:
   ``BuildWatchlistDigest``, ``GetMarketContext``, ``rank_watchlist``,
   ``top_attention``, ``render_brief``. No second scoring model, no second
   ranking.

There is no scheduler, no daemon and no retry loop here, for the identical
reason ``dhruva-news poll`` has none: the owner decides when this runs by
typing the command, once, and this module runs each phase once and stops.

**The cutoff discipline this module exists to get right.** Every read that
produces the final brief happens *after* both write phases have reached a
terminal state, using a clock reading taken at that point -- never a cutoff
captured before either phase ran. An early cutoff is exactly the defect an
earlier revision of ``dhruva-marketdata refresh`` was flagged for: coverage
computed before a fetch cannot see what the fetch just wrote. This module's
three clock reads (market phase, news phase, final brief) are three separate
calls for that reason, not a stylistic choice.

**Market-data refusal is terminal for the whole run.** ``NOT_AUTHENTICATED``
and a GDELT rate limit are both benign, expected, and do not stop the other
phases -- the brief still renders, from whatever is already stored, with the
gap stated plainly. A market-data *refusal*
(``RESOLUTION_REFUSED``/``BENCHMARK_UNMAPPED``/``INGEST_REFUSED``) is
different in kind: it is a data-quality signal from
``IngestDailyHistory``/``ArchiveOwnerInstrumentMaster`` about the pipeline
itself, not a "nothing to do today" state, so this module stops immediately
and does not attempt news or a brief. Already-stored data is completely
unaffected by the refusal (the whole point of the atomic batch it protects),
so `dhruva-digest --brief` remains available immediately afterward if the
owner wants to see it anyway.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx2

from dhruva.contexts.intelligence.application.news_polling import (
    PollNewsFeeds,
    PollNewsFeedsCommand,
)
from dhruva.contexts.intelligence.application.research_observations import (
    FreezeAttentionObservation,
    FreezeAttentionObservationCommand,
)
from dhruva.contexts.intelligence.application.universe import linkable_universe
from dhruva.contexts.intelligence.application.watchlist_digest import (
    BuildWatchlistDigest,
    BuildWatchlistDigestQuery,
)
from dhruva.contexts.intelligence.domain.attention import ATTENTION_REVISION, top_attention
from dhruva.contexts.intelligence.domain.digest import (
    DIGEST_REVISION,
    MAX_ITEMS_PER_INSTRUMENT,
)
from dhruva.contexts.intelligence.domain.entity_linking import ENTITY_LINKING_REVISION
from dhruva.contexts.intelligence.domain.events import EVENT_CLASSIFICATION_REVISION
from dhruva.contexts.intelligence.domain.news import NEWS_IDENTITY_REVISION
from dhruva.contexts.intelligence.domain.research_observation import (
    ObservationAppendResult,
    ObservationProvenance,
    ObservationSourceHealth,
    ObservationSourceStatus,
)
from dhruva.contexts.intelligence.domain.search import plan_search_phrases
from dhruva.contexts.intelligence.domain.sentiment import SENTIMENT_RULESET_REVISION
from dhruva.contexts.intelligence.infrastructure.gdelt.feed import (
    GdeltDocFeed,
    GdeltQuery,
    GdeltRequestTiming,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.queries import gdelt_query_for
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.attention_presentation import (
    rank_watchlist,
    render_brief,
)
from dhruva.contexts.intelligence.interfaces.digest_export import (
    body_fingerprint,
    market_summary,
)
from dhruva.contexts.intelligence.interfaces.research_packet import (
    PACKET_SCHEMA_VERSION,
    build_packet,
)
from dhruva.contexts.marketdata.api import (
    DEFAULT_MULTI_DAY_SESSIONS,
    MARKET_CONTEXT_REVISION,
    GetMarketContext,
    GetMarketContextQuery,
    contexts_by_instrument,
)
from dhruva.contexts.marketdata.infrastructure.persistence.unit_of_work import (
    SqlAlchemyMarketDataUnitOfWork,
)
from dhruva.contexts.platform.infrastructure.crypto import MasterKeyProvider
from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.contexts.platform.infrastructure.database.identity_unit_of_work import (
    SqlAlchemyIdentityUnitOfWork,
)
from dhruva.contexts.reference.api import GetSharedWatchlist
from dhruva.contexts.reference.infrastructure import SqlAlchemyReferenceUnitOfWork
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, ValidationError
from dhruva.shared.time.clock import SystemClock
from dhruva.workers.cli_arguments import BRIEF_TOP_DEFAULT, parse_account
from dhruva.workers.marketdata import (
    CoverageStatus,
    MarketDataRefreshOutcome,
    MarketDataRefreshStatus,
    read_coverage,
    refresh_market_data,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.contexts.intelligence.application.news_polling import (
        NewsFeed,
        PollNewsFeedsResult,
    )
    from dhruva.contexts.intelligence.domain.attention import ResearchAttention
    from dhruva.contexts.intelligence.domain.digest import WatchlistDigest
    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
    from dhruva.contexts.marketdata.api import MarketContext
    from dhruva.contexts.marketdata.domain.ports import DailyHistorySource
    from dhruva.contexts.reference.domain.ports import InstrumentMasterSource
    from dhruva.shared.config.settings import Settings
    from dhruva.shared.identity import AccountId, InstrumentId
    from dhruva.shared.time.clock import Clock


__all__ = [
    "PhaseStatus",
    "build_parser",
    "main",
]

#: Every phase reached a healthy terminal state.
_EXIT_OK = 0
#: At least one phase reported a benign, expected gap -- no session yet, a
#: rate limit, a provider hiccup -- but nothing was corrupted and the brief
#: rendered anyway.
_EXIT_DEGRADED = 1
#: A phase refused outright. Distinguishable from degraded because an
#: operator's runbook step needs to be able to tell "wait and retry" from
#: "something is wrong, go look."
_EXIT_REFUSED = 2

_SAFETY = (
    "Orchestrates three existing, explicit operations -- market-data refresh, "
    "GDELT news poll, and the compact research brief -- through their own "
    "application interfaces, never a subprocess. No scheduler, no daemon, no "
    "retry loop: it runs once per invocation. No option here accepts a "
    "secret, no order is placed, and no NSE fetch is attempted."
)


class PhaseStatus(StrEnum):
    """How one phase of the refresh ended, independent of which phase it was."""

    #: Reached a healthy terminal state; nothing to act on.
    HEALTHY = "HEALTHY"
    #: A benign, expected gap -- not authenticated yet, rate-limited, a
    #: provider hiccup. Nothing was corrupted; the run continues.
    DEGRADED = "DEGRADED"
    #: A hard refusal. For the market-data phase this stops the whole run.
    REFUSED = "REFUSED"


#: Ordered so the worse of two statuses can be picked with ``max``.
_SEVERITY: dict[PhaseStatus, int] = {
    PhaseStatus.HEALTHY: 0,
    PhaseStatus.DEGRADED: 1,
    PhaseStatus.REFUSED: 2,
}
_EXIT_FOR_SEVERITY: dict[int, int] = {0: _EXIT_OK, 1: _EXIT_DEGRADED, 2: _EXIT_REFUSED}

_MARKET_STATUS_TO_PHASE: dict[MarketDataRefreshStatus, PhaseStatus] = {
    MarketDataRefreshStatus.ALREADY_SUFFICIENT: PhaseStatus.HEALTHY,
    MarketDataRefreshStatus.MAPPING_REFRESHED: PhaseStatus.HEALTHY,
    MarketDataRefreshStatus.INGESTED: PhaseStatus.HEALTHY,
    MarketDataRefreshStatus.NOT_AUTHENTICATED: PhaseStatus.DEGRADED,
    MarketDataRefreshStatus.RESOLUTION_REFUSED: PhaseStatus.REFUSED,
    MarketDataRefreshStatus.BENCHMARK_UNMAPPED: PhaseStatus.REFUSED,
    MarketDataRefreshStatus.INGEST_REFUSED: PhaseStatus.REFUSED,
}


@dataclass(frozen=True, slots=True)
class MarketPhaseReport:
    """The market-data phase, boiled down to what the summary line needs."""

    status: PhaseStatus
    outcome: MarketDataRefreshOutcome
    headline: str


@dataclass(frozen=True, slots=True)
class NewsPhaseReport:
    """The news phase, boiled down to what the summary line needs."""

    status: PhaseStatus
    headline: str
    result: PollNewsFeedsResult | None = None


@dataclass(frozen=True, slots=True)
class ResolvedBrief:
    """The one PIT-resolved state shared by rendering, packet hashing and freezing."""

    digest: WatchlistDigest
    contexts: dict[InstrumentId, MarketContext]
    ranked: tuple[ResearchAttention, ...]
    selected: tuple[ResearchAttention, ...]
    text: str


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dhruva-refresh",
        description=(
            "Run the owner's normal research refresh: market-data refresh, "
            "GDELT news poll, then the compact research brief -- one explicit "
            "invocation, no scheduler."
        ),
        epilog=_SAFETY,
    )
    parser.add_argument("--account", required=True, help="account the refresh is attributed to")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "show which phases would run and why, without any network call, "
            "database write, or broker session access"
        ),
    )
    return parser


async def _universe(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    known_at: datetime,
) -> tuple[LinkableInstrument, ...]:
    """Read the approved active watchlist and project it for news matching."""
    watchlist = await GetSharedWatchlist(
        lambda account: SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)
    ).execute(account_id=account_id, effective_on=known_at.date(), known_at=known_at)
    return linkable_universe(watchlist)


def _planned_queries(
    universe: tuple[LinkableInstrument, ...], settings: Settings, *, on: datetime
) -> tuple[GdeltQuery, ...]:
    """Return the queries one news pass would issue, exactly as ``dhruva-news poll`` plans them."""
    news = settings.news
    if news.watchlist_queries_enabled:
        plan = plan_search_phrases(universe, on=on.date(), batch_size=news.batch_size)
        return tuple(
            gdelt_query_for(batch, timespan=news.timespan, max_records=news.max_records)
            for batch in plan.batches
        )
    if news.query_override is None:
        return ()
    return (
        GdeltQuery(query=news.query_override, timespan=news.timespan, max_records=news.max_records),
    )


def _market_phase_report(outcome: MarketDataRefreshOutcome) -> MarketPhaseReport:
    """Reduce a market-data outcome to the one line and detail an owner reads first."""
    status = _MARKET_STATUS_TO_PHASE[outcome.status]
    ready = outcome.coverage.counts["watchlist"]
    if outcome.status is MarketDataRefreshStatus.ALREADY_SUFFICIENT:
        headline = f"{ready} watchlist instruments ready; no provider call was needed"
    elif outcome.status is MarketDataRefreshStatus.INGESTED:
        result = outcome.ingest_result
        added = result.bars_added if result is not None else 0
        headline = f"{ready} watchlist instruments ready, {added} bar(s) added"
    elif outcome.status is MarketDataRefreshStatus.MAPPING_REFRESHED:
        headline = f"{ready} watchlist instruments ready; mapping refreshed, no bars needed"
    elif outcome.status is MarketDataRefreshStatus.NOT_AUTHENTICATED:
        state = outcome.auth_state.value if outcome.auth_state is not None else "unknown"
        headline = (
            f"no Zerodha session ({state}); showing {ready} already-archived "
            f"instrument(s) as stored, without checking for fresher bars"
        )
    else:
        headline = "refused during market-data refresh; see diagnostics below"
    return MarketPhaseReport(status=status, outcome=outcome, headline=headline)


async def _run_news_phase(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    now: datetime,
    news_feeds: Sequence[NewsFeed] | None,
) -> NewsPhaseReport:
    """Poll GDELT exactly as ``dhruva-news poll`` does, and reduce the result to one line.

    ``news_feeds``, when given, replaces the real GDELT transport entirely --
    the same seam ``dhruva-marketdata refresh`` exposes for its own provider,
    used here so this phase is testable with no HTTP call ever attempted. When
    it is ``None`` (every real invocation), feeds are built from the planned
    queries exactly as ``dhruva-news poll`` builds them.
    """
    universe = await _universe(session_factory, account_id=account_id, known_at=now)

    try:
        if news_feeds is not None:
            feeds: Sequence[NewsFeed] = news_feeds
            result = await PollNewsFeeds(
                feeds,
                lambda account: SqlAlchemyIntelligenceUnitOfWork(
                    session_factory, account_id=account
                ),
            ).execute(
                PollNewsFeedsCommand(account_id=account_id, universe=universe, analysed_at=now)
            )
        else:
            queries = _planned_queries(universe, settings, on=now)
            if not queries:
                return NewsPhaseReport(
                    status=PhaseStatus.DEGRADED,
                    headline="no queryable instruments; nothing was requested",
                )
            news = settings.news
            async with httpx2.AsyncClient(
                timeout=news.timeout_seconds, follow_redirects=True
            ) as client:
                clock = SystemClock()
                timing = GdeltRequestTiming(min_interval_seconds=news.min_request_interval_seconds)
                built = tuple(
                    GdeltDocFeed(client, clock, query, timing=timing) for query in queries
                )
                result = await PollNewsFeeds(
                    built,
                    lambda account: SqlAlchemyIntelligenceUnitOfWork(
                        session_factory, account_id=account
                    ),
                ).execute(
                    PollNewsFeedsCommand(account_id=account_id, universe=universe, analysed_at=now)
                )
    except DhruvaError as error:
        return NewsPhaseReport(status=PhaseStatus.REFUSED, headline=f"refused -- {error}")

    if result.rate_limited:
        headline = f"{result.items_offered} item(s) ingested before the provider stopped the pass"
        return NewsPhaseReport(status=PhaseStatus.DEGRADED, headline=headline, result=result)
    if result.unhealthy:
        headline = f"{len(result.unhealthy)} batch(es) did not reach a usable answer"
        return NewsPhaseReport(status=PhaseStatus.DEGRADED, headline=headline, result=result)
    headline = f"{result.items_offered} item(s) offered, {result.revisions_added} newly archived"
    return NewsPhaseReport(status=PhaseStatus.HEALTHY, headline=headline, result=result)


async def _resolve_brief(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    as_of: datetime,
    settings: Settings,
) -> ResolvedBrief:
    """Resolve once, then share the identical state with every downstream consumer."""
    universe = await _universe(session_factory, account_id=account_id, known_at=as_of)
    window = timedelta(days=settings.news.lookback_days)
    digest = await BuildWatchlistDigest(
        lambda account: SqlAlchemyIntelligenceUnitOfWork(session_factory, account_id=account)
    ).execute(
        BuildWatchlistDigestQuery(
            account_id=account_id,
            universe=universe,
            known_at=as_of,
            published_from=as_of - window,
            published_to=as_of,
            max_items=MAX_ITEMS_PER_INSTRUMENT,
        )
    )
    contexts = contexts_by_instrument(
        await GetMarketContext(
            lambda account: SqlAlchemyMarketDataUnitOfWork(session_factory, account_id=account)
        ).execute(
            GetMarketContextQuery(
                account_id=account_id,
                instrument_ids=tuple(entry.instrument_id for entry in universe),
                known_at=as_of,
                sessions=DEFAULT_MULTI_DAY_SESSIONS,
            )
        )
    )
    ranked = rank_watchlist(digest, contexts)
    selected = top_attention(ranked, limit=BRIEF_TOP_DEFAULT)
    return ResolvedBrief(
        digest=digest,
        contexts=contexts,
        ranked=ranked,
        selected=selected,
        text=render_brief(selected, digest=digest, contexts=contexts, total_ranked=len(ranked)),
    )


async def _render_brief(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    as_of: datetime,
    settings: Settings,
) -> str:
    """Compatibility wrapper for callers that need only the resolved rendering."""
    resolved = await _resolve_brief(
        session_factory,
        account_id=account_id,
        as_of=as_of,
        settings=settings,
    )
    return resolved.text


async def _freeze_attention_observation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    resolved: ResolvedBrief,
    market_report: MarketPhaseReport,
    news_report: NewsPhaseReport,
) -> ObservationAppendResult:
    """Persist exactly the state already resolved for the final brief."""
    cutoff = resolved.digest.known_at
    packet = build_packet(
        resolved.selected,
        ranked=resolved.ranked,
        digest=resolved.digest,
        contexts=resolved.contexts,
        account_id=account_id,
        generated_at=cutoff,
        requested_top=BRIEF_TOP_DEFAULT,
    )
    packet_hash = str(packet["envelope"]["body_sha256"])
    market_fingerprints = {
        instrument_id: body_fingerprint(market_summary(context))
        for instrument_id, context in resolved.contexts.items()
    }
    reasons = tuple(
        reason
        for status, reason in (
            (market_report.status, f"market data: {market_report.headline}"),
            (news_report.status, f"news: {news_report.headline}"),
        )
        if status is not PhaseStatus.HEALTHY
    )
    provenance = ObservationProvenance(
        attention_revision=ATTENTION_REVISION,
        digest_revision=resolved.digest.revision,
        digest_policy_revision=DIGEST_REVISION,
        entity_linking_revision=ENTITY_LINKING_REVISION,
        event_classification_revision=EVENT_CLASSIFICATION_REVISION,
        market_context_revision=MARKET_CONTEXT_REVISION,
        news_identity_revision=NEWS_IDENTITY_REVISION,
        sentiment_revision=SENTIMENT_RULESET_REVISION,
        packet_schema_revision=PACKET_SCHEMA_VERSION,
    )
    health = ObservationSourceHealth(
        market=ObservationSourceStatus(market_report.status.value),
        news=ObservationSourceStatus(news_report.status.value),
        degraded_reasons=reasons,
    )
    return await FreezeAttentionObservation(
        lambda account: SqlAlchemyIntelligenceUnitOfWork(session_factory, account_id=account)
    ).execute(
        FreezeAttentionObservationCommand(
            account_id=account_id,
            cutoff=cutoff,
            # One injected instant identifies both the PIT read and its immediate
            # freeze. No second wall-clock value can alter logical identity.
            recorded_at=cutoff,
            digest=resolved.digest,
            ranked=resolved.ranked,
            market_context_fingerprints=market_fingerprints,
            packet_body_sha256=packet_hash,
            provenance=provenance,
            source_health=health,
        )
    )


def _render_summary(
    account_id: AccountId,
    market_report: MarketPhaseReport,
    news_report: NewsPhaseReport,
    brief_as_of: datetime,
    frozen: ObservationAppendResult,
) -> str:
    """Render the compact, owner-oriented header -- one status line per phase."""
    return "\n".join(
        (
            "DHRUVA research refresh",
            f"account     : {account_id}",
            f"market data : {market_report.status.value}",
            f"              {market_report.headline}",
            f"news        : {news_report.status.value}",
            f"              {news_report.headline}",
            f"brief cutoff: {brief_as_of.isoformat()}",
            "observation : ATTENTION_OBSERVATION "
            f"({'appended' if frozen.created else 'already stored'})",
            f"              {frozen.stored.observation.observation_sha256}",
        )
    )


def _render_market_refusal(market_report: MarketPhaseReport) -> str:
    """Render the terminal banner when a market-data refusal halts the whole run."""
    return "\n".join(
        (
            "DHRUVA research refresh",
            f"market data : {market_report.status.value}",
            f"              {market_report.headline}",
            "",
            "refresh halted: a market-data refusal is a data-quality signal, not a "
            "quiet day, so news and the brief were not attempted. Already-stored "
            "data is unaffected -- run `dhruva-digest --brief` to see it, or "
            "`dhruva-marketdata refresh` for full diagnostics.",
        )
    )


async def _dry_run(
    *,
    account_id: AccountId,
    settings: Settings,
    clock: Clock,
    reference_uow_factory: Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    marketdata_uow_factory: Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
) -> int:
    """Report which phases would run, making no network call and no write."""
    as_of = clock.now()
    coverage = await read_coverage(
        account_id=account_id,
        as_of=as_of,
        reference_uow_factory=reference_uow_factory,
        marketdata_uow_factory=marketdata_uow_factory,
    )
    insufficient = sum(1 for entry in coverage.entries if entry.status is not CoverageStatus.READY)
    market_line = (
        "market data : would skip -- local coverage is already sufficient"
        if insufficient == 0
        else f"market data : would call Zerodha -- {insufficient} instrument(s) need bars"
    )

    watchlist = await GetSharedWatchlist(reference_uow_factory).execute(
        account_id=account_id, effective_on=as_of.date(), known_at=as_of
    )
    universe = linkable_universe(watchlist)
    queries = _planned_queries(universe, settings, on=as_of)
    news_line = (
        f"news        : would issue {len(queries)} GDELT batch(es)"
        if queries
        else "news        : would skip -- no queryable instruments"
    )

    sys.stdout.write(
        "\n".join(
            (
                "DHRUVA research refresh -- dry run",
                f"as of       : {as_of.isoformat()}",
                market_line,
                news_line,
                "brief cutoff: would render after both phases, from whatever is stored",
                "",
                "--dry-run: no network call was made and nothing was written.",
            )
        )
        + "\n"
    )
    return _EXIT_OK


async def run(
    argv: Sequence[str] | None = None,
    *,
    instrument_source: InstrumentMasterSource | None = None,
    history_source: DailyHistorySource | None = None,
    news_feeds: Sequence[NewsFeed] | None = None,
    clock: Clock | None = None,
) -> int:
    """Run the three phases against a freshly built engine.

    ``instrument_source``/``history_source`` are the exact seam
    ``dhruva-marketdata refresh`` exposes, passed straight through to
    :func:`~dhruva.workers.marketdata.refresh_market_data` unchanged, so the
    market-data phase is testable with no HTTP call ever attempted.
    ``news_feeds`` is the analogous seam for the news phase -- when given, it
    replaces the real GDELT transport entirely, exactly as ``news_feeds`` in
    ``test_news_workflow.py`` replaces it for ``dhruva-news poll``. ``clock``
    is injected (ADR-011) and read once per phase; the brief's own cutoff is a
    fourth, later read, taken only after both write phases have finished.
    """
    args = build_parser().parse_args(argv)
    account_id = parse_account(args.account)
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
        if args.dry_run:
            return await _dry_run(
                account_id=account_id,
                settings=settings,
                clock=active_clock,
                reference_uow_factory=reference_uow,
                marketdata_uow_factory=marketdata_uow,
            )

        market_as_of = active_clock.now()
        market_outcome = await refresh_market_data(
            account_id=account_id,
            as_of=market_as_of,
            settings=settings,
            reference_uow_factory=reference_uow,
            marketdata_uow_factory=marketdata_uow,
            identity_uow_factory=identity_uow,
            key_provider=MasterKeyProvider(settings.crypto.master_key),
            clock=active_clock,
            instrument_source=instrument_source,
            history_source=history_source,
        )
        market_report = _market_phase_report(market_outcome)

        if market_report.status is PhaseStatus.REFUSED:
            sys.stdout.write(_render_market_refusal(market_report) + "\n")
            if market_outcome.stderr:
                sys.stderr.write(market_outcome.stderr)
            return _EXIT_REFUSED

        news_now = active_clock.now()
        news_report = await _run_news_phase(
            settings,
            session_factory,
            account_id=account_id,
            now=news_now,
            news_feeds=news_feeds,
        )

        brief_as_of = active_clock.now()
        resolved = await _resolve_brief(
            session_factory, account_id=account_id, as_of=brief_as_of, settings=settings
        )
        frozen = await _freeze_attention_observation(
            session_factory,
            account_id=account_id,
            resolved=resolved,
            market_report=market_report,
            news_report=news_report,
        )

        summary = _render_summary(account_id, market_report, news_report, brief_as_of, frozen)
        sys.stdout.write(summary + "\n\n" + resolved.text + "\n")

        severity = max(_SEVERITY[market_report.status], _SEVERITY[news_report.status])
        return _EXIT_FOR_SEVERITY[severity]
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    try:
        return asyncio.run(run(argv))
    except ValidationError as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_REFUSED
    except DhruvaError as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
