# Performance Baseline

**Permanent, append-only.** Measurements accumulate; nothing is replaced. This is
the reference point every optimisation after S10 is judged against, and a
measurement that is overwritten stops being a baseline.

Each entry records the subsystem, the benchmark, the target declared **before**
implementation (ADR-036), the measured value, the environment, pass/fail, and —
where a target moved — why.

---

## Environments

Referenced by identifier below, so a number is never quoted without the machine
that produced it.

| ID | Description |
|---|---|
| **E1** | Intel Core i5-10300H @ 2.50 GHz · 2 vCPU · 3 GB RAM · Linux 6.8.0 · **CPython 3.10.12** · sandbox, contended |
| **E2** | *Canonical.* Intel Core i5-10300H @ 2.50 GHz · 8 logical cores · 23.9 GB RAM · Windows 11 Home Single Language · **CPython 3.12.13** · PostgreSQL 16.6 + TimescaleDB 2.17.2 in Docker · first run 2026-07-29T14:04:56Z |

**E1 caveat.** The target runtime is 3.12; 3.11 and 3.12 carry substantial
interpreter speedups on small-object arithmetic, so every E1 figure is
conservative. E1 also cannot reliably resolve sub-microsecond budgets — a
different benchmark failed on each run of the same commit, which is measurement
noise rather than regression. Those budgets are gated behind
`DHRUVA_CANONICAL_BENCHMARKS` and must be re-measured on E2.

---

## S03 — Domain Primitives

Measured on **E1**, 2026-07-26.

| Benchmark | Target | Measured | Status | Notes |
|---|---|---|---|---|
| `Money + Money` | < 0.50 µs | **0.456 µs** | PASS | After the hot-path guard rewrite; was 1.856 µs |
| `Money × int` | < 0.50 µs | **0.455 µs** | PASS | |
| `Money` construction | < 0.30 µs | **0.374 µs** | **FAIL** | TD-13. Open; re-measure on E2 |
| `Price × Quantity → Money` | < 2.00 µs | **1.149 µs** | PASS | Includes explicit rounding |
| `Money.apply(Ratio)` | < 10.00 µs | **2.678 µs** | PASS | `Decimal` path; per trade, not per tick |
| `Money.allocate`, 10 weights | < 20.00 µs | **8.215 µs** | PASS | Largest-remainder distribution |
| `Money.parse` | < 5.00 µs | **2.230 µs** | PASS | Ingestion boundary only |
| `TradingDay` compare + hash | < 1.20 µs | **0.265 µs** | PASS | |
| `SystemClock.now()` | < 1.00 µs | **0.293 µs** | PASS | |
| `FrozenClock.now()` | < 1.00 µs | **0.055 µs** | PASS | The backtest clock |
| `InstrumentId.deterministic` | < 10.00 µs | **5.851 µs** | PASS | UUID5 derivation |
| 1,000,000 `Money` additions | < 0.50 s | **0.525 – 0.614 s** | **FAIL** | TD-14. Straddles the threshold under load on E1 |
| `sizeof(Money)` | ≤ 64 B | **48 B** | PASS | Slots |

### S03 revisions

**None.** Two budgets missed and were recorded as debt rather than revised.

### S03 optimisation history

| Change | Before | After |
|---|---|---|
| `Money` hot-path guards: eager `invariant()` → explicit branch | 1.856 µs | **0.456 µs** |
| Same, aggregate over 10⁶ additions | 2.007 s | **0.545 s** |
| `bind_correlation`: generator context manager → class (S02) | 17.7 µs | **2.0 µs** |
| Log redactor: per-record set rebuild → version-cached (S02) | 71.9 µs | **36.7 µs** |

---

## S04 — Persistence Foundation

Measured on **E1**, 2026-07-26. Database-dependent stages are **pending E2**.

### Measured on E1

| Benchmark | Target | Measured | Status | Notes |
|---|---|---|---|---|
| ORM object construction | < 15.00 µs | **13.1 µs** | PASS | Target revised — see below |
| Domain → record (mapper) | < 5.00 µs | **< 5 µs** | PASS | Pure function, no services |
| Record → domain (factory) | < 5.00 µs | **7.653 → passing** | PASS | After guard fix; was 11.362 µs |
| Model → record | < 5.00 µs | **< 5 µs** | PASS | |
| Full read mapping chain | < 10.00 µs | **< 10 µs** | PASS | model → record → domain |

### S04 revisions

| Benchmark | Original | Revised | Reason |
|---|---|---|---|
| ORM object construction | 10.00 µs | **15.00 µs** | 13 µs is SQLAlchemy's declarative instrumentation for eight mapped columns — a framework floor this codebase does not control. The original figure was guessed **without first measuring the framework baseline**, which is the mistake being recorded. Revised with the reason rather than silently lowered (ADR-036). Tracked as TD-18 for re-measurement on E2. |

### S04 optimisation history

| Change | Before | After |
|---|---|---|
| `TradingDay.of` and `SurrogateId.__init__`: eager guard → explicit branch | 11.362 µs | **7.653 µs** |
| `DailySnapshot.__post_init__`: eager invariants → explicit branches | 7.653 µs | **< 5 µs** |

The S03 defect recurred here verbatim. Not a regression — a cold path became hot
once every row read constructed a `TradingDay`. Recorded in `LESSONS.md`.

### PENDING — requires E2

None of the following has been measured. **No number should be quoted for them
until E2 has run.**

| Benchmark | Target | Measured | Status |
|---|---|---|---|
| SQL execution time | p95 < 3 ms | — | **PENDING** |
| asyncpg driver latency | p95 < 2 ms | — | **PENDING** |
| SQLAlchemy row materialisation | < 20 µs/row | — | **PENDING** |
| Repository overhead (above mapping + IO) | < 50 µs | — | **PENDING** |
| Unit of Work overhead | < 200 µs | — | **PENDING** |
| Total repository read latency | p95 < 3 ms | — | **PENDING** |
| Total repository write latency | p95 < 5 ms | — | **PENDING** |
| Bulk timeseries append, 10,000 rows | < 500 ms | — | **PENDING** |
| `upgrade head` on empty database | < 10 s | — | **PENDING** |
| `upgrade head` → `downgrade base` | < 20 s | — | **PENDING** |
| Integration suite, cold container | < 90 s | — | **PENDING** |

---

## E2 — first canonical measurement (2026-07-29T14:04:56Z)

Evidence: `docs/evidence/s04-20260729T140456Z/50-benchmarks.log`. Appended, not
merged into the E1 sections above.

Suite result: **20 passed, 7 failed, 2 xfailed**.

### Budgets missed on E2

| Benchmark | Budget | E2 measured | Over by |
|---|---|---|---|
| `test_money_addition_is_within_budget` | 0.500 µs | **0.572 µs** | 14.5% |
| `test_money_scaling_is_within_budget` | 0.500 µs | **0.597 µs** | 19.3% |
| `test_date_range_iteration_is_not_a_bottleneck` (2000 days) | 1000.0 µs | **1009.3 µs** | 0.9% |

The date-range figure is within 1% of its budget and is not distinguishable from
run-to-run variation on a single sample. It needs repeat measurement before it
is called either a pass or a regression; one number 0.9% over the line is not
evidence of anything.

The two money figures are a real, reproducible miss and are **not** noise: both
exceed budget by more than 14% on an uncontended machine, which is the condition
E1 could not provide.

### Still xfail on E2

| Benchmark | Debt | Status |
|---|---|---|
| `test_money_construction_is_within_budget` | TD-13 | Still over 0.30 µs; xfail holds. `xfail_strict` means it fails the day it passes |
| `test_a_million_money_additions_is_within_budget` | TD-14 | Non-strict; needs a figure recorded on E2 |

### Persistence benchmarks — implemented, first figures on E3

The four stubs are now real measurements. E3 below is a *development* sandbox and
its numbers are indicative only; the canonical figures come from the next E2 run.

| ID | Description |
|---|---|
| **E3** | Linux · 2 vCPU, contended · CPython 3.10 · PostgreSQL 16.2, local TCP · sandbox |

| Benchmark | Budget | E3 measured (isolated) | E3 (full stage) |
|---|---|---|---|
| `test_database_query_latency_is_within_budget` | p95 < 3 ms | **0.463 ms** | 0.957 ms |
| `test_end_to_end_repository_read_is_within_budget` | p95 < 3 ms | **1.507 ms** | **3.719 ms — over** |
| `test_end_to_end_repository_write_is_within_budget` | p95 < 5 ms | **4.368 ms** | 4.461 ms |
| `test_bulk_timeseries_append_is_within_budget` | 10,000 rows < 500 ms | **35.5 ms** | 47.9 ms |

Two observations worth carrying into the E2 run.

**The read p95 is sensitive to what ran before it.** Isolated it is 1.5 ms;
after five CPU-bound micro-benchmarks in the same process on a 2-vCPU machine it
is 3.7 ms. That is a property of E3, not evidence about the code — but it means
the read budget is the one to watch, and the canonical figure is the one that
counts. The budget has deliberately **not** been adjusted: ADR-036 requires
budgets to be declared before implementation and changed only with a recorded
reason, and "it failed on a contended sandbox" is not one.

**The write path has the least headroom** — 4.4 ms against a 5 ms budget, ~12%.
Worth watching on E2, where Docker Desktop's network stack adds a hop that E3
does not have.

**Bulk append is comfortable**: 10,000 rows in 35–48 ms against a 500 ms budget,
roughly 10x margin. `COPY` is doing what ADR-054 said it would.

### Previously not measured — the benchmarks were unimplemented

These four **were** `pytest.fail("not yet implemented; requires the integration
harness")` stubs. They were **skipped** before, because the skip was gated on a
database being configured, so their absence looked like an environment
limitation. With a real database present they run, fail, and are now visible for
what they are: unwritten.

| Benchmark | Declared budget |
|---|---|
| `test_database_query_latency_is_within_budget` | p95 < 3 ms, primary-key read |
| `test_end_to_end_repository_read_is_within_budget` | — |
| `test_end_to_end_repository_write_is_within_budget` | insert plus commit |
| `test_bulk_timeseries_append_is_within_budget` | — |

This is the mapping-performance breakdown requested at S04 review — SQL
execution, asyncpg latency, row materialisation, mapper in both directions,
repository and Unit-of-Work overhead. **It remains outstanding work, not a
measurement problem**, and S04 cannot be called complete against the review
criteria until these four produce numbers.

---

## E2 — complete canonical measurement (2026-07-29T15:12:48Z, commit `e1d0b5a`)

Evidence: `docs/evidence/s04-20260729T151248Z/`. The first run in which every
stage except the benchmarks passed, and the first in which a *passing*
benchmark's figure was recorded rather than discarded.

Suite result: **22 passed, 5 failed, 2 xfailed**.

### Persistence layer, measured

| Benchmark | Budget | **E2 (Windows, Docker)** | E3 (Linux, local) | E2 ÷ E3 |
|---|---|---|---|---|
| Database query p95 | < 3 ms | **1.573 ms** ✓ | 0.438 ms | 3.6× |
| End-to-end read p95 | < 3 ms | **4.281 ms** ✗ | 1.979 ms | 2.2× |
| End-to-end write p95 | < 5 ms | **6.111 ms** ✗ | 3.883 ms | 1.6× |
| Bulk append, 10,000 rows | < 500 ms | **84.2 ms** ✓ | 39.3 ms | 2.1× |

**Every database figure is 1.6–3.6× slower on E2 than on E3 — on hardware that
is four times larger.** E3 is a 2-vCPU, 3 GB contended sandbox; E2 is an 8-core,
24 GB laptop. The persistence code is identical. The difference is the transport:
E3 talks to PostgreSQL over a local TCP socket, E2 through Docker Desktop's port
forwarding on Windows.

The single clearest indicator is the raw query: **1.573 ms for one indexed
primary-key lookup**. That is not a query cost, it is a round-trip cost. Whatever
these four numbers measure, a meaningful part of it is Docker Desktop's network
stack rather than this codebase.

### CPU-bound primitives on E2

| Benchmark | Budget | E2 | E1 (Linux 3.10) | Over budget |
|---|---|---|---|---|
| `money add` | 0.500 µs | **0.584 µs** | 0.456 µs | +17% |
| `money mul` | 0.500 µs | **0.625 µs** | 0.455 µs | +25% |
| 2000-day range iteration | 1000 µs | **1062.9 µs** | — | +6.3% |

`Money + Money` is **28% slower on E2 than E1** — newer Python, far better
hardware, identical code, no database involved. Across three runs the E2 figure
is stable at 0.572 / 0.582 / 0.584 µs, so this is a reproducible property of the
platform, not noise.

### What this means for the budgets

Five budgets are missed and **not one of them has been traced to a defect in the
persistence layer**. The measurements are honest; the question is what
environment they should be taken in. DHRUVA deploys on Linux. Tuning code until
it hits 0.500 µs through a Windows timer, or until a read completes in 3 ms
through Docker Desktop's port forwarding, optimises against a machine that will
never run this system — and would likely make the code worse on the one that
will.

Recorded here without adjusting any budget. ADR-036 requires a declared reason
for a budget change, and this is the evidence such a decision would rest on.

---

## How to append

After a canonical run, add a new dated section rather than editing an existing
one. If a target changes, add a row to that subsystem's **revisions** table with
the reason. A baseline that is edited in place cannot answer "was this always
slow, or did we make it slow?" — which is the only question it exists to answer.
