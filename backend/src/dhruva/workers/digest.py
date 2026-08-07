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
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.application.universe import linkable_universe
from dhruva.contexts.intelligence.application.watchlist_digest import (
    BuildWatchlistDigest,
    BuildWatchlistDigestQuery,
    instruments_by_symbol,
)
from dhruva.contexts.intelligence.domain.digest import MAX_ITEMS_PER_INSTRUMENT
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.digest_presentation import render_digest
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

__all__ = ["build_parser", "main"]

#: Nothing was stored for any instrument in the window. Zero, because it is a
#: complete and correct answer -- and a runbook that treated a quiet day as a
#: failure would be red more often than not.
_EXIT_OK = 0
#: Input or configuration the command will not act on.
_EXIT_REFUSED = 2

_MAX_ITEMS_CEILING = 50


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
        "--max-items",
        type=int,
        default=MAX_ITEMS_PER_INSTRUMENT,
        help=f"items shown per instrument (default {MAX_ITEMS_PER_INSTRUMENT})",
    )
    return parser


def _account(raw: str) -> AccountId:
    """Parse an account identifier, or refuse the command."""
    try:
        return AccountId.parse(raw)
    except DhruvaError as error:
        raise ValidationError("--account is not a valid account identifier") from error


def _instant(raw: str | None) -> datetime:
    """Parse an explicit ISO-8601 cutoff, defaulting to now in UTC."""
    if raw is None:
        return SystemClock().now()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValidationError("--as-of is not an ISO-8601 timestamp", value=raw) from error
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _window(days: int | None, fallback: int) -> timedelta:
    """Return the publication window, refusing a non-positive one."""
    chosen = fallback if days is None else days
    if chosen < 1:
        raise ValidationError("--days must be a positive number of days", days=chosen)
    return timedelta(days=chosen)


def _max_items(requested: int) -> int:
    """Bound what one section prints, refusing values that are not a digest."""
    if requested < 1 or requested > _MAX_ITEMS_CEILING:
        raise ValidationError(
            "--max-items must be between 1 and the digest ceiling",
            requested=requested,
            maximum=_MAX_ITEMS_CEILING,
        )
    return requested


def _select(
    universe: tuple[LinkableInstrument, ...],
    symbols: Sequence[str] | None,
) -> tuple[LinkableInstrument, ...]:
    """Narrow the universe, refusing a symbol that is not on the watchlist.

    Refusing matters here in a way it does not for the whole-watchlist digest:
    a mistyped symbol would otherwise produce a confident, empty, entirely
    truthful-looking report about an instrument DHRUVA does not follow.
    """
    if not symbols:
        return universe
    chosen = instruments_by_symbol(universe, symbols)
    found = {entry.canonical_symbol.upper() for entry in chosen}
    missing = sorted({symbol.upper() for symbol in symbols} - found)
    if missing:
        known = ", ".join(sorted(entry.canonical_symbol for entry in universe)) or "none"
        raise ValidationError(
            f"not on the approved watchlist at this cutoff: {', '.join(missing)}. "
            f"Known symbols: {known}"
        )
    return chosen


async def run(argv: Sequence[str] | None = None) -> int:
    """Read the archive once and print the digest."""
    args = build_parser().parse_args(argv)
    settings = load_settings()
    account_id = _account(args.account)
    as_of = _instant(args.as_of)
    window = _window(args.days, settings.news.lookback_days)
    max_items = _max_items(args.max_items)

    engine = build_engine(settings.db)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    try:
        watchlist = await GetSharedWatchlist(
            lambda account: SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)
        ).execute(account_id=account_id, effective_on=as_of.date(), known_at=as_of)
        universe = _select(linkable_universe(watchlist), args.symbol)

        digest = await BuildWatchlistDigest(
            lambda account: SqlAlchemyIntelligenceUnitOfWork(session_factory, account_id=account)
        ).execute(
            BuildWatchlistDigestQuery(
                account_id=account_id,
                universe=universe,
                known_at=as_of,
                published_from=as_of - window,
                published_to=as_of,
                max_items=max_items,
            )
        )
    finally:
        await engine.dispose()

    sys.stdout.write(render_digest(digest) + "\n")
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
