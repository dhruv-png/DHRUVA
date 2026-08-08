"""``dhruva-digest`` -- what changed for the watchlist, as of one instant.

A composition root and an operator tool. It is strictly read-only: it opens one
transaction, reads, prints and exits. There is no network call on this path at
all -- the digest is composed from what earlier polls already stored, so it works
with the provider unreachable, rate-limited, or deliberately not run today.

That separation is the point. `dhruva-news poll` is the only thing that talks to
a provider and the only thing that writes; this reads. A morning question about
the watchlist should not be able to trigger a fetch, and an unavailable provider
should not be able to make yesterday's evidence unreadable.

It reports and it does not advise. Nothing here produces a signal, a score, a
target or a recommendation, and the deterministic verdicts it prints were
decided at ingestion by rulesets whose revisions are stored beside them.

Two archives are read at one cutoff: the news archive for what was written, and
the daily-bar archive for what the closes did. Both reads take the same
``known_at``, so a section cannot pair yesterday's headline with tomorrow's
price -- and both go through their own context's application boundary, because a
report assembling raw rows from two schemas is how the point-in-time rule ends
up implemented twice and enforced once.

``--changes`` is the one mode that reads twice rather than once: two fully
independent reads, one per cutoff, each with its own ``known_at`` -- never a
single read compared against a remembered "last time." There is nothing to
remember here; a change report is reproducible from the two timestamps alone.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.application.universe import linkable_universe
from dhruva.contexts.intelligence.application.watchlist_digest import (
    BuildWatchlistDigest,
    BuildWatchlistDigestQuery,
)
from dhruva.contexts.intelligence.domain.attention import top_attention
from dhruva.contexts.intelligence.domain.changes import compare_watchlist
from dhruva.contexts.intelligence.domain.digest import MAX_ITEMS_PER_INSTRUMENT
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.attention_presentation import (
    rank_watchlist,
    render_attention,
    render_brief,
    render_changes,
)
from dhruva.contexts.intelligence.interfaces.digest_presentation import render_digest
from dhruva.contexts.marketdata.api import (
    DEFAULT_MULTI_DAY_SESSIONS,
    GetMarketContext,
    GetMarketContextQuery,
    contexts_by_instrument,
)
from dhruva.contexts.marketdata.infrastructure.persistence.unit_of_work import (
    SqlAlchemyMarketDataUnitOfWork,
)
from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.contexts.reference.api import GetSharedWatchlist
from dhruva.contexts.reference.infrastructure import SqlAlchemyReferenceUnitOfWork
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, ValidationError
from dhruva.workers.cli_arguments import (
    BRIEF_TOP_DEFAULT,
    parse_account,
    parse_change_window,
    parse_cutoff,
    parse_max_items,
    parse_sessions,
    parse_top,
    parse_window,
    select_instruments,
    validate_brief_options,
    validate_changes_options,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.contexts.intelligence.domain.digest import WatchlistDigest
    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
    from dhruva.contexts.marketdata.domain.market_context import MarketContext
    from dhruva.shared.identity import AccountId, InstrumentId


__all__ = ["build_parser", "main"]

#: Nothing was stored for any instrument in the window. Zero, because it is a
#: complete and correct answer -- and a runbook that treated a quiet day as a
#: failure would be red more often than not.
_EXIT_OK = 0
#: Input or configuration the command will not act on.
_EXIT_REFUSED = 2


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dhruva-digest",
        description=(
            "Summarise what DHRUVA had archived about the approved watchlist at "
            "an explicit instant. Read-only: no network call, no write, no "
            "recommendation."
        ),
        epilog=(
            "Reports stored facts, not advice. Sentiment is a deterministic "
            "lexical baseline and cannot on its own support a trading decision. "
        )
        + "NSE filings are not an input; automated NSE ingestion is deferred.",
    )
    parser.add_argument("--account", required=True, help="account the read is attributed to")
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 knowledge cutoff; nothing first seen after it is used (default: now)",
    )
    parser.add_argument("--days", type=int, default=None, help="days of publication history")
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        metavar="SYMBOL",
        help="restrict to this canonical symbol; repeatable (default: the whole watchlist)",
    )
    parser.add_argument(
        "--no-market",
        action="store_true",
        help="omit market context and report archived news only",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=DEFAULT_MULTI_DAY_SESSIONS,
        help=f"sessions in the multi-day return (default {DEFAULT_MULTI_DAY_SESSIONS})",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=MAX_ITEMS_PER_INSTRUMENT,
        help=f"items shown per instrument (default {MAX_ITEMS_PER_INSTRUMENT})",
    )
    parser.add_argument(
        "--ranked",
        action="store_true",
        help=(
            "prepend a deterministic research-attention ranking -- observable "
            "price/volume/news magnitude only, never a recommendation"
        ),
    )
    parser.add_argument(
        "--brief",
        action="store_true",
        help=(
            "print a compact top-N research brief instead of the full digest -- "
            "attention verdict plus a small amount of market and news detail, "
            "never a recommendation; mutually exclusive with --ranked"
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help=f"instruments shown by --brief (default {BRIEF_TOP_DEFAULT}); requires --brief",
    )
    parser.add_argument(
        "--changes",
        action="store_true",
        help=(
            "print a deterministic comparison of research attention between "
            "--from and --to instead of a single-cutoff digest; never a "
            "recommendation; mutually exclusive with --ranked, --brief and --as-of"
        ),
    )
    parser.add_argument(
        "--from",
        dest="from_cutoff",
        default=None,
        metavar="TIMESTAMP",
        help="ISO-8601 earlier cutoff a change report compares from; requires --changes",
    )
    parser.add_argument(
        "--to",
        dest="to_cutoff",
        default=None,
        metavar="TIMESTAMP",
        help="ISO-8601 later cutoff a change report compares to; requires --changes",
    )
    return parser


async def _universe_at(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    cutoff: datetime,
    symbols: Sequence[str] | None,
) -> tuple[LinkableInstrument, ...]:
    """Resolve the watchlist as it read at ``cutoff``, narrowed by ``--symbol``."""
    watchlist = await GetSharedWatchlist(
        lambda account: SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)
    ).execute(account_id=account_id, effective_on=cutoff.date(), known_at=cutoff)
    return select_instruments(linkable_universe(watchlist), symbols)


async def _digest_at(  # noqa: PLR0913 - one keyword per independently varied input
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    universe: tuple[LinkableInstrument, ...],
    known_at: datetime,
    window: timedelta,
    max_items: int,
) -> WatchlistDigest:
    """Read the news archive for one instrument set at one cutoff."""
    return await BuildWatchlistDigest(
        lambda account: SqlAlchemyIntelligenceUnitOfWork(session_factory, account_id=account)
    ).execute(
        BuildWatchlistDigestQuery(
            account_id=account_id,
            universe=universe,
            known_at=known_at,
            published_from=known_at - window,
            published_to=known_at,
            max_items=max_items,
        )
    )


async def _contexts_at(  # noqa: PLR0913 - one keyword per independently varied input
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: AccountId,
    universe: tuple[LinkableInstrument, ...],
    known_at: datetime,
    sessions: int,
    no_market: bool,
) -> dict[InstrumentId, MarketContext] | None:
    """Read market context for one instrument set at one cutoff, or omit it entirely."""
    if no_market:
        return None
    summaries = await GetMarketContext(
        lambda account: SqlAlchemyMarketDataUnitOfWork(session_factory, account_id=account)
    ).execute(
        GetMarketContextQuery(
            account_id=account_id,
            instrument_ids=tuple(entry.instrument_id for entry in universe),
            known_at=known_at,
            sessions=sessions,
        )
    )
    return contexts_by_instrument(summaries)


async def _render_single_cutoff(  # noqa: PLR0913 - one keyword per independently varied input
    session_factory: async_sessionmaker[AsyncSession],
    args: argparse.Namespace,
    *,
    account_id: AccountId,
    as_of: datetime,
    window: timedelta,
    max_items: int,
    top_n: int,
) -> str:
    """Render the plain digest, or --ranked, or --brief, for one cutoff."""
    universe = await _universe_at(
        session_factory, account_id=account_id, cutoff=as_of, symbols=args.symbol
    )
    digest = await _digest_at(
        session_factory,
        account_id=account_id,
        universe=universe,
        known_at=as_of,
        window=window,
        max_items=max_items,
    )
    contexts = await _contexts_at(
        session_factory,
        account_id=account_id,
        universe=universe,
        known_at=as_of,
        sessions=parse_sessions(args.sessions),
        no_market=args.no_market,
    )

    if args.brief:
        ranked = rank_watchlist(digest, contexts)
        selected = top_attention(ranked, limit=top_n)
        return render_brief(selected, digest=digest, contexts=contexts, total_ranked=len(ranked))
    output = render_digest(digest, contexts)
    if args.ranked:
        ranked = rank_watchlist(digest, contexts)
        output = f"{render_attention(ranked)}\n\n{output}"
    return output


async def _render_changes(  # noqa: PLR0913 - one keyword per independently varied input
    session_factory: async_sessionmaker[AsyncSession],
    args: argparse.Namespace,
    *,
    account_id: AccountId,
    from_cutoff: datetime,
    to_cutoff: datetime,
    window: timedelta,
    max_items: int,
) -> str:
    """Render a deterministic comparison between two independently-read cutoffs.

    The watchlist itself is resolved once, at ``to_cutoff`` -- what changed for
    the instruments currently followed, not a reconciliation of two different
    watchlist definitions, which would be a different report about the
    reference context rather than about research attention.
    """
    universe = await _universe_at(
        session_factory, account_id=account_id, cutoff=to_cutoff, symbols=args.symbol
    )
    sessions = parse_sessions(args.sessions)

    digest_before = await _digest_at(
        session_factory,
        account_id=account_id,
        universe=universe,
        known_at=from_cutoff,
        window=window,
        max_items=max_items,
    )
    digest_after = await _digest_at(
        session_factory,
        account_id=account_id,
        universe=universe,
        known_at=to_cutoff,
        window=window,
        max_items=max_items,
    )
    contexts_before = await _contexts_at(
        session_factory,
        account_id=account_id,
        universe=universe,
        known_at=from_cutoff,
        sessions=sessions,
        no_market=args.no_market,
    )
    contexts_after = await _contexts_at(
        session_factory,
        account_id=account_id,
        universe=universe,
        known_at=to_cutoff,
        sessions=sessions,
        no_market=args.no_market,
    )

    ranked_before = rank_watchlist(digest_before, contexts_before)
    ranked_after = rank_watchlist(digest_after, contexts_after)
    changes = compare_watchlist(ranked_before, ranked_after, after_digest=digest_after)
    return render_changes(changes, from_cutoff=from_cutoff, to_cutoff=to_cutoff)


async def run(argv: Sequence[str] | None = None) -> int:
    """Read the archive once (or twice, for --changes) and print the result."""
    args = build_parser().parse_args(argv)
    validate_brief_options(brief=args.brief, ranked=args.ranked, top=args.top)
    validate_changes_options(
        changes=args.changes,
        ranked=args.ranked,
        brief=args.brief,
        as_of=args.as_of,
        from_cutoff=args.from_cutoff,
        to_cutoff=args.to_cutoff,
    )
    top_n = parse_top(args.top) if args.top is not None else BRIEF_TOP_DEFAULT
    settings = load_settings()
    account_id = parse_account(args.account)
    window = parse_window(args.days, settings.news.lookback_days)
    max_items = parse_max_items(args.max_items)

    engine = build_engine(settings.db)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    try:
        if args.changes:
            from_cutoff, to_cutoff = parse_change_window(args.from_cutoff, args.to_cutoff)
            output = await _render_changes(
                session_factory,
                args,
                account_id=account_id,
                from_cutoff=from_cutoff,
                to_cutoff=to_cutoff,
                window=window,
                max_items=max_items,
            )
        else:
            as_of = parse_cutoff(args.as_of)
            output = await _render_single_cutoff(
                session_factory,
                args,
                account_id=account_id,
                as_of=as_of,
                window=window,
                max_items=max_items,
                top_n=top_n,
            )
    finally:
        await engine.dispose()

    sys.stdout.write(output + "\n")
    return _EXIT_OK


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
