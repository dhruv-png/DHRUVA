"""Public universe, lifecycle, preflight, bias, and planner tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from dhruva.ingest.public_exchange import (
    DisappearanceState,
    ObservationState,
    PublicBar,
    PublicFileFormat,
    PublicSecurity,
    inspect_public_file,
)
from dhruva.ingest.public_reconstruction import (
    BiasSeverity,
    LiquidityUniverseRule,
    build_acquisition_plan,
    build_bias_report,
    compute_liquidity_memberships,
    inspect_drop,
    public_preflight,
    reconstruct_security_observations,
)


def _sessions(count: int) -> tuple[date, ...]:
    values: list[date] = []
    current = date(2024, 1, 1)
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return tuple(values)


def _bar(on: date, *, volume: int = 200000, symbol: str = "ALPHA") -> PublicBar:
    return PublicBar(
        f"bar-{on:%Y%m%d}-{symbol.lower()}",
        on,
        symbol,
        "EQ",
        "INE000A01001" if symbol == "ALPHA" else "INE000B01009",
        "101" if symbol == "ALPHA" else "102",
        Decimal(100),
        Decimal(102),
        Decimal(99),
        Decimal(100),
        volume,
        Decimal(100) * volume,
        100,
        None,
        PublicFileFormat.NSE_CM_UDIFF_BHAVCOPY_V1,
    )


def test_liquidity_universe_uses_only_trailing_observable_rows() -> None:
    """Future liquidity cannot revise a historical eligibility decision."""
    sessions = _sessions(5)
    rule = LiquidityUniverseRule(
        lookback_sessions=3,
        warmup_sessions=3,
        minimum_traded_sessions=2,
        minimum_median_rupee_turnover=Decimal("1000"),
    )
    baseline = tuple(_bar(day, volume=20) for day in sessions[:3])
    with_future = (*baseline, _bar(sessions[3], volume=10_000_000))
    before = compute_liquidity_memberships(baseline, expected_sessions=sessions, rule=rule)
    after = compute_liquidity_memberships(with_future, expected_sessions=sessions, rule=rule)
    baseline_cutoff = next(item for item in before if item.cutoff == sessions[2])
    future_cutoff = next(item for item in after if item.cutoff == sessions[2])
    assert baseline_cutoff == future_cutoff
    assert baseline_cutoff.eligible


def test_no_trade_day_is_not_delisted_and_inactive_history_is_retained() -> None:
    """An observed listing without a trade remains listed, not delisted."""
    sessions = _sessions(3)
    security = PublicSecurity(
        "security-1",
        sessions[0],
        "ALPHA",
        "EQ",
        "INE000A01001",
        "101",
        "Alpha Industries",
        "ACTIVE",
        date(2020, 1, 1),
        PublicFileFormat.NSE_CM_MII_SECURITY_V1,
    )
    observations = reconstruct_security_observations(
        (security,),
        (_bar(sessions[0]), _bar(sessions[2])),
        expected_sessions=sessions,
    )
    middle = next(item for item in observations if item.on == sessions[1])
    assert middle.state is ObservationState.INFERRED_LISTED
    assert middle.disappearance is None


def test_source_reported_suspension_and_delisting_are_distinct() -> None:
    """Retain source-reported suspension and delisting as distinct states."""
    sessions = _sessions(2)
    suspended = PublicSecurity(
        "security-s",
        sessions[0],
        "ALPHA",
        "EQ",
        "INE000A01001",
        "101",
        "Alpha Industries",
        "SUSPENDED",
        None,
        PublicFileFormat.NSE_CM_MII_SECURITY_V1,
    )
    delisted = replace(
        suspended, source_row_id="security-d", snapshot_date=sessions[1], status="DELISTED"
    )
    observations = reconstruct_security_observations(
        (suspended, delisted), (), expected_sessions=sessions
    )
    assert observations[0].state is ObservationState.SUSPENDED_IF_KNOWN
    assert observations[1].state is ObservationState.DELISTED
    assert observations[1].disappearance is DisappearanceState.CONFIRMED_DELISTED


def _modern(path: Path, *, close: str = "104") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
        "OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol\n"
        f"2024-07-08,2024-07-08,CM,NSE,STK,101,INE000A01001,ALPHA,EQ,100,105,99,{close},100000\n",
        encoding="utf-8",
    )
    return path


def test_preflight_is_deterministic_and_flags_unknown_schema(tmp_path: Path) -> None:
    """Preflight is byte-stable and rejects an unknown schema."""
    _modern(tmp_path / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv")
    (tmp_path / "bad.csv").write_text("bad,schema\n1,2\n", encoding="utf-8")
    inspections = inspect_drop(tmp_path)
    first = public_preflight(inspections)
    second = public_preflight(inspections)
    assert first.export_bytes() == second.export_bytes()
    assert first.readiness == "REJECTED"
    assert first.unsupported_files == ("bad.csv",)


def test_duplicate_and_corrected_files_are_distinguished(tmp_path: Path) -> None:
    """Distinguish byte duplicates from competing corrected source files."""
    name = "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv"
    first = _modern(tmp_path / "a" / name)
    second = _modern(tmp_path / "b" / name)
    duplicate = public_preflight((inspect_public_file(first), inspect_public_file(second)))
    assert duplicate.duplicate_files
    _modern(second, close="103")
    corrected = public_preflight((inspect_public_file(first), inspect_public_file(second)))
    assert corrected.revised_files
    assert "corrected/revised source files require explicit selection" in corrected.blockers


def test_archive_weekday_gap_is_not_silently_called_a_holiday(tmp_path: Path) -> None:
    """Report a weekday archive gap without inventing a holiday calendar."""
    first = _modern(tmp_path / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv")
    third = _modern(tmp_path / "BhavCopy_NSE_CM_0_0_0_20240710_F_0000.csv")
    report = public_preflight((inspect_public_file(first), inspect_public_file(third)))
    assert report.archive_gap_weekdays == ("2024-07-09",)
    assert any("may be exchange holidays" in item for item in report.warnings)


def test_bias_report_has_ten_named_risks_and_no_confidence_percentage(tmp_path: Path) -> None:
    """Emit named qualitative risks without fabricated confidence scores."""
    source = _modern(tmp_path / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv")
    report = public_preflight((inspect_public_file(source),))
    findings = build_bias_report(report)
    assert len(findings) == 10
    assert {item.severity for item in findings} <= set(BiasSeverity)
    assert all("%" not in item.basis for item in findings)


def test_acquisition_plans_are_network_free_and_scale_for_5_8_10_years() -> None:
    """Plan five-, eight-, and ten-year manual drops without network access."""
    counts = []
    for years in (5, 8, 10):
        plan = build_acquisition_plan(
            source="nse",
            from_date=date(2026 - years, 1, 1),
            to_date=date(2026, 1, 1),
        )
        counts.append(plan.expected_weekday_sessions)
        assert plan.automation_status == "AUTOMATION_UNCLEAR"
        assert plan.terms_review_required
        assert plan.approximate_disk_bytes is None
    assert counts == sorted(counts)
