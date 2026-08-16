# Historical evaluation data source diligence

Status: architecture and public-documentation review only, 2026-08-16. No
subscription was purchased, no terms were accepted, and no authenticated source
was called. A public product page is evidence of a capability claim, not proof
of contract rights, completeness, PIT publication timestamps, or suitability.

## Approval gate

A source cannot move to `TECHNICALLY_SUITABLE` and then owner approval until a
retained proposal/sample/contract answers all of these:

- at least 8–10 years and approximately 2,000 NSE sessions;
- historical constituents or market-universe membership, including effective
  additions/removals and re-entry;
- delisted and suspended security coverage plus immutable security identifiers;
- symbol, ISIN, and provider mapping history with effective and publication time;
- split, bonus, dividend, rights, merger, demerger, symbol-change, and delisting
  events, including revisions and publication timestamps;
- exact raw/price-adjusted/total-return methodology and reproducible factors;
- index price and TRI coverage with explicit basis;
- correction/restatement delivery and point-in-time `known_at` semantics;
- bulk/API delivery, deterministic replay, and an auditable dataset manifest;
- explicit personal-household automated download, local storage, retention,
  derived-research, backup, and non-redistribution rights;
- price, stability/support, Indian-market depth, rate limits, and termination
  consequences.

Any unanswered critical item remains a blocker. Marketing text never grants a
right that the contract does not.

## Public-documentation matrix

| Candidate | Publicly supported capability | Critical unknowns | Status |
|---|---|---|---|
| NSE Indices subscription | Official page describes ongoing and historical index data and constituent fields such as identifiers, weights, prices, and market capitalization. | Historical change-file depth, source publication timestamps, revision delivery, household automation/retention, redistribution, price, and TRI contract scope require a written proposal. | `OWNER_APPROVAL_REQUIRED`, `PIT_UNPROVEN`, `LICENSING_UNCLEAR` |
| NSE Data & Analytics / Infofeed | Official material lists EOD, historical, corporate, and master-data products, with file/SFTP delivery. | A single product bundle covering delisted NSE cash equities, universe membership, known-at revisions, corporate actions, and personal retention is not established publicly. | `DOCUMENTATION_REVIEWED`, `PIT_UNPROVEN`, `LICENSING_UNCLEAR` |
| BSE information products | Official tariff describes EOD/historical trade and corporate-action data for research/backtesting. | DHRUVA's primary evaluation venue is NSE; cross-exchange identity, NSE membership, delistings, PIT timestamps, and personal rights remain unresolved. | `DOCUMENTATION_REVIEWED`, not sufficient alone |
| Global Datafeeds | Public API docs expose EOD history, identifiers, index constituents, and corporate-action endpoints. | Current capability table states only 30 days of corporate-action history and no index-constituent history; adjusted-price methodology, delisted depth, PIT revisions, rights, and price need written confirmation. | `PIT_UNPROVEN`, `LICENSING_UNCLEAR` |
| LSEG datasets | Public catalogue advertises deep corporate-action history, trading-status events including delisted/suspended, and NSE-linked constituent/history products. | Exact India exchange/universe depth, point-in-time delivery, package composition, household licensing, automated retention, and price require a proposal and sample. | `DOCUMENTATION_REVIEWED`, `OWNER_APPROVAL_REQUIRED`, `PIT_UNPROVEN` |

Primary public references reviewed:

- [NSE Indices data subscription](https://www.nseindia.com/static/nse-indices/index-data-subscription)
- [NSE EOD/historical data subscription](https://www.nseindia.com/static/market-data/eod-historical-data-subscription)
- [NIFTY historical and total-return reports](https://www.niftyindices.com/reports/historical-data)
- [BSE information products tariff](https://www.bseindia.com/downloads1/Information_Products_Pricing_Sheet.pdf)
- [Global Datafeeds capability table](https://docs.globaldatafeeds.in/type-of-corporate-data-available-1142925m0)
- [Global Datafeeds historical API](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/introduction/type-of-data-available/)
- [LSEG corporate actions](https://www.lseg.com/en/data-catalogue/corporate-actions)
- [LSEG NSE-linked index data](https://www.lseg.com/en/data-catalogue/indices-benchmarks/pricing/indices-national-stock-exchange-of-india-nsei-ll)

## Acquisition decision

No candidate is approved. The smallest promising owner-side next action is to
request written, non-binding proposals and samples from NSE Indices/NSE Data &
Analytics and one commercial comparator. The request must include the approval
gate verbatim. DHRUVA must not scrape NSE pages or infer rights from public
downloads.
