"""``dhruva-data`` -- provider diligence and explicit network-free dataset intake."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.contexts.reference.infrastructure.persistence.historical_dataset import (
    HistoricalDatasetRepository,
)
from dhruva.ingest.historical_dataset import canonical_json_bytes, preflight_dataset
from dhruva.ingest.historical_import import import_historical_dataset
from dhruva.ingest.provider_diligence import PROVIDERS, diligence_export_bytes
from dhruva.ingest.synthetic_historical_dataset import generate_synthetic_dataset
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError
from dhruva.workers.cli_arguments import parse_account

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["build_parser", "main", "run"]

_EXIT_OK = 0
_EXIT_REFUSED = 2


def build_parser() -> argparse.ArgumentParser:
    """Build the closed, local-only command surface."""
    parser = argparse.ArgumentParser(
        prog="dhruva-data",
        description="Research providers and validate/import retained historical files.",
        epilog=(
            "No subcommand logs in to a provider, downloads data, purchases a licence, "
            "or contacts a broker. Import requires an explicit --apply."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    providers = sub.add_parser("providers", help="print current public-source diligence")
    providers.add_argument("--json", action="store_true", help="emit canonical machine JSON")
    providers.add_argument("--report", type=Path, help="also write canonical JSON to this file")

    preflight = sub.add_parser("preflight", help="validate a delivery without database/network")
    preflight.add_argument("--manifest", type=Path, required=True)
    preflight.add_argument("--report", type=Path, help="write deterministic preflight JSON")

    sample = sub.add_parser("sample", help="generate the three-year synthetic TEST DATA pack")
    sample.add_argument("--output", type=Path, required=True)
    sample.add_argument("--corrupt", action="store_true", help="generate a hash-failing pack")

    apply = sub.add_parser("import", help="atomically apply a READY delivery to local PostgreSQL")
    apply.add_argument("--manifest", type=Path, required=True)
    apply.add_argument("--account", required=True)
    apply.add_argument(
        "--apply",
        action="store_true",
        help="mandatory acknowledgement that this command writes retained local facts",
    )
    apply.add_argument("--report", type=Path, help="write deterministic result JSON")

    status = sub.add_parser("dataset-status", help="read one imported dataset revision")
    status.add_argument("--dataset", required=True)
    status.add_argument("--account", required=True)
    provenance = sub.add_parser("provenance", help="read bounded source-row provenance")
    provenance.add_argument("--dataset", required=True)
    provenance.add_argument("--account", required=True)
    provenance.add_argument("--limit", type=int, default=1000)
    return parser


def _write(path: Path | None, payload: bytes) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


async def _dataset_status(dataset_id: str, account: str) -> bytes:
    account_id = parse_account(account)
    engine = build_engine(load_settings().db)
    sessions = build_session_factory(engine)
    try:
        async with sessions() as session:
            await session.execute(
                text("SELECT set_config(:setting, :account_id, true)"),
                {
                    "setting": "dhruva.current_account_id",
                    "account_id": str(account_id.value),
                },
            )
            status = await HistoricalDatasetRepository(session).get_status(
                account_id=account_id.value, dataset_id=dataset_id
            )
            return (
                canonical_json_bytes(
                    {"schema": "dhruva.historical-dataset-status.v1", **asdict(status)}
                )
                + b"\n"
            )
    finally:
        await engine.dispose()


async def _dataset_provenance(dataset_id: str, account: str, limit: int) -> bytes:
    account_id = parse_account(account)
    engine = build_engine(load_settings().db)
    sessions = build_session_factory(engine)
    try:
        async with sessions() as session:
            await session.execute(
                text("SELECT set_config(:setting, :account_id, true)"),
                {
                    "setting": "dhruva.current_account_id",
                    "account_id": str(account_id.value),
                },
            )
            rows = await HistoricalDatasetRepository(session).list_provenance(
                account_id=account_id.value,
                dataset_id=dataset_id,
                limit=limit,
            )
            return (
                canonical_json_bytes(
                    {
                        "schema": "dhruva.historical-dataset-provenance.v1",
                        "dataset_id": dataset_id,
                        "rows": [asdict(item) for item in rows],
                    }
                )
                + b"\n"
            )
    finally:
        await engine.dispose()


async def run(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911
    """Run one explicit operation and return a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "providers":
        payload = diligence_export_bytes()
        _write(args.report, payload)
        if args.json:
            sys.stdout.buffer.write(payload)
        else:
            sys.stdout.write("No provider is approved. Scores prioritize owner diligence only.\n")
            for item in sorted(
                PROVIDERS, key=lambda value: (-value.weighted_score, value.provider_id)
            ):
                sys.stdout.write(
                    f"{item.provider_name:24} {item.weighted_score:3} "
                    f"{item.disposition.value:26} blockers={len(item.blocking_unknowns)}\n"
                )
        return _EXIT_OK
    if args.command == "preflight":
        report = preflight_dataset(args.manifest)
        payload = report.export_bytes()
        _write(args.report, payload)
        sys.stdout.buffer.write(payload)
        return _EXIT_OK if report.import_decision.value == "READY" else _EXIT_REFUSED
    if args.command == "sample":
        manifest = generate_synthetic_dataset(args.output, corrupt=args.corrupt)
        sys.stdout.write(f"Synthetic TEST DATA manifest: {manifest}\n")
        return _EXIT_OK
    if args.command == "dataset-status":
        sys.stdout.buffer.write(await _dataset_status(args.dataset, args.account))
        return _EXIT_OK
    if args.command == "provenance":
        sys.stdout.buffer.write(await _dataset_provenance(args.dataset, args.account, args.limit))
        return _EXIT_OK
    if not args.apply:
        sys.stdout.write(
            "Refused: import requires --apply; preflight remains available without a database.\n"
        )
        return _EXIT_REFUSED
    account_id = parse_account(args.account)
    engine = build_engine(load_settings().db)
    sessions = build_session_factory(engine)
    try:
        result = await import_historical_dataset(
            args.manifest, account_id=account_id, session_factory=sessions
        )
        payload = result.export_bytes()
        _write(args.report, payload)
        sys.stdout.buffer.write(payload)
        return _EXIT_OK
    finally:
        await engine.dispose()


def main() -> None:
    """CLI adapter with stable refusal semantics."""
    try:
        raise SystemExit(asyncio.run(run()))
    except (DhruvaError, OSError, ValueError, json.JSONDecodeError) as error:
        sys.stderr.write(f"Refused: {error}\n")
        raise SystemExit(_EXIT_REFUSED) from error
