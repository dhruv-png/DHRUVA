"""``dhruva-news`` -- poll GDELT for watchlist news, and read the archive back.

A composition root and an operator tool, in the same sense as ``dhruva-dlq``: it
is the only module in this chain permitted to build an engine and an HTTP client
from settings and hand them to infrastructure. Everything below it receives its
collaborators and can be tested without either.

Two subcommands, and the split between them is the point.

``poll`` reaches the network and writes. It builds the search plan from the
approved watchlist, issues one bounded request per batch, and appends whatever
the healthy batches returned through the existing ingestion path. It stops the
moment a batch is rate-limited and says which one.

``show`` touches no network at all. It answers "what did DHRUVA know about this
instrument at this instant?" against the stored archive, which is the question a
backtest asks and the one a person asks the morning after a move.

There is no scheduler here and no daemon. Something has to decide when a poll
runs, and until that decision is made and reviewed the owner makes it by typing
the command. A background loop that quietly hammered a free service would be the
easiest possible way to lose access to it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx2

from dhruva.contexts.intelligence.application.news_ingestion import (
    GetArchivedNews,
    GetArchivedNewsQuery,
)
from dhruva.contexts.intelligence.application.news_polling import (
    PollNewsFeeds,
    PollNewsFeedsCommand,
)
from dhruva.contexts.intelligence.application.universe import linkable_universe
from dhruva.contexts.intelligence.domain.search import SearchPlan, plan_search_phrases
from dhruva.contexts.intelligence.infrastructure.gdelt.feed import GdeltDocFeed, GdeltQuery
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import GDELT_ATTRIBUTION_URL
from dhruva.contexts.intelligence.infrastructure.gdelt.queries import gdelt_query_for
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.news_presentation import (
    NSE_UNAVAILABLE_NOTICE,
    render_batch_outcomes,
    render_ingestion_counts,
    render_items,
    render_plan,
)
from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.contexts.reference.api import GetSharedWatchlist
from dhruva.contexts.reference.infrastructure import SqlAlchemyReferenceUnitOfWork
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, ValidationError
from dhruva.shared.identity import AccountId
from dhruva.shared.time.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
    from dhruva.shared.config.settings import NewsSettings, Settings

__all__ = ["build_parser", "main"]

#: Exit status when a poll reached the provider but the provider refused. The
#: run is not broken and the operator is not to blame; something upstream said
#: no, and a runbook step needs to be able to tell that from success.
_EXIT_UNHEALTHY = 1
#: Exit status for input or configuration the command will not act on.
_EXIT_REFUSED = 2


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dhruva-news",
        description=(
            "Poll GDELT for news about the approved watchlist and read the "
            "point-in-time archive back. Read-only against the provider; no "
            "article body is ever fetched."
        ),
        epilog=NSE_UNAVAILABLE_NOTICE,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="fetch GDELT metadata and ingest it")
    poll.add_argument("--account", required=True, help="account the pass is attributed to")
    poll.add_argument(
        "--dry-run",
        action="store_true",
        help="print the search plan and exit without contacting the provider",
    )

    show = sub.add_parser("show", help="read archived news for one instrument")
    show.add_argument("--account", required=True, help="account the read is attributed to")
    show.add_argument("--symbol", required=True, help="canonical NSE symbol, such as SBIN")
    show.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 knowledge cutoff; nothing first seen after it is returned (default: now)",
    )
    show.add_argument("--days", type=int, default=None, help="days of publication history to read")
    show.add_argument("--limit", type=int, default=None, help="maximum items to print")

    return parser


def _account(raw: str) -> AccountId:
    """Parse an account identifier, or refuse the command."""
    try:
        return AccountId.parse(raw)
    except DhruvaError as error:
        raise ValidationError("--account is not a valid account identifier") from error


def _instant(raw: str | None, *, field: str) -> datetime:
    """Parse an explicit ISO-8601 instant, defaulting to now in UTC."""
    if raw is None:
        return SystemClock().now()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValidationError(f"{field} is not an ISO-8601 timestamp", value=raw) from error
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _universe(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    known_at: datetime,
) -> tuple[LinkableInstrument, ...]:
    """Read the approved active watchlist and project it for news matching."""
    watchlist = GetSharedWatchlist(
        lambda account: SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)
    )
    members = await watchlist.execute(
        account_id=account_id,
        effective_on=known_at.date(),
        known_at=known_at,
    )
    return linkable_universe(members)


def _feeds(
    client: httpx2.AsyncClient,
    queries: Sequence[GdeltQuery],
) -> tuple[GdeltDocFeed, ...]:
    """Build one bounded transport per planned query, in the planned order."""
    clock = SystemClock()
    return tuple(GdeltDocFeed(client, clock, query) for query in queries)


def _planned_queries(
    plan: SearchPlan,
    settings: NewsSettings,
) -> tuple[GdeltQuery, ...]:
    """Return one documented query per planned batch."""
    return tuple(
        gdelt_query_for(batch, timespan=settings.timespan, max_records=settings.max_records)
        for batch in plan.batches
    )


def _override_query(settings: NewsSettings) -> GdeltQuery:
    """Return the single query an explicit override runs instead of a plan."""
    assert settings.query_override is not None  # noqa: S101 - guarded by the caller
    return GdeltQuery(
        query=settings.query_override,
        timespan=settings.timespan,
        max_records=settings.max_records,
    )


async def _poll(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    dry_run: bool,
) -> int:
    """Run one polling pass and report it batch by batch."""
    news = settings.news
    now = SystemClock().now()
    universe = await _universe(session_factory, account_id=account_id, known_at=now)

    if news.watchlist_queries_enabled:
        plan = plan_search_phrases(universe, on=now.date(), batch_size=news.batch_size)
        queries = _planned_queries(plan, news)
        sys.stdout.write(render_plan(plan) + "\n")
    else:
        # Structurally exclusive: the override *is* the pass, and no watchlist
        # batch is built to be accidentally issued alongside it.
        plan = SearchPlan(batches=(), unqueryable=(), rejected=())
        queries = (_override_query(news),)
        sys.stdout.write(f"search plan: explicit override -- {queries[0].query}\n")

    if not queries:
        sys.stdout.write("no queryable instruments; nothing was requested\n")
        return _EXIT_UNHEALTHY

    for index, query in enumerate(queries, start=1):
        sys.stdout.write(f"    query {index}: {query.query}\n")
    if dry_run:
        sys.stdout.write("\n--dry-run: no request was issued and nothing was stored.\n")
        return 0

    async with httpx2.AsyncClient(timeout=news.timeout_seconds, follow_redirects=True) as client:
        result = await PollNewsFeeds(
            _feeds(client, queries),
            lambda account: SqlAlchemyIntelligenceUnitOfWork(session_factory, account_id=account),
        ).execute(PollNewsFeedsCommand(account_id=account_id, universe=universe, analysed_at=now))

    sys.stdout.write("\nbatches\n" + render_batch_outcomes(result.outcomes) + "\n")
    sys.stdout.write("\ningested\n" + render_ingestion_counts(result) + "\n")
    return _poll_verdict(result.unhealthy, rate_limited=bool(result.rate_limited))


def _poll_verdict(unhealthy: Sequence[object], *, rate_limited: bool) -> int:
    """Print the closing verdict and return the process exit status."""
    sys.stdout.write(f"\nData source: The GDELT Project -- {GDELT_ATTRIBUTION_URL}\n")
    sys.stdout.write(NSE_UNAVAILABLE_NOTICE + "\n")
    if rate_limited:
        sys.stderr.write(
            "\nThe provider rate-limited this pass, so it stopped early and later "
            "batches were not issued. Everything the earlier batches returned was "
            "ingested. Wait before running again; do not run it in a loop.\n"
        )
        return _EXIT_UNHEALTHY
    if unhealthy:
        sys.stderr.write(f"\n{len(unhealthy)} batch(es) did not reach a usable answer.\n")
        return _EXIT_UNHEALTHY
    return 0


async def _show(  # noqa: PLR0913 - a composition root names its collaborators
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    symbol: str,
    as_of: datetime,
    days: int | None,
    limit: int | None,
) -> int:
    """Read the archive at one instant for one approved instrument."""
    news = settings.news
    universe = await _universe(session_factory, account_id=account_id, known_at=as_of)
    known = {instrument.canonical_symbol for instrument in universe}
    if symbol not in known:
        sys.stderr.write(
            f"{symbol!r} is not on the approved watchlist as it stood at "
            f"{as_of.isoformat()}. Known symbols: {', '.join(sorted(known)) or 'none'}\n"
        )
        return _EXIT_REFUSED

    window = timedelta(days=days if days is not None else news.lookback_days)
    items = await GetArchivedNews(
        lambda account: SqlAlchemyIntelligenceUnitOfWork(session_factory, account_id=account)
    ).execute(
        GetArchivedNewsQuery(
            account_id=account_id,
            known_at=as_of,
            published_from=as_of - window,
            published_to=as_of,
            canonical_symbol=symbol,
            limit=limit if limit is not None else news.result_limit,
        )
    )

    sys.stdout.write(
        f"{symbol} as known at {as_of.isoformat()}, published within {window.days} day(s)\n\n"
    )
    sys.stdout.write(render_items(items) + "\n")
    sys.stdout.write(f"\nData source: The GDELT Project -- {GDELT_ATTRIBUTION_URL}\n")
    sys.stdout.write(NSE_UNAVAILABLE_NOTICE + "\n")
    return 0


async def run(argv: Sequence[str] | None = None) -> int:
    """Execute one subcommand, returning the process exit status."""
    args = build_parser().parse_args(argv)
    settings = load_settings()
    account_id = _account(args.account)
    engine = build_engine(settings.db)
    session_factory = build_session_factory(engine)

    try:
        if args.command == "poll":
            return await _poll(
                settings, session_factory, account_id=account_id, dry_run=args.dry_run
            )
        return await _show(
            settings,
            session_factory,
            account_id=account_id,
            symbol=args.symbol,
            as_of=_instant(args.as_of, field="--as-of"),
            days=args.days,
            limit=args.limit,
        )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point.

    A ``DhruvaError`` becomes a message and a status rather than a traceback.
    An operator running this wants to be told what is wrong, not shown where it
    was noticed.
    """
    try:
        return asyncio.run(run(argv))
    except ValidationError as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_REFUSED
    except DhruvaError as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_UNHEALTHY


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
