"""Schema-derived public exchange mapper tests; fixture values are synthetic."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dhruva.ingest.public_exchange import (
    PublicFileFormat,
    inspect_public_file,
    iter_public_actions,
    iter_public_bars,
    iter_public_benchmarks,
    iter_public_securities,
)


def _write(path: Path, header: str, row: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{header}\n{row}\n", encoding="utf-8")
    return path


def _modern(path: Path) -> Path:
    return _write(
        path,
        "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
        "OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd",
        "2024-07-08,2024-07-08,CM,NSE,STK,101,INE000A01001,ALPHA,EQ,100,105,99,"
        "104,100000,10200000,400",
    )


def test_modern_udiff_mapping_preserves_raw_ohlcv_and_ids(tmp_path: Path) -> None:
    """Map modern UDiFF fields without adjustment or identity loss."""
    source = _modern(tmp_path / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv")
    inspection = inspect_public_file(source)
    bar = next(iter_public_bars(inspection))
    assert inspection.format is PublicFileFormat.NSE_CM_UDIFF_BHAVCOPY_V1
    assert (bar.trading_date, bar.symbol, bar.series) == (date(2024, 7, 8), "ALPHA", "EQ")
    assert (bar.isin, bar.security_id, bar.close, bar.volume) == (
        "INE000A01001",
        "101",
        Decimal("104"),
        100000,
    )


def test_legacy_bhavcopy_mapping(tmp_path: Path) -> None:
    """Map the discontinued legacy capital-market bhavcopy explicitly."""
    source = _write(
        tmp_path / "cm08JUL2024bhav.csv",
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN",
        "ALPHA,EQ,100,105,99,104,100000,10200000,08-JUL-2024,400,INE000A01001",
    )
    bar = next(iter_public_bars(inspect_public_file(source)))
    assert bar.source_format is PublicFileFormat.NSE_CM_LEGACY_BHAVCOPY_V1
    assert bar.trade_count == 400


def test_full_bhavcopy_delivery_mapping(tmp_path: Path) -> None:
    """Map full bhavcopy turnover and delivery fields with declared units."""
    source = _write(
        tmp_path / "sec_bhavdata_full_08072024.csv",
        "SYMBOL,SERIES,DATE1,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,"
        "TTL_TRD_QNTY,TURNOVER_LACS,NO_OF_TRADES,DELIV_QTY",
        "ALPHA,EQ,08-JUL-2024,100,105,99,104,100000,102,400,55000",
    )
    bar = next(iter_public_bars(inspect_public_file(source)))
    assert bar.source_format is PublicFileFormat.NSE_FULL_BHAVCOPY_DELIVERABLE_V1
    assert (bar.delivery_quantity, bar.turnover) == (55000, Decimal("10200000"))


def test_mii_security_snapshot_mapping(tmp_path: Path) -> None:
    """Map the official MII security snapshot and lifecycle fields."""
    source = _write(
        tmp_path / "NSE_CM_security_08072024.csv",
        "FinInstrmId,TckrSymb,SctySrs,ISIN,FinInstrmNm,SctySts,ListgDt",
        "101,ALPHA,EQ,INE000A01001,Alpha Industries,ACTIVE,2020-01-01",
    )
    security = next(iter_public_securities(inspect_public_file(source)))
    assert security.source_format is PublicFileFormat.NSE_CM_MII_SECURITY_V1
    assert (security.snapshot_date, security.listing_date) == (
        date(2024, 7, 8),
        date(2020, 1, 1),
    )


@pytest.mark.parametrize(
    ("purpose", "event", "ratio", "cash"),
    [
        ("Bonus 2:1", "BONUS", (Decimal(2), Decimal(1)), None),
        ("Stock Split From Rs 10 To Rs 2", "SPLIT", (None, None), None),
        ("Dividend - Rs 12.50 Per Share", "DIVIDEND", (None, None), Decimal("12.50")),
        ("Demerger", "DEMERGER", (None, None), None),
        ("Rights 3:7", "RIGHTS_ISSUE", (Decimal(3), Decimal(7)), None),
    ],
)
def test_corporate_actions_parse_only_explicit_values(
    tmp_path: Path,
    purpose: str,
    event: str,
    ratio: tuple[Decimal | None, Decimal | None],
    cash: Decimal | None,
) -> None:
    """Parse only action values explicitly stated in the official row."""
    source = _write(
        tmp_path / "corporate-actions.csv",
        "SYMBOL,COMPANY NAME,SERIES,PURPOSE,FACE VALUE,EX-DATE,RECORD DATE",
        f"ALPHA,Alpha Industries,EQ,{purpose},10,08-Jul-2024,09-Jul-2024",
    )
    action = next(iter_public_actions(inspect_public_file(source)))
    assert (action.event_type, action.ratio_numerator, action.ratio_denominator) == (
        event,
        *ratio,
    )
    assert action.cash_value == cash


def test_price_index_and_tri_are_separate_identities(tmp_path: Path) -> None:
    """Keep price-index and total-return-index histories distinct."""
    price = _write(
        tmp_path / "nifty-price.csv",
        "Date,Open,High,Low,Close,Shares Traded",
        "08-Jul-2024,24000,24100,23900,24050,200000000",
    )
    tri = _write(
        tmp_path / "nifty-tri.csv",
        "Date,Total Returns Index",
        "08-Jul-2024,35000",
    )
    price_bar = next(iter_public_benchmarks(inspect_public_file(price)))
    tri_bar = next(iter_public_benchmarks(inspect_public_file(tri)))
    assert (price_bar.benchmark_id, price_bar.basis) == (
        "nifty50-price-index",
        "PRICE_INDEX",
    )
    assert (tri_bar.benchmark_id, tri_bar.basis) == (
        "nifty50-total-return-index",
        "TOTAL_RETURN_INDEX",
    )


def test_unsupported_or_ambiguous_schema_is_not_coerced(tmp_path: Path) -> None:
    """Reject an unknown header set instead of guessing a source schema."""
    source = _write(tmp_path / "unknown.csv", "symbol,close", "ALPHA,100")
    inspection = inspect_public_file(source)
    assert inspection.format is PublicFileFormat.UNKNOWN
    with pytest.raises(ValueError, match="supported bar format"):
        tuple(iter_public_bars(inspection))


def test_inspection_does_not_mutate_source(tmp_path: Path) -> None:
    """Inspection leaves owner-provided source evidence byte-identical."""
    source = _modern(tmp_path / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    inspect_public_file(source)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_invalid_isin_fails_closed(tmp_path: Path) -> None:
    """Reject malformed identity evidence before canonical mapping."""
    source = _modern(tmp_path / "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv")
    source.write_text(source.read_text().replace("INE000A01001", "BAD"), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid ISIN"):
        tuple(iter_public_bars(inspect_public_file(source)))
