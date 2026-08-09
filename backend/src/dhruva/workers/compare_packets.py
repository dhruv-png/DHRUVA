"""``dhruva-compare-packets`` -- compare two already-exported research packets.

A pure file-to-file comparison. It reads two ``dhruva.research-packet.v1``
files from disk, validates each independently
(:func:`~dhruva.contexts.intelligence.interfaces.packet_comparison.read_packet`
checks schema version, recorded fingerprint, and every required field), and
reports what mechanically differs between them -- nothing else. It opens no
database connection, builds no HTTP client, and contacts no provider: the
two files are the entire, immutable source of truth, which is what lets a
comparison run today still mean the same thing years later. Unlike every
other command in this package, ``run`` here is synchronous -- there is no
``async`` boundary to cross, because nothing here ever awaits anything.

Distinct from ``dhruva-digest --changes``, which resolves two point-in-time
reads fresh from the live database at two cutoffs. That command answers
"what does DHRUVA know now, compared between two instants"; this one answers
"what do these two saved files say", and keeps answering it even when
PostgreSQL, Zerodha and GDELT are all unreachable -- this module imports
none of the machinery that would let it reach any of them.

The account is never a command-line argument here: it comes from the packets
themselves. A caller who wants a specific account's comparison supplies two
files already exported for that account; this command's only job is to
refuse to compare two files that disagree about whose research state they
describe.

**It will not overwrite** an optional ``--output`` file, for the identical
reason ``dhruva-export`` refuses to: an owner keeps a comparison in order to
be able to say what changed, and a command that silently replaced
yesterday's would destroy the only copy of that fact.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.interfaces.digest_export import serialise_snapshot
from dhruva.contexts.intelligence.interfaces.packet_comparison import (
    COMPARISON_SCHEMA_VERSION,
    build_comparison_export,
    compare_packets,
    read_packet,
    render_packet_comparison,
    validate_packet_pair,
)
from dhruva.contexts.intelligence.interfaces.research_packet import PACKET_SCHEMA_VERSION
from dhruva.shared.errors import DhruvaError, ValidationError
from dhruva.shared.time.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["build_parser", "main"]

_EXIT_OK = 0
_EXIT_REFUSED = 2


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="dhruva-compare-packets",
        description=(
            "Compare two already-exported dhruva.research-packet.v1 files and "
            "report what mechanically differs. File-to-file only: no database "
            "connection, no provider call, no refresh, no recommendation."
        ),
        epilog=(
            f"Reads {PACKET_SCHEMA_VERSION} packets only; each file's own "
            "body_sha256 is verified before comparison. --from and --to must "
            "name the same account_id -- comparing two accounts' research "
            "states is refused, not merged. An instrument absent "
            "from one packet's top-N array is reported as a true attention "
            "entry/exit only when that packet's own watchlist_summary proves "
            "its array held every score-positive instrument -- otherwise it is "
            "reported as entering or leaving that packet's selection, which is "
            "the strongest claim the files can support -- and its archived "
            "news, if any, is shown but never claimed as newly added, since "
            "no comparable earlier news list exists for an absent instrument. "
            "MARKET_CHANGED compares only the market's stored facts (close, "
            "date, returns, volume, availability), never the packet's own "
            "as_of/staleness fields, so an unchanged instrument re-exported "
            "later is not reported as changed merely because time passed. "
            "Distinct from "
            "dhruva-digest --changes, which compares two fresh database reads "
            f"rather than two saved files. --output writes schema "
            f"{COMPARISON_SCHEMA_VERSION}."
        ),
    )
    parser.add_argument(
        "--from", dest="from_path", required=True, type=Path, help="earlier packet file"
    )
    parser.add_argument("--to", dest="to_path", required=True, type=Path, help="later packet file")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional file to write the comparison as canonical JSON",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite --output if it already exists",
    )
    return parser


def _read_bytes(path: Path) -> bytes:
    """Read one packet file, refusing safely rather than letting an OSError propagate."""
    if not path.exists():
        raise ValidationError(f"{path} does not exist")
    if not path.is_file():
        raise ValidationError(f"{path} is not a regular file")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ValidationError(f"{path} could not be read", detail=str(error)) from error


def _resolve_output_path(output: Path, *, force: bool) -> Path:
    """Return the path to write, refusing to destroy an existing comparison.

    The identical fail-closed rule ``dhruva-export`` applies to its own
    ``--output``: an existing file is never replaced without ``--force``.
    """
    if output.exists() and not force:
        raise ValidationError(
            f"{output} already exists. Pass --force to replace it, or choose "
            "another path; a comparison is not overwritten by accident."
        )
    if output.exists() and not output.is_file():
        raise ValidationError(f"{output} exists and is not a regular file")
    parent = output.parent
    if not parent.exists():
        raise ValidationError(f"the directory {parent} does not exist")
    return output


def run(argv: Sequence[str] | None = None) -> int:
    """Read, validate and compare the two packets, printing the result.

    Synchronous throughout: nothing here is awaited, because nothing here
    performs I/O beyond reading two local files and, optionally, writing a
    third.
    """
    args = build_parser().parse_args(argv)

    destination = (
        None if args.output is None else _resolve_output_path(args.output, force=args.force)
    )

    before = read_packet(_read_bytes(args.from_path))
    after = read_packet(_read_bytes(args.to_path))
    validate_packet_pair(before, after)

    changes = compare_packets(before, after)
    sys.stdout.write(render_packet_comparison(changes, before=before, after=after) + "\n")

    if destination is not None:
        export = build_comparison_export(
            changes, before=before, after=after, generated_at=SystemClock().now()
        )
        destination.write_text(serialise_snapshot(export), encoding="utf-8")
        sys.stdout.write(f"\nwrote {destination}\n")

    return _EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    try:
        return run(argv)
    except ValidationError as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_REFUSED
    except DhruvaError as error:
        sys.stderr.write(f"{error}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
