"""``dhruva-export`` -- write a deterministic research artifact to a JSON file.

A sibling of ``dhruva-digest`` reading exactly the same path: the same watchlist,
the same news archive, the same daily bars, the same cutoff. What differs is only
where the answer goes. If the two ever disagreed about what was knowable at an
instant, one of them would be wrong, so neither has its own read.

Two body shapes, chosen by ``--packet``. The default writes the full-watchlist
snapshot every instrument this command has always exported
(:func:`~dhruva.contexts.intelligence.interfaces.digest_export.build_snapshot`,
schema ``dhruva.research-snapshot.v1``, unchanged). ``--packet`` writes the
compact, top-N research-attention artifact
(:func:`~dhruva.contexts.intelligence.interfaces.research_packet.build_packet`,
schema ``dhruva.research-packet.v1``) -- the same ranking
``dhruva-digest --brief`` renders as text, serialised the same way the full
snapshot already is. Neither mode ranks or reads anything the other does not;
they differ only in which already-resolved read models reach the file.

Read-only against PostgreSQL and against the network, which here means the
network is not contacted at all. An export is evidence about what was already
stored; going to fetch something first would make it evidence about now.
Nothing here triggers ``dhruva-refresh`` or any other write path -- refreshing
first, if that is wanted, is a separate, explicit command.

**It will not overwrite.** An existing file is refused unless ``--force`` is
given. An export is the thing somebody keeps in order to be able to say what
they knew, and a command that silently replaced yesterday's would destroy the
only copy of a fact at the moment it became inconvenient.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.application.universe import linkable_universe
from dhruva.contexts.intelligence.application.watchlist_digest import (
    BuildWatchlistDigest,
    BuildWatchlistDigestQuery,
)
from dhruva.contexts.intelligence.domain.attention import top_attention
from dhruva.contexts.intelligence.domain.digest import MAX_ITEMS_PER_INSTRUMENT
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.attention_presentation import rank_watchlist
from dhruva.contexts.intelligence.interfaces.digest_export import (
    EXPORT_SCHEMA_VERSION,
    build_snapshot,
    serialise_snapshot,
)
from dhruva.contexts.intelligence.interfaces.research_packet import (
    PACKET_SCHEMA_VERSION,
    build_packet,
)
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
from dhruva.shared.identity import AccountId
from dhruva.shared.time.clock import SystemClock
from dhruva.workers.cli_arguments import (
    BRIEF_TOP_DEFAULT,
    parse_account,
    parse_cutoff,
    parse_max_items,
    parse_sessions,
    parse_top,
    parse_window,
    select_instruments,
    validate_packet_options,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["build_parser", "main"]

_EXIT_OK = 0
_EXIT_REFUSED = 2


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dhruva-export",
        description=(
            "Write a deterministic, self-describing JSON export of what DHRUVA "
            "had archived about the approved watchlist at an explicit instant. "
            "Read-only: no network call, no write to the database, no "
            "recommendation."
        ),
        epilog=(
            f"Full export schema {EXPORT_SCHEMA_VERSION}: the body is "
            "byte-for-byte stable for a given database state, cutoff and "
            "options, but the envelope's generated_at is the wall clock at "
            "export time, so the whole file still varies between runs. "
            f"--packet schema {PACKET_SCHEMA_VERSION}: generated_at is the "
            "packet's own resolved --as-of cutoff, not the wall clock, so "
            "the entire file -- envelope included -- is byte-for-byte stable "
            "for a given database state, cutoff and options. NSE filings are "
            "not an input."
        ),
    )
    parser.add_argument("--account", required=True, help="account the read is attributed to")
    parser.add_argument("--output", required=True, type=Path, help="file to write")
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
        help="omit market context and export archived news only",
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
        help=f"items exported per instrument (default {MAX_ITEMS_PER_INSTRUMENT})",
    )
    parser.add_argument(
        "--packet",
        action="store_true",
        help=(
            "write the compact top-N research-attention packet instead of the "
            "full watchlist snapshot -- same ranking dhruva-digest --brief "
            f"renders, schema {PACKET_SCHEMA_VERSION}; never a recommendation"
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help=f"instruments included in --packet (default {BRIEF_TOP_DEFAULT}); requires --packet",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the output file if it already exists",
    )
    return parser


def _destination(output: Path, *, force: bool) -> Path:
    """Return the path to write, refusing to destroy an existing export.

    Fail-closed. An export is what somebody keeps in order to say what they
    knew at a point in time; overwriting one silently would remove the only copy
    of a fact at the moment it became inconvenient, and the cost of being wrong
    in the other direction is retyping the command with ``--force``.
    """
    if output.exists() and not force:
        raise ValidationError(
            f"{output} already exists. Pass --force to replace it, or choose "
            "another path; an export is not overwritten by accident."
        )
    if output.exists() and not output.is_file():
        raise ValidationError(f"{output} exists and is not a regular file")
    parent = output.parent
    if not parent.exists():
        raise ValidationError(f"the directory {parent} does not exist")
    return output


async def run(argv: Sequence[str] | None = None) -> int:
    """Read the archives once at the cutoff and write the export."""
    args = build_parser().parse_args(argv)
    validate_packet_options(packet=args.packet, top=args.top)
    top_n = parse_top(args.top) if args.top is not None else BRIEF_TOP_DEFAULT
    settings = load_settings()
    account_id: AccountId = parse_account(args.account)
    as_of = parse_cutoff(args.as_of)
    window = parse_window(args.days, settings.news.lookback_days)
    max_items = parse_max_items(args.max_items)
    sessions = parse_sessions(args.sessions)
    destination = _destination(args.output, force=args.force)

    engine = build_engine(settings.db)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    try:
        watchlist = await GetSharedWatchlist(
            lambda account: SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)
        ).execute(account_id=account_id, effective_on=as_of.date(), known_at=as_of)
        universe = select_instruments(linkable_universe(watchlist), args.symbol)

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
        contexts = (
            None
            if args.no_market
            else contexts_by_instrument(
                await GetMarketContext(
                    lambda account: SqlAlchemyMarketDataUnitOfWork(
                        session_factory, account_id=account
                    )
                ).execute(
                    GetMarketContextQuery(
                        account_id=account_id,
                        instrument_ids=tuple(entry.instrument_id for entry in universe),
                        known_at=as_of,
                        sessions=sessions,
                    )
                )
            )
        )
    finally:
        await engine.dispose()

    export: dict[str, Any]
    if args.packet:
        ranked = rank_watchlist(digest, contexts)
        selected = top_attention(ranked, limit=top_n)
        # Deterministic, not the wall clock: digest.known_at is the resolved
        # PIT cutoff (== as_of), itself a pure function of the persisted
        # state, account and --as-of. Using it here -- rather than
        # SystemClock().now() -- is what makes the *entire* packet file,
        # envelope included, byte-for-byte identical across repeated exports
        # of the same state and cutoff, not merely its body_sha256-verified
        # body. The full snapshot below intentionally keeps the wall clock;
        # this determinism promise is specific to dhruva.research-packet.v1.
        export = build_packet(
            selected,
            ranked=ranked,
            digest=digest,
            contexts=contexts,
            account_id=account_id,
            generated_at=digest.known_at,
            requested_top=top_n,
        )
        schema_version = PACKET_SCHEMA_VERSION
        headline_count = len(selected)
    else:
        export = build_snapshot(
            digest, contexts, account_id=account_id, generated_at=SystemClock().now()
        )
        schema_version = EXPORT_SCHEMA_VERSION
        headline_count = len(digest.sections)
    destination.write_text(serialise_snapshot(export), encoding="utf-8")

    sys.stdout.write(
        f"wrote {destination}\n"
        f"  schema     : {schema_version}\n"
        f"  as of      : {as_of.isoformat()}\n"
        f"  instruments: {headline_count}\n"
        f"  items      : {digest.items_reported}\n"
        f"  body sha256: {export['envelope']['body_sha256']}\n"
    )
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
