"""Reviewed public-exchange capability claims with fail-closed statuses.

The matrix is deliberately static and versioned.  It records what the cited
official pages establish; it is not a live availability probe and it never
contacts an exchange.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "PUBLIC_CAPABILITY_MATRIX_REVISION",
    "PUBLIC_CAPABILITY_MATRIX_SCHEMA",
    "PUBLIC_SOURCE_CAPABILITIES",
    "CapabilityStatus",
    "PublicSourceCapability",
    "public_capability_export_bytes",
]

PUBLIC_CAPABILITY_MATRIX_SCHEMA: Final = "dhruva.public-source-capability-matrix.v1"
PUBLIC_CAPABILITY_MATRIX_REVISION: Final = "public-source-review-2026-08-16"


class CapabilityStatus(StrEnum):
    """Strict source-research conclusion; uncertainty is never support."""

    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"
    MANUAL_ONLY = "MANUAL_ONLY"
    AUTOMATION_UNCLEAR = "AUTOMATION_UNCLEAR"
    TERMS_REVIEW_REQUIRED = "TERMS_REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class PublicSourceCapability:
    """One official report family and its reviewed research capabilities."""

    source_id: str
    source_name: str
    official_domain: str
    dataset_report_name: str
    official_url: str
    daily_ohlcv: CapabilityStatus
    delivery_quantity: CapabilityStatus
    trade_count: CapabilityStatus
    series: CapabilityStatus
    isin: CapabilityStatus
    exchange_security_code: CapabilityStatus
    symbol: CapabilityStatus
    company_name: CapabilityStatus
    listed_status: CapabilityStatus
    listing_date: CapabilityStatus
    delisting_date: CapabilityStatus
    symbol_history: CapabilityStatus
    historical_security_snapshots: CapabilityStatus
    corporate_actions: CapabilityStatus
    split: CapabilityStatus
    bonus: CapabilityStatus
    dividend: CapabilityStatus
    rights: CapabilityStatus
    merger_demerger: CapabilityStatus
    index_membership: CapabilityStatus
    historical_index_constituents: CapabilityStatus
    benchmark_price_index: CapabilityStatus
    benchmark_tri: CapabilityStatus
    known_at_semantics: CapabilityStatus
    publication_datetime: CapabilityStatus
    revisions: CapabilityStatus
    historical_depth: str
    download_mechanism: str
    automation_status: CapabilityStatus
    manual_download_support: CapabilityStatus
    retention_licensing_clarity: CapabilityStatus
    research_suitability: CapabilityStatus
    pit_suitability: CapabilityStatus
    survivorship_suitability: CapabilityStatus
    major_blockers: tuple[str, ...]


U = CapabilityStatus.UNKNOWN
N = CapabilityStatus.NOT_SUPPORTED
S = CapabilityStatus.SUPPORTED
P = CapabilityStatus.PARTIAL
M = CapabilityStatus.MANUAL_ONLY
A = CapabilityStatus.AUTOMATION_UNCLEAR
T = CapabilityStatus.TERMS_REVIEW_REQUIRED


PUBLIC_SOURCE_CAPABILITIES: Final = (
    PublicSourceCapability(
        source_id="nse_cm_udiff_bhavcopy",
        source_name="National Stock Exchange of India",
        official_domain="nseindia.com",
        dataset_report_name="CM-UDiFF Common Bhavcopy Final",
        official_url="https://www.nseindia.com/all-reports",
        daily_ohlcv=S,
        delivery_quantity=N,
        trade_count=S,
        series=S,
        isin=S,
        exchange_security_code=S,
        symbol=S,
        company_name=P,
        listed_status=N,
        listing_date=N,
        delisting_date=N,
        symbol_history=N,
        historical_security_snapshots=N,
        corporate_actions=N,
        split=N,
        bonus=N,
        dividend=N,
        rights=N,
        merger_demerger=N,
        index_membership=N,
        historical_index_constituents=N,
        benchmark_price_index=N,
        benchmark_tri=N,
        known_at_semantics=P,
        publication_datetime=U,
        revisions=U,
        historical_depth=(
            "Current official format from 2024 transition; archive depth not proven here"
        ),
        download_mechanism="NSE daily/monthly reports interface",
        automation_status=A,
        manual_download_support=S,
        retention_licensing_clarity=P,
        research_suitability=S,
        pit_suitability=P,
        survivorship_suitability=N,
        major_blockers=(
            "prospective retrieval time is absent from files downloaded later",
            "revision history and complete public archive depth are unproven",
        ),
    ),
    PublicSourceCapability(
        source_id="nse_full_bhavcopy_deliverable",
        source_name="National Stock Exchange of India",
        official_domain="nseindia.com",
        dataset_report_name="Full Bhavcopy and Security Deliverable data",
        official_url="https://www.nseindia.com/all-reports",
        daily_ohlcv=S,
        delivery_quantity=S,
        trade_count=S,
        series=S,
        isin=N,
        exchange_security_code=N,
        symbol=S,
        company_name=N,
        listed_status=N,
        listing_date=N,
        delisting_date=N,
        symbol_history=N,
        historical_security_snapshots=N,
        corporate_actions=N,
        split=N,
        bonus=N,
        dividend=N,
        rights=N,
        merger_demerger=N,
        index_membership=N,
        historical_index_constituents=N,
        benchmark_price_index=N,
        benchmark_tri=N,
        known_at_semantics=P,
        publication_datetime=U,
        revisions=U,
        historical_depth="Public daily/archive coverage varies; completeness not established",
        download_mechanism="NSE daily/monthly reports interface",
        automation_status=A,
        manual_download_support=S,
        retention_licensing_clarity=P,
        research_suitability=S,
        pit_suitability=P,
        survivorship_suitability=N,
        major_blockers=("requires separate identity evidence", "archive completeness unproven"),
    ),
    PublicSourceCapability(
        source_id="nse_cm_mii_security",
        source_name="National Stock Exchange of India",
        official_domain="nseindia.com",
        dataset_report_name="CM - MII - Security File (NSE Listed securities)",
        official_url="https://www.nseindia.com/all-reports",
        daily_ohlcv=N,
        delivery_quantity=N,
        trade_count=N,
        series=S,
        isin=S,
        exchange_security_code=S,
        symbol=S,
        company_name=S,
        listed_status=S,
        listing_date=P,
        delisting_date=U,
        symbol_history=P,
        historical_security_snapshots=P,
        corporate_actions=N,
        split=N,
        bonus=N,
        dividend=N,
        rights=N,
        merger_demerger=N,
        index_membership=N,
        historical_index_constituents=N,
        benchmark_price_index=N,
        benchmark_tri=N,
        known_at_semantics=P,
        publication_datetime=U,
        revisions=U,
        historical_depth=(
            "Daily website dissemination effective 2024-02-05; older public depth unknown"
        ),
        download_mechanism="NSE daily reports .csv.gz",
        automation_status=A,
        manual_download_support=S,
        retention_licensing_clarity=P,
        research_suitability=S,
        pit_suitability=P,
        survivorship_suitability=P,
        major_blockers=("historical snapshot completeness predates public dissemination",),
    ),
    PublicSourceCapability(
        source_id="nse_corporate_actions",
        source_name="National Stock Exchange of India",
        official_domain="nseindia.com",
        dataset_report_name="Corporate Filings - Corporate Actions",
        official_url="https://www.nseindia.com/companies-listing/corporate-filings-actions",
        daily_ohlcv=N,
        delivery_quantity=N,
        trade_count=N,
        series=S,
        isin=N,
        exchange_security_code=N,
        symbol=S,
        company_name=S,
        listed_status=N,
        listing_date=N,
        delisting_date=P,
        symbol_history=P,
        historical_security_snapshots=N,
        corporate_actions=S,
        split=S,
        bonus=S,
        dividend=S,
        rights=S,
        merger_demerger=P,
        index_membership=N,
        historical_index_constituents=N,
        benchmark_price_index=N,
        benchmark_tri=N,
        known_at_semantics=P,
        publication_datetime=U,
        revisions=U,
        historical_depth="Query-dependent; full archive completeness is not established",
        download_mechanism="Official table CSV download",
        automation_status=A,
        manual_download_support=S,
        retention_licensing_clarity=P,
        research_suitability=S,
        pit_suitability=P,
        survivorship_suitability=P,
        major_blockers=(
            "purpose text may not encode a safe numeric ratio",
            "revision history unknown",
        ),
    ),
    PublicSourceCapability(
        source_id="nse_nifty_history",
        source_name="National Stock Exchange of India",
        official_domain="nseindia.com",
        dataset_report_name="Historical Index Data and Total Returns Index Values",
        official_url="https://www.nseindia.com/resources/historical-reports-capital-market-daily-monthly-archives",
        daily_ohlcv=S,
        delivery_quantity=N,
        trade_count=N,
        series=N,
        isin=N,
        exchange_security_code=N,
        symbol=S,
        company_name=N,
        listed_status=N,
        listing_date=N,
        delisting_date=N,
        symbol_history=N,
        historical_security_snapshots=N,
        corporate_actions=N,
        split=N,
        bonus=N,
        dividend=N,
        rights=N,
        merger_demerger=N,
        index_membership=N,
        historical_index_constituents=U,
        benchmark_price_index=S,
        benchmark_tri=S,
        known_at_semantics=P,
        publication_datetime=U,
        revisions=U,
        historical_depth="NSE states NIFTY 50 price and TRI series are observed from July 1990",
        download_mechanism="Official historical report CSV download",
        automation_status=A,
        manual_download_support=S,
        retention_licensing_clarity=P,
        research_suitability=S,
        pit_suitability=P,
        survivorship_suitability=N,
        major_blockers=("historical constituent PIT series is not established by this report",),
    ),
    PublicSourceCapability(
        source_id="bse_historical_equity",
        source_name="BSE Limited",
        official_domain="bseindia.com",
        dataset_report_name="Equity bhavcopy/history, reference and corporate data",
        official_url="https://www.bseindia.com/markets/equity/EQReports/StockPrcHistori.aspx",
        daily_ohlcv=P,
        delivery_quantity=U,
        trade_count=U,
        series=U,
        isin=U,
        exchange_security_code=S,
        symbol=P,
        company_name=P,
        listed_status=P,
        listing_date=U,
        delisting_date=P,
        symbol_history=P,
        historical_security_snapshots=U,
        corporate_actions=P,
        split=P,
        bonus=P,
        dividend=P,
        rights=P,
        merger_demerger=P,
        index_membership=U,
        historical_index_constituents=U,
        benchmark_price_index=P,
        benchmark_tri=U,
        known_at_semantics=U,
        publication_datetime=U,
        revisions=U,
        historical_depth=(
            "Public page and paid historical products coexist; free bulk depth unknown"
        ),
        download_mechanism="Official pages; no supported DHRUVA bulk schema",
        automation_status=T,
        manual_download_support=P,
        retention_licensing_clarity=T,
        research_suitability=P,
        pit_suitability=U,
        survivorship_suitability=U,
        major_blockers=(
            "official free bulk schema was not established",
            "BSE separately prices historical market and corporate data",
        ),
    ),
)


def public_capability_export_bytes() -> bytes:
    """Return canonical machine-readable capability evidence."""
    body = {
        "schema": PUBLIC_CAPABILITY_MATRIX_SCHEMA,
        "revision": PUBLIC_CAPABILITY_MATRIX_REVISION,
        "sources": [asdict(item) for item in PUBLIC_SOURCE_CAPABILITIES],
    }
    return (
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
