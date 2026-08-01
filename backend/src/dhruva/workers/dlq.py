"""``dhruva-dlq`` -- inspect and re-queue dead letters (ADR-064).

A composition root. It is the only module in this file's dependency chain
allowed to build an engine from settings and hand it to infrastructure; the
store it drives contains no configuration and cannot read a credential.

Why a command and not an endpoint
---------------------------------
ADR-064 makes replay explicit, and TD-S05-6 records that a UI is S3x. What
matters now is that returning an event to the queue is a deliberate act with an
audit trail -- a shell history and a log line -- rather than a button. The
command prints what it is about to do and requires ``--yes`` before it changes
anything, because the failure mode of a bulk re-queue is an outage.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING
from uuid import UUID

from dhruva.contexts.platform.infrastructure.database.engine import build_engine
from dhruva.contexts.platform.infrastructure.messaging import DeadLetterStore
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.contexts.platform.infrastructure.messaging import DeadLetter

__all__ = ["main"]

#: Truncation width for the last error in a listing. Long enough to identify a
#: failure, short enough that a terminal shows one row per line -- a listing that
#: wraps is a listing nobody scans.
_ERROR_WIDTH = 72


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dhruva-dlq",
        description="Inspect and re-queue dead-lettered events (ADR-064).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="show dead letters, oldest first")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--event-type", default=None)

    sub.add_parser("count", help="show how many events are dead-lettered")

    one = sub.add_parser("requeue", help="return one event to the delivery queue")
    one.add_argument("event_id")
    one.add_argument("--yes", action="store_true", help="required; this changes state")

    many = sub.add_parser("requeue-all", help="return a bounded batch to the delivery queue")
    many.add_argument("--limit", type=int, default=50)
    many.add_argument("--event-type", default=None)
    many.add_argument("--yes", action="store_true", help="required; this changes state")

    return parser


def render(letters: Sequence[DeadLetter]) -> str:
    """Render a listing, one event per line."""
    if not letters:
        return "no dead letters"
    lines = [f"{'dead lettered at':<26} {'event id':<38} {'attempts':>8}  event type / reason"]
    for letter in letters:
        reason = (letter.last_error or "").replace("\n", " ")[:_ERROR_WIDTH]
        lines.append(
            f"{letter.dead_lettered_at.isoformat():<26} {letter.event_id!s:<38} "
            f"{letter.attempts:>8}  {letter.event_type}: {reason}"
        )
    return "\n".join(lines)


async def run(argv: Sequence[str] | None = None) -> int:
    """Execute one command, returning the process exit status."""
    args = build_parser().parse_args(argv)
    settings = load_settings()
    engine = build_engine(settings.db)
    store = DeadLetterStore(engine)

    try:
        return await _dispatch(store, args)
    finally:
        await engine.dispose()


async def _dispatch(store: DeadLetterStore, args: argparse.Namespace) -> int:
    """Run the selected subcommand against an already-built store."""
    if args.command == "count":
        sys.stdout.write(f"{await store.count()}\n")
        return 0

    if args.command == "list":
        letters = await store.list(limit=args.limit, event_type=args.event_type)
        sys.stdout.write(render(letters) + "\n")
        return 0

    if not args.yes:
        sys.stderr.write(
            "refusing to re-queue without --yes. Returning events to the queue is a "
            "deliberate act (ADR-064), and a bulk re-queue of poison messages is an "
            "outage.\n"
        )
        return 2

    if args.command == "requeue":
        verdict = await store.requeue(UUID(args.event_id))
        if verdict.permitted:
            sys.stdout.write(f"re-queued {args.event_id}\n")
            return 0
        sys.stderr.write(f"refused: {verdict.refusal}\n")
        return 1

    report = await store.requeue_all(limit=args.limit, event_type=args.event_type)
    sys.stdout.write(f"re-queued {report.requeued}\n")
    for refusal, count in sorted(report.refused.items()):
        sys.stdout.write(f"refused {count} as {refusal}\n")
    # A refusal is not a failure of the command -- it is the command reporting
    # correctly. Exit zero, and let the counts speak.
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point.

    Translates a ``DhruvaError`` into a message and a status rather than a
    traceback: an operator running this during an incident needs to be told what
    is wrong, not shown where in the call stack it was noticed.
    """
    try:
        return asyncio.run(run(argv))
    except DhruvaError as error:
        sys.stderr.write(f"{error}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
