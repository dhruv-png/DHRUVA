# Session Log

One entry per working session: what was done, what was decided, what is next.
Its purpose is resumability (risk R15) — a six-month gap should cost an hour of
reading, not a week of archaeology.

---

## 2026-07-26 — Master Project Plan

**Done.** Authored the Master Project Plan: governing constraints, 26 architecture
decisions, 9 bounded contexts, 46 subsystems, dependency graph, 7 gates, 15-risk
register, Definition of Done.

**Decided.** Grounded the plan in three external constraints that turned out to be
load-bearing: Kite's snapshot tick semantics and instrument caps; SEBI's retail algo
framework, mandatory since 2026-04-01; and the NSE/BSE expiry regime change of
2025-09-01.

**Next.** Product Owner review.

---

## 2026-07-26 — Plan approved, reissued as v1.1

**Done.** Incorporated amendments A1–A5. Added ADR-027 (ADR discipline) and ADR-028
(MCP-first delivery). Added Gate G-MCP. Corrected the §5 Risk/Trading dependency to
be one-way, which made the context graph acyclic.

**Decided.** MCP-first: Stage 1 is S01–S20 plus a thin dashboard, ~28 weeks, no
execution capability. Stage 2 is not authorised until G-MCP passes.

**Next.** S01.

---

## 2026-07-26 — S01 Repository, Tooling & CI Skeleton

**Done.** Monorepo scaffold; nine bounded-context packages with four layers and a
public `api` module each; three composition roots; shared kernel and tooling
packages. Toolchain: uv, ruff, mypy strict, pytest, import-linter, pre-commit.
Two purpose-built architectural controls written as tested code —
`dhruva.tooling.boundaries` and `dhruva.tooling.adr_guard`. CI and security
workflows. Docker Compose data stack. 28 ADR files generated from the plan and
checksum-registered. 60 tests.

**Decided.** Context-to-context rules are enforced by a bespoke AST checker rather
than by import-linter, which cannot express "only via `api`" without a
combinatorial explosion of contracts. The §5 dependency matrix lives as a single
readable dictionary in `boundaries.py` and is the single source of truth.

Compose ships data services only. Empty application containers would be
placeholders, and the Golden Rules forbid placeholders.

**Next.** S02 — Core Runtime: config, logging, errors, tracing.

**Open.** Q2 (streaming universe), Q3 (cloud region) and Q4 (Kite historical
entitlement) are not blocking until S09/S10. They will be raised with a
recommendation before then.

---

## 2026-07-26 — S02 Core Runtime complete

**Done.** Configuration, logging with redaction, error taxonomy, correlation,
health/readiness/metrics registries, the `dhruva-api` observability component,
boundary rule R5, hash-pinned lockfiles, `docs/BUILD.md`, secret-hygiene tests,
and a six-budget benchmark suite. 335 tests at 99.91% coverage; all six gates
green; all six budgets met.

**Decided.** ADR-031 recorded as a clarification of §5 rather than a redesign.
ADR-037…041 written; the four runtime ADRs were renumbered from 031–035 because
the v1.3 policies claimed those numbers and ADR-027 forbids reuse.

Benchmarking found two real inefficiencies that measurement-after-the-fact would
have missed: the correlation binder's generator machinery (17.7 → 2.0 µs) and the
redactor rebuilding its secret set per record (71.9 → 36.7 µs).

Benchmark methodology was revised mid-implementation: microsecond-scale budgets
use best-of-batched-means rather than per-iteration tails, because a
`perf_counter` call costs a large fraction of what it measures. Recorded rather
than silently applied.

**Next.** S03 — Domain Primitives & Shared Kernel, after S02 approval.

**Open.** TD-01 and TD-02 both need a Python 3.12 host.

---

## 2026-08-02 — AR-002 personal-product transition

**Done.** Preserved local benchmark commit `5179a31` and the accepted S06 history,
merged both into `main` without rewriting the immutable `v0.6.0` tag, validated
the merged tree, and pushed merge commit `4202d73`. Created and published
`mvp-personal-swing-assistant` from that checkpoint. Reissued the active roadmap
as `DHRUVA_PERSONAL_MVP_PLAN.md` and marked the institutional S07–S46 sequence as
historical and indefinitely deferred.

**Validated.** 1,919 tests passed, 5 intentional skips, 29 benchmark deselections,
one documented non-strict XPASS and 96.37% coverage. Ruff lint/format, strict
mypy, import-linter, custom boundaries, ADR guard and diff checks passed.

**Decided.** AR-002 narrows delivery to a private end-of-day research and
paper-trading product for two users across four milestones. Zerodha read-only
market data may cost up to ₹500/month. News and local sentiment must have zero
recurring provider cost; paid news and hosted inference cannot block completion.
Official NSE sources, resilient public discovery/RSS, prospective point-in-time
archiving and a deterministic local sentiment fallback are binding.

**Next.** MVP 1 — shared watchlist plus the first vertical market-data slice.

**Open.** The exact real watchlist and Zerodha credentials are intentionally not
needed until fixture-backed behaviour is complete and an explicit provider smoke
test is ready.

---

## 2026-08-02 — Owner universe supplied; MVP 1 reference slice

**Done.** The owner supplied the exact twenty-symbol shared watchlist, resolving
the watchlist stop condition. Recorded ADR-075 and implemented stable instrument
identity plus append-only effective/recorded identity and membership revisions.
The committed configuration preserves exact names and punctuation, owner-approved
aliases and historical names, sector classifications and the Adani Group warning
dimension. Nifty 50 is persisted separately as the market benchmark.

**Decided.** Static configuration records futures research intent, never permanent
eligibility. Each Zerodha instrument-master refresh must prove current active
contracts, positive lots and unambiguous underlying mapping. Missing contracts
degrade that instrument to cash-only analysis instead of fabricating one or
failing the daily scan.

**Next.** Validate migration `0013_reference_watchlist`, commit this reference
slice, then implement the read-only Zerodha instrument-master adapter.

**Open.** Zerodha app credentials remain unnecessary until the fixture-backed
adapter is ready for its first explicit provider smoke test.

---

## 2026-08-02 — Read-only Kite instrument discovery

**Done.** Recorded ADR-076 and implemented the first read-only Kite vertical
against the official v3 instrument-master contract. The strict CSV adapter uses
bounded payloads, three-attempt retry limits, explicit authentication/rate-limit
errors, exact decimals, injected time and secret-safe headers. Sanitized fixture
coverage resolves every approved cash identity plus Nifty 50 and retains every
active unambiguous futures month. Expired contracts, options, invalid lots and
ambiguous expiries cannot create eligibility; cash analysis remains available.

**Decided.** Direct HTTP through the already pinned BSD-3-Clause `httpx2` client
is smaller and safer than the all-capabilities official SDK, whose order, GTT,
holdings and positions surfaces are outside DHRUVA. Contract identity derives
from stable underlying, exchange and expiry; provider tokens remain dated
attributes. The existing pin moved from development-only to runtime, both
hash-pinned lockfiles were regenerated, and the deployment audit found no known
vulnerabilities.

**Next.** Persist and archive each daily instrument master, resolved cash mapping,
actual futures contract revision and explicit availability result.

**Validated.** 1,980 tests passed, 5 intentional skips, 29 benchmark
deselections and one documented non-strict XFAIL; coverage is 95.59%. Ruff,
strict mypy over 303 files, import-linter, custom boundaries, ADR guard, lock
consistency, package build and diff checks pass. The exact deployment lock has no
known vulnerability; its temporary CycloneDX 1.4 verification contains 62
components, `httpx2==2.9.1`, no vulnerability and no machine-local path.

**Open.** Real credentials and the redirect URL remain unnecessary until the
fixture-backed archive and historical-data paths are ready for one explicit
provider smoke test.

---

## 2026-08-02 — Daily instrument-master archive

**Done.** Added reversible migration `0014_instrument_archive` and a vertical
fetch-resolve-archive use case. Each provider trading date retains the exact CSV
bytes as bounded deterministic gzip plus SHA-256, row count and retrieval time.
Versioned resolution rows retain explicit cash and futures availability reasons;
cash tokens and actual futures contract metadata are append-only daily revisions.
An application query reconstructs and revalidates the archived snapshot and
resolved owner universe without exposing SQLAlchemy or Kite response types.

**Decided.** The provider dump is global reference evidence rather than tenant
state, while the transaction still carries the requesting account context.
Provider/date is an immutable daily key: identical retries preserve the original
retrieval instant and add nothing, but different bytes fail closed. Actual
contract identity remains underlying plus exchange plus expiry. Unambiguous
expired contracts are persisted with `EXPIRED` status for historical research,
but only active, selected observations can establish current availability.
Resolver revision `instrument-discovery-v1` is part of every mapped-fact key so a
future algorithm change cannot silently reinterpret an old snapshot.

**Validated.** Focused reference coverage passes 43 unit tests and 12 real
TimescaleDB integration tests. Migration downgrade to `0013`, upgrade to the sole
`0014` head and Alembic autogenerate drift checks pass. Tests prove exact raw-byte
round-trip, idempotency, conflicting-source refusal, token turnover with stable
contract identity, explicit expired observations and transaction rollback. The
complete suite passes 1,994 tests with 5 intentional skips, 29 benchmark
deselections, one documented non-strict XFAIL and 95.27% coverage. Ruff lint and
format over 323 files, strict mypy over 307 files, all four import-linter
contracts, custom boundaries, ADR guard and diff checks pass.

**Next.** Extend the same read-only adapter with daily historical candles and
persist validated cash/index OHLCV with adjustment and completeness state.

**Open.** No credentials are required yet. The first real-provider smoke test
remains the next credential-gated stop condition after the fixture-backed
historical-data path is complete.

---

## 2026-08-02 — Cash and Nifty daily market history

**Done.** Activated the Market Data context with a provider-neutral daily-candle
domain, inward source/store ports, a narrow Kite historical adapter, synchronized
cash/index ingestion, point-in-time application queries and reversible migration
`0015_daily_market_bars`. The adapter implements only documented GET history,
uses exact decimal JSON parsing, preserves optional OI, bounds range/bytes/rows,
and serializes at two requests per second below Kite's documented three.

**Decided.** Kite history enters as adjustment `UNKNOWN`; neither raw nor adjusted
is assumed without corporate-action evidence. `daily-history-quality-v1` uses
Nifty as the explicit session calendar, labels bars after an injected completion
cutoff `INCOMPLETE`, and blocks unexplained close moves above 35% for reconciliation.
Bar-content revisions are independent of retrieval range and rotating provider
tokens, while later corrections append and remain invisible to earlier
knowledge-time queries. At no more than 50 daily symbols, plain PostgreSQL is
smaller and safer than adding hypertable compression or retention policy.

**Validated.** Twenty focused unit tests and sixteen real TimescaleDB integration
tests pass. They cover exact parsing, retry and host security, impossible OHLCV,
stale and missing benchmark sessions, incompleteness, mixed adjustment refusal,
possible corporate actions, idempotency, corrections, point-in-time reads,
rollback, migration downgrade/re-upgrade and autogenerate drift. The complete
non-Redis suite passes with 2,026 tests, five intentional skips, 29 benchmark
deselections, one documented non-strict XFAIL and 94.34% coverage. Ruff lint and
format check pass (339 files already formatted), strict mypy passes across 322
source files, all four import-linter contracts remain intact, and the custom
boundary checker, ADR guard, whitespace check and sole Alembic head
`0015_daily_market_bars` are green.

**Next.** Add actual futures daily OHLCV/OI from archived contract revisions,
then define and test the deterministic continuous research-series roll policy.

**Open.** Corporate-action verification and the explicit credential-gated Kite
smoke test remain future steps; no credential is needed for the next fixture-backed
futures history slice.

---

## 2026-08-03 — Actual futures contract history

**Done.** Completed the handed-over futures slice. `IngestActualFuturesHistory`
resolves every unambiguous contract in a persisted daily archive — active and
expired alike — into bounded, open-interest-enabled, explicitly non-continuous
provider requests, validates them against the archived Nifty session calendar,
and appends them in one transaction. The shared parts of the cash path were
lifted into `fetch_history_batches`, `build_daily_series` and
`append_daily_series` so both refreshes share one set of semantics rather than
two copies of them.

**Decided.** Two guards in the draft were unreachable and were removed rather
than left as decoration: contract identity, instrument kind and provider token
are already guaranteed because `fetch_history_batches` refuses a batch answering
a different request, and open interest is already guaranteed because
`DailyHistoryBatch` refuses a response whose candles omit it when the request
asked for it. Unreachable defence reads like protection and measures like
coverage, so the guarantee is stated in the validator's docstring instead. A
continuous research series is never requested from the provider; it is a derived,
versioned artefact and only actual contracts are fetched and stored. An archive
older than the required session is stale, not a smaller success. One contract
identity may not appear twice in one archive, and a refresh may not mix providers
with the archive that named its contracts.

**Validated.** Nineteen new focused unit tests: lifecycle counts across active
and expired contracts, open-interest persistence, benchmark-read-but-never-
written, no-continuous-request, explicit incompleteness, idempotent retry,
non-index benchmark, out-of-range cutoffs, stale archive, benchmark/archive
mapping mismatch, absent Nifty mapping, no contracts in range, repeated contract
identity, provider mismatch, stale benchmark calendar, non-benchmark sessions,
missing in-life sessions and stale contract history. Two further tests cover the
helpers the cash path now shares: a provider answering a different request, and a
refresh mixing providers.

Owner-run figures, recorded exactly: the focused daily-history and
futures-history unit tests pass **23**; the complete marketdata unit suite passes
**39**; the focused daily-market-bars integration test passes; `git diff --check`
passes. Canonical Windows run `docs/evidence/s04-20260803T073839Z/` passes every
functional stage — database versions, Ruff lint and format, strict mypy,
import-linter, custom boundaries, ADR guard, the complete unit suite, migration
upgrade, downgrade and re-upgrade, history, current, empty autogenerate drift,
and the integration suite at **231 passed with one non-strict XPASS**. Windows
benchmarks: **25 passed, 2 failed, 2 xfailed**; both failures are informational
and non-gating under ADR-060 §2.

**Decided (benchmarks).** The manifest reads `OVERALL: FAIL - 50-benchmarks`.
Under ADR-060 §2 that stage is informational on E2 and does not gate. The two
misses are `money add` 0.572 µs and `money mul` 0.589 µs against 0.500 µs
budgets — the lowest and second-lowest figures in four recorded E2 runs, inside
the band ADR-060 already documents as a stable platform property. Nothing in
this slice can reach them: no module under `contexts/marketdata` or
`contexts/reference` imports `dhruva.shared.money`. In the other direction three
budgets that were over on 2026-07-29 are now met on E2 — end-to-end read
2.429 ms, end-to-end write 4.501 ms and the 2000-day range iteration. No budget
was adjusted and no Money code was touched; the figures are recorded in
`PERFORMANCE_BASELINE.md` under a new dated E2 section per ADR-060 §2.

**Next.** Define and test the deterministic liquidity-aware roll policy and the
versioned continuous research series over these actual-contract facts.

**Open.** Two harness gaps found while resolving the benchmark verdict, each
belonging in its own commit rather than bundled here. First, the CI `benchmarks`
job does not set `DHRUVA_CANONICAL_BENCHMARKS`, so every sub-microsecond budget —
including both of today's misses and TD-13 — is skipped on the authoritative
Linux environment, and ADR-060 A3–A5 cannot close on evidence that job produces.
Second, `canonical_validation.ps1` records each stage's exit code and continues
by design but never calls `exit`, so the shell status is 0 even when a gating
stage fails; the manifest is the only verdict. Corporate-action verification and
the credential-gated Kite smoke test remain future steps.

---

## 2026-08-03 — Continuous futures research series

**Done.** Both harness gaps above are closed, each in its own commit: the Linux
benchmark job now sets `DHRUVA_CANONICAL_BENCHMARKS` (`9082a49`), and
`canonical_validation.ps1` now reports a second `GATING` verdict and returns it
as the shell status, with `50-benchmarks` the sole informational stage
(`2793fb5`). Today's run is the first under that harness and behaved exactly as
designed: `OVERALL: FAIL`, `GATING: PASS`, exit 0.

Then the roll policy and the continuous series. `build_continuous_series`
stitches persisted actual-contract history into one versioned research series
under `continuous-futures-roll-v1`: roll when the back month beats the front on
**both** volume and open interest, otherwise when the expiry buffer is reached,
counted in benchmark sessions. Every bar names its source contract, token,
expiry and lot; every roll records the decision date, roll date, reason, both
closes, both volumes, both open interests and the visible price gap.

**Decided.** A roll is decided on one session and takes effect on the next; a
rule that switched on the session it read the close of would be trading a bar it
had already seen. Requiring both volume and open interest to cross keeps the
series from depending on which day a scan happened to run — one side alone is an
ordinary noisy session. The buffer counts sessions, not calendar days, so a
holiday cannot move the fallback roll.

A roll is *materialised* from the two bars that actually abut the change rather
than from the pair the decision was about. The named successor can itself be
illiquid or expire in the gap, and provenance assembled from the real pair stays
true when that happens. `_reason_for` refuses to let a liquidity decision explain
a roll that landed on a different contract; that becomes an expiry fallback,
because that is what it was.

The series is research output and the types refuse to pretend otherwise. It has
no identity, no token, no lot and no `MarketInstrumentKind` — which is what the
daily-bar table and every provider request are keyed by, so a stitched price has
nothing to be persisted or requested as. `execution_contract_on` is the only
bridge to a fill and always answers with an actual contract at its then-effective
lot size. v1 emits unadjusted prices and records the gap; back-adjustment would
be a new policy revision consuming exactly that number.

Derived on read, not stored. Every input is already persisted point-in-time and
the rule is deterministic and versioned, so a second table could only ever
disagree with its own inputs, and the first time it did the question "which is
right?" would have no answer.

**Known v1 property.** If the front contract goes silent for several sessions
before expiry, the series shows the real gap rather than jumping early to a
further-out month. Deterministic, visible in the bar dates, and pinned by test.

**Validated.** Forty-one focused tests — twenty-eight over the roll rule and the
research-only guarantees, thirteen over the point-in-time derivation. The
complete marketdata unit suite passes **80**. Canonical Windows run
`docs/evidence/s04-20260803T084014Z/` passes every gating stage: database
versions, Ruff lint and format, strict mypy, import-linter, custom boundaries,
ADR guard, the complete unit suite at **2,091 passed with 5 skipped and one
XPASS**, migration upgrade, downgrade and re-upgrade, history, current, empty
autogenerate drift, and the integration suite at **231 passed with one
non-strict XPASS**.

**Benchmarks: 23 passed, 4 failed, 2 xfailed, informational and non-gating under
ADR-060 §2.** The misses are end-to-end read 3.750 ms, end-to-end write
5.726 ms, `money add` 0.573 µs and `money mul` 0.604 µs. The two database
budgets were measured as *met* on this same machine sixty-two minutes earlier
(2.429 ms and 4.501 ms) from a tree differing only by a pure in-memory roll rule
that touches no repository, no session and no ORM. A read budget that swings 54%
within an hour is measuring Docker Desktop's transport, not this codebase —
ADR-060's central finding, now visible inside a single morning. No budget was
adjusted, no Money code touched, and the harness was not altered as part of this
slice. Figures recorded in `PERFORMANCE_BASELINE.md` under a new dated section.

**Next.** Provider-neutral news ingestion: official NSE sources first, then at
least one legally usable zero-cost broader source, with idempotent ingestion,
deduplication, entity linking and point-in-time archiving.

**Open.** ADR-060 A3–A5 stay open until the Linux benchmark job reports its
first figures for the primitive budgets. Corporate-action verification and the
credential-gated Kite smoke test remain future steps. No credential is needed
for the next slice.
