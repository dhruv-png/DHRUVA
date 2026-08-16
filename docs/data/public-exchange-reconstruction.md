# Public exchange historical reconstruction

Status: implemented, manual-drop-first, network-free, and diagnostic-only.
Research review date: 2026-08-16. Machine matrix revision:
`public-source-review-2026-08-16`.

This path turns owner-retained official exchange reports into the existing six
canonical CSV roles. It is useful because a frozen liquidity universe spanning
inactive names is materially less selection-biased than replaying today's
twenty-stock watchlist. It is not institutional point-in-time data, a complete
corporate-action archive, a redistribution licence, or investment evidence.

## Official evidence and access decision

The review used official sources, not third-party format blogs:

- NSE [All Reports](https://www.nseindia.com/all-reports) names the current
  `CM-UDiFF Common Bhavcopy Final`, `Full Bhavcopy and Security Deliverable
  data`, and `CM - MII - Security File`; it also says the prior CM bhavcopy and
  common bhavcopy were discontinued from 2024-07-08 in favour of UDiFF.
- NSE circular [NSE/MSD/60315](https://nsearchives.nseindia.com/content/circulars/MSD60315.pdf)
  establishes daily website dissemination of the MII security master from
  2024-02-05. The later interoperability circular
  [NSE/MSD/67344](https://nsearchives.nseindia.com/content/circulars/MSD67344.pdf)
  distinguishes the NSE-only and NSE-plus-BSE-exclusive security files.
- The official [UDiFF guidance](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/UDiFF%20guidance%20document_Ver1.0.pdf)
  establishes the standardized naming/format family. DHRUVA still detects the
  concrete cash-bhavcopy schema by exact headers and rejects unknown revisions.
- NSE [daily/monthly archives](https://www.nseindia.com/resources/historical-reports-capital-market-daily-monthly-archives)
  expose separate historical index and Total Returns Index values. NSE's
  [index FAQ](https://www.nseindia.com/static/products-services/indices-faqs)
  states that NIFTY 50 price and TRI series exist from July 1990; the
  [TRI description](https://www.nseindia.com/static/products-services/indices-total-returns-index)
  explains that TRI includes price movement and dividend receipts.
- NSE's [corporate-action page](https://www.nseindia.com/companies-listing/corporate-filings-actions)
  exposes symbol, company, series, purpose, face value, ex-date and record date
  with CSV download. Historical completeness and revision history remain
  unknown.
- SEBI's [research/analysis data-sharing policy](https://www.sebi.gov.in/legal/circulars/dec-2024/policy-for-sharing-data-for-the-purpose-of-research-analysis_90088.html)
  requires MIIs to publish their own policies. SEBI's
  [equity cash-market curation](https://www.sebi.gov.in/curation/equity_cash_market.html)
  points users to official exchange sources. Public availability is not treated
  as redistribution permission.
- BSE's official [information-products tariff](https://www.bseindia.com/downloads1/Information_Products_Pricing_Sheet.pdf)
  separately prices historical market/corporate data. BSE pages show public
  security/history/notices, but this review did not establish a stable free
  bulk equity schema and retention terms. DHRUVA therefore implements no BSE
  mapper and records `TERMS_REVIEW_REQUIRED`/`UNKNOWN` rather than guessing.

NSE report pages are designed for public/manual download, but automated archive
retrieval terms, endpoint stability, full depth, throttling, and revision
behaviour were not sufficiently established. DHRUVA has no downloader. The
owner uses the normal official interface, respects access controls, retains
original bytes locally, and does not redistribute them.

## Machine capability matrix

`dhruva-public sources --json` emits
`dhruva.public-source-capability-matrix.v1`. Every field is one of
`SUPPORTED`, `PARTIAL`, `NOT_SUPPORTED`, `UNKNOWN`, `MANUAL_ONLY`,
`AUTOMATION_UNCLEAR`, or `TERMS_REVIEW_REQUIRED`. Absence of contrary evidence
never becomes `SUPPORTED`.

Supported NSE offline layouts are:

| Mapper revision | Recognition |
|---|---|
| `NSE_CM_UDIFF_BHAVCOPY_V1` | ISO-tag headers including `TradDt`, `FinInstrmId`, `ISIN`, `TckrSymb`, `SctySrs`, OHLC and `TtlTradgVol` |
| `NSE_CM_LEGACY_BHAVCOPY_V1` | legacy `SYMBOL`, `SERIES`, OHLC, `TOTTRDQTY`, `TIMESTAMP`, `ISIN` |
| `NSE_FULL_BHAVCOPY_DELIVERABLE_V1` | `DATE1`, `*_PRICE`, `TTL_TRD_QNTY`, `DELIV_QTY`; needs separate unambiguous identity evidence |
| `NSE_CM_MII_SECURITY_V1` | `FinInstrmId`, `TckrSymb`, `SctySrs`, `ISIN` plus a documented security-name field |
| `NSE_CORPORATE_ACTIONS_V1` | official CSV table fields including purpose/ex/record date |
| `NSE_NIFTY_PRICE_INDEX_V1` | date/OHLC/shares-traded history |
| `NSE_NIFTY_TRI_V1` | date and `Total Returns Index` |

ZIP must contain exactly one CSV; gzip and plain CSV are accepted. Files are
hashed before parsing. Symlinks, path escapes, oversized ZIP members, malformed
headers, invalid identifiers/dates/numbers, unknown schemas, duplicate bars,
and conflicting identities fail closed. Original bytes are never rewritten.

## Evidence, time, and return semantics

The explicit origin classes are `LICENSED_VENDOR`,
`PUBLIC_EXCHANGE_ARCHIVE`, `PUBLIC_RECONSTRUCTED`, `OWNER_PROVIDED`, and
`SYNTHETIC_TEST`. This pipeline emits `PUBLIC_RECONSTRUCTED` and persists that
class with the accepted dataset revision.

Market/action dates support event-time reconstruction. A file downloaded in
2026 for a 2018 session carries `RETRIEVED_LATER`; DHRUVA does not claim it was
prospectively archived in 2018. Other defined knowledge states are
`PUBLICATION_DATE_KNOWN`, `FILE_DATE_ONLY`, `REVISION_HISTORY_UNKNOWN`, and
`PROSPECTIVE_ARCHIVE`. Current public reconstruction cannot satisfy strict PIT.

Bhavcopy bars are `RAW_PRICE`/`RAW`. They are immutable and never silently
back-adjusted. An explicit split/bonus ratio may be retained as
`SOURCE_REPORTED`; price movement cannot create a ratio. Purpose text and every
unresolved action remain in `public-action-evidence.jsonl` even when the stricter
canonical action contract refuses to materialize an exact transformation.
Dividends do not turn raw bars into total return. Price-index and TRI histories
use different benchmark ids and one selected basis per canonical dataset.

## Identity and lifecycle reconstruction

ISIN is the preferred continuity key, followed by an exchange security id.
Symbol/series-only bars join only when exactly one retained security identity
matches; zero or multiple matches fail closed. A ticker rename under one ISIN
creates an effective identity revision, not a new economic history.

Per date, reconstruction distinguishes `OBSERVED_TRADED`, `OBSERVED_LISTED`,
`INFERRED_LISTED`, `NOT_OBSERVED`, `DELISTED`, `SUSPENDED_IF_KNOWN`, and
`UNKNOWN`. No trade is not delisting. A disappeared name becomes
`NO_LONGER_OBSERVED`, never `CONFIRMED_DELISTED` without source evidence. Other
terminal diagnostics are `MAPPING_UNRESOLVED` and `SUSPENSION_UNKNOWN`.
These instrument/date facts are retained in `security-observations.csv`; they
are diagnostic reconstruction evidence alongside, not a substitute for, the
six canonical import roles.

## Frozen liquidity universe

`PUBLIC_LIQUID_NSE_UNIVERSE_V0` (`public-liquid-nse-v0`) is frozen as:

- series `EQ` or `BE`;
- 60-session lookback and 60 observed-session warm-up;
- at least 48 traded sessions in that window;
- trailing median rupee turnover at least INR 10,000,000;
- latest observed close at least INR 10;
- decision after cutoff T's completed session, using rows no later than T.

Turnover uses the official field when present and otherwise raw close × volume.
Missing sessions contribute non-participation. Future volume cannot revise a
past decision. Eligible intervals close when a rule stops passing; old bars,
identities, and membership intervals remain. The rule is not tuned from model
outcomes.

## Preflight, manifests, and readiness v3

The source reconstruction manifest is
`dhruva.public-reconstruction-manifest.v1`; it records original filename/hash,
manual acquisition, retrieval time, source URL identifier/schema, mapper,
identity/universe reconstruction revisions, time/return/benchmark semantics,
frozen rule and limitations. The generated canonical manifest remains
`dhruva.historical-dataset-manifest.v1` and uses append-only import/provenance.

Public preflight reports unknown schema, same-hash duplicates, conflicting
corrections, weekday archive gaps, report-family coverage and the exact
`RETRIEVED_LATER` limitation. A weekday gap is not called a holiday without a
reviewed calendar. Scientific PIT/action/delisting/revision blockers remain in
canonical preflight but do not prohibit an explicitly acknowledged local
import when hashes, schemas, identities, source-selection and local research
rights pass.

Readiness schema `dhruva.evaluation-data-readiness.v3` adds evidence class,
known-at semantics, inactive retention, archive-gap status, reconstruction
revision and terms status. Public evaluation is prominently
`PUBLIC_RECONSTRUCTED` (or at best `PARTIAL_PIT` with stronger evidence), never
silently `EVALUATION_READY`. The bias report separately rates survivorship,
selection, delisting, look-ahead, actions, identity, missing data, benchmark
basis, archive reconstruction and revision history as LOW/MEDIUM/HIGH/UNKNOWN;
it has no invented confidence percentage.
