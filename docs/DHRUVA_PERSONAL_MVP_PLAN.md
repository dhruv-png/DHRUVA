# D.H.R.U.V.A — PERSONAL SWING-ASSISTANT MVP PLAN

**Dynamic Heuristic Regime Understanding & Volatility Analytics**

Private end-of-day research and paper-trading system for two family users

| Field | Value |
|---|---|
| Document ID | DHRUVA-MPP-PERSONAL |
| Version | **1.0 — APPROVED** |
| Date | 2 August 2026 |
| Status | **ACTIVE ROADMAP** |
| Authority | Product Owner approval recorded by AR-002 |
| Foundation | Immutable `v0.6.0`; merged default-branch checkpoint `4202d73` |
| Replaces | The S07–S46 sequence as the active delivery roadmap |

> **The institutional S07–S46 roadmap is indefinitely deferred.** The original
> Master Project Plan and accepted ADRs remain permanent historical and
> architectural reference. This plan is the reissued product plan required by
> AR-002 and is the only active delivery roadmap.

## 1. Mission and completion outcome

DHRUVA will become a complete, usable, private end-of-day swing-trading research
and paper-trading system for the owner and his father. It supports Indian NSE cash
equities, Nifty 50 market context, eligible NSE equity futures, current news,
local financial sentiment, transparent strategies, honest backtests, isolated
paper portfolios, a private web dashboard, scheduled operations, alerts and a
trade journal.

The product is functionally complete when both users can sign in separately and,
without command-line or database intervention:

- maintain one shared watchlist;
- refresh market and news data;
- inspect Nifty market conditions and instrument history;
- scan equities and eligible futures and understand every pass or failure;
- run versioned backtests with explicit assumptions;
- accept, reject and monitor paper positions in separate portfolios;
- review risk, performance, related news, sentiment and journal notes; and
- operate scheduled scans, alerts and recovery flows from the private web app.

Every optional source must fail visibly. Missing, stale or incomplete data must
never silently become a valid recommendation.

## 2. Binding scope and safety boundary

### Active scope

- Indian NSE cash equities from the shared watchlist.
- Nifty 50 as the benchmark and market-regime input.
- Nifty 50 futures and active, liquid stock futures for eligible watchlist names.
- Daily OHLCV, futures open interest, contract, expiry and lot-size metadata.
- Official corporate information and legally usable zero-cost broader news.
- Local sentiment, event classification, entity linking and point-in-time news
  features.
- Transparent quantitative scans, historical backtests and paper trading.
- Optional ML-assisted ranking only after honest chronological validation.
- A private dashboard, in-app alerts, scheduled jobs and exportable reports.

### Explicitly outside active scope

- Options, commodities, cryptocurrency, forex and tick-level or high-frequency
  strategies.
- Public SaaS onboarding, billing and social features.
- Live broker orders of any kind, including GTT, modification or cancellation.
- Holdings or positions import, automatic stops or targets and margin-funded real
  trading.
- Unrestricted strategy mining or LLM-generated trade decisions.

All outputs are research or paper-trading outputs. News may add context, warnings
or ranking evidence, but sentiment alone cannot create a trade. Risk controls
override strategy and model outputs. An explanatory language model, if ever
added, may describe persisted facts but may not invent prices, news, scores,
entries, stops, targets, probabilities or results.

## 3. Binding budget and provider policy

- Zerodha Kite Connect is approved at up to **₹500 per month** for read-only
  market data and session handling.
- Recurring news-data expenditure is **₹0**.
- Recurring sentiment or language-model API expenditure is **₹0**.
- No paid news, historical-news, sentiment or hosted language-model service may
  become a completion dependency.

News source priority is:

1. Official NSE RSS and public corporate-information feeds.
2. GDELT public discovery as a non-critical broader-news supplement.
3. Legally usable publisher or organisation RSS feeds.
4. Sanitised fixtures for deterministic tests and credential-free demonstrations.

Unless a source clearly permits more, DHRUVA stores identifiers, canonical URLs,
titles, permitted snippets, timestamps, attribution, affected entities, event and
sentiment results, and deduplication metadata—not full article text. It does not
scrape paywalls, bypass controls or imitate a browser to evade provider policy.
The dashboard links to the original publisher for the complete item.

Sentiment runs locally. A deterministic lexical baseline is always available.
Any compact financial model must have a verified licence, immutable revision and
checksums; use safetensors where available, prohibit remote code, keep weights
outside Git, work from an offline cache, and fail safely when absent.

Free sources are not assumed to provide historically complete news. DHRUVA begins
prospective point-in-time archiving at first deployment and records publication,
first-observed and revision times separately. Later corrections cannot leak into
earlier backtests. When history is inadequate, predictive-news claims remain
explicitly unvalidated and ML excludes the feature instead of fabricating it.

## 4. Architecture continuity

The accepted `v0.6.0` foundation is extended, not replaced:

- the modular monolith and enforced bounded-context layers remain;
- provider integrations remain behind inward-facing ports;
- PostgreSQL/TimescaleDB remains the production and integration-test database;
- bitemporal and explicit-as-of rules govern anything a backtest reads;
- prices remain unadjusted at rest unless provenance proves otherwise;
- repositories never commit and the Unit of Work owns transactions;
- optimistic concurrency, the outbox, append-only audit and secret controls remain;
- configuration enters through the shared configuration boundary; and
- existing lint, type, architecture, ADR, coverage and security gates cannot be
  weakened.

New public contracts, persisted models, dependencies and migrations are designed
as narrow vertical slices. Major architectural decisions receive ADRs; a scope or
gate change requires another owner-approved Architecture Revision.

## 5. Active roadmap

There are exactly four product milestones. Engineering slices may be smaller but
must roll up to one of these outcomes.

### MVP 1 — Unified market, futures and news data

Deliver one effective-dated shared watchlist, initially about 20 fixture symbols
and configurable to at least 50. Each member carries a canonical NSE symbol,
company name, aliases and former names, ISIN where available, sector, cash mapping,
futures eligibility and active dates. DHRUVA does not select the owner's real
securities; fixtures remain until the exact list is supplied.

Add a read-only Zerodha adapter for instrument masters, historical daily candles,
freshness quotes, open interest, expiry, lot size and authentication state. Domain
and application code must not expose SDK types, provider URLs or response schemas.
Ordinary tests use sanitised fixtures; real smoke tests are explicit and
credential-gated. No order endpoint is implemented or called.

Persist cash-equity bars with source, retrieval time, adjustment state and
completeness. Reject duplicates, impossible OHLC relationships, non-positive
prices, negative volume, stale or incomplete sessions, missing benchmark sessions,
corporate-action discontinuities and incompatible adjusted/raw series.

Persist actual futures contracts, daily OHLCV/OI and effective contract metadata.
Archive each trading day's instrument master. Construct a versioned continuous
research series with source contracts, deterministic liquidity-aware roll dates,
expiry fallback, adjustment method, original prices and visible roll gaps.
Research can read actual or continuous series; execution simulation always maps
to the actual contract and its then-effective lot size.

Implement provider-neutral current-news ingestion from official NSE sources and
at least one legally usable zero-cost broader source. Store point-in-time metadata
and permitted snippets. Map items using names, symbols, explicit aliases,
benchmark/macro entities and versioned evidence; avoid common-word symbol
collisions. Deduplicate URLs, syndicated headlines, repeated filings and retries
while preserving attribution.

Classify bounded event categories independently from sentiment. Produce local,
deterministic `positive`, `neutral`, `negative`, `mixed` or `uncertain` results
with normalized scores, confidence, model identity/revision, input hash, inference
time, explanation metadata and abstention reason. The lexical baseline remains
functional when an optional model is unavailable.

MVP 1 is complete when fixture-backed or real equity, futures and news ingestion
is idempotent; quality failures and provider outages are visible; mapped,
deduplicated, event-classified and sentiment-scored results are exposed through
application queries; and tests prove point-in-time, licensing-minimal storage and
no-secret-leakage behaviour.

### MVP 2 — Quant scanning, backtesting and paper trading

Implement the approved cash trend-aligned pullback scan without optimization. It
uses an explicit as-of date, Nifty above a rising 200-session average, stock above
a rising 50-session average, the 50 above the 200, 60-session benchmark-relative
return, ATR(14), distance from the 20-session high, median 20-session rupee
turnover, pullback swing low and proposed stop distance. Results are `accepted`,
`developing`, `rejected` or `unavailable`, always with reasons.

Implement a minimal long/short paper-only futures trend/pullback strategy using
underlying regime, contract trend, volatility, volume/OI quality, expiry/roll
state, lot risk, costs and slippage. Reject near-expiry, stale, illiquid, gapped,
incomplete or budget-incompatible contracts. OI direction alone is never a
signal. News supplies warnings and ranking context, not an independent setup.

Build a deterministic daily backtester for cash equities and actual futures
contracts. Model next-session execution, gaps, stops, targets, ambiguous bars,
slippage, effective-dated Indian costs, expiry, rolls, lots, margin, daily MTM,
cashflows, portfolio limits, corporate actions, stale data and rejected orders.
Continuous series may create signals but simulated fills use actual contracts.
Report expectancy, hit rate, drawdown, exposure, turnover, costs, benchmark and
regime breakdowns, MFE/MAE and assumptions.

Keep two isolated cash portfolios and two isolated futures portfolios—one per
user. Cash defaults: ₹200,000 capital, per-trade risk `min(₹1,000, 0.5%)`, at most
five positions, aggregate open risk 2.5%, stock value 20%, sector value 30%, no
shorting or leverage. Futures start disabled per user: per-trade risk
`min(₹500, 0.25%)`, at most one position, aggregate risk 0.5%, with lot, margin and
stress checks. All positions are paper-only.

Persist versioned signals, proposals, paper orders/fills, positions, stops,
targets, expiry/roll events, MTM, costs, results, MFE/MAE, strategy/news/model
provenance, user ownership and journal entries. Backtest and paper modes share
calculation semantics.

MVP 2 is complete when both users can run reproducible scans/backtests and manage
separate paper portfolios with enforced risk, realistic costs, point-in-time data,
clear explanations and deterministic reruns.

### MVP 3 — News intelligence and optional ML-assisted ranking

Create point-in-time feature snapshots for polarity probabilities, mixed score,
article and independent-source counts, official-announcement presence,
recency-weighted sentiment, disagreement, high-impact events, macro/benchmark
sentiment and source-credibility distribution.

Compare chronological untouched periods for rules alone, simple event filters and
sentiment ranking. Measure expectancy, drawdown, hit rate, calibration, turnover,
missed trades, avoided losses and results by event and regime. A negative finding
is acceptable and does not remove useful news context.

Only with sufficient samples, evaluate regularized logistic regression before one
small tree model. Use expanding windows, an untouched holdout, calibration,
feature-stability checks, reproducible versions and abstention. Reject any model
that fails the transparent baseline, worsens risk/turnover, is uncalibrated,
regime-fragile or depends on unavailable news. Never use random time-series splits.

MVP 3 is complete when point-in-time features and an honest impact study exist,
the chosen ranking method is versioned, unreliable models are automatically
rejected, and explanations distinguish observed facts, calculations, model output
and uncertainty.

### MVP 4 — Dashboard, alerts and operational completion

Deliver the smallest maintainable private web application consistent with the
repository. Required screens are login, daily overview, shared watchlist, equity
detail, futures detail, news/sentiment, backtest reports, paper portfolio, trade
journal, settings and system health.

The overview shows update freshness, Nifty regime, accepted/developing/rejected
setups, high-impact news, market sentiment, open paper positions, aggregate risk
and scheduled-job state. Instrument pages show chart, strategy state, trigger,
stop, target convention, size/lots, rupee risk, trend/volatility/volume evidence,
futures OI/expiry/roll state, news, sentiment, timestamps and an explicit
research/paper-only label. Backtest pages separate equity/futures results and show
costs, drawdown, benchmark/regime/news comparisons, untouched-period results,
versions, assumptions and limitations.

Paper screens allow proposal acceptance/rejection, open/closed position review,
notes, stops/targets, futures MTM, risk, performance and deviation journaling.
In-app alerts cover setup state, entry zones, paper stop/target proximity, futures
expiry/roll, high-impact negative or mixed news, data/provider/authentication/job
failures and excessive paper risk. Alerts deduplicate until state or severity
changes. External notification adapters remain provider-neutral and may await
owner credentials.

Provide UI operations for Zerodha authorization, data/news refresh, manual and
scheduled scans, model availability/setup, backtest launch, job/provider health,
failed-job retry, confirmed paper reset, report/journal export and a Windows local
deployment runbook. Normal use must not require SQL or command-line access.

MVP 4 is complete when the two-user fixture-backed daily journey works at mobile
width, failures are accessible and recoverable, scheduled operations are visible,
and the complete private system passes its final validation.

## 6. Delivery, quality and evidence

Each coherent slice is inspected, planned, implemented vertically, tested,
reviewed in full, documented, committed with explicit paths and pushed normally.
The feature branch is not merged and no release is tagged without separate owner
approval. No secret, model weight or machine-local path enters Git.

Focused tests accompany every slice. Persistence semantics use real PostgreSQL,
never SQLite. The final system must pass the default and integration suites,
coverage, Ruff lint/format, strict mypy, import-linter, custom boundaries, ADR
guard, migration upgrade/downgrade/re-upgrade and one-head checks, autogenerate
drift, dependency audit, SBOM equality, packaging, `git diff --check`, a complete
fixture-backed daily workflow and optional credential-gated provider smoke tests.

Provider and model dependencies require current official documentation, a clear
acceptable licence, maintenance and advisory review, narrow reproducible pins and
updated deployment audit/SBOM. Safer dependency-free adapters and fixtures are
preferred when they meet the contract.

## 7. Genuine owner stop conditions

Work pauses only for remote divergence or protected-branch approval; the first
credential-gated Zerodha smoke test; the exact real watchlist; an unavoidable
external account/redirect/notification setup; unclear provider storage terms; no
legally usable zero-cost source for even current news; inadequate agreed-scope
Zerodha futures history; an irreplaceably unclear model/dataset licence; an
unavoidable destructive migration or public/persisted product choice; conflict
with an accepted security invariant; or a required force-push, merge, deployment,
release tag or live-order capability.

Ordinary defects, fixture work, safe migrations, dependency narrowing,
refactoring, documentation, tests, commits and normal pushes are not stop
conditions.

## 8. Current progress

| Milestone | State |
|---|---|
| Repository and roadmap transition | **Complete** — S06 and `5179a31` preserved on `main`; MVP branch created |
| MVP 1 | **In progress** |
| MVP 2 | Not started |
| MVP 3 | Not started |
| MVP 4 | Not started |
