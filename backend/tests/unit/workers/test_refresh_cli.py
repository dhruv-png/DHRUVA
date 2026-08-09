"""``dhruva-refresh`` -- the command surface, phase classification, and safety scan.

What is asserted here: the parser carries no secret-bearing option, no phase
of composition reaches an order, options, NSE, scheduler or ML/LLM module,
market-data outcomes classify into the right severity with an honest
headline, and severity/exit-code arithmetic is exact. The full three-phase
composition against a real database is proved in
``tests/integration/test_refresh_workflow.py``; nothing here opens a socket
or a connection.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import pytest

from dhruva.contexts.marketdata.application.daily_history import IngestDailyHistoryResult
from dhruva.contexts.platform.domain.broker.session import BrokerAuthState
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.workers import marketdata
from dhruva.workers import refresh as cli
from dhruva.workers.marketdata import (
    CoverageStatus,
    CoverageSummary,
    InstrumentCoverage,
    MarketDataRefreshOutcome,
    MarketDataRefreshStatus,
)

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("owner-family")
AS_OF: Final = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

#: The vocabulary this slice was explicitly told never to introduce.
_FORBIDDEN_MODULE_FRAGMENTS = (
    "trading",
    "risk",
    "order",
    "options",
    "websocket",
    "nse",
    "celery",
    "schedule",
    "beat",
    "cron",
    "llm",
    "openai",
    "anthropic",
)


def _coverage(status: CoverageStatus, *, symbol: str = "SBIN") -> InstrumentCoverage:
    return InstrumentCoverage(
        instrument_id=InstrumentId.deterministic("reference", f"nse-equity-{symbol.lower()}"),
        canonical_symbol=symbol,
        company_name=f"{symbol} Limited",
        mapped=status is not CoverageStatus.MISSING_MAPPING,
        source_instrument_id=None,
        earliest_complete=None,
        latest_complete=date(2026, 8, 9),
        bar_count=7,
        enough_history=status in (CoverageStatus.STALE, CoverageStatus.READY),
        fresh=status is CoverageStatus.READY,
        status=status,
    )


def _summary(*statuses: CoverageStatus) -> CoverageSummary:
    return CoverageSummary(
        as_of=AS_OF,
        entries=tuple(_coverage(status, symbol=f"S{n}") for n, status in enumerate(statuses)),
    )


def _outcome(
    status: MarketDataRefreshStatus,
    *,
    coverage: CoverageSummary | None = None,
    auth_state: BrokerAuthState | None = None,
    ingest_result: IngestDailyHistoryResult | None = None,
) -> MarketDataRefreshOutcome:
    return MarketDataRefreshOutcome(
        status=status,
        exit_code=0,
        coverage=coverage if coverage is not None else _summary(CoverageStatus.READY),
        auth_state=auth_state,
        ingest_result=ingest_result,
    )


# --------------------------------------------------------------------------- #
# The command surface
# --------------------------------------------------------------------------- #

_FORBIDDEN_IN_OPTIONS = (
    "secret",
    "token",
    "password",
    "passwd",
    "pin",
    "totp",
    "otp",
    "key",
    "credential",
)


def test_no_option_anywhere_can_carry_a_secret() -> None:
    """No new surface for a secret to leak through, matching every sibling CLI."""
    parser = cli.build_parser()
    names = [option for action in parser._actions for option in action.option_strings]

    assert names
    for name in names:
        lowered = name.lower()
        for forbidden in _FORBIDDEN_IN_OPTIONS:
            assert forbidden not in lowered, f"{name} could carry a secret"


def test_account_is_required() -> None:
    """An account-less default would read or write one operator's data as another's."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_account_and_dry_run_are_the_whole_surface() -> None:
    """The normal invocation needs only the account alias -- no placeholder value."""
    args = cli.build_parser().parse_args(["--account", "owner-family"])

    assert args.account == "owner-family"
    assert args.dry_run is False

    dry = cli.build_parser().parse_args(["--account", "owner-family", "--dry-run"])
    assert dry.dry_run is True


def test_the_safety_notice_states_what_this_will_not_do() -> None:
    """Printed on --help, before an owner with a live session runs it."""
    epilog = (cli.build_parser().epilog or "").lower()

    for promise in ("no scheduler", "no order is placed", "no nse fetch"):
        assert promise in epilog


# --------------------------------------------------------------------------- #
# No reachable order, options, NSE, scheduler or ML/LLM path
# --------------------------------------------------------------------------- #


def _imported_module_roots() -> set[str]:
    """Return every top-level module name this file imports, at any depth."""
    source = Path(inspect.getfile(cli)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def test_no_forbidden_module_is_imported() -> None:
    """Order placement, options, NSE, a scheduler and ML/LLM are all unreachable.

    A static import scan rather than a runtime probe: the whole point is that
    this module never even names one of these modules, so there is nothing to
    call at any input.
    """
    imports = _imported_module_roots()
    for module in imports:
        lowered = module.lower()
        for forbidden in _FORBIDDEN_MODULE_FRAGMENTS:
            assert forbidden not in lowered, f"{module!r} imports forbidden fragment {forbidden!r}"


def test_no_daemon_or_scheduler_flag_exists() -> None:
    """Nothing on the command surface starts a loop or a background process."""
    parser = cli.build_parser()
    names = {option for action in parser._actions for option in action.option_strings}

    for forbidden in ("--daemon", "--loop", "--watch", "--interval", "--cron"):
        assert forbidden not in names


# --------------------------------------------------------------------------- #
# Market-data outcome -> phase status and headline
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected_phase"),
    [
        (MarketDataRefreshStatus.ALREADY_SUFFICIENT, cli.PhaseStatus.HEALTHY),
        (MarketDataRefreshStatus.MAPPING_REFRESHED, cli.PhaseStatus.HEALTHY),
        (MarketDataRefreshStatus.INGESTED, cli.PhaseStatus.HEALTHY),
        (MarketDataRefreshStatus.NOT_AUTHENTICATED, cli.PhaseStatus.DEGRADED),
        (MarketDataRefreshStatus.RESOLUTION_REFUSED, cli.PhaseStatus.REFUSED),
        (MarketDataRefreshStatus.BENCHMARK_UNMAPPED, cli.PhaseStatus.REFUSED),
        (MarketDataRefreshStatus.INGEST_REFUSED, cli.PhaseStatus.REFUSED),
    ],
)
def test_every_market_status_classifies_to_the_documented_phase(
    status: MarketDataRefreshStatus, expected_phase: cli.PhaseStatus
) -> None:
    """The exact HEALTHY/DEGRADED/REFUSED mapping this slice's report commits to."""
    report = cli._market_phase_report(_outcome(status))

    assert report.status is expected_phase


def test_not_authenticated_headline_names_the_broker_auth_state() -> None:
    """A missing session is stated as such, not folded into a generic refusal."""
    outcome = _outcome(
        MarketDataRefreshStatus.NOT_AUTHENTICATED,
        auth_state=BrokerAuthState.SESSION_EXPIRED,
    )

    report = cli._market_phase_report(outcome)

    assert "SESSION_EXPIRED" in report.headline
    assert "already-archived" in report.headline


def test_not_authenticated_headline_carries_no_secret() -> None:
    """Only the auth *state* enum value ever appears -- never a token or key."""
    outcome = _outcome(
        MarketDataRefreshStatus.NOT_AUTHENTICATED, auth_state=BrokerAuthState.SESSION_EXPIRED
    )

    report = cli._market_phase_report(outcome)

    for forbidden in ("token", "secret", "key="):
        assert forbidden not in report.headline.lower()


def test_ingested_headline_names_bars_added() -> None:
    """The count that actually changed is in the headline, not just a verdict word."""
    outcome = _outcome(
        MarketDataRefreshStatus.INGESTED,
        coverage=_summary(CoverageStatus.READY, CoverageStatus.READY),
        ingest_result=IngestDailyHistoryResult(
            instruments=2,
            bars_added=14,
            bars_unchanged=0,
            incomplete_bars=0,
            required_through=date(2026, 8, 9),
            quality_revision="daily-history-quality-v1",
        ),
    )

    report = cli._market_phase_report(outcome)

    assert "14 bar(s) added" in report.headline
    assert "2 watchlist instruments ready" in report.headline


def test_already_sufficient_headline_names_the_ready_count() -> None:
    """A healthy no-op is stated in the same shape as a healthy write."""
    outcome = _outcome(
        MarketDataRefreshStatus.ALREADY_SUFFICIENT,
        coverage=_summary(CoverageStatus.READY, CoverageStatus.READY, CoverageStatus.READY),
    )

    report = cli._market_phase_report(outcome)

    assert "3 watchlist instruments ready" in report.headline


def test_a_refusal_headline_defers_to_the_printed_diagnostics() -> None:
    """The compact summary line never repeats the full refusal text twice."""
    report = cli._market_phase_report(_outcome(MarketDataRefreshStatus.INGEST_REFUSED))

    assert "diagnostics" in report.headline


# --------------------------------------------------------------------------- #
# Severity and exit codes
# --------------------------------------------------------------------------- #


def test_severity_is_strictly_ordered() -> None:
    """HEALTHY < DEGRADED < REFUSED, so max() picks the worse of two phases."""
    assert (
        cli._SEVERITY[cli.PhaseStatus.HEALTHY]
        < cli._SEVERITY[cli.PhaseStatus.DEGRADED]
        < cli._SEVERITY[cli.PhaseStatus.REFUSED]
    )


@pytest.mark.parametrize(
    ("severity", "expected_exit"),
    [(0, cli._EXIT_OK), (1, cli._EXIT_DEGRADED), (2, cli._EXIT_REFUSED)],
)
def test_exit_code_matches_documented_severity(severity: int, expected_exit: int) -> None:
    """The three exit codes this slice promises, and nothing else."""
    assert cli._EXIT_FOR_SEVERITY[severity] == expected_exit


def test_exit_codes_are_zero_one_two() -> None:
    """Matches the vocabulary dhruva-news already uses: OK, degraded, refused."""
    assert (cli._EXIT_OK, cli._EXIT_DEGRADED, cli._EXIT_REFUSED) == (0, 1, 2)


# --------------------------------------------------------------------------- #
# Reused, not reimplemented
# --------------------------------------------------------------------------- #


def test_refresh_reuses_marketdatas_own_outcome_types() -> None:
    """A change to the market-data outcome shape is felt here, not duplicated."""
    refresh_module = vars(cli)
    assert refresh_module["MarketDataRefreshOutcome"] is marketdata.MarketDataRefreshOutcome
    assert refresh_module["MarketDataRefreshStatus"] is marketdata.MarketDataRefreshStatus
    assert refresh_module["refresh_market_data"] is marketdata.refresh_market_data
