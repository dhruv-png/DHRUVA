"""``dhruva-reference seed`` -- put the approved watchlist into the database.

Until this existed, every read command in the system worked only against rows a
test had inserted. `dhruva-news poll` had no instruments to build query phrases
from, and `dhruva-digest` truthfully reported that no instruments were on the
watchlist. The loader, the use case and the repository were all written and
tested; nothing called them.

Deliberately the least capable command here. It reads one committed JSON file,
writes reference rows, and exits. **No network of any kind** -- no Zerodha, no
provider, no credential, no session. Instrument discovery and daily bars are
separate concerns that do need a broker, and this command must remain runnable
on a laptop with no broker relationship at all.

Idempotent by the archive's own rules rather than by a check here. Identity and
membership revisions are effective-dated and content-addressed, so running this
twice adds nothing and changes no ``recorded_at``; the report distinguishes what
was written from what was already there.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.contexts.reference.api import ConfigureReferenceUniverse
from dhruva.contexts.reference.infrastructure import (
    SqlAlchemyReferenceUnitOfWork,
    load_owner_universe,
)
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, ValidationError
from dhruva.workers.cli_arguments import parse_account, parse_cutoff

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["build_parser", "main"]

_EXIT_OK = 0
_EXIT_REFUSED = 2

_SAFETY = (
    "Research configuration only. Reads one committed file and writes reference "
    "rows: no network call, no broker credential, no NSE filings, no order "
    "placement, no scheduler, and no investment recommendation."
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dhruva-reference",
        description=(
            "Load the owner-approved watchlist into the database from committed "
            "configuration. Safe to run repeatedly."
        ),
        epilog=_SAFETY,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="write the approved watchlist configuration")
    seed.add_argument(
        "--account",
        required=True,
        help=(
            "account to attribute the watchlist to: an identifier "
            "(acct_<uuid> or a bare UUID), or a stable lowercase label such as "
            "'owner-family' from which one is derived. Use the same value here "
            "and in every read command."
        ),
    )
    seed.add_argument(
        "--recorded-at",
        default=None,
        help=(
            "ISO-8601 instant this configuration is recorded at (default: now). "
            "It is knowledge time, not an effective date: the watchlist's own "
            "effective dates come from the committed configuration."
        ),
    )
    seed.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be written and exit without touching the database",
    )
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    """Load the committed universe and persist it in one transaction."""
    args = build_parser().parse_args(argv)
    settings = load_settings()
    account_id = parse_account(args.account)
    recorded_at = parse_cutoff(args.recorded_at)

    # Built before any connection: a malformed configuration file should fail
    # without having opened a transaction, and the dry run needs the same
    # command the real run would submit rather than a description of it.
    command = load_owner_universe(account_id, recorded_at=recorded_at)
    watchlisted = tuple(item for item in command.definitions if item.included_in_watchlist)
    excluded = tuple(item for item in command.definitions if not item.included_in_watchlist)

    sys.stdout.write(
        f"account        : {account_id}\n"
        f"source         : {command.source}\n"
        f"source revision: {command.source_revision}\n"
        f"recorded at    : {recorded_at.isoformat()}\n"
        f"definitions    : {len(command.definitions)}\n"
        f"on watchlist   : {len(watchlisted)}\n"
    )
    for item in excluded:
        # Named rather than counted. An instrument in the configuration that is
        # deliberately not on the shared watchlist -- the Nifty 50 benchmark --
        # would otherwise look like something that failed to load.
        sys.stdout.write(
            f"  not on the watchlist: {item.canonical_symbol} ({item.kind}), "
            "stored as reference identity only\n"
        )

    if args.dry_run:
        sys.stdout.write("\n--dry-run: nothing was written.\n")
        return _EXIT_OK

    engine = build_engine(settings.db)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    try:
        result = await ConfigureReferenceUniverse(
            lambda account: SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)
        ).execute(command)
    finally:
        await engine.dispose()

    sys.stdout.write(
        "\nwritten\n"
        f"    identities  : {result.identities_added} added, "
        f"{result.identities_unchanged} already present\n"
        f"    memberships : {result.memberships_added} added, "
        f"{result.memberships_unchanged} already present\n"
        "\nThere is no separate 'updated' count: identity and membership "
        "revisions are append-only and effective-dated, so a changed fact "
        "arrives as a new revision beside the old one rather than replacing it.\n"
        f"\nUse --account {args.account} for dhruva-news, dhruva-digest and "
        "dhruva-export, or they will read an empty watchlist.\n"
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
