"""Network-free detection and streaming parsing of reviewed public files."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import posixpath
import re
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final, TextIO

from defusedxml import ElementTree  # type: ignore[import-untyped]

__all__ = [
    "DisappearanceState",
    "EvidenceClass",
    "KnownAtSemantics",
    "ObservationState",
    "PublicBar",
    "PublicBenchmarkBar",
    "PublicCorporateAction",
    "PublicFileFormat",
    "PublicFileInspection",
    "PublicSecurity",
    "inspect_public_file",
    "iter_public_actions",
    "iter_public_bars",
    "iter_public_benchmarks",
    "iter_public_securities",
]

_MAX_HEADER_BYTES = 256_000
_MAX_ARCHIVE_MEMBERS = 8
_MAX_XLSX_ARCHIVE_MEMBERS = 128
_MAX_UNCOMPRESSED_BYTES = 2_000_000_000
_MAX_XLSX_COLUMNS = 16_384
_MAX_XLSX_COLUMN_LETTERS = 3
_MAX_TEXT_CHARS = 4096
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")
_SAFE_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&._-]{0,31}\Z")
_RATIO = re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)(?![0-9.])")
_DIVIDEND = re.compile(r"(?:RS\.?|RE\.?|INR)\s*[-:]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


class EvidenceClass(StrEnum):
    """Origin class kept distinct from quality/readiness."""

    LICENSED_VENDOR = "LICENSED_VENDOR"
    PUBLIC_EXCHANGE_ARCHIVE = "PUBLIC_EXCHANGE_ARCHIVE"
    PUBLIC_RECONSTRUCTED = "PUBLIC_RECONSTRUCTED"
    OWNER_PROVIDED = "OWNER_PROVIDED"
    SYNTHETIC_TEST = "SYNTHETIC_TEST"


class KnownAtSemantics(StrEnum):
    """What a retained public file proves about knowledge time."""

    PUBLICATION_DATE_KNOWN = "PUBLICATION_DATE_KNOWN"
    FILE_DATE_ONLY = "FILE_DATE_ONLY"
    RETRIEVED_LATER = "RETRIEVED_LATER"
    REVISION_HISTORY_UNKNOWN = "REVISION_HISTORY_UNKNOWN"
    PROSPECTIVE_ARCHIVE = "PROSPECTIVE_ARCHIVE"


class PublicFileFormat(StrEnum):
    """Exact supported source layouts; UNKNOWN is never coerced."""

    NSE_CM_UDIFF_BHAVCOPY_V1 = "NSE_CM_UDIFF_BHAVCOPY_V1"
    NSE_CM_LEGACY_BHAVCOPY_V1 = "NSE_CM_LEGACY_BHAVCOPY_V1"
    NSE_FULL_BHAVCOPY_DELIVERABLE_V1 = "NSE_FULL_BHAVCOPY_DELIVERABLE_V1"
    NSE_EXCHANGE_MONTHLY_TRANSACTION_V1 = "NSE_EXCHANGE_MONTHLY_TRANSACTION_V1"
    NSE_CM_MII_SECURITY_V1 = "NSE_CM_MII_SECURITY_V1"
    NSE_CORPORATE_ACTIONS_V1 = "NSE_CORPORATE_ACTIONS_V1"
    NSE_NIFTY_PRICE_INDEX_V1 = "NSE_NIFTY_PRICE_INDEX_V1"
    NSE_NIFTY_TRI_V1 = "NSE_NIFTY_TRI_V1"
    UNKNOWN = "UNKNOWN"


class ObservationState(StrEnum):
    """Event-date security observation without fabricated lifecycle facts."""

    OBSERVED_TRADED = "OBSERVED_TRADED"
    OBSERVED_LISTED = "OBSERVED_LISTED"
    INFERRED_LISTED = "INFERRED_LISTED"
    NOT_OBSERVED = "NOT_OBSERVED"
    DELISTED = "DELISTED"
    SUSPENDED_IF_KNOWN = "SUSPENDED_IF_KNOWN"
    UNKNOWN = "UNKNOWN"


class DisappearanceState(StrEnum):
    """Terminal reconstruction conclusion."""

    CONFIRMED_DELISTED = "CONFIRMED_DELISTED"
    NO_LONGER_OBSERVED = "NO_LONGER_OBSERVED"
    MAPPING_UNRESOLVED = "MAPPING_UNRESOLVED"
    SUSPENSION_UNKNOWN = "SUSPENSION_UNKNOWN"


@dataclass(frozen=True, slots=True)
class PublicFileInspection:
    """Immutable source-file identity and detected schema."""

    path: Path
    original_filename: str
    sha256: str
    size_bytes: int
    format: PublicFileFormat
    source_schema_revision: str
    header: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PublicBar:
    """Raw public cash-equity OHLCV observation."""

    source_row_id: str
    trading_date: date
    symbol: str
    series: str
    isin: str | None
    security_id: str | None
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    turnover: Decimal | None
    trade_count: int | None
    delivery_quantity: int | None
    source_format: PublicFileFormat
    company_name: str | None = None


@dataclass(frozen=True, slots=True)
class PublicSecurity:
    """One row from an official listed-security snapshot."""

    source_row_id: str
    snapshot_date: date
    symbol: str
    series: str
    isin: str | None
    security_id: str
    company_name: str
    status: str
    listing_date: date | None
    source_format: PublicFileFormat


@dataclass(frozen=True, slots=True)
class PublicCorporateAction:
    """Source-reported action; ratios remain absent unless text is explicit."""

    source_row_id: str
    symbol: str
    series: str
    company_name: str
    purpose: str
    event_type: str
    ex_date: date
    record_date: date | None
    ratio_numerator: Decimal | None
    ratio_denominator: Decimal | None
    cash_value: Decimal | None
    source_format: PublicFileFormat


@dataclass(frozen=True, slots=True)
class PublicBenchmarkBar:
    """Official NIFTY price-index or TRI observation."""

    source_row_id: str
    trading_date: date
    benchmark_id: str
    basis: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source_format: PublicFileFormat


_NSE_EXCHANGE_MONTHLY_TRANSACTION_HEADERS: Final = (
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

_SIGNATURES: Final[tuple[tuple[PublicFileFormat, frozenset[str]], ...]] = (
    (
        PublicFileFormat.NSE_EXCHANGE_MONTHLY_TRANSACTION_V1,
        frozenset(_NSE_EXCHANGE_MONTHLY_TRANSACTION_HEADERS),
    ),
    (
        PublicFileFormat.NSE_CM_UDIFF_BHAVCOPY_V1,
        frozenset(
            {
                "TradDt",
                "Sgmt",
                "FinInstrmId",
                "TckrSymb",
                "SctySrs",
                "OpnPric",
                "HghPric",
                "LwPric",
                "ClsPric",
                "TtlTradgVol",
            }
        ),
    ),
    (
        PublicFileFormat.NSE_CM_LEGACY_BHAVCOPY_V1,
        frozenset(
            {
                "SYMBOL",
                "SERIES",
                "OPEN",
                "HIGH",
                "LOW",
                "CLOSE",
                "TOTTRDQTY",
                "TIMESTAMP",
                "ISIN",
            }
        ),
    ),
    (
        PublicFileFormat.NSE_FULL_BHAVCOPY_DELIVERABLE_V1,
        frozenset(
            {
                "SYMBOL",
                "SERIES",
                "DATE1",
                "OPEN_PRICE",
                "HIGH_PRICE",
                "LOW_PRICE",
                "CLOSE_PRICE",
                "TTL_TRD_QNTY",
                "DELIV_QTY",
            }
        ),
    ),
    (
        PublicFileFormat.NSE_CORPORATE_ACTIONS_V1,
        frozenset({"SYMBOL", "COMPANY NAME", "SERIES", "PURPOSE", "EX-DATE", "RECORD DATE"}),
    ),
    (
        PublicFileFormat.NSE_NIFTY_PRICE_INDEX_V1,
        frozenset({"Date", "Open", "High", "Low", "Close", "Shares Traded"}),
    ),
    (PublicFileFormat.NSE_NIFTY_TRI_V1, frozenset({"Date", "Total Returns Index"})),
)


def inspect_public_file(path: Path) -> PublicFileInspection:
    """Hash and header-detect one local source without modifying it."""
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError("public source must be a regular non-symlink file")
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError("public source file is empty")
    digest = hashlib.sha256()
    with resolved.open("rb") as binary:
        for chunk in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(chunk)
    if resolved.name.lower().endswith(".xlsx"):
        header = _xlsx_header(resolved)
    else:
        with _open_csv(resolved) as stream:
            reader = csv.reader(stream)
            try:
                header = tuple(_clean_header(item) for item in next(reader))
            except StopIteration as error:
                raise ValueError("public source has no CSV header") from error
    detected = _detect(header, resolved.name)
    return PublicFileInspection(
        path=resolved,
        original_filename=resolved.name,
        sha256=digest.hexdigest(),
        size_bytes=size,
        format=detected,
        source_schema_revision=detected.value.lower(),
        header=header,
    )


def _detect(header: tuple[str, ...], filename: str) -> PublicFileFormat:
    fields = frozenset(header)
    matches = [file_format for file_format, required in _SIGNATURES if required <= fields]
    security_required = {"FinInstrmId", "TckrSymb", "SctySrs", "ISIN"}
    security_names = {"FinInstrmNm", "SctyNm", "SecurityName", "SECURITY_NAME"}
    if security_required <= fields and fields & security_names and "TradDt" not in fields:
        matches.append(PublicFileFormat.NSE_CM_MII_SECURITY_V1)
    if len(matches) != 1:
        return PublicFileFormat.UNKNOWN
    detected = matches[0]
    lower = filename.lower()
    if detected is PublicFileFormat.NSE_EXCHANGE_MONTHLY_TRANSACTION_V1 and not lower.endswith(
        ".xlsx"
    ):
        return PublicFileFormat.UNKNOWN
    if (
        detected is PublicFileFormat.NSE_EXCHANGE_MONTHLY_TRANSACTION_V1
        and header != _NSE_EXCHANGE_MONTHLY_TRANSACTION_HEADERS
    ):
        return PublicFileFormat.UNKNOWN
    if detected is PublicFileFormat.NSE_CM_MII_SECURITY_V1 and not (
        lower.endswith(".csv") or lower.endswith(".csv.gz") or lower.endswith(".gz")
    ):
        return PublicFileFormat.UNKNOWN
    return detected


def iter_public_bars(inspection: PublicFileInspection) -> Iterator[PublicBar]:
    """Stream raw equity rows from one supported bhavcopy."""
    supported = {
        PublicFileFormat.NSE_CM_UDIFF_BHAVCOPY_V1,
        PublicFileFormat.NSE_CM_LEGACY_BHAVCOPY_V1,
        PublicFileFormat.NSE_FULL_BHAVCOPY_DELIVERABLE_V1,
        PublicFileFormat.NSE_EXCHANGE_MONTHLY_TRANSACTION_V1,
    }
    if inspection.format not in supported:
        raise ValueError(f"not a supported bar format: {inspection.format.value}")
    if inspection.format is PublicFileFormat.NSE_EXCHANGE_MONTHLY_TRANSACTION_V1:
        yield from _iter_monthly_bars(inspection)
        return
    with _open_csv(inspection.path) as stream:
        for number, row in enumerate(csv.DictReader(stream), start=2):
            clean = _clean_row(row)
            yield _bar(clean, inspection.format, inspection.sha256, number)


def _iter_monthly_bars(inspection: PublicFileInspection) -> Iterator[PublicBar]:
    rows = _iter_xlsx_rows(inspection.path)
    try:
        header = tuple(_clean_header(item) for item in next(rows))
    except StopIteration as error:
        raise ValueError("monthly exchange report has no Transaction Data header") from error
    for number, values in enumerate(rows, start=2):
        if len(values) > len(header):
            raise ValueError("monthly exchange row has more values than headers")
        padded = (*values, *("" for _ in range(len(header) - len(values))))
        row = dict(zip(header, padded, strict=True))
        product = row["Product"].strip()
        if product not in {"Equity", "Equity SME"}:
            continue
        if row["Trading Status"].strip() != "Traded":
            continue
        if row["Exchange"].strip() != "NSE":
            raise ValueError("monthly exchange equity row is not NSE")
        if row["Available for Trading"].strip() != "Y":
            raise ValueError("monthly exchange traded row is not marked available")
        isin = _isin(_required(row["ISIN"], "ISIN"))
        if isin is None:
            raise ValueError("monthly exchange equity row requires ISIN")
        yield PublicBar(
            source_row_id=_row_id(inspection.sha256, number),
            trading_date=_xlsx_day(row["Date"]),
            symbol=_symbol(row["Symbol"]),
            series=_series(row["Instrument Type (Series)"]),
            isin=isin,
            security_id=None,
            open=_decimal(row["Open Price"]),
            high=_decimal(row["High Price"]),
            low=_decimal(row["Low Price"]),
            close=_decimal(row["Close Price"]),
            volume=_integer(row["Traded Quantity"]),
            turnover=_decimal(row["Turnover (in Rs)"]),
            trade_count=_integer(row["Trade Count"]),
            delivery_quantity=None,
            source_format=inspection.format,
            company_name=_optional(row["Issuer Name"]),
        )


def _bar(
    row: Mapping[str, str], file_format: PublicFileFormat, digest: str, number: int
) -> PublicBar:
    if file_format is PublicFileFormat.NSE_CM_UDIFF_BHAVCOPY_V1:
        on = _day(row["TradDt"])
        symbol, series = row["TckrSymb"], row["SctySrs"]
        return PublicBar(
            _row_id(digest, number),
            on,
            _symbol(symbol),
            _series(series),
            _isin(row.get("ISIN", "")),
            _optional(row.get("FinInstrmId", "")),
            _decimal(row["OpnPric"]),
            _decimal(row["HghPric"]),
            _decimal(row["LwPric"]),
            _decimal(row["ClsPric"]),
            _integer(row["TtlTradgVol"]),
            _optional_decimal(row.get("TtlTrfVal", "")),
            _optional_integer(row.get("TtlNbOfTxsExctd", "")),
            None,
            file_format,
        )
    if file_format is PublicFileFormat.NSE_CM_LEGACY_BHAVCOPY_V1:
        return PublicBar(
            _row_id(digest, number),
            _day(row["TIMESTAMP"]),
            _symbol(row["SYMBOL"]),
            _series(row["SERIES"]),
            _isin(row.get("ISIN", "")),
            None,
            _decimal(row["OPEN"]),
            _decimal(row["HIGH"]),
            _decimal(row["LOW"]),
            _decimal(row["CLOSE"]),
            _integer(row["TOTTRDQTY"]),
            _optional_decimal(row.get("TOTTRDVAL", "")),
            _optional_integer(row.get("TOTALTRADES", "")),
            None,
            file_format,
        )
    return PublicBar(
        _row_id(digest, number),
        _day(row["DATE1"]),
        _symbol(row["SYMBOL"]),
        _series(row["SERIES"]),
        None,
        None,
        _decimal(row["OPEN_PRICE"]),
        _decimal(row["HIGH_PRICE"]),
        _decimal(row["LOW_PRICE"]),
        _decimal(row["CLOSE_PRICE"]),
        _integer(row["TTL_TRD_QNTY"]),
        _optional_decimal(row.get("TURNOVER_LACS", ""), multiplier=Decimal("100000")),
        _optional_integer(row.get("NO_OF_TRADES", "")),
        _optional_integer(row.get("DELIV_QTY", "")),
        file_format,
    )


def iter_public_securities(inspection: PublicFileInspection) -> Iterator[PublicSecurity]:
    """Stream a reviewed MII security snapshot."""
    if inspection.format is not PublicFileFormat.NSE_CM_MII_SECURITY_V1:
        raise ValueError("not an NSE MII security file")
    snapshot = _date_from_filename(inspection.original_filename)
    with _open_csv(inspection.path) as stream:
        for number, raw in enumerate(csv.DictReader(stream), start=2):
            row = _clean_row(raw)
            name = _first(row, "FinInstrmNm", "SctyNm", "SecurityName", "SECURITY_NAME")
            listing = _optional_day(_first(row, "ListgDt", "ListingDate", "LISTING_DATE"))
            status = _first(row, "SctySts", "SecurityStatus", "STATUS") or "UNKNOWN"
            yield PublicSecurity(
                _row_id(inspection.sha256, number),
                snapshot,
                _symbol(row["TckrSymb"]),
                _series(row["SctySrs"]),
                _isin(row.get("ISIN", "")),
                _required(row["FinInstrmId"], "security id"),
                _required(name, "security name"),
                status,
                listing,
                inspection.format,
            )


def iter_public_actions(inspection: PublicFileInspection) -> Iterator[PublicCorporateAction]:
    """Stream corporate actions and parse only explicit values."""
    if inspection.format is not PublicFileFormat.NSE_CORPORATE_ACTIONS_V1:
        raise ValueError("not an NSE corporate-action CSV")
    with _open_csv(inspection.path) as stream:
        for number, raw in enumerate(csv.DictReader(stream), start=2):
            row = _clean_row(raw)
            purpose = _required(row["PURPOSE"], "purpose")
            event = _action_type(purpose)
            ratio = _RATIO.search(purpose) if event in {"SPLIT", "BONUS", "RIGHTS_ISSUE"} else None
            dividend = _DIVIDEND.search(purpose) if event == "DIVIDEND" else None
            yield PublicCorporateAction(
                _row_id(inspection.sha256, number),
                _symbol(row["SYMBOL"]),
                _series(row["SERIES"]),
                _required(row["COMPANY NAME"], "company name"),
                purpose,
                event,
                _day(row["EX-DATE"]),
                _optional_day(row.get("RECORD DATE", "")),
                None if ratio is None else _decimal(ratio.group(1)),
                None if ratio is None else _decimal(ratio.group(2)),
                None if dividend is None else _decimal(dividend.group(1)),
                inspection.format,
            )


def iter_public_benchmarks(inspection: PublicFileInspection) -> Iterator[PublicBenchmarkBar]:
    """Stream official NIFTY price or TRI rows without conflating identities."""
    formats = {
        PublicFileFormat.NSE_NIFTY_PRICE_INDEX_V1,
        PublicFileFormat.NSE_NIFTY_TRI_V1,
    }
    if inspection.format not in formats:
        raise ValueError("not a supported NSE benchmark CSV")
    with _open_csv(inspection.path) as stream:
        for number, raw in enumerate(csv.DictReader(stream), start=2):
            row = _clean_row(raw)
            if inspection.format is PublicFileFormat.NSE_NIFTY_TRI_V1:
                value = _decimal(row["Total Returns Index"])
                yield PublicBenchmarkBar(
                    _row_id(inspection.sha256, number),
                    _day(row["Date"]),
                    "nifty50-total-return-index",
                    "TOTAL_RETURN_INDEX",
                    value,
                    value,
                    value,
                    value,
                    0,
                    inspection.format,
                )
            else:
                yield PublicBenchmarkBar(
                    _row_id(inspection.sha256, number),
                    _day(row["Date"]),
                    "nifty50-price-index",
                    "PRICE_INDEX",
                    _decimal(row["Open"]),
                    _decimal(row["High"]),
                    _decimal(row["Low"]),
                    _decimal(row["Close"]),
                    _integer(row.get("Shares Traded", "0")),
                    inspection.format,
                )


class _TextContext:
    def __init__(self, stream: TextIO, closers: tuple[object, ...]) -> None:
        self.stream = stream
        self.closers = closers

    def __enter__(self) -> TextIO:
        return self.stream

    def __exit__(self, *_args: object) -> None:
        for closer in self.closers:
            close = getattr(closer, "close", None)
            if close is not None:
                close()


def _open_csv(path: Path) -> _TextContext:
    lower = path.name.lower()
    if lower.endswith(".zip"):
        archive = zipfile.ZipFile(path)
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if len(infos) != 1 or len(infos) > _MAX_ARCHIVE_MEMBERS:
            archive.close()
            raise ValueError("ZIP must contain exactly one regular CSV member")
        info = infos[0]
        if info.file_size > _MAX_UNCOMPRESSED_BYTES or not info.filename.lower().endswith(".csv"):
            archive.close()
            raise ValueError("unsafe ZIP member")
        zip_binary = archive.open(info)
        zip_text = io.TextIOWrapper(zip_binary, encoding="utf-8-sig", newline="")
        return _TextContext(zip_text, (zip_text, zip_binary, archive))
    if lower.endswith(".gz"):
        gzip_binary = gzip.open(path, "rb")  # noqa: SIM115 - owned by returned context
        gzip_text = io.TextIOWrapper(gzip_binary, encoding="utf-8-sig", newline="")
        return _TextContext(gzip_text, (gzip_text, gzip_binary))
    plain_text = path.open("r", encoding="utf-8-sig", newline="")
    return _TextContext(plain_text, (plain_text,))


_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _xlsx_header(path: Path) -> tuple[str, ...]:
    rows = _iter_xlsx_rows(path)
    try:
        return tuple(_clean_header(item) for item in next(rows))
    except StopIteration as error:
        raise ValueError("monthly exchange report has no Transaction Data header") from error


def _validated_xlsx_members(archive: zipfile.ZipFile) -> set[str]:
    infos = [item for item in archive.infolist() if not item.is_dir()]
    if not infos or len(infos) > _MAX_XLSX_ARCHIVE_MEMBERS:
        raise ValueError("unsafe XLSX member count")
    total_size = 0
    names: set[str] = set()
    for info in infos:
        normalized = posixpath.normpath(info.filename.replace("\\", "/"))
        if (
            normalized.startswith("/")
            or normalized == ".."
            or normalized.startswith("../")
            or info.file_size > _MAX_UNCOMPRESSED_BYTES
        ):
            raise ValueError("unsafe XLSX member")
        total_size += info.file_size
        if total_size > _MAX_UNCOMPRESSED_BYTES or normalized in names:
            raise ValueError("unsafe XLSX archive")
        names.add(normalized)
    return names


def _xlsx_sheet_path(archive: zipfile.ZipFile, names: set[str]) -> str:
    workbook_name = "xl/workbook.xml"
    relationships_name = "xl/_rels/workbook.xml.rels"
    if workbook_name not in names or relationships_name not in names:
        raise ValueError("XLSX workbook metadata is absent")
    workbook = ElementTree.fromstring(archive.read(workbook_name))
    sheet = next(
        (
            item
            for item in workbook.findall(f".//{{{_SPREADSHEET_NS}}}sheet")
            if item.attrib.get("name") == "Transaction Data"
        ),
        None,
    )
    if sheet is None:
        raise ValueError("XLSX Transaction Data sheet is absent")
    relationship_id = sheet.attrib.get(f"{{{_OFFICE_REL_NS}}}id")
    relationships = ElementTree.fromstring(archive.read(relationships_name))
    relationship = next(
        (
            item
            for item in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
            if item.attrib.get("Id") == relationship_id
        ),
        None,
    )
    if relationship is None:
        raise ValueError("XLSX Transaction Data relationship is absent")
    target = relationship.attrib.get("Target", "")
    sheet_path = posixpath.normpath(posixpath.join("xl", target))
    if sheet_path not in names or not sheet_path.startswith("xl/worksheets/"):
        raise ValueError("unsafe XLSX Transaction Data target")
    return sheet_path


def _xlsx_shared_strings(archive: zipfile.ZipFile, names: set[str]) -> tuple[str, ...]:
    name = "xl/sharedStrings.xml"
    if name not in names:
        return ()
    strings: list[str] = []
    with archive.open(name) as stream:
        for _event, element in ElementTree.iterparse(stream, events=("end",)):
            if element.tag != f"{{{_SPREADSHEET_NS}}}si":
                continue
            strings.append(
                "".join(item.text or "" for item in element.iter(f"{{{_SPREADSHEET_NS}}}t"))
            )
            element.clear()
    return tuple(strings)


def _xlsx_column_index(reference: str) -> int:
    letters = reference.rstrip("0123456789").upper()
    if not letters or len(letters) > _MAX_XLSX_COLUMN_LETTERS:
        raise ValueError("unsafe XLSX cell reference")
    result = 0
    for letter in letters:
        if not "A" <= letter <= "Z":
            raise ValueError("unsafe XLSX cell reference")
        result = result * 26 + ord(letter) - ord("A") + 1
    return result - 1


def _xlsx_cell_value(cell: ElementTree.Element, shared: tuple[str, ...]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(item.text or "" for item in cell.iter(f"{{{_SPREADSHEET_NS}}}t"))
    value = cell.find(f"{{{_SPREADSHEET_NS}}}v")
    raw = "" if value is None or value.text is None else value.text
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (IndexError, ValueError) as error:
            raise ValueError("invalid XLSX shared-string reference") from error
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw


def _iter_xlsx_rows(path: Path) -> Iterator[tuple[str, ...]]:
    with zipfile.ZipFile(path) as archive:
        names = _validated_xlsx_members(archive)
        sheet_path = _xlsx_sheet_path(archive, names)
        shared = _xlsx_shared_strings(archive, names)
        with archive.open(sheet_path) as stream:
            for _event, element in ElementTree.iterparse(stream, events=("end",)):
                if element.tag != f"{{{_SPREADSHEET_NS}}}row":
                    continue
                values: list[str] = []
                for cell in element.findall(f"{{{_SPREADSHEET_NS}}}c"):
                    index = _xlsx_column_index(cell.attrib.get("r", ""))
                    if index >= _MAX_XLSX_COLUMNS:
                        raise ValueError("unsafe XLSX column")
                    if index >= len(values):
                        values.extend("" for _ in range(index - len(values) + 1))
                    values[index] = _xlsx_cell_value(cell, shared).strip()
                yield tuple(values)
                element.clear()


def _clean_header(value: str) -> str:
    cleaned = value.strip().lstrip("\ufeff")
    if not cleaned or len(cleaned.encode()) > _MAX_HEADER_BYTES:
        raise ValueError("unsafe CSV header")
    return cleaned


def _clean_row(row: Mapping[str | None, str | None]) -> dict[str, str]:
    if None in row:
        raise ValueError("CSV row has more values than headers")
    return {_clean_header(str(key)): (value or "").strip() for key, value in row.items()}


def _row_id(digest: str, number: int) -> str:
    return f"public-{digest[:16]}-{number}"


def _day(value: str) -> date:
    raw = _required(value, "date")
    for date_format in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            if date_format == "%Y-%m-%d":
                return date.fromisoformat(raw)
            return datetime.strptime(raw, date_format).date()  # noqa: DTZ007
        except (ValueError, AttributeError):
            continue
    raise ValueError(f"unsupported public date: {raw}")


def _xlsx_day(value: str) -> date:
    raw = _required(value, "date")
    try:
        serial = Decimal(raw)
    except InvalidOperation:
        return _day(raw)
    if serial != serial.to_integral_value() or not Decimal(1) <= serial <= Decimal(2_958_465):
        raise ValueError(f"unsupported XLSX date serial: {raw}")
    return date(1899, 12, 30) + timedelta(days=int(serial))


def _optional_day(value: str) -> date | None:
    return None if not value or value == "-" else _day(value)


def _date_from_filename(filename: str) -> date:
    match = re.search(r"(?<!\d)(\d{8})(?!\d)", filename)
    if match is None:
        raise ValueError("security snapshot filename must contain DDMMYYYY")
    raw = match.group(1)
    return date(int(raw[4:]), int(raw[2:4]), int(raw[:2]))


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(_required(value, "decimal").replace(",", ""))
    except InvalidOperation as error:
        raise ValueError("invalid decimal") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("decimal must be finite and non-negative")
    return parsed


def _optional_decimal(value: str, *, multiplier: Decimal = Decimal(1)) -> Decimal | None:
    return None if not value or value == "-" else _decimal(value) * multiplier


def _integer(value: str) -> int:
    parsed = int(Decimal(_required(value, "integer").replace(",", "")))
    if parsed < 0:
        raise ValueError("integer must be non-negative")
    return parsed


def _optional_integer(value: str) -> int | None:
    return None if not value or value == "-" else _integer(value)


def _symbol(value: str) -> str:
    symbol = _required(value, "symbol").upper()
    if not _SAFE_SYMBOL.fullmatch(symbol):
        raise ValueError(f"unsafe symbol: {symbol}")
    return symbol


def _series(value: str) -> str:
    series = _required(value, "series").upper()
    if not re.fullmatch(r"[A-Z0-9]{1,4}", series):
        raise ValueError(f"unsafe series: {series}")
    return series


def _isin(value: str) -> str | None:
    candidate = value.strip().upper()
    if not candidate or candidate == "-":
        return None
    if not _ISIN.fullmatch(candidate):
        raise ValueError(f"invalid ISIN: {candidate}")
    return candidate


def _required(value: str, label: str) -> str:
    if not value.strip() or len(value) > _MAX_TEXT_CHARS:
        raise ValueError(f"missing or unsafe {label}")
    return value.strip()


def _optional(value: str) -> str | None:
    return None if not value or value == "-" else value


def _first(row: Mapping[str, str], *names: str) -> str:
    return next((row[name] for name in names if row.get(name)), "")


def _action_type(purpose: str) -> str:
    upper = purpose.upper()
    for token, event in (
        ("DEMERGER", "DEMERGER"),
        ("MERGER", "MERGER"),
        ("AMALGAM", "MERGER"),
        ("DELIST", "DELISTING"),
        ("SYMBOL", "SYMBOL_CHANGE"),
        ("NAME CHANGE", "SYMBOL_CHANGE"),
        ("SPLIT", "SPLIT"),
        ("SUB-DIVISION", "SPLIT"),
        ("BONUS", "BONUS"),
        ("RIGHT", "RIGHTS_ISSUE"),
        ("DIVIDEND", "DIVIDEND"),
    ):
        if token in upper:
            return event
    return "OTHER"
