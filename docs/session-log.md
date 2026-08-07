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

---

## 2026-08-03 — News domain and lexical sentiment baseline

**Done.** Activated the Intelligence context with a pure, provider-neutral news
foundation and no persistence. `news.py` holds source credibility tiers, item
identity, canonical-URL reduction, the permitted title and bounded snippet, both
timestamps, deterministic fingerprints and five ordered deduplication rules.
`events.py` assigns exactly one of eighteen bounded event categories.
`entity_linking.py` maps a headline onto approved instruments at its publication
date. `sentiment.py` is the deterministic lexical baseline over the five
required labels.

**Decided.** Deduplication is five named rules tried strongest first — provider
item id, canonical URL, repeated filing, syndicated headline, rewritten headline
— so a verdict always has exactly one explanation and every decision names the
rule and the original it repeats. Nothing scores similarity. The rewrite rule is
set equality over significant tokens on the same publication date and declines
entirely below six tokens, because short headlines share their few words by
coincidence. It catches reordering and not "bags" → "bagged"; a test asserts
that, because the failure mode worth preventing is two contracts collapsing into
one.

Entity linking answers matched, ambiguous or unresolved, and one wording naming
two instruments is reported as candidates rather than as findings — a confident
wrong link is worse than no link. A bare canonical symbol needs its exact ticker
spelling, always: `ETERNAL` is a company and "eternal optimism" is a mood.
Corporate suffixes are stripped only when at least two tokens survive, so
"ICICI Bank Limited" matches the way headlines write it while "Eternal Limited"
can never shorten to the adjective. Former names stay usable for one year past
the rename, because nobody renames a company in print on the day its
shareholders do. Aliases never replace canonical symbols.

Sentiment is wording, not markets, and the module says so first. Severity
outranks the mixed rule: a governance term is not balanced out by a good quarter
in the same sentence. Negation inverts and halves, so "cleared of fraud" is
relief rather than triumph. Hedging with nothing to hedge is UNCERTAIN, not
NEUTRAL. Event category stays independent of sentiment throughout.

Nothing here stores an article body, and the type cannot hold one. No migration,
no model, no adapter, no HTTP client and no job enter this slice; a test walks
the import graph and the source text to keep it that way.

**Repaired.** The first canonical run failed one gating stage:
`docs/evidence/s04-20260803T103655Z/` records `12-mypy` exit 1 —
`tests\unit\intelligence\test_entity_linking.py:94: error: Function is missing a
return type annotation [no-untyped-def]`. A test helper lacked its return type.
Because it was untyped, mypy stopped analysing its callers and hid a second
helper whose `**kwargs: object` carried a `type: ignore[arg-type]`. Both helpers
were given explicit parameters and return types in one file and the ignore was
deleted rather than left to justify itself. No ignore was added, no cast, no
`Any`, no configuration change and no behaviour change. The slice now contains
no `type: ignore` at all. The failing evidence is kept: a repair is only
evidence alongside what it repaired.

**Validated.** Owner-run Windows figures, recorded exactly: focused intelligence
tests **127 passed**; strict mypy over the configured `src` and `tests` scope
**passed with no issues in 337 source files**; the complete unit suite **2,218
passed, 5 skipped, 1 XPASS**; the integration suite **231 passed with one
non-strict XPASS**; `git diff --check` passed. Every gating canonical stage
passed in `docs/evidence/s04-20260803T104556Z/` — `GATING: PASS`, shell exit
status 0.

**Benchmarks: 22 passed, 5 failed, 2 xfailed, informational and non-gating under
ADR-060 §2.** Three E2 runs of the same commit lineage this morning moved the
end-to-end read budget 2.429 → 3.750 → 4.862 ms against an unchanged 3 ms
threshold, while `money add` sat at 0.572, 0.573 and 0.581 µs. A database budget
that doubles over a morning, measured against a pure text domain that touches no
repository, no session and no ORM, is not measuring this codebase. Recorded in
`PERFORMANCE_BASELINE.md`; no budget adjusted, no Money or benchmark code
touched.

**Next.** The persistence and provider half: news models and migration,
repository and application ports, the official NSE adapter, one legally usable
zero-cost broader source, idempotent ingestion and point-in-time storage.

**Open.** No credential is required for the next slice; the official NSE feeds
and the broader public source are anonymous. A compact local sentiment model
remains optional and absent, which is exactly the condition this baseline was
built to be the fallback for.

---

## 2026-08-03 — Point-in-time news archive

**Done.** Persistence for the news domain, deliberately without any provider
adapter. Migration `0016_news_archive` adds three append-only tables:
`news_item_revision` (one observed version of one item), `news_analysis` (one
pass of the event, sentiment and linking rulesets over it) and
`news_entity_link` (the instruments that pass resolved). Inward ports, a
repository with point-in-time reads, an account-scoped unit of work, and an
`IngestNewsItems` use case that deduplicates against both the archive and the
batch, classifies the survivors and appends everything in one transaction.

**Decided.** Row identity is `uuid5` over `(source_key, provider_item_id,
content_revision)` with `ON CONFLICT DO NOTHING`, so a re-poll is a no-op rather
than a duplicate. `first_seen_at` is deliberately outside every key: the second
poll must leave the first observation's timestamp where it was, or the archive
would quietly claim DHRUVA saw everything for the first time today. A correction
changes `content_revision` and therefore appends beside the original; a read at
the earlier cutoff still returns the earlier wording. There is no update path at
all.

The title and snippet bounds are enforced twice — in the domain type and again
as database check constraints. The licence position is that DHRUVA stores a
headline and a short extract, and a control living only in Python is one an
ad-hoc script can walk past. `first_seen_at >= published_at` is a constraint for
the same reason: an item observed before it was published is a clock defect, not
a fact.

Duplicates are stored with the rule that judged them and the item they repeat,
named rather than hashed — attribution has to survive being read back, and a
digest of an identity cannot be turned into one again. Entity links carry the
stable `instrument_id` alongside the symbol, the match kind and the matched
text; deriving identity from a ticker is what ADR-009 forbids, and reconstructing
the match kind from its relevance would let a row disagree with itself.

An analysis carries all three ruleset revisions in its key, so re-running an
unchanged ruleset is idempotent while a changed one appends a second analysis
beside the first. Nothing is ever reinterpreted in place.

**Repaired, in three rounds.** The schema was correct throughout; every defect
was in the harness or in a stale assumption.

First, seven integration tests failed while each passed alone. The shared
`truncated_after_test` fixture truncates a hard-coded table list and the three
news tables were missing from it, so rows survived into the next test and
`pytest-randomly` decided which one noticed. The fixture's own docstring
predicts exactly this. While fixing it, the rollback test was strengthened: it
appended an empty tuple, which returns early without staging anything and would
have passed even with rollback broken.

Second, one test failed deterministically: the archived match kind was
`COMPANY_NAME` where the test expected `ALIAS`. The linker was right.
"Hindustan Aeronautics" is reachable twice over for that fixture — the
registered name minus its corporate suffix *and* a listed alias — and the
registered name is the stronger evidence, so precedence gives it 0.90 rather
than 0.80. Persistence had round-tripped every field exactly. The test now
computes its expectation from the linker itself and compares structurally, which
is stronger than the hand-written constant that was wrong: it catches corruption
in any field for any headline and cannot go stale.

Third, canonical validation found three gating failures. Strict mypy wanted a
concrete annotation on a list only ever filled by `extend`, and refused
`Model.__table__.insert()` because `__table__` is typed `FromClause`; both fixed
narrowly, with no ignore, cast or `Any` anywhere in the slice. And three
historical migration tests hard-coded `0015_daily_market_bars` as the
repository's current head — a second, silent claim that adding 0016 falsified in
tests that have nothing to do with news. They now take a new session-scoped
`sole_alembic_head` fixture that reads the head from `ScriptDirectory` and
asserts there is exactly one. The duplication is removed rather than updated: no
future migration needs to edit a historical test, and a branched history now
fails loudly at the fixture instead of quietly satisfying an equality against
one of two heads.

**Validated.** Owner-run Windows figures, recorded exactly: focused intelligence
unit suite **144 passed**; focused migration and news integration tests **16
passed**; strict mypy over the configured `src` and `tests` scope **passed with
no issues in 346 source files**. Canonical run
`docs/evidence/s04-20260803T170908Z/` passed every gating stage — database
versions, Ruff lint and format, strict mypy, import-linter, custom boundaries,
ADR guard, the complete unit suite at **2,256 passed with 5 skipped and one
XPASS**, migration upgrade, downgrade and re-upgrade, history, current, **empty
autogenerate drift**, and the integration suite at **244 passed with one
non-strict XPASS**. `GATING: PASS`, shell exit status 0.

**Benchmarks: 23 passed, 4 failed, 2 xfailed, informational and non-gating under
ADR-060 §2.** Across four E2 runs today the end-to-end read went 2.429 → 3.750 →
4.862 → 6.023 ms against an unchanged 3 ms budget — 2.5× and monotonic — while
`money add` moved 0.572 → 0.584 µs and `money mul` 0.589 → 0.598 µs. The
database-bound pair drifts over a working day; the CPU-bound pair does not. This
slice adds three empty tables and a repository nothing else calls. Recorded in
`PERFORMANCE_BASELINE.md`; no budget adjusted, no Money or benchmark code
touched.

**Next.** The provider half: the official NSE ingestion adapter, one legally
usable zero-cost broader source, provider payload mapping behind the existing
ports, source-health handling and the ingestion wiring.

**Open.** Both feeds are anonymous, so no credential is needed. The three
`text[]` columns are the first native arrays in the schema; autogenerate drift
came back empty, so the model and migration agree on them.

---

## 2026-08-06 — GDELT news metadata, and NSE deferred

**Decided first, built second.** The source-selection rule was applied before
any adapter was written, and it changed the shape of the slice.

**GDELT is approved.** Its Terms of Use
(<https://www.gdeltproject.org/about.html>) grant "unlimited and unrestricted
use for any academic, commercial, or governmental use of any kind without
fee", with
redistribution permitted provided "a citation to the GDELT Project and a link to
this website". Access is the documented DOC 2.0 API, anonymous, no key, no
recurring cost, and its `artlist` response carries article *metadata* only — so
the storage model already built is the model the API supports.

**NSE is deferred in full.** The owner reviewed NSE's current Terms of Use and
Copyright Policy and identified four independent obstacles: content may not be
stored in an electronic retrieval system without prior written permission;
systematic or automated collection is prohibited; NSE information may not be
used for gaming, virtual trading or simulation, and DHRUVA includes paper
trading; and
content may not be aggregated or duplicated unless expressly made available for
download. The Copyright Policy's personal/non-commercial allowance covers
viewing, printing and downloading — it does not clearly extend to database
storage, and the Hyperlinking Policy separately requires written permission to
link. That an RSS endpoint answers anonymous requests was explicitly *not*
treated as evidence of permission. No NSE code exists in the repository; the
decision and its reconsideration conditions are recorded in
`docs/decisions/news-source-selection.md`. It is a product-risk decision, not a
legal opinion.

**Done.** Provider-neutral `SourceHealth` and `NewsFetchResult` in the domain; a
narrow GDELT DOC 2.0 HTTP transport; a pure deterministic mapper; and a
`PollNewsFeeds` orchestration that polls every configured feed and hands the
healthy items to the existing ingestion path in one pass. No schema change, no
migration, no scheduler, no new dependency.

**Decided.** Operational outcomes and payload outcomes are settled in different
places — the transport classifies HTTP, the mapper classifies bytes, and only
bytes cross between them, which is why every failure mode is testable without a
network. A result may carry items only when `HEALTHY`, enforced by invariant, so
there is no path by which a failed poll smuggles an item into ingestion. 429 is
`RATE_LIMITED` and is not retried: being asked to slow down means wait, not try
harder. Timeouts and 5xx get three bounded attempts. A 4xx on a documented
request is `UNSUPPORTED_SCHEMA`, because retrying an unchanged rejected request
only repeats the mistake. `PollNewsFeeds` ingests once over the union of healthy
feeds rather than once per feed, so an item syndicated across two sources is
caught in the same batch; a failing feed degrades coverage and its status stays
visible in the result rather than being raised or silently dropped.

Two honesty notes are carried in the data rather than assumed away. GDELT's
`seendate` is when *GDELT* saw the article, not when the publisher stamped it,
so it becomes `published_at` as an upper bound, with `first_seen_at` as
DHRUVA's own retrieval instant. And GDELT supplies no per-item identifier, so
identity is
derived from the canonical URL and prefixed `url-sha256:` to say so —
inventing a field and inventing a derivation are different acts, and only the
second is
defensible.

**Attribution.** Every persisted item names GDELT as its source, carries
`https://gdeltproject.org` as the required citation link, names the originating
publisher's domain in the source display name, and keeps the publisher's own
article URL. Any future dashboard presentation must display that citation and
link. DHRUVA never fetches the article itself; the adapter contains no code that
could crawl it.

**Deliberately not persisted.** GDELT supplies `language` and `sourcecountry`
and this slice stores neither. There is no column for them, no migration 0017
was added, and nothing in the code or documentation implies otherwise. Adding
nullable columns because a provider happens to supply a field is how a schema
accumulates data nobody reads.

**Fixtures are synthetic.** Every GDELT fixture is hand-constructed against the
documented field names — no live response was captured. Domains are reserved
`.test` names, headlines are invented, and nothing is copied from any third
party. `backend/tests/fixtures/gdelt/README.md` records this and asks that the
first real sanitised capture replace them.

**Validated.** Owner-run Windows figures, recorded exactly: focused intelligence
suite **195 passed**; strict mypy **passed with no issues in 354 source files**;
`git diff --check` passed. Canonical run
`docs/evidence/s04-20260806T142033Z/` passed **every gating stage** — database
versions, Ruff lint and format, strict mypy, import-linter, custom boundaries,
ADR guard, the complete unit suite at **2,307 passed with 5 skipped and one
XPASS**, migration upgrade, downgrade and re-upgrade, history, current, empty
autogenerate drift, and the integration suite at **244 passed with one
non-strict XPASS**. `GATING: PASS`, shell exit status 0.

**Live smoke test — what it did and did not prove.** The owner ran
`scripts/gdelt_smoke_test.py` against the real anonymous endpoint. Exact result:
`health: RATE_LIMITED`, `http: 429`, `retry-after: not supplied by the server`,
`items: 0`, `sha256: e3b0c442…b855` (the empty-payload digest),
`mapper: gdelt-doc2-transport-v1`.

That establishes exactly two things: the real endpoint is reachable, and the
adapter classified a 429 correctly without retrying and without converting it
into an empty success. **Live article-payload schema compatibility remains
unverified.** No live response body has ever been observed, so the mapper's
field handling is proven only against synthetic fixtures. GDELT publishes no
rate-limit policy, so there is nothing to comply with beyond backing off; the
smoke script's default query was already reduced to a single phrase and
`maxrecords=1`, it now reports `Retry-After` unconditionally including when
absent, and it offers an opt-in single bounded retry. The production feed's 429
behaviour was not changed.

**Environment note.** Canonical validation ran against local host port **55632**
because Windows currently reserves TCP 55411–55510, which contains the script's
default 55432. The committed `scripts/canonical_validation.ps1` was **not**
modified; the owner used a temporary local copy. Making the container port
explicitly configurable, with 55432 retained as the default, is a follow-up and
was deliberately kept out of this feature commit — a validation-harness change
and a feature change should not share a commit.

**Next.** A composition or user-visible read path rather than another provider.

**Open.** Live payload schema verification, whenever a smoke run gets past the
rate limit; capturing and sanitising that response would let the synthetic
fixtures be replaced by a real one. NSE stays deferred pending written
permission or a licensed data agreement.

---

## 2026-08-06 — an operator can run the news pipeline end to end

**Done.** Two commands, `dhruva-news poll` and `dhruva-news show`. The first
builds deterministic query batches from the approved watchlist, issues one
bounded GDELT request per batch, and ingests what came back through the existing
path. The second answers "what did DHRUVA know about this instrument at this
instant?" against the stored archive, touching no network at all. No migration,
no scheduler, no new dependency, no new provider.

**Decided.** A bare exchange symbol is never a search phrase. `HAL`, `PNB`,
`M&M` and `SBI` are precise inside NSE and ambiguous everywhere else; searching
news for `HAL` returns the computer from *2001*. Symbols identify instruments,
company names find articles, and conflating the two is how a scan acquires
evidence about the wrong company. The company name is the first phrase, with a
trailing corporate suffix dropped only when two tokens survive it, so "Adani
Power Limited" becomes the phrase headlines use while "Eternal Limited" stays
whole rather than decaying into an adjective.

Nothing is dropped quietly. Every refused candidate comes back with the rule
that refused it, and an instrument left with no usable phrase comes back as
unqueryable. Coverage somebody believes in but does not have is the failure
this is guarding against, and silence is exactly what produces it.

Two phrases per instrument, eight phrases per batch. Both numbers are about the
provider rather than about us: the only live evidence of GDELT's throttling is
an HTTP 429 on a single anonymous request, so what matters is keeping the
request count small. One expression containing the whole watchlist would be
fewer requests still, and is refused as unreviewable — and because a single
rate-limited response would then lose the entire pass.

**Being told to slow down ends the pass.** A source that has just asked for a
slower rate is not persuaded by the next five requests, and issuing them anyway
is the behaviour a provider blocks rather than throttles. Later batches come
back marked as never issued — not empty, not failed, not asked — and everything
the earlier batches returned is still ingested, because turning a pacing request
into data loss would mean re-fetching the same articles tomorrow. Every other
unhealthy outcome is reported and the pass continues: a malformed payload from
one query says nothing about the next one.

**An override is exclusive structurally.** Setting a query expression turns
watchlist mode off by being the only query built; there is no second boolean to
disagree with it. Two independent switches encoding one decision is how a
configuration ends up contradicting itself with the code quietly picking a
winner, so `watchlist_queries_enabled` is derived rather than settable.

**Found by running it.** The planner was exercised against the real
twenty-symbol universe rather than against invented instruments, and that
produced two defects no unit test as written would have caught. Comparing an
alias to the canonical symbol case-insensitively refused `IndiGo` along with
`INDIGO`, which would have left that airline searchable only as "InterGlobe
Aviation" — a phrase no headline about it contains. NSE symbols are uppercase by
invariant, so the comparison is now case-sensitive and the brand survives while
the ticker does not. Separately, a former name outside its grace period was
being filtered out silently; it is now returned as refused with reason
`EXPIRED`, because an operator who cannot see that "Zomato" stopped being
searched cannot explain why coverage changed.

**Reading is point-in-time or it is nothing.** `--as-of` is a knowledge cutoff.
A correction observed after it stays invisible and the earlier wording is
returned; both revisions are stored and neither overwrites the other. The result
limit bounds what is printed rather than what is scanned, and that is written
down: pushing it into SQL means deciding there which revision wins, and that
decision belongs with the point-in-time rules. It becomes a repository concern
when a window is large enough for the difference to be measurable.

**Attribution is enforced where it cannot be forgotten.** `NewsSource` already
refuses to exist without a canonical homepage, so an unattributable item never
reaches the archive; the renderer then always prints the source, the citation
link and the publisher's own URL. Every command also states that NSE filings are
not an input, because a news list with no filings in it reads as "nothing was
announced" unless it says otherwise.

**Validated in the sandbox.** Ruff lint and format clean across `src` and
`tests`; import-linter 4 contracts kept; custom boundary checker and ADR guard
both OK; strict mypy clean on every new and changed module and test file; **349
focused tests pass** across the intelligence, news-CLI and news-settings suites,
stable across four random orderings. The 19 new PostgreSQL integration tests
**collect** but could not be executed — this environment has no container
runtime, so the database-backed evidence has to come from the owner's Windows
run.

**Open.** Live payload schema verification still needs a smoke run that gets
past the rate limit. The canonical host-port configurability repair remains a
separate follow-up and is deliberately not in this commit.

---

## 2026-08-06 — news workflow validated on Windows and pushed

**Done.** Commit `3823739` was validated on the owner's Windows machine and
pushed; local and remote heads match and the working tree is clean. This entry
records the evidence, and it is a separate commit from the feature it describes
because the feature was already published and history is not rewritten to make a
document tidier.

**Canonical evidence.** `docs/evidence/s04-20260806T154207Z/`, captured against
commit `3823739` on branch `mvp-personal-swing-assistant`. Every one of the
sixteen stages passed except benchmarks: database versions, Ruff check, Ruff
format (**385 files already formatted**), strict mypy (**no issues found in 366
source files**), import-linter, the boundary checker, the ADR guard, the unit
suite (**2,480 passed, 5 skipped, 29 deselected, 1 XPASS in 78.24s**), migration
upgrade, downgrade and re-upgrade, history, current, empty autogenerate drift,
and the integration suite (**263 passed, 2,251 deselected, 1 non-strict XPASS in
66.23s**). `GATING: PASS - every gating stage exited 0.` Shell exit status 0.

A second, partial run is also on disk at `docs/evidence/s04-20260806T154549Z/` —
eight stages, stopped after the unit suite, `OVERALL: PASS`. It is committed
alongside the full run rather than deleted: an evidence directory that exists
and is not in the repository is a gap somebody will later have to explain.

**Benchmarks failed and stay informational** under ADR-060 §2: end-to-end read
8.167 ms and write 23.517 ms, both the worst figures recorded across five runs,
alongside `money add` at 0.560 µs — its *best*. The database query improved to
0.929 ms in the same run the write nearly doubled. Full numbers and the argument
are in `docs/PERFORMANCE_BASELINE.md`.

**Not recorded, because it was not supplied.** The owner's report carried
unfilled placeholders for the focused unit-test summary, the focused integration
summary and the mypy summary. The canonical figures above are read directly from
the evidence logs in the repository, so they are quoted rather than
reconstructed; the three focused-run totals are simply absent and were not
invented. The
canonical unit and integration suites cover the same tests, so nothing material
is missing — only the narrower counts.

**Environment.** Windows 11, Python 3.12.13, uv 0.11.32, Docker 29.2.1,
SQLAlchemy 2.0.44, Alembic 1.16.5, asyncpg 0.30.0. Canonical validation again
used local host port **55632**, because Windows still reserves TCP 55411–55510.
The committed `scripts/canonical_validation.ps1` was not modified; an untracked
temporary copy was used and deleted. Two runs in a row needing the same manual
edit is the argument for the next slice.

**Next.** Make the canonical database host port an explicit parameter, with
55432 preserved as the default.

---

## 2026-08-06 — the canonical database port is a parameter

**Done.** `scripts/canonical_validation.ps1` takes `-ContainerPort`, defaulting
to 55432. Nothing else about the script changed, and no feature behaviour was
touched.

**Why now.** Two consecutive canonical runs needed the same manual edit, because
Windows currently reserves TCP 55411-55510 and 55432 sits inside it. Each time,
the owner copied the script, changed one number, ran the copy and deleted it.
That works, and it quietly costs the thing the evidence exists for: a run
recorded against commit X was produced by a script that is not the committed
script at commit X. Nobody reading the evidence six months later can tell how
the copy differed. A parameter makes the deviation a recorded argument instead
of an unrecorded edit.

**Decided.** The port is validated by a `[ValidateRange(1, 65535)]` attribute,
so an impossible value is refused by PowerShell before the script body runs --
before Docker is contacted and before an evidence directory is created for a run
that cannot happen.

Availability is checked before the image is pulled, and the two ways a port can
be unusable are reported separately because they need different fixes: something
is already listening there, or Windows has reserved the range and nothing may
listen there at all. The second is parsed out of `netsh interface ipv4 show
excludedportrange`, best effort -- if that output is not in the expected shape
the check falls back to a plain bind attempt, because an unrecognised format
must not turn into a false accusation about the port.

**The script never picks a port itself.** A run that quietly moved would record
a port nobody chose, and the next person to hit the collision would have no
evidence it had ever happened. Refusing with the flag that fixes it is worth
more than succeeding by accident. A test pins this.

`-ContainerPort` together with `-DatabaseUrl` is refused rather than resolved.
Honouring the URL would make the port silently do nothing; honouring the port
would connect somewhere the caller never named. Neither is defensible, so the
script declines to choose.

The port appears in the evidence twice, answering different questions.
`01-environment.log` records what was *requested*, because it is written before
provisioning is decided; `02-database-target.log` records what was actually
published. They differ exactly when a stale inherited URL is discarded and a
container is started after the environment log was already written.

**Two PowerShell traps, avoided deliberately and pinned.** A backtick inside a
double-quoted string is an escape character, so a command name quoted that way
renders as a newline plus the rest of the word -- quoting a command inside an
error message silently corrupts the message the operator is meant to act on. And
PowerShell 5.1 will not parse a double-quoted string nested inside a `$()`
subexpression of another double-quoted string, which the script already had a
comment about. Both are parse-time or render-time failures that no successful
run can catch, and this environment has no PowerShell to catch them either. They
are now two tests over the script text -- the substitute for an interpreter I do
not have.

**Validated.** Ruff lint and format clean; strict mypy clean; **37 tests pass in
`test_toolchain_config.py`**, 11 of them new. **The script itself was not
executed** -- no PowerShell here, and no permission to install one. Its
behaviour is asserted structurally, and a real run on Windows is still required.

**Next.** Owner-side canonical validation, ideally once with `-ContainerPort
55632` and once with no arguments, so both paths are exercised.

---

## 2026-08-07 — both port paths validated, and 55432 worked

**Done.** Commit `3ab4224` validated on Windows twice, once per port path. Both
runs are committed in full.

| Run | Evidence | Port | Result |
|---|---|---|---|
| Explicit | `docs/evidence/s04-20260807T122441Z/` | 55632 via `-ContainerPort` | `GATING: PASS`, exit 0 |
| Default | `docs/evidence/s04-20260807T123309Z/` | 55432, default | `GATING: PASS`, exit 0 |

Every gating stage passed in both. `50-benchmarks` was the only failure in
either, informational under ADR-060 §2. Identical totals across the two runs:
strict mypy **no issues found in 366 source files**, Ruff format **385 files
already formatted**, import-linter **4 contracts kept, 0 broken**, unit suite
**2,490 passed, 5 skipped, 29 deselected, 1 XPASS**, integration suite **263
passed, 2,261 deselected, 1 XPASS**.

**The port propagated cleanly, and that was checked rather than assumed.** In
the 55632 run every port reference in every log is 55632; in the 55432 run every
one is 55432. Neither run contains a single reference to the other's port, so
the value reached Docker, the connection URL, the `DHRUVA_DB__*` variables
alembic reads, the migration stages and the test session without anything
falling back to a hard-coded default. `01-environment.log` records
`container_port: 55632 (supplied via -ContainerPort)` and `55432 (default)`
respectively, and `02-database-target.log` agrees with each.

**55432 worked this time, and that is not a retraction.** Yesterday the default
port could not be bound, and today it could. Both observations are true and
neither generalises. Windows reserves TCP ranges for Hyper-V and WinNAT, those
reservations are allocated dynamically, and they move across reboots and as
other services claim and release ports. So:

- an earlier Windows state placed 55432 inside an excluded range and prevented
  binding;
- this session bound 55432 successfully;
- excluded-port state therefore varies across sessions, reboots and system
  state;
- **no permanent conclusion should be drawn** in either direction. 55432 is not
  known-safe and it is not known-reserved.

That is exactly why `-ContainerPort` is worth having. It is not a workaround for
a defect that has now gone away; it is the reproducible recovery path for a
condition that comes and goes and that nothing in this repository controls.

**Reviewed against the real run.** Range validation, no automatic port
selection, consistent propagation to Docker and the URL, selected-or-default
status recorded in both evidence logs, the `-DatabaseUrl` conflict refusal, the
pre-flight running before container startup, and diagnostics naming both the
port and the remedy — all present, and the two runs exercise the propagation
end to end. No PowerShell parse error occurred in either run, the first live
confirmation that the two string traps the tests guard were in fact avoided.

**The Docker pull warning is not a defect.** The default run logged a Docker Hub
authentication/network timeout while fetching an anonymous token, then started
the container from the locally cached image and completed successfully.

The script does not check `docker pull`'s exit status, and that is correct
rather than an oversight. The pull is an optimisation; `docker run` is the gate.
If the image is cached, a failed pull costs nothing and the run proceeds on the
image that is present. If it is not cached, `docker run` attempts its own pull,
fails, returns non-zero, and the script throws. There is no path on which an
unusable image is silently tolerated, so there is nothing to repair, and making
a failed pull fatal would turn a transient registry hiccup into a failed
validation run on a machine that had everything it needed. **No change was
made.**

One observation left for the owner rather than acted on: `docker pull` writes to
the console, not to an evidence log, so a run that used a cached image is
indistinguishable in the committed evidence from one that pulled fresh.
Recording the resolved image digest would close that, and it is a decision about
what evidence should contain — not a bug fix, and not something to fold into a
run that was asked to change nothing.

**Benchmarks.** The two runs are nine minutes apart on the same commit with no
code between them, and every one of the seven figures got worse in the second:
the database query by 76%, bulk append by 50%, `money add` by 63%, the 2000-day
range iteration by 71%. `money add` adds two Decimal-backed values in pure
Python and touches nothing external; it cannot legitimately move 63% in nine
minutes. Treated as informational, nothing optimised, no budget touched. Figures
in `docs/PERFORMANCE_BASELINE.md`.

**Next.** A read-only point-in-time watchlist digest over the archive.

---

## 2026-08-07 — a read-only watchlist digest

**Done.** `dhruva-digest` answers "what materially changed for my tracked
instruments as of this timestamp?" from what earlier polls already stored. One
section per watchlist instrument, an explicit cutoff, and **no network call on
this path at all** — the digest works with the provider unreachable,
rate-limited, or simply not run today. No migration, no new column, no new
dependency, no scheduler, no dashboard.

**Decided: it borrows both of its orderings rather than inventing one.** The
event classifier's rule order is already a statement about significance — "what
a reader must not miss comes before what merely describes" — so that order,
exposed as `event_precedence`, ranks entries within an instrument and ranks
instruments against each other. A governance finding outranks an order win
because the classifier already said so. Writing a second importance scale here
would eventually disagree with the first, and there would be no way to tell
which one was wrong.

**It recomputes nothing.** Every category, sentiment verdict and instrument link
comes back exactly as some earlier ingestion wrote it. The digest never re-reads
a headline, which is precisely what makes it incapable of disagreeing with the
archive it claims to summarise.

**One read, not twenty.** The archive is queried once over the whole window with
no symbol filter and grouped in memory. Per-instrument queries would issue
twenty statements to answer one question and — worse — let two instruments see
different snapshots of a table that is still being appended to. The `limit` that
exists on the underlying read is deliberately *not* used: truncating the window
before grouping would make whichever instruments sorted last look quiet. The
bound belongs on what each section shows, which is where the domain applies it.

**What it refuses to hide.** Every instrument gets a section, including the
silent ones, because a missing section is indistinguishable from a lost one
and "nothing happened" is the usual answer. A truncated section reports how
many items it withheld. An ambiguous instrument link is shown and marked.
Sentiment is tallied and never averaged — the mean of POSITIVE and NEGATIVE is
NEUTRAL, which is the one thing a split verdict does not mean. An unknown
`--symbol` is refused with the list of approved symbols, because a typo would
otherwise produce a confident, empty and entirely truthful-looking report
about an instrument DHRUVA does not follow.

**It reports and does not advise.** A tool that ranks instruments and highlights
findings is one careless sentence from reading as a recommendation. The
disclaimer states that the ordering is the classifier's precedence and not a
view on the instruments, and a test scans DHRUVA's own framing for advice verbs.
Quoted headlines are deliberately out of that scope: they are third-party text
shown with attribution and a link, and censoring a publisher's words would
misrepresent the source.

**Tested against the real rulesets.** The digest tests run the actual event
classifier, sentiment baseline and entity linker over real headlines rather than
arranging verdicts by hand — hand-labelled fixtures would let the tests pass
while the digest disagreed with the archive. The ordering assertions were
checked against printed classifier output to confirm they are not vacuous:
`FRAUD_GOVERNANCE` really does outrank `ORDER_WIN`.

**Validated.** Ruff lint and format clean; strict mypy clean on all five new
modules and all three new test files; import-linter 4 contracts kept; boundary
checker and ADR guard OK; **439 focused tests pass**, stable across three random
orderings. The 8 new PostgreSQL integration tests collect but could not run —
no container runtime here, so the database-backed evidence needs a Windows run.

**Next.** Owner-side canonical validation of the digest slice.

---

## 2026-08-07 — digest validated on Windows

**Done.** Commit `3524752` validated and pushed. Evidence:
`docs/evidence/s04-20260807T131304Z/`, captured against that commit on
`mvp-personal-swing-assistant`.

Every gating stage passed. Strict mypy **no issues found in 373 source files**;
Ruff check clean and Ruff format **392 files already formatted**; import-linter
**4 contracts kept, 0 broken**; boundaries and ADR guard OK; unit suite **2,551
passed, 5 skipped, 29 deselected, 1 XPASS in 83.30s**; every migration stage and
the empty autogenerate drift green; integration suite **271 passed, 2,314
deselected, 1 non-strict XPASS in 62.25s**. `GATING: PASS`, shell exit status 0.

The owner separately reported the focused digest integration suite at **8
passed, exit 0**, and the focused intelligence/workers suite and strict mypy
both passing with exit 0.

**The port parameter did its job.** This is the first canonical run to use
`-ContainerPort 55632` rather than a hand-edited copy of the script, and the
evidence says so in both places: `01-environment.log` records `container_port:
55632 (supplied via -ContainerPort)` and `02-database-target.log` agrees, with
`DHRUVA_DB__PORT: 55632` and a matching URL. The committed script is the script
that ran, which was the entire point of the repair.

**Benchmarks failed and stay informational** under ADR-060 §2 — three failures
this time rather than five. The end-to-end read came back *inside* budget at
2.859 ms, its best recorded figure, in the very same run the write reached
28.661 ms, its worst and 5.7× budget. Those two share a connection pool and a
container. `money add` and `money mul` returned to 0.564 and 0.585 µs after a
0.929/0.969 µs excursion forty minutes earlier — a 39% swing on a pure-Python
`Decimal` addition, recovered without anybody touching anything. The 2000-day
range iteration passed, its fourth verdict change in six runs. Nothing
optimised, no budget adjusted; figures in `docs/PERFORMANCE_BASELINE.md`.

**Next.** Point-in-time market context inside the digest, from the existing
daily bars.

---

## 2026-08-07 — market context in the digest

**Done.** `dhruva-digest` now shows what the stored daily bars did beside what
was written. One close-to-close change, one bounded multi-day return, one
volume ratio, per instrument, at the same cutoff the news is read at. No
migration, no new provider, no network call on the read path, no indicator.

**Decided: the reasoning lives in `marketdata`, not in the digest.** What a
close-to-close change means is market-data knowledge, so
`summarise_recent_bars` and `MarketContext` sit in that context's domain and
reach the digest through `marketdata.api`. The intelligence digest is
unchanged and still knows nothing about prices; the composition root reads
both archives and the renderer places them side by side. That keeps the two
contexts genuinely decoupled and keeps every boundary rule trivially satisfied
rather than argued about.

**Deliberately no indicators.** A moving average or an oscillator would look
more sophisticated and would be a judgement this system has not earned the
right to make inside something that claims only to report. Every figure shown
is a subtraction or a ratio a reader can check by hand against the bars it
came from.

**Nothing is computed from a bar that is not there.** A five-session return
needs six bars; where they do not exist the value is absent and the reason is
recorded. Substituting a shorter window would produce a number whose label
lies, and a reader comparing two instruments would be comparing different
periods without being told.

**Found by writing it.** Staleness and history were originally one enum, and
rendering a real digest showed the consequence: a single bar thirty-three days
old came back as `INSUFFICIENT_HISTORY` and silently *not* stale, because one
value had won. They are independent facts — a series can be both too short to
compare and too old to trust — so availability now means how much history and
`is_stale` is computed from the dates. A test pins the case that exposed it. A
second, smaller defect surfaced the same way: the invariant requiring an
absent series to explain itself accepted an empty string, which renders as "no
data -- " and is exactly the silent gap the type exists to prevent.

**The cutoff governs both archives.** The news read and the bar read take the
same `known_at`, so a section cannot pair yesterday's headline with tomorrow's
price. A bar retrieved after the cutoff stays invisible even when its trading
date is earlier, and an integration test asserts precisely that against
PostgreSQL — it is the property that makes a digest usable as backtest
evidence rather than as a screenshot.

**Validated.** Ruff lint and format clean; strict mypy clean on every new and
changed module and test file; import-linter 4 contracts kept; boundary checker
and ADR guard OK; **557 focused tests pass**. Two failures in
`test_zerodha_history.py` are a Python 3.10 `fromisoformat` limitation in this
sandbox's shim, in a file this slice does not touch — confirmed by stashing
the work and reproducing them unchanged. The 15 new PostgreSQL integration
tests collect but could not run here.

**Next.** Owner-side validation, then a deterministic attributed research-
snapshot export.

---

## 2026-08-07 — market context validated on Windows

**Done.** Commit `4ef9c82` validated and pushed. Evidence:
`docs/evidence/s04-20260807T134354Z/`, captured against that commit on `mvp-
personal-swing-assistant`, host port 55632 supplied via `-ContainerPort` and
recorded as such in both `01-environment.log` and `02-database-target.log`.

Every gating stage passed. Ruff check clean and Ruff format **396 files
already formatted**; strict mypy **no issues found in 377 source files**;
import-linter **4 contracts kept, 0 broken**; boundaries and ADR guard OK;
unit suite **2,606 passed, 5 skipped, 29 deselected, 1 XPASS in 82.82s**;
every migration stage green with empty autogenerate drift; integration suite
**286 passed, 2,354 deselected, 1 non-strict XPASS in 66.70s**. `GATING:
PASS`, shell exit status 0.

The owner separately reported the focused market-context PostgreSQL suite at
**15 integration tests passed, exit 0**, the focused marketdata/workers unit
suite passing with exit 0, and strict mypy passing with exit 0.

**Market-context behaviour is now owner-validated against real PostgreSQL**,
not only against fakes: the one-day close change, the bounded multi-session
return, the volume comparison against the mean of the preceding sessions, the
stale flag computed independently of history sufficiency, the explicit
insufficient-history and no-data states, the cutoff applying to the news read
and the bar read alike, a bar recorded after the cutoff staying invisible even
when its trading date is earlier, and the digest read path performing no write
and no network call.

**Benchmarks failed and stay informational** under ADR-060 §2. This run
produced the most useful evidence yet about what they measure: the end-to-end
read missed its budget by **29 microseconds** and the write by **31** — about
1% and 0.6%. Thirty minutes earlier the same write measured 28.661 ms, 5.7×
budget, while the read passed at 2.859 ms. A quantity that lands within 1% of
a threshold on one run and 470% past it on the next is not measuring the code
under test. The 2000-day range iteration failed at 1038.1 µs having passed
thirty minutes before, its fifth verdict change in seven runs. Nothing
optimised, no budget adjusted; figures in `docs/PERFORMANCE_BASELINE.md`.

**Next.** A deterministic attributed research-snapshot export over the same
read path.

---

## 2026-08-07 — a deterministic research snapshot

**Done.** `dhruva-export` writes a deterministic, self-describing JSON
snapshot of the digest and its market context. Same read path as `dhruva-
digest`, same cutoff, same argument rules — only the destination differs.
Read-only against PostgreSQL and against the network, no migration, no new
dependency.

**Decided: the determinism contract is a structural split, not a promise.**
The body holds everything that is a function of database state, account,
cutoff and schema version, and is byte-for-byte identical across runs. The
envelope holds the one thing that cannot be — the instant of writing — plus a
SHA-256 of the canonical body. So two snapshots of one cutoff differ in
exactly one field and can be diffed meaningfully, and `body_sha256` names a
state of knowledge rather than a moment of writing. A reader can recompute it
from the file alone, without the database or this code, and detect an edit.

**Exact values are exported as strings.** JSON's only number is binary
floating point. A close of 143.50 that round-tripped as 143.49999999999997
would make a snapshot disagree with the database it was taken from, which is
precisely the failure an audit artefact must not have.

**It will not overwrite.** An existing file is refused unless `--force` is
given, and a missing parent directory is refused before the database is read.
A snapshot is what somebody keeps in order to be able to say what they knew;
silently replacing yesterday's would destroy the only copy of a fact at the
moment it became inconvenient. `--force` permits replacing a file, never
removing a directory.

**Shared argument rules were extracted rather than copied.** The export needs
the same account, cutoff, window, bound and symbol-selection rules the digest
uses, so those moved into `workers/cli_arguments.py` and both commands import
them. Two copies would agree until one was fixed, and then a snapshot would
describe a different instant from the digest it was supposed to be a record
of. The first draft imported the digest's private helpers directly, which
worked and was wrong.

**Gaps stay explicit.** `requested: false` means market context was switched
off; `NO_DATA` means it was looked for and the archive was empty. Collapsing
the two would let a news-only snapshot read as evidence that no prices
existed. `items_withheld` reports truncation so a bounded section cannot be
mistaken for a complete one.

**Nothing sensitive leaves.** No raw provider payload, no article body, no
credential, no machine-local path. The account appears as its surrogate
identifier. Tests assert the absence of each, because a snapshot is a file
somebody may email to themselves.

**Validated.** Ruff lint and format clean; strict mypy clean on every new and
changed module and test file; import-linter 4 contracts kept; boundary checker
and ADR guard OK; **555 focused tests pass** — 27 for the snapshot
serialisation, 18 for the export command, the rest unchanged. The two
`test_zerodha_history.py` failures remain the known Python 3.10
`fromisoformat` limitation in this sandbox's shim, in a file this slice does
not touch. The 15 new PostgreSQL integration tests collect but could not run
here.

**Next.** Owner-side validation of the export slice.
