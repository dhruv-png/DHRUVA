"""Generate a deterministic three-year TEST DATA delivery for offline validation."""

from __future__ import annotations

import csv
import hashlib
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from dhruva.ingest.historical_dataset import (
    CANONICAL_SCHEMAS,
    MANIFEST_SCHEMA,
    canonical_json_bytes,
)

__all__ = ["generate_synthetic_dataset"]

_ACQUIRED: Final = "2023-01-15T12:00:00Z"
_START: Final = date(2020, 1, 1)
_END: Final = date(2022, 12, 30)
_WEEKDAY_COUNT: Final = 5


def _row(**values: object) -> dict[str, str]:
    return {key: "" if value is None else str(value) for key, value in values.items()}


def _business_days(start: date, end: date) -> tuple[date, ...]:
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < _WEEKDAY_COUNT:
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)


def _known(day: date, *, hour: int = 18) -> str:
    return f"{day.isoformat()}T{hour:02d}:00:00Z"


def _identities() -> list[dict[str, str]]:
    rows = []
    securities = (
        ("SEC1", "ALPHA", "Alpha Industries", "INE000A01001", _START, None),
        ("SEC2", "BETA", "Beta Limited", "INE000B01009", _START, date(2021, 6, 30)),
        ("SEC2", "BETANEW", "Beta Limited", "INE000B01009", date(2021, 7, 1), None),
        ("SEC3", "GAMMA", "Gamma Services", "INE000C01007", _START, None),
        ("SEC4", "IPOCO", "IPO Company", "INE000D01005", date(2021, 4, 1), None),
        ("SEC5", "REENTRY", "Reentry Products", "INE000E01003", _START, None),
        ("SEC6", "MERGED", "Merged Ventures", "INE000F01001", _START, date(2022, 6, 30)),
    )
    for index, (security, symbol, company, isin, start, end) in enumerate(securities, 1):
        rows.append(
            _row(
                source_row_id=f"identity-{index:03d}",
                source_security_id=security,
                instrument_kind="EQUITY",
                canonical_symbol=symbol,
                company_name=company,
                isin=isin,
                exchange="NSE",
                provider_instrument_token=None,
                exchange_token=None,
                valid_from=start,
                valid_to=end,
                known_at=_known(start),
                source_revision=f"identity-r{index:03d}",
            )
        )
    return rows


def _definition() -> list[dict[str, str]]:
    return [
        _row(
            source_row_id="universe-001",
            universe_id="synthetic-pit-market",
            label="Synthetic PIT Market (TEST DATA)",
            kind="HISTORICAL_MARKET_UNIVERSE",
            known_at="2020-01-01T18:00:00Z",
            source_revision="fixture-r1",
            historical_membership_available="true",
            removals_included="true",
            delistings_included="true",
            pit_known_at_available="true",
            instrument_lifecycle_available="true",
            corporate_action_coverage_available="true",
            licensing_confirmed="true",
            return_basis="PRICE_ADJUSTED",
            benchmark_basis="PRICE_INDEX",
            benchmark_history_available="true",
        )
    ]


def _memberships() -> list[dict[str, str]]:
    periods = (
        ("SEC1", _START, None, False),
        ("SEC2", _START, None, False),
        ("SEC3", _START, None, False),
        ("SEC4", date(2021, 4, 1), None, False),
        ("SEC5", _START, date(2020, 9, 30), False),
        ("SEC5", date(2021, 3, 1), None, False),
        ("SEC6", _START, date(2022, 6, 30), True),
    )
    return [
        _row(
            source_row_id=f"membership-{index:03d}",
            universe_id="synthetic-pit-market",
            source_security_id=security,
            effective_from=start,
            effective_to=end,
            known_at=_known(start),
            source_revision="fixture-r1",
            membership_reason="SOURCE_REPORTED",
            is_delisted=str(delisted).lower(),
        )
        for index, (security, start, end, delisted) in enumerate(periods, 1)
    ]


def _actions() -> list[dict[str, str]]:
    values = (
        ("SEC1", "SPLIT", date(2021, 5, 17), "2", "1", None, None),
        ("SEC2", "SYMBOL_CHANGE", date(2021, 7, 1), None, None, None, None),
        ("SEC3", "BONUS", date(2022, 2, 14), "1", "1", None, None),
        ("SEC3", "DIVIDEND", date(2022, 8, 18), None, None, "4.50", None),
        ("SEC6", "MERGER", date(2022, 7, 1), None, None, None, "SEC1"),
        ("SEC6", "DELISTING", date(2022, 6, 30), None, None, None, None),
    )
    return [
        _row(
            source_row_id=f"action-{index:03d}",
            source_security_id=security,
            event_type=event,
            effective_date=day,
            ex_date=day,
            record_date=day + timedelta(days=1),
            known_at=_known(day),
            ratio_numerator=numerator,
            ratio_denominator=denominator,
            cash_value=cash,
            currency="INR" if cash is not None else None,
            related_source_security_id=related,
            verification="VERIFIED",
            source_revision=f"action-r{index:03d}",
        )
        for index, (security, event, day, numerator, denominator, cash, related) in enumerate(
            values, 1
        )
    ]


def _daily_bars() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    active = {
        "SEC1": (_START, _END),
        "SEC2": (_START, _END),
        "SEC3": (_START, _END),
        "SEC4": (date(2021, 4, 1), _END),
        "SEC5": (_START, _END),
        "SEC6": (_START, date(2022, 6, 30)),
    }
    row_id = 0
    for security_index, (security, (start, end)) in enumerate(active.items(), 1):
        for sequence, day in enumerate(_business_days(start, end)):
            row_id += 1
            close = Decimal(50 + security_index * 20) + Decimal(sequence) / Decimal(50)
            rows.append(
                _row(
                    source_row_id=f"bar-{row_id:06d}",
                    source_security_id=security,
                    instrument_kind="CASH_EQUITY",
                    trading_date=day,
                    open=close - Decimal("0.20"),
                    high=close + Decimal("0.50"),
                    low=close - Decimal("0.50"),
                    close=close,
                    volume=100000 + sequence,
                    currency="INR",
                    exchange="NSE",
                    known_at=_known(day),
                    adjustment_status="VERIFIED",
                    source_revision="bars-r1",
                )
            )
            if security == "SEC1" and day == date(2021, 8, 2):
                row_id += 1
                rows.append(
                    _row(
                        **{
                            **rows[-1],
                            "source_row_id": f"bar-{row_id:06d}",
                            "close": close + Decimal("0.01"),
                            "high": close + Decimal("0.51"),
                            "known_at": _known(day, hour=20),
                            "source_revision": "bars-r2",
                        }
                    )
                )
    return rows


def _benchmark_bars() -> list[dict[str, str]]:
    rows = []
    for sequence, day in enumerate(_business_days(_START, _END), 1):
        close = Decimal(12000) + Decimal(sequence) * Decimal("1.25")
        rows.append(
            _row(
                source_row_id=f"benchmark-{sequence:06d}",
                benchmark_id="NIFTY50",
                canonical_symbol="NIFTY 50",
                trading_date=day,
                open=close - 5,
                high=close + 10,
                low=close - 10,
                close=close,
                volume=0,
                currency="INR",
                exchange="NSE",
                known_at=_known(day),
                benchmark_basis="PRICE_INDEX",
                adjustment_status="VERIFIED",
                source_revision="benchmark-r1",
            )
        )
    return rows


def generate_synthetic_dataset(root: Path, *, corrupt: bool = False) -> Path:
    """Create a complete sample pack and return its manifest path.

    ``corrupt=True`` changes one bar after checksums are written, exercising the
    quarantine path without constructing a subtly invalid production example.
    """
    root.mkdir(parents=True, exist_ok=True)
    payloads = {
        "instrument_identities": _identities(),
        "universe_definitions": _definition(),
        "universe_memberships": _memberships(),
        "corporate_actions": _actions(),
        "daily_bars": _daily_bars(),
        "benchmark_bars": _benchmark_bars(),
    }
    files = []
    for role, rows in payloads.items():
        schema, columns = CANONICAL_SCHEMAS[role]
        path = root / f"{role}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        content = path.read_bytes()
        files.append(
            {
                "role": role,
                "path": path.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "row_count": len(rows),
                "schema": schema,
                "media_type": "text/csv",
            }
        )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "dataset_id": "synthetic-india-pit-2020-2022",
        "provider": {
            "id": "fixture_provider",
            "name": "DHRUVA Synthetic Fixture",
            "product": "TEST DATA — NOT REAL MARKET DATA",
        },
        "license": {
            "reference": "synthetic-test-data",
            "status": "AUTOMATED_ANALYSIS_CONFIRMED",
        },
        "acquired_at": _ACQUIRED,
        "coverage": {"start": str(_START), "end": str(_END)},
        "source_revision": "fixture-r1",
        "market": {
            "currency": "INR",
            "exchange": "NSE",
            "timezone": "Asia/Kolkata",
            "price_basis": "OHLCV",
            "adjustment_basis": "PRICE_ADJUSTED_VERIFIED",
            "return_basis": "PRICE_ADJUSTED",
            "benchmark_basis": "PRICE_INDEX",
        },
        "known_at_semantics": "SOURCE_OBSERVED_AT",
        "import_policy": "APPEND_ONLY",
        "capabilities": {
            "historical_membership": True,
            "removals": True,
            "delistings": True,
            "inactive_securities": True,
            "instrument_lifecycle": True,
            "corporate_actions": True,
            "dividends": True,
            "publication_timestamps": True,
            "revision_history": True,
            "pit_known_at": True,
        },
        "files": files,
        "notes": (
            "SYNTHETIC TEST DATA ONLY: IPO, delisting, symbol change, re-entry, "
            "actions, merger, benchmark and corrected bar."
        ),
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    if corrupt:
        daily_path = root / "daily_bars.csv"
        daily_path.write_bytes(daily_path.read_bytes() + b"CORRUPTED\n")
    return manifest_path
