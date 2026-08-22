"""Schema-derived public exchange mapper tests; fixture values are synthetic."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

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


_MONTHLY_HEADERS = (
    "Year",
    "Month",
    "Day",
    "Date",
    "Product",
    "ISIN",
    "Symbol",
    "Issuer Name",
    "CIN of Issuer",
    "Exchange",
    "Platform",
    "Instrument Type (Series)",
    "Listing Status",
    "Available for Trading",
    "Trading Status",
    "Trade Term/Type",
    "Previous Close Price",
    "Open Price",
    "High Price",
    "Low Price",
    "Last Traded Price",
    "Close Price",
    "VWAP (Turnover/ Total Traded Quantity)",
    "Trade Count",
    "Traded Quantity",
    "Turnover (in Rs)",
)


def _column_name(index: int) -> str:
    result = ""
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _monthly_xlsx(
    path: Path,
    headers: tuple[str, ...] = _MONTHLY_HEADERS,
) -> Path:
    strings = list(headers)
    rows = (
        (
            "2024",
            "July",
            "MONDAY",
            (date(2024, 7, 8) - date(1899, 12, 30)).days,
            "Equity",
            "INE000A01001",
            "ALPHA",
            "Alpha Industries",
            "N.A",
            "NSE",
            "N.A",
            "EQ",
            "Listed",
            "Y",
            "Traded",
            "N.A",
            99,
            100,
            105,
            99,
            103,
            104,
            102,
            400,
            100000,
            10200000,
        ),
        (
            "2024",
            "July",
            "MONDAY",
            (date(2024, 7, 8) - date(1899, 12, 30)).days,
            "Equity SME",
            "INE000A01002",
            "SMALLCO",
            "",
            "N.A",
            "NSE",
            "SME",
            "SM",
            "Listed",
            "Y",
            "Traded",
            "N.A",
            49,
            50,
            51,
            48,
            50,
            50,
            50,
            12,
            1000,
            50000,
        ),
        (
            "2024",
            "July",
            "MONDAY",
            (date(2024, 7, 8) - date(1899, 12, 30)).days,
            "Debt",
            "INE000A01001",
            "SKIPME",
            "Debt Instrument",
            "N.A",
            "NSE",
            "N.A",
            "N1",
            "Listed",
            "Y",
            "Traded",
            "N.A",
            99,
            100,
            105,
            99,
            103,
            104,
            102,
            400,
            100000,
            10200000,
        ),
    )
    for row in rows:
        strings.extend(str(value) for value in row if isinstance(value, str))
    string_index = {value: index for index, value in enumerate(dict.fromkeys(strings))}
    unique_strings = tuple(string_index)

    def cells(row_number: int, values: tuple[object, ...]) -> str:
        output: list[str] = []
        for index, value in enumerate(values, start=1):
            reference = f"{_column_name(index)}{row_number}"
            if isinstance(value, str):
                output.append(f'<c r="{reference}" t="s"><v>{string_index[value]}</v></c>')
            else:
                output.append(f'<c r="{reference}"><v>{value}</v></c>')
        return "".join(output)

    sheet_rows = [f'<row r="1">{cells(1, headers)}</row>']
    sheet_rows.extend(
        f'<row r="{number}">{cells(number, row)}</row>' for number, row in enumerate(rows, start=2)
    )
    shared = "".join(f"<si><t>{escape(value)}</t></si>" for value in unique_strings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook '
            'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            '<sheet name="Transaction Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>",
        )
    return path


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


def test_exchange_monthly_xlsx_maps_daily_traded_equities_offline(tmp_path: Path) -> None:
    """Map exact monthly Transaction Data headers and exclude non-equity products."""
    source = _monthly_xlsx(tmp_path / "Exchange_Data_CM_Segment_202407.xlsx")
    inspection = inspect_public_file(source)
    bars = tuple(iter_public_bars(inspection))
    assert inspection.format is PublicFileFormat.NSE_EXCHANGE_MONTHLY_TRANSACTION_V1
    assert len(bars) == 2
    bar = next(item for item in bars if item.symbol == "ALPHA")
    assert (bar.trading_date, bar.symbol, bar.series, bar.isin) == (
        date(2024, 7, 8),
        "ALPHA",
        "EQ",
        "INE000A01001",
    )
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("99"),
        Decimal("104"),
    )
    assert (bar.volume, bar.turnover, bar.trade_count, bar.company_name) == (
        100000,
        Decimal("10200000"),
        400,
        "Alpha Industries",
    )
    assert next(item for item in bars if item.symbol == "SMALLCO").company_name is None


def test_exchange_monthly_xlsx_rejects_an_unknown_header_revision(tmp_path: Path) -> None:
    """Fail closed when the monthly workbook adds or changes a source column."""
    source = _monthly_xlsx(
        tmp_path / "Exchange_Data_CM_changed.xlsx",
        (*_MONTHLY_HEADERS, "Unexpected Field"),
    )
    assert inspect_public_file(source).format is PublicFileFormat.UNKNOWN


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
