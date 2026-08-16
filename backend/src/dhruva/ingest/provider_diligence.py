"""Current public-source provider diligence without approval or purchase semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final

from dhruva.ingest.historical_dataset import canonical_json_bytes

__all__ = [
    "DILIGENCE_SCHEMA",
    "OWNER_DILIGENCE_SHORTLIST",
    "PROVIDERS",
    "QUESTIONNAIRE",
    "ClaimStatus",
    "ProviderAssessment",
    "ProviderDisposition",
    "diligence_export_bytes",
]

DILIGENCE_SCHEMA: Final = "dhruva.provider-diligence.v1"
RESEARCHED_ON: Final = "2026-08-16"
OWNER_DILIGENCE_SHORTLIST: Final = (
    "nse_data",
    "factset",
    "lseg",
    "globaldatafeeds",
)


class ClaimStatus(StrEnum):
    """Evidence vocabulary; an absent public statement always remains unknown."""

    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"
    DOCUMENTATION_UNCLEAR = "DOCUMENTATION_UNCLEAR"
    OWNER_CONFIRMATION_REQUIRED = "OWNER_CONFIRMATION_REQUIRED"


class ProviderDisposition(StrEnum):
    """Research priority, never procurement approval."""

    REJECT = "REJECT"
    LOW_PRIORITY = "LOW_PRIORITY"
    INVESTIGATE = "INVESTIGATE"
    SHORTLIST = "SHORTLIST"
    OWNER_DILIGENCE_REQUIRED = "OWNER_DILIGENCE_REQUIRED"


@dataclass(frozen=True, slots=True)
class ProviderAssessment:
    """Public evidence and separately visible five-point factor scores."""

    provider_id: str
    provider_name: str
    product: str
    india_nse_bse: ClaimStatus
    inactive_securities: ClaimStatus
    historical_constituents: ClaimStatus
    corporate_actions: ClaimStatus
    pit_publication_time: ClaimStatus
    revisions: ClaimStatus
    local_retention: ClaimStatus
    delivery: str
    history_depth: str
    technical_fit: int
    research_integrity_fit: int
    licensing_clarity: int
    implementation_ease: int
    cost_transparency: int
    disposition: ProviderDisposition
    blocking_unknowns: tuple[str, ...]
    sources: tuple[str, ...]

    @property
    def weighted_score(self) -> int:
        """Return a documented 0-100 priority score, without overriding blockers."""
        return round(
            20
            * (
                self.technical_fit * 0.25
                + self.research_integrity_fit * 0.35
                + self.licensing_clarity * 0.20
                + self.implementation_ease * 0.10
                + self.cost_transparency * 0.10
            )
        )


def _assessment(  # noqa: PLR0913 - matrix rows remain explicit and reviewable
    provider_id: str,
    provider_name: str,
    product: str,
    *,
    india: ClaimStatus,
    inactive: ClaimStatus,
    constituents: ClaimStatus,
    actions: ClaimStatus,
    pit: ClaimStatus,
    revisions: ClaimStatus,
    retention: ClaimStatus = ClaimStatus.OWNER_CONFIRMATION_REQUIRED,
    delivery: str,
    depth: str,
    scores: tuple[int, int, int, int, int],
    disposition: ProviderDisposition,
    unknowns: tuple[str, ...],
    sources: tuple[str, ...],
) -> ProviderAssessment:
    return ProviderAssessment(
        provider_id,
        provider_name,
        product,
        india,
        inactive,
        constituents,
        actions,
        pit,
        revisions,
        retention,
        delivery,
        depth,
        *scores,
        disposition,
        unknowns,
        sources,
    )


PROVIDERS: Final = (
    _assessment(
        "nse_data",
        "NSE Data & Analytics",
        "EOD/Historical + Corporate + Indices subscriptions",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.DOCUMENTATION_UNCLEAR,
        constituents=ClaimStatus.SUPPORTED,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.DOCUMENTATION_UNCLEAR,
        revisions=ClaimStatus.DOCUMENTATION_UNCLEAR,
        delivery="SFTP/files; product-specific subscriptions",
        depth="Historical products advertised; exact combined depth requires quotation",
        scores=(4, 4, 3, 3, 2),
        disposition=ProviderDisposition.OWNER_DILIGENCE_REQUIRED,
        unknowns=(
            "combined product completeness and cross-product identifiers",
            "publication/knowledge timestamps and revision retention",
            "personal automated-analysis and indefinite local-retention rights",
            "total price including indices, corporate actions and taxes",
        ),
        sources=(
            "https://www.nseindia.com/static/market-data/eod-historical-data-subscription",
            "https://www.nseindia.com/static/market-data/corporate-data-subscription",
            "https://www.nseindia.com/static/nse-indices/index-data-subscription",
            "https://www.nseindia.com/static/market-data/nse-data-policy",
        ),
    ),
    _assessment(
        "factset",
        "FactSet",
        "Global Prices + Symbology + Fundamentals PIT",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.SUPPORTED,
        constituents=ClaimStatus.DOCUMENTATION_UNCLEAR,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.PARTIALLY_SUPPORTED,
        revisions=ClaimStatus.PARTIALLY_SUPPORTED,
        delivery="REST APIs and enterprise feeds",
        depth="Prices/actions from 2006; Fundamentals PIT from 1999",
        scores=(5, 4, 2, 4, 1),
        disposition=ProviderDisposition.OWNER_DILIGENCE_REQUIRED,
        unknowns=(
            "historical Indian index membership with known-at timestamps",
            "household/personal licensing and indefinite local retention",
            "exact bundle price and minimum contract",
        ),
        sources=(
            "https://developer.factset.com/api-catalog/factset-global-prices-api",
            "https://developer.factset.com/api-catalog/symbology-api",
            "https://www.factset.com/marketplace/catalog/product/factset-fundamentals-point-in-time",
            "https://www.factset.com/solutions/data/real-time-market-data/real-time-exchange-coverage",
        ),
    ),
    _assessment(
        "lseg",
        "LSEG",
        "Data Catalogue / Indices / Workspace corporate actions",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.PARTIALLY_SUPPORTED,
        constituents=ClaimStatus.SUPPORTED,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.DOCUMENTATION_UNCLEAR,
        revisions=ClaimStatus.PARTIALLY_SUPPORTED,
        delivery="APIs, files and enterprise delivery",
        depth="Broad multi-decade catalogue; exact India product depth unquoted",
        scores=(5, 4, 2, 3, 1),
        disposition=ProviderDisposition.OWNER_DILIGENCE_REQUIRED,
        unknowns=(
            "NSE/BSE inactive-security completeness",
            "knowledge-time fields for membership/action revisions",
            "personal licence, retention rights and price",
        ),
        sources=(
            "https://www.lseg.com/en/data-catalogue",
            "https://www.lseg.com/en/data-catalogue/indices-benchmarks",
            "https://developers.lseg.com/en/article-catalog/article/building-historical-index-constituents",
            "https://developers.lseg.com/en/article-catalog/article/workspace-corporate-actions-content-set-guide",
        ),
    ),
    _assessment(
        "globaldatafeeds",
        "Global Datafeeds",
        "Historical EOD + corporate actions + index constituents",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.UNKNOWN,
        constituents=ClaimStatus.PARTIALLY_SUPPORTED,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="APIs plus emailed monthly constituent CSV",
        depth="EOD history advertised; constituent archive depth not public",
        scores=(4, 3, 3, 4, 3),
        disposition=ProviderDisposition.OWNER_DILIGENCE_REQUIRED,
        unknowns=(
            "historical constituent archive and removal/delisting completeness",
            "publication timestamps, corrections and immutable revisions",
            "local retention and automated research rights",
        ),
        sources=(
            "https://globaldatafeeds.in/fundamental-data-apis/",
            "https://docs.globaldatafeeds.in/index-constituents-933202m0",
            "https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/introduction/type-of-data-available/",
            "https://globaldatafeeds.in/authorised-data-vendors/",
        ),
    ),
    _assessment(
        "cmie_prowess",
        "CMIE Prowess",
        "ProwessIQ",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.PARTIALLY_SUPPORTED,
        constituents=ClaimStatus.SUPPORTED,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="Desktop/web interface; export/API terms unclear publicly",
        depth="Company time series from 1989",
        scores=(3, 4, 2, 2, 1),
        disposition=ProviderDisposition.INVESTIGATE,
        unknowns=(
            "bulk/API delivery and machine-readable identifiers",
            "known-at/revision semantics",
            "retention/export rights and price",
        ),
        sources=(
            "https://prowess.cmie.com/",
            "https://www.cmie.com/kommon/bin/sr.php?kall=wproducts&portal_code=030010010100000000000000000000000000000000000&prd=prowessiq&tabno=7010",
        ),
    ),
    _assessment(
        "capitaline",
        "Capitaline",
        "Capitaline database",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.PARTIALLY_SUPPORTED,
        constituents=ClaimStatus.DOCUMENTATION_UNCLEAR,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="Web database; bulk/API terms unclear publicly",
        depth="Up to ten years of share prices advertised",
        scores=(3, 3, 2, 2, 1),
        disposition=ProviderDisposition.INVESTIGATE,
        unknowns=("historical membership", "PIT/revisions", "bulk rights and price"),
        sources=(
            "https://capitaline.com/Index.aspx",
            "https://www.capitaline.com/Demo/Help/index.htm",
        ),
    ),
    _assessment(
        "accord_ace",
        "Accord Fintech",
        "ACE datafeed",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.UNKNOWN,
        constituents=ClaimStatus.UNKNOWN,
        actions=ClaimStatus.PARTIALLY_SUPPORTED,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="API/FTP datafeed",
        depth="Historical/EOD advertised; exact depth not public",
        scores=(4, 2, 2, 4, 1),
        disposition=ProviderDisposition.INVESTIGATE,
        unknowns=("constituent history", "inactive coverage", "PIT/revisions/licence/price"),
        sources=("https://www.accordfintech.com/corporate",),
    ),
    _assessment(
        "bloomberg",
        "Bloomberg",
        "Data License / Enterprise reference data",
        india=ClaimStatus.PARTIALLY_SUPPORTED,
        inactive=ClaimStatus.SUPPORTED,
        constituents=ClaimStatus.DOCUMENTATION_UNCLEAR,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.SUPPORTED,
        revisions=ClaimStatus.PARTIALLY_SUPPORTED,
        delivery="REST, SFTP and cloud enterprise delivery",
        depth="20+ years advertised for Data License",
        scores=(5, 4, 1, 3, 1),
        disposition=ProviderDisposition.LOW_PRIORITY,
        unknowns=("India-specific membership scope", "personal licence/retention", "price"),
        sources=(
            "https://professional.bloomberg.com/products/data/enterprise-catalog/reference/",
            "https://professional.bloomberg.com/products/data/enterprise-catalog/investment-research-data/",
            "https://professional.bloomberg.com/products/data/data-management/data-license/",
        ),
    ),
    _assessment(
        "truedata",
        "TrueData",
        "Market Data APIs",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.UNKNOWN,
        constituents=ClaimStatus.UNKNOWN,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="APIs",
        depth="Not established publicly for this milestone",
        scores=(3, 2, 2, 4, 3),
        disposition=ProviderDisposition.LOW_PRIORITY,
        unknowns=("PIT universe", "inactive/lifecycle history", "revision retention"),
        sources=("https://www.truedata.in/market-data-apis",),
    ),
    _assessment(
        "accelpix",
        "Accelpix",
        "PIX APIs",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.UNKNOWN,
        constituents=ClaimStatus.UNKNOWN,
        actions=ClaimStatus.UNKNOWN,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="Python/REST APIs",
        depth="EOD/intraday history advertised; depth unclear",
        scores=(3, 1, 2, 4, 3),
        disposition=ProviderDisposition.LOW_PRIORITY,
        unknowns=("membership", "actions", "inactive coverage", "PIT/revisions/retention"),
        sources=(
            "https://support.accelpix.com/portal/en/kb/articles/pix-apis-realtime-and-historical-data-in-python",
            "https://accelpix.com/",
        ),
    ),
    _assessment(
        "twelve_data",
        "Twelve Data",
        "Historical market data API",
        india=ClaimStatus.SUPPORTED,
        inactive=ClaimStatus.UNKNOWN,
        constituents=ClaimStatus.NOT_SUPPORTED,
        actions=ClaimStatus.PARTIALLY_SUPPORTED,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="REST API",
        depth="Symbol-dependent historical time series",
        scores=(3, 1, 3, 5, 4),
        disposition=ProviderDisposition.REJECT,
        unknowns=("inactive universe", "PIT/revision and retention semantics"),
        sources=(
            "https://twelvedata.com/docs",
            "https://twelvedata.com/markets/531286/stock/nse/infy/historical-data/dividends",
        ),
    ),
    _assessment(
        "eodhd",
        "EODHD",
        "Historical Data API",
        india=ClaimStatus.DOCUMENTATION_UNCLEAR,
        inactive=ClaimStatus.PARTIALLY_SUPPORTED,
        constituents=ClaimStatus.NOT_SUPPORTED,
        actions=ClaimStatus.PARTIALLY_SUPPORTED,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="REST API",
        depth="15-20 years advertised for Asian exchanges",
        scores=(2, 1, 3, 5, 5),
        disposition=ProviderDisposition.REJECT,
        unknowns=("current NSE/BSE coverage", "historical membership", "PIT/revisions"),
        sources=(
            "https://eodhd.com/list-of-stock-markets",
            "https://eodhd.com/financial-apis/api-for-historical-data-and-volumes",
            "https://eodhd.com/financial-academy/financial-faq/survivorship-bias-free-financial-analysis",
        ),
    ),
    _assessment(
        "bse_data",
        "BSE",
        "Information Products",
        india=ClaimStatus.PARTIALLY_SUPPORTED,
        inactive=ClaimStatus.DOCUMENTATION_UNCLEAR,
        constituents=ClaimStatus.PARTIALLY_SUPPORTED,
        actions=ClaimStatus.SUPPORTED,
        pit=ClaimStatus.UNKNOWN,
        revisions=ClaimStatus.UNKNOWN,
        delivery="Exchange files/subscriptions",
        depth="Historical products advertised",
        scores=(2, 2, 3, 3, 3),
        disposition=ProviderDisposition.LOW_PRIORITY,
        unknowns=("NSE coverage absent", "PIT/revisions", "retention rights"),
        sources=("https://www.bseindia.com/downloads1/Information_Products_Pricing_Sheet.pdf",),
    ),
)


QUESTIONNAIRE: Final = (
    "List exact NSE and BSE equity coverage, including inactive, suspended, "
    "merged and delisted securities.",
    "Provide historical constituent membership with effective-from, effective-to "
    "and publication timestamps.",
    "State price/corporate-action history depth and whether raw, price-adjusted "
    "and total-return series are distinct.",
    "List every corporate-action type and the source, ex, record, effective, "
    "announcement and revision timestamps.",
    "Explain identifier continuity across ISIN, symbol, exchange, merger, "
    "demerger and relisting changes.",
    "Describe corrections: immutable revision IDs, retrieval of superseded "
    "values and correction publication time.",
    "Confirm in writing personal/household use, automated local analysis and "
    "indefinite local retention rights.",
    "Confirm whether derived research artifacts may be retained after subscription termination.",
    "Provide delivery schemas, sample files, manifests, checksums, row limits "
    "and API/file rate limits.",
    "Quote the complete recurring and one-time price, taxes, minimum term, "
    "cancellation and data-deletion duties.",
    "Identify redistribution, display, model-training, benchmarking and "
    "derived-data restrictions separately.",
    "Provide service-level, backfill, incident-notification and "
    "historical-restatement commitments.",
)


def diligence_export_bytes() -> bytes:
    """Export deterministic research evidence; no provider is marked approved."""
    return (
        canonical_json_bytes(
            {
                "schema": DILIGENCE_SCHEMA,
                "researched_on": RESEARCHED_ON,
                "score_scale": "0-5 per factor; weighted 0-100",
                "weights": {
                    "technical_fit": 0.25,
                    "research_integrity_fit": 0.35,
                    "licensing_clarity": 0.20,
                    "implementation_ease": 0.10,
                    "cost_transparency": 0.10,
                },
                "approval_status": "NO_PROVIDER_APPROVED",
                "owner_diligence_shortlist": OWNER_DILIGENCE_SHORTLIST,
                "providers": [
                    {**asdict(item), "weighted_score": item.weighted_score} for item in PROVIDERS
                ],
                "questionnaire": QUESTIONNAIRE,
            }
        )
        + b"\n"
    )
