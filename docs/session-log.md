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
