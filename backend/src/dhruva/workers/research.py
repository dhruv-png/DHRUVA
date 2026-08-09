"""Inspect the append-only research evidence clock without any provider call.

``dhruva-research history`` reads only the local PostgreSQL observation ledger.
It never resolves a watchlist, rebuilds a score, polls news or requests market
data; the point is to inspect what was frozen then, not reinterpret it now.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.application.research_observations import (
    ListResearchObservations,
    ListResearchObservationsQuery,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, ValidationError
from dhruva.workers.cli_arguments import parse_account

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.contexts.intelligence.domain.research_observation import (
        StoredResearchObservation,
    )

__all__ = ["build_parser", "main", "run"]

_EXIT_OK = 0
_EXIT_REFUSED = 2
_DEFAULT_LIMIT = 20
_DEFAULT_TOP = 5
_MAX_TOP = 20


def build_parser() -> argparse.ArgumentParser:
    """Build the local-only research-ledger command surface."""
    parser = argparse.ArgumentParser(
        prog="dhruva-research",
        description="Inspect immutable research observations stored in the local database.",
        epilog=(
            "Local database read only: no Zerodha, GDELT or NSE request, no score "
            "recalculation, no recommendation and no order."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    history = subcommands.add_parser(
        "history",
        help="show the newest frozen attention observations",
    )
    history.add_argument("--account", required=True, help="account whose observations are read")
    history.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"maximum observations to show (default {_DEFAULT_LIMIT}, maximum 100)",
    )
    history.add_argument(
        "--top",
        type=int,
        default=_DEFAULT_TOP,
        help=f"attention-bearing members to show per observation (default {_DEFAULT_TOP})",
    )
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    """Read and render local observation history, disposing the engine afterward."""
    args = build_parser().parse_args(argv)
    account_id = parse_account(args.account)
    if not 1 <= args.top <= _MAX_TOP:
        raise ValidationError(f"research history top count must be between 1 and {_MAX_TOP}")

    settings = load_settings()
    engine = build_engine(settings.db)
    session_factory = build_session_factory(engine)
    try:
        rows = await ListResearchObservations(
            lambda account: SqlAlchemyIntelligenceUnitOfWork(
                session_factory,
                account_id=account,
            )
        ).execute(ListResearchObservationsQuery(account_id=account_id, limit=args.limit))
        sys.stdout.write(_render_history(rows, top=args.top) + "\n")
        return _EXIT_OK
    finally:
        await engine.dispose()


def _render_history(rows: Sequence[StoredResearchObservation], *, top: int) -> str:
    """Render compact immutable facts without turning attention into advice."""
    lines = [
        "DHRUVA research observation history",
        "ATTENTION_OBSERVATION records unusual observable activity; it is not a recommendation.",
        "",
    ]
    if not rows:
        lines.append("No research observations have been frozen for this account.")
        return "\n".join(lines)

    for stored in rows:
        observation = stored.observation
        lines.extend(
            (
                f"{observation.cutoff.isoformat()}  {observation.observation_type.value}",
                f"  status: {observation.status.value}  ranked: {len(observation.members)}  "
                f"attention-bearing: {observation.attention_count}",
                f"  fingerprint: {observation.observation_sha256}",
                f"  packet body: {observation.packet_body_sha256}",
            )
        )
        if stored.supersedes_sha256 is not None:
            lines.append(f"  supersedes: {stored.supersedes_sha256}")
        health = observation.source_health
        lines.append(f"  sources: market {health.market.value}; news {health.news.value}")
        lines.extend(f"    degraded: {reason}" for reason in health.degraded_reasons)
        noteworthy = tuple(member for member in observation.members if member.score > 0)[:top]
        if noteworthy:
            lines.append(
                "  top attention: "
                + ", ".join(
                    f"{member.rank}. {member.canonical_symbol} {member.band.value} ({member.score})"
                    for member in noteworthy
                )
            )
        else:
            lines.append("  top attention: none; the full quiet universe was still preserved")
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    try:
        return asyncio.run(run(argv))
    except (ValidationError, DhruvaError) as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
