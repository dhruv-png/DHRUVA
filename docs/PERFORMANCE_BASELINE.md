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

## E2 — canonical measurement (2026-08-03T07:38:39Z, commit `7f541b6` + the uncommitted futures-history slice)

Evidence: `docs/evidence/s04-20260803T073839Z/`. First canonical run on the
`mvp-personal-swing-assistant` branch. Every functional stage passed; only
`50-benchmarks` is non-zero, which ADR-060 §2 records as informational on E2.

Suite result: **25 passed, 2 failed, 2 xfailed, 2051 deselected**, against
22 passed / 5 failed on 2026-07-29.

### Persistence layer — three budgets close on E2

| Benchmark | Budget | **E2, 2026-08-03** | E2, 2026-07-29 | Verdict |
|---|---|---|---|---|
| Database query p95 | < 3 ms | **1.021 ms** | 1.573 ms | ✓ met |
| End-to-end read p95 | < 3 ms | **2.429 ms** | 4.281 ms ✗ | ✓ **now met** |
| End-to-end write p95 | < 5 ms | **4.501 ms** | 6.111 ms ✗ | ✓ **now met** |
| Bulk append, 10,000 rows | < 500 ms | **69.9 ms** | 84.2 ms | ✓ met |
| 2000-day range iteration | < 1000 µs | passed | 1062.9 µs ✗ | ✓ **now met** |

Every database figure improved by 17–43% on the same machine with no change to
the persistence layer, which is consistent with the ADR-060 finding that these
numbers are dominated by Docker Desktop's transport rather than by this codebase.
Three of the five budgets missed on 2026-07-29 are now measured as met on E2.
Under ADR-060 A4 they are closed *on E2*; they remain unverified on the
authoritative Linux CI environment.

### CPU-bound primitives on E2

| Benchmark | Budget | **E2, 2026-08-03** | E2 range, 2026-07-29 | Over budget |
|---|---|---|---|---|
| `money add` | 0.500 µs | **0.572 µs** | 0.571 – 0.584 µs | +14% |
| `money mul` | 0.500 µs | **0.589 µs** | 0.587 – 0.625 µs | +18% |

Both figures sit at the bottom of their previously recorded E2 range — `money
add` ties the lowest E2 measurement ever taken and `money mul` is faster than
three of the four prior runs. **This is not a regression.** It is the stable
platform property ADR-060 documents, re-observed on a fourth run.

The slice under test cannot reach this code: no module under
`contexts/marketdata` or `contexts/reference` imports `dhruva.shared.money`, and
the benchmark constructs `Money` directly.

No budget adjusted. TD-13 and TD-14 remain open; see the note below on why the
Linux figures ADR-060 A3–A5 require have not yet been produced.

### Gap — the authoritative environment does not measure these budgets

`tests/benchmarks/test_primitives.py` skips every sub-microsecond budget unless
`DHRUVA_CANONICAL_BENCHMARKS` is set, and its comment states that "CI sets it on
the benchmark job only". The `benchmarks` job in `.github/workflows/ci.yml` sets
only `DHRUVA_TEST_DATABASE_URL`. `money add`, `money mul`, `Money` construction
(TD-13) and the million-addition aggregate (TD-14) are therefore **skipped** on
Linux CI, so ADR-060 A3, A4 and A5 cannot close on the evidence that job
produces. Recorded here rather than fixed in passing; the fix is one environment
variable and belongs in its own commit.

---

## E2 — canonical measurement (2026-08-03T08:40:14Z, commit `2793fb5` + the uncommitted continuous-futures slice)

Evidence: `docs/evidence/s04-20260803T084014Z/`. Every gating stage passed; the
manifest reads `OVERALL: FAIL`, `GATING: PASS`, shell exit status 0 — the first
run under the two-verdict harness, behaving as ADR-060 §2 describes.

Suite result: **23 passed, 4 failed, 2 xfailed, 2097 deselected**.

### The same machine, sixty-two minutes apart

| Benchmark | Budget | **08:40:14Z** | 07:38:39Z | Verdict |
|---|---|---|---|---|
| Database query p95 | < 3 ms | **1.070 ms** | 1.021 ms | ✓ met |
| End-to-end read p95 | < 3 ms | **3.750 ms** ✗ | 2.429 ms ✓ | **reverted** |
| End-to-end write p95 | < 5 ms | **5.726 ms** ✗ | 4.501 ms ✓ | **reverted** |
| Bulk append, 10,000 rows | < 500 ms | **76.0 ms** | 69.9 ms | ✓ met |
| `money add` | 0.500 µs | **0.573 µs** ✗ | 0.572 µs | +15% |
| `money mul` | 0.500 µs | **0.604 µs** ✗ | 0.589 µs | +21% |

**This is the most useful benchmark evidence recorded so far, and it is not
about the code.** Two budgets were measured as met on E2 at 07:38 and as missed
at 08:40, on the same machine, from a tree whose only difference is a pure
in-memory roll rule with no database path. Nothing in the continuous-futures
slice touches the repository, the session, the ORM or `dhruva.shared.money`.

A pair of budgets that swings 54% on the read and 27% on the write inside an
hour is not measuring this codebase. It is measuring Docker Desktop's transport
on a laptop under whatever else that laptop was doing — exactly the finding
ADR-060 §1 rests on, now demonstrated within a single morning rather than across
two environments. The 2026-08-03 07:38 entry above should be read with that in
mind: those two budgets were not *closed* on E2, they were sampled favourably.

The Money figures are the stable half of the picture. Four E2 runs now give
`money add` 0.571–0.584 µs and `money mul` 0.587–0.625 µs, and today's 0.573 and
0.604 sit inside both. A reproducible platform property, unchanged.

No budget adjusted, no Money code touched, nothing in this slice reaches either
measurement. All four remain unverified on the authoritative Linux environment.

### Still unmeasured where it counts

`ci(benchmarks)` (commit `9082a49`) now sets `DHRUVA_CANONICAL_BENCHMARKS` on the
Linux job, so the primitive budgets will run there for the first time. Until that
job has reported, ADR-060 A3–A5 stay open and every figure in this section
remains informational.

---

## E2 — canonical measurement (2026-08-03T10:45:56Z, commit `c9c612c` + the uncommitted news-domain slice)

Evidence: `docs/evidence/s04-20260803T104556Z/`. `GATING: PASS`, shell exit
status 0; `50-benchmarks` is the only failure and is informational under
ADR-060 §2. The paired run `docs/evidence/s04-20260803T103655Z/` is the gating
failure that preceded it (`12-mypy`), kept because a repair is only evidence
alongside what it repaired.

Suite result: **22 passed, 5 failed, 2 xfailed, 2224 deselected**.

### Three runs, one morning, one machine

| Benchmark | Budget | 07:38:39Z | 08:40:14Z | **10:45:56Z** |
|---|---|---|---|---|
| Database query p95 | < 3 ms | 1.021 ms ✓ | 1.070 ms ✓ | **1.228 ms** ✓ |
| End-to-end read p95 | < 3 ms | 2.429 ms ✓ | 3.750 ms ✗ | **4.862 ms** ✗ |
| End-to-end write p95 | < 5 ms | 4.501 ms ✓ | 5.726 ms ✗ | **6.058 ms** ✗ |
| Bulk append, 10,000 rows | < 500 ms | 69.9 ms ✓ | 76.0 ms ✓ | **75.3 ms** ✓ |
| `money add` | 0.500 µs | 0.572 µs ✗ | 0.573 µs ✗ | **0.581 µs** ✗ |
| `money mul` | 0.500 µs | 0.589 µs ✗ | 0.604 µs ✗ | **0.580 µs** ✗ |
| 2000-day range iteration | < 1000 µs | passed ✓ | passed ✓ | **failed** ✗ |

The end-to-end read has now drifted from 2.429 ms to 4.862 ms — **twice its
morning figure, against an unchanged 3 ms budget** — across three runs of the
same commit lineage on the same laptop. The slice measured here is a pure text
domain: no repository, no session, no ORM, no query. The write budget drifted
the same direction over the same hours.

Meanwhile `money add` moved 0.572 → 0.573 → 0.581 µs and `money mul` moved
0.589 → 0.604 → 0.580 µs. The CPU-bound pair is flat; the database-bound pair
walked steadily upward while the machine did other work.

That contrast is the finding. A budget whose measurement doubles over a morning
without a line of relevant code changing is not measuring the code, and no
amount of optimisation would have moved it. ADR-060 §1 put enforcement on Linux
CI for exactly this reason, and three same-day E2 runs now demonstrate it more
plainly than the original two-environment comparison did.

The 2000-day range iteration failed here having passed in both earlier runs
today, which is the same story in a fourth place.

No budget adjusted, no Money or benchmark code touched, and nothing in the news
domain reaches any of these paths. All seven remain unverified on the
authoritative Linux environment.

---

## E2 — canonical measurement (2026-08-03T17:09:08Z, commit `2838374` + the uncommitted news-archive slice)

Evidence: `docs/evidence/s04-20260803T170908Z/`. `GATING: PASS`, shell exit
status 0; `50-benchmarks` is the only failure and is informational under
ADR-060 §2. The paired run `docs/evidence/s04-20260803T164517Z/` is the gating
failure that preceded it — strict mypy plus three stale migration-head
assertions — kept because a repair is only evidence alongside what it repaired.

Suite result: **23 passed, 4 failed, 2 xfailed, 2262 deselected**.

### The drift continues, and it is still not the code

| Benchmark | Budget | 07:38Z | 08:40Z | 10:45Z | **17:09Z** |
|---|---|---|---|---|---|
| Database query p95 | < 3 ms | 1.021 ✓ | 1.070 ✓ | 1.228 ✓ | **1.790 ms** ✓ |
| End-to-end read p95 | < 3 ms | 2.429 ✓ | 3.750 ✗ | 4.862 ✗ | **6.023 ms** ✗ |
| End-to-end write p95 | < 5 ms | 4.501 ✓ | 5.726 ✗ | 6.058 ✗ | **7.992 ms** ✗ |
| Bulk append, 10,000 rows | < 500 ms | 69.9 ✓ | 76.0 ✓ | 75.3 ✓ | **82.8 ms** ✓ |
| `money add` | 0.500 µs | 0.572 ✗ | 0.573 ✗ | 0.581 ✗ | **0.584 µs** ✗ |
| `money mul` | 0.500 µs | 0.589 ✗ | 0.604 ✗ | 0.580 ✗ | **0.598 µs** ✗ |

Four runs across one day. The end-to-end read has gone 2.429 → 3.750 → 4.862 →
**6.023 ms** against an unchanged 3 ms budget: **2.5× its morning figure**, and
monotonic. The write followed the same curve. Meanwhile `money add` moved 0.572
→ 0.584 µs and `money mul` 0.589 → 0.598 µs — flat to within a hundredth of a
microsecond across the same twelve hours.

The database-bound pair drifts; the CPU-bound pair does not. That is not a
property of code that changed between runs — this slice adds three empty tables
and a repository nothing else calls. It is a laptop accumulating work over a
day, seen through Docker Desktop's transport. A budget whose measurement grows
2.5× while the machine stays up is measuring the machine.

The 2000-day range iteration passed again here, having failed at 10:45 and
passed at 07:38 and 08:40. Same story, fourth place.

No budget adjusted, no Money or benchmark code touched. All six remain
unverified on the authoritative Linux environment.

---

## E2 — canonical measurement (2026-08-06T14:20:33Z, commit `d29af6b` + the uncommitted GDELT slice)

Evidence: `docs/evidence/s04-20260806T142033Z/`. `GATING: PASS`, shell exit
status 0; `50-benchmarks` is the only failure and is informational under
ADR-060 §2.

**Environment note.** This run used local host port **55632** rather than the
script's default 55432, because Windows currently reserves TCP 55411–55510. The
committed `scripts/canonical_validation.ps1` was *not* modified — the owner
ran a temporary local copy, since deleted. Making the port configurable is
tracked as a
follow-up, not folded into a feature commit.

Suite result: **23 passed, 4 failed, 2 xfailed, 2313 deselected**.

### The variance is now unmistakable

| Benchmark | Budget | 07:38Z 08-03 | 10:45Z 08-03 | 17:09Z 08-03 | **14:20Z 08-06** |
|---|---|---|---|---|---|
| Database query p95 | < 3 ms | 1.021 ✓ | 1.228 ✓ | 1.790 ✓ | **1.150 ms** ✓ |
| End-to-end read p95 | < 3 ms | 2.429 ✓ | 4.862 ✗ | 6.023 ✗ | **2.910 ms** ✓ |
| End-to-end write p95 | < 5 ms | 4.501 ✓ | 6.058 ✗ | 7.992 ✗ | **15.811 ms** ✗ |
| Bulk append, 10,000 rows | < 500 ms | 69.9 ✓ | 75.3 ✓ | 82.8 ✓ | **68.3 ms** ✓ |
| `money add` | 0.500 µs | 0.572 ✗ | 0.581 ✗ | 0.584 ✗ | **0.591 µs** ✗ |
| `money mul` | 0.500 µs | 0.589 ✗ | 0.580 ✗ | 0.598 ✗ | **0.594 µs** ✗ |

The end-to-end read **came back inside its budget** at 2.910 ms after three runs
outside it, peaking at 6.023 ms. In the same run the write went the other way
and
reached **15.811 ms — 3.5× its budget and roughly double its worst previous
figure**. Two database budgets moving in opposite directions in a single run,
three days after the last one, is not a signal about code.

Across all four runs `money add` has moved 0.572 → 0.591 µs and `money mul`
0.589 → 0.594 µs. Nineteen and five thousandths of a microsecond, against a
database read that has ranged 2.4–6.0 ms and a write that has ranged
4.5–15.8 ms.

The 2000-day range iteration failed here having passed at 17:09.

Nothing in the slice under test touches any of these paths: the GDELT adapter is
an HTTP transport, a pure text mapper and an orchestration class, with no schema
change and no new dependency. No budget adjusted, no Money or benchmark code
touched. All remain unverified on the authoritative Linux environment.

## E2 — canonical measurement (2026-08-06T15:42:07Z, commit `3823739`)

Evidence: `docs/evidence/s04-20260806T154207Z/`. `GATING: PASS`, shell exit
status 0; `50-benchmarks` is the only failure and is informational under
ADR-060 §2. First canonical run of a commit rather than of an uncommitted
slice — the news-workflow commit was already pushed when this ran.

**Environment note.** Local host port **55632** again, because Windows still
reserves TCP 55411–55510. The committed `scripts/canonical_validation.ps1` was
*not* modified; the owner ran an untracked temporary copy and deleted it
afterwards. This is the second run to need the workaround, which is what
promoted the port repair from a note to the next slice.

Suite result: **23 passed, 4 failed, 2 xfailed, 2486 deselected**.

### Five runs, and the pattern has not changed

| Benchmark | Budget | 07:38Z 08-03 | 10:45Z 08-03 | 17:09Z 08-03 | 14:20Z 08-06 | **15:42Z 08-06** |
|---|---|---|---|---|---|---|
| Database query p95 | < 3 ms | 1.021 ✓ | 1.228 ✓ | 1.790 ✓ | 1.150 ✓ | **0.929 ms** ✓ |
| End-to-end read p95 | < 3 ms | 2.429 ✓ | 4.862 ✗ | 6.023 ✗ | 2.910 ✓ | **8.167 ms** ✗ |
| End-to-end write p95 | < 5 ms | 4.501 ✓ | 6.058 ✗ | 7.992 ✗ | 15.811 ✗ | **23.517 ms** ✗ |
| Bulk append, 10,000 rows | < 500 ms | 69.9 ✓ | 75.3 ✓ | 82.8 ✓ | 68.3 ✓ | **84.6 ms** ✓ |
| `money add` | 0.500 µs | 0.572 ✗ | 0.581 ✗ | 0.584 ✗ | 0.591 ✗ | **0.560 µs** ✗ |
| `money mul` | 0.500 µs | 0.589 ✗ | 0.580 ✗ | 0.598 ✗ | 0.594 ✗ | **0.594 µs** ✗ |

Both database round trips reached their **worst figures yet** — the read at
8.167 ms after coming back inside budget at 2.910 ms three hours earlier, the
write at 23.517 ms against a 5 ms budget. Three hours, same machine, same
Python 3.12.13, and the write moved 15.8 → 23.5 ms while the query moved
1.150 → 0.929 ms. A measurement where two numbers on the same connection pool
disagree about direction is measuring the machine.

Meanwhile `money add` **improved** to 0.560 µs, its best of the five runs, and
`money mul` landed on 0.594 µs for the second time. Across five runs those two
have occupied a 31- and 18-nanosecond band respectively while the write has
ranged 4.5–23.5 ms. They are stable and they are over budget; the budget is the
thing that has not been re-measured on the authoritative environment.

The 2000-day range iteration **passed** here, having failed at 14:20Z. That is
the third time it has changed verdict without a code change, and it is the
cleanest single illustration of why ADR-060 §2 makes these informational on
Windows.

Nothing in the commit under test touches any of these paths. The news workflow
adds a pure phrase planner, a string builder, an orchestration rule, a renderer
and a CLI; it adds no schema change, no migration, no dependency and no query.
No budget adjusted, no Money or benchmark code touched. All six remain
unverified on the authoritative Linux environment.

---

## How to append

After a canonical run, add a new dated section rather than editing an existing
one. If a target changes, add a row to that subsystem's **revisions** table with
the reason. A baseline that is edited in place cannot answer "was this always
slow, or did we make it slow?" — which is the only question it exists to answer.
