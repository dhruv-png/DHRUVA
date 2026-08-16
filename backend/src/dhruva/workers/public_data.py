"""``dhruva-public`` -- network-free public exchange reconstruction tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.ingest.historical_import import import_historical_dataset
from dhruva.ingest.public_dataset import build_public_dataset
from dhruva.ingest.public_reconstruction import (
    build_acquisition_plan,
    build_bias_report,
    canonical_json_bytes,
    inspect_drop,
    public_preflight,
)
from dhruva.ingest.public_sources import (
    PUBLIC_SOURCE_CAPABILITIES,
    public_capability_export_bytes,
)
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError
from dhruva.workers.cli_arguments import parse_account

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["build_parser", "main", "run"]

_EXIT_OK = 0
_EXIT_REFUSED = 2


def _drop_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--drop", type=Path, required=True, help="local ignored raw-data root")


def build_parser() -> argparse.ArgumentParser:
    """Build the closed command surface; no command implements HTTP."""
    parser = argparse.ArgumentParser(
        prog="dhruva-public",
        description="Inspect, reconstruct, and import manually retained public exchange files.",
        epilog=(
            "Every source operation is network-free. Use official exchange pages manually; "
            "no downloader, browser impersonation, CAPTCHA handling, or access bypass exists."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sources = sub.add_parser("sources", help="print reviewed public capability matrix")
    sources.add_argument("--json", action="store_true")
    sources.add_argument("--report", type=Path)
    inspect = sub.add_parser("inspect-drop", help="hash and detect local source formats")
    _drop_argument(inspect)
    inspect.add_argument("--report", type=Path)
    preflight = sub.add_parser(
        "preflight", help="detect gaps, duplicates, corrections, and blockers"
    )
    _drop_argument(preflight)
    preflight.add_argument("--report", type=Path)
    for command, help_text in (
        ("build-manifest", "build canonical files and reconstruction manifest"),
        ("reconstruct-universe", "reconstruct the frozen liquidity universe and canonical pack"),
    ):
        build = sub.add_parser(command, help=help_text)
        _drop_argument(build)
        build.add_argument("--output", type=Path, required=True)
        build.add_argument(
            "--retrieved-at", required=True, help="UTC ISO instant for manual retrieval"
        )
        build.add_argument(
            "--benchmark-basis",
            choices=("PRICE_INDEX", "TOTAL_RETURN_INDEX"),
            default="PRICE_INDEX",
        )
    coverage = sub.add_parser("coverage", help="print owner-readable reconstruction scorecard")
    _drop_argument(coverage)
    coverage.add_argument("--report", type=Path)
    bias = sub.add_parser("bias", help="print deterministic scientific limitations")
    _drop_argument(bias)
    bias.add_argument("--report", type=Path)
    plan = sub.add_parser("plan", help="plan manual acquisition without downloading")
    plan.add_argument("--source", choices=("nse",), required=True)
    plan.add_argument("--from", dest="from_date", type=date.fromisoformat, required=True)
    plan.add_argument("--to", dest="to_date", type=date.fromisoformat, required=True)
    plan.add_argument("--local-root", type=Path)
    plan.add_argument("--report", type=Path)
    apply = sub.add_parser("import", help="apply an already READY canonical manifest")
    apply.add_argument("--manifest", type=Path, required=True)
    apply.add_argument("--account", required=True)
    apply.add_argument(
        "--apply", action="store_true", help="mandatory authoritative-write acknowledgement"
    )
    apply.add_argument("--report", type=Path)
    return parser


def _write(path: Path | None, payload: bytes) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _retrieved_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("--retrieved-at must be an explicit UTC instant")
    return parsed.astimezone(UTC)


def _inspection_payload(drop: Path) -> bytes:
    rows = [
        {
            **asdict(item),
            "path": item.path.name,
        }
        for item in inspect_drop(drop)
    ]
    return (
        canonical_json_bytes({"schema": "dhruva.public-drop-inspection.v1", "files": rows}) + b"\n"
    )


def _coverage_payload(drop: Path) -> bytes:
    report = public_preflight(inspect_drop(drop))
    blockers = list(report.blockers)
    warnings = list(report.warnings)
    body = {
        "schema": "dhruva.public-reconstruction-scorecard.v1",
        "title": "Public Historical Dataset Reconstruction",
        "coverage": {"start": report.coverage_start, "end": report.coverage_end},
        "trading_sessions": "KNOWN_AFTER_RECONSTRUCTION",
        "securities_observed": "KNOWN_AFTER_RECONSTRUCTION",
        "inactive_securities_retained": report.security_snapshots_present,
        "confirmed_delistings": "SOURCE_REPORTED_ONLY",
        "unresolved_disappearances": "RETAINED_AS_NO_LONGER_OBSERVED",
        "symbol_changes": "ISIN_CONTINUITY_WHERE_UNAMBIGUOUS",
        "isin_continuity": "PARTIAL" if report.security_snapshots_present else "UNKNOWN",
        "corporate_actions": report.corporate_actions_present,
        "benchmark": "NIFTY50_PRICE_INDEX" if report.benchmark_price_present else "UNAVAILABLE",
        "tri_available": report.benchmark_tri_present,
        "return_basis": "RAW_PRICE",
        "known_at_semantics": report.known_at_semantics,
        "archive_gaps": len(report.archive_gap_weekdays),
        "universe_reconstruction": "PUBLIC_LIQUID_NSE_UNIVERSE_V0",
        "survivorship_status": "SURVIVORSHIP_APPROXIMATE",
        "pit_status": "PIT_KNOWN_AT_UNAVAILABLE",
        "evidence_classification": report.evidence_class,
        "critical_blockers": blockers,
        "warnings": warnings,
        "usable_for": [
            "feature development",
            "retrospective diagnostics",
            "walk-forward diagnostics",
        ],
        "not_suitable_for": [
            "institutional PIT validation",
            "survivorship-safe claims",
            "real-money confidence by itself",
            "redistribution claims",
        ],
    }
    return canonical_json_bytes(body) + b"\n"


async def run(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911
    """Execute one local operation."""
    args = build_parser().parse_args(argv)
    if args.command == "sources":
        payload = public_capability_export_bytes()
        _write(args.report, payload)
        if args.json:
            sys.stdout.buffer.write(payload)
        else:
            for item in PUBLIC_SOURCE_CAPABILITIES:
                sys.stdout.write(
                    f"{item.source_id:34} research={item.research_suitability.value:12} "
                    f"automation={item.automation_status.value}\n"
                )
        return _EXIT_OK
    if args.command == "inspect-drop":
        payload = _inspection_payload(args.drop)
        _write(args.report, payload)
        sys.stdout.buffer.write(payload)
        return _EXIT_OK
    if args.command == "preflight":
        report = public_preflight(inspect_drop(args.drop))
        payload = report.export_bytes()
        _write(args.report, payload)
        sys.stdout.buffer.write(payload)
        return _EXIT_OK if report.readiness == "PUBLIC_RECONSTRUCTED" else _EXIT_REFUSED
    if args.command in {"build-manifest", "reconstruct-universe"}:
        build_result = build_public_dataset(
            inspect_drop(args.drop),
            destination=args.output,
            retrieved_at=_retrieved_at(args.retrieved_at),
            benchmark_basis=args.benchmark_basis,
        )
        sys.stdout.buffer.write(
            canonical_json_bytes(
                {
                    "schema": "dhruva.public-build-result.v1",
                    **asdict(build_result),
                    "canonical_manifest": str(build_result.canonical_manifest),
                    "reconstruction_manifest": str(build_result.reconstruction_manifest),
                }
            )
            + b"\n"
        )
        return _EXIT_OK
    if args.command == "coverage":
        payload = _coverage_payload(args.drop)
        _write(args.report, payload)
        sys.stdout.buffer.write(payload)
        return _EXIT_OK
    if args.command == "bias":
        preflight = public_preflight(inspect_drop(args.drop))
        payload = (
            canonical_json_bytes(
                {
                    "schema": "dhruva.public-reconstruction-bias.v1",
                    "findings": [asdict(item) for item in build_bias_report(preflight)],
                }
            )
            + b"\n"
        )
        _write(args.report, payload)
        sys.stdout.buffer.write(payload)
        return _EXIT_OK
    if args.command == "plan":
        payload = build_acquisition_plan(
            source=args.source,
            from_date=args.from_date,
            to_date=args.to_date,
            local_root=args.local_root,
        ).export_bytes()
        _write(args.report, payload)
        sys.stdout.buffer.write(payload)
        return _EXIT_OK
    if not args.apply:
        sys.stdout.write("Refused: import requires --apply; use preflight first.\n")
        return _EXIT_REFUSED
    account_id = parse_account(args.account)
    engine = build_engine(load_settings().db)
    sessions = build_session_factory(engine)
    try:
        import_result = await import_historical_dataset(
            args.manifest, account_id=account_id, session_factory=sessions
        )
        payload = import_result.export_bytes()
        _write(args.report, payload)
        sys.stdout.buffer.write(payload)
        return _EXIT_OK
    finally:
        await engine.dispose()


def main() -> None:
    """Translate expected contract failures to a stable refusal exit."""
    try:
        raise SystemExit(asyncio.run(run()))
    except (DhruvaError, OSError, ValueError, json.JSONDecodeError) as error:
        sys.stderr.write(f"Refused: {error}\n")
        raise SystemExit(_EXIT_REFUSED) from error
