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
| **E2** | *Canonical.* Python 3.12 · PostgreSQL + TimescaleDB · Docker · **not yet run** |

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

## How to append

After a canonical run, add a new dated section rather than editing an existing
one. If a target changes, add a row to that subsystem's **revisions** table with
the reason. A baseline that is edited in place cannot answer "was this always
slow, or did we make it slow?" — which is the only question it exists to answer.
