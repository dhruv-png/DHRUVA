# S03 — Domain Primitives & Shared Kernel

| Field | Value |
|---|---|
| Subsystem | S03 |
| Phase | P0 — Platform Kernel |
| Stage | 1 (MCP) |
| Depends on | S02 |
| Blocks | S04 and every subsystem thereafter |
| Complexity | M → **L** (see §2.4) |
| Estimate | 5 → **7 sessions** |
| Risk | **CRIT** — see §1.2 |
| Status | **COMPLETE — awaiting final approval** |

---

## 1. Overview

### 1.1 What this is

The vocabulary every other subsystem speaks: money, prices, quantities, trading
days, time, and the base types for domain events and invariant enforcement.

Roughly forty modules will import these types. A backtest will construct millions
of them. Every rupee the platform ever reports passes through `Money`.

### 1.2 Why this is the highest-risk subsystem in Phase 0

S01 and S02 were risk `LOW`. S03 is `CRIT`, for a reason worth stating plainly:

**Its defects are silent and its cost of change is superlinear.** A wrong
boundary rule fails the build. A wrong `Money` rounding policy produces plausible
numbers that are quietly wrong, and by S12 the cost engine, by S24 the P&L, and
by S30 every backtest inherit the error. Nothing crashes. The reports simply do
not match a contract note, and the first person to notice may be the Product
Owner reconciling a real trade months later.

This is the subsystem where "prioritise correctness over speed" is not a
preference but the only defensible engineering position.

### 1.3 What changed during architecture

Seven decisions surfaced that the plan did not anticipate. **One supersedes an
approved ADR.** All are presented in §8 for Design Review before any code is
written, per the Product Owner's standing instruction.

The most consequential finding: **paise are not enough precision.** The plan's
ADR-005 fixes money at integer paise, which is correct for equities and options.
Currency derivatives on NSE quote to four decimal places — USD/INR ticks at
₹0.0025. A `Price` type that cannot represent a quarter-paise would silently
round every currency-derivative price, and the error would be invisible until
someone reconciled a position. §8 proposes the fix.

## 2. Responsibilities

### 2.1 Owns

| Concept | Type |
|---|---|
| Monetary amount | `Money` |
| Price per unit | `Price` |
| Order/position size | `Quantity`, `SignedQuantity`, `Side` |
| Proportions and rates | `Ratio`, `BasisPoints` |
| Rounding rules | `RoundingPolicy` and the named Indian-market policies |
| Instrument identity | `InstrumentId` |
| Market session date | `TradingDay` + `TradingCalendar` port |
| Half-open intervals | `DateRange`, `TimeRange` |
| Time source | `Clock` port, `SystemClock`, `FrozenClock` |
| Domain events | `DomainEvent` base, envelope fields |
| Invariant enforcement | `invariant()` guard helper |

### 2.2 Does not own

- Currency conversion — no multi-currency trading is planned; `Currency` exists
  as a type so that `Money` is not silently INR-only, but conversion is out of scope.
- Instrument *metadata* (lot size, tick size, expiry) → **S07**. S03 owns the
  identity, not the attributes.
- The trading calendar *implementation* → **S08**. S03 owns the port.
- Persistence codecs → **S04**. Value objects know nothing of SQLAlchemy.
- Serialisation adapters — same reasoning as ADR-031's precedent: adapters live
  at the boundary, not on the type.

### 2.3 Explicitly reconsidered and dropped

**`Result`.** The plan lists a `Result` type in S03's scope. Adding it now would
give the codebase two error-signalling idioms alongside the ADR-038 exception
taxonomy, and the boundary between them would be relitigated in every review for
the next five years. Proposed for removal in **ADR-047**.

### 2.4 Why the estimate grew from 5 to 7 sessions

Three additions, none of which were visible from the roadmap:

1. Sub-paise price precision (§1.3) requires a scale-aware design rather than a
   single integer scale — roughly one extra session.
2. Property-based testing of monetary invariants is not optional here. Money that
   is right on the twelve cases someone thought of is not money that is right.
3. A new boundary rule (R6) and its self-tests.

## 3. Functional Requirements

| # | Requirement | Verified by |
|---|---|---|
| FR-01 | `Money` arithmetic is exact — no representable-value loss under any sequence of add/subtract | property test |
| FR-02 | Constructing `Money` from a `float` raises. Always. | unit + boundary rule R6 |
| FR-03 | `Money + Money` requires identical currency; mismatch raises | unit |
| FR-04 | `Money × Money` is not expressible — no such operation exists | type check + unit |
| FR-05 | `Price × Quantity → Money`, with an explicitly supplied rounding policy | unit |
| FR-06 | `Price` represents at least 4 decimal places (currency derivatives) | unit |
| FR-07 | Division and rate application require an explicit `RoundingPolicy`; there is no default | unit |
| FR-08 | `Money.allocate()` distributes an amount across weights losing **zero** minor units | property test |
| FR-09 | `Quantity` is unsigned; a negative value raises | unit |
| FR-10 | `SignedQuantity` exists separately, for position deltas only | unit |
| FR-11 | `TradingDay` cannot be constructed without a calendar | unit |
| FR-12 | `TradingDay` arithmetic (`+ n sessions`) goes through the calendar, never `timedelta` | unit |
| FR-13 | `DateRange` and `TimeRange` are half-open `[start, end)`; inverted ranges raise | property test |
| FR-14 | All time is timezone-aware UTC; naive `datetime` raises at the boundary | unit + ruff `DTZ` |
| FR-15 | `Clock` is a port; `datetime.now()` appears nowhere outside its adapters | unit + lint |
| FR-16 | `FrozenClock` supports deterministic advance for backtests | unit |
| FR-17 | Every value object is frozen, hashable, and slotted | unit (reflective) |
| FR-18 | Every value object round-trips through its canonical string form | property test |
| FR-19 | Invariant violations raise a typed `SafetyError`, never `AssertionError` | unit |
| FR-20 | No module in `shared/money` references `float` | **boundary rule R6** |

## 4. Architecture

### 4.1 The dimensional model

The central idea: **these are physically different quantities, and the type system
should refuse to mix them.**

```
        Quantity  ──────┐
       (unsigned int)   │
                        ├──  Price × Quantity ── round ──▶  Money
        Price  ─────────┘                                    │
    (int µ-units, 6dp)                                        │
                                                              │
        Ratio / BasisPoints  ──── Money × Ratio ── round ──────┤
                                                              │
                                          Money ± Money ───────┘
                                          Money × int
```

**Operations that exist:**

| Expression | Result | Notes |
|---|---|---|
| `Money + Money` | `Money` | same currency enforced |
| `Money × int` | `Money` | exact, no rounding needed |
| `Money ÷ int` | `Money` | **requires** a `RoundingPolicy` |
| `Money.apply(Ratio)` | `Money` | **requires** a `RoundingPolicy` |
| `Money.allocate([weights])` | `list[Money]` | lossless — see §4.4 |
| `Price × Quantity` | `Money` | **requires** a `RoundingPolicy` |
| `Price − Price` | `Price` | a spread is still a price |
| `Money ÷ Money` | `Ratio` | dimensionless |

**Operations that deliberately do not exist:** `Money × Money`,
`Price × Price`, `Money + Price`, `Money + int`. Each is a category error that
today would be caught only by a reviewer noticing an implausible number.

### 4.2 Representation

| Type | Internal | Scale | Why |
|---|---|---|---|
| `Money` | `int` minor units | currency-defined (INR: 2) | Exact, fast, and identical to what `BIGINT` stores |
| `Price` | `int` micro-units | **6 dp**, fixed | Covers equities (2dp), options (2dp) and currency derivatives (4dp) with headroom |
| `Quantity` | `int` | 0 dp | Shares and contracts are integers; lots are an instrument attribute (S07) |
| `Ratio` | `Decimal` | arbitrary | Applied per trade, not per tick — clarity beats speed here |

`Decimal` remains the **parsing and formatting** type. It never carries state.

### 4.3 Why not `Decimal` internally

ADR-005 specifies `Money` wrapping `Decimal`. Architecture surfaced three
problems, and **ADR-042 proposes superseding it** (§8):

1. **Correctness.** `Decimal` arithmetic depends on an ambient, thread-local
   context. Two processes with different `getcontext().prec` produce different
   results from identical inputs — which is a determinism defect (ADR-011's
   concern, one layer down) and would be near-impossible to diagnose.
2. **Fidelity to storage.** We persist `BIGINT` paise. An `int` needs no
   conversion, so no conversion can be wrong.
3. **Speed.** `Decimal` is roughly an order of magnitude slower than `int`. A
   backtest performs millions of monetary operations; this is measurable, and
   §6 sets a budget for it.

ADR-005's *intent* — exactness, no float, `BIGINT` paise storage — is fully
preserved. Only the internal representation changes.

### 4.4 Lossless allocation

Splitting ₹100.00 three ways gives ₹33.33 × 3 = ₹99.99. One paisa vanishes.

Across a platform apportioning brokerage across legs, STT across fills, and P&L
across lots, vanishing paise accumulate into reconciliation failures that are
genuinely painful to trace.

`Money.allocate(weights)` uses largest-remainder distribution: the sum of the
parts **always** equals the whole, exactly, for any weights. This is asserted by
property test over arbitrary amounts and weight vectors — not by three examples.

### 4.5 Rounding

There is **no default rounding anywhere**. Every operation that can lose
precision takes an explicit `RoundingPolicy`.

Policies are named after the rule they implement, not after the mathematical
mode, because the rule is what a reviewer needs to check against a circular:

```
RoundingPolicy.STT_NEAREST_RUPEE        # STT rounds to the nearest rupee
RoundingPolicy.CHARGE_TO_PAISE          # brokerage, exchange fees: 2dp
RoundingPolicy.GST_TO_PAISE             # GST on charges: 2dp
RoundingPolicy.PRICE_TO_TICK            # instrument tick size (S07 supplies it)
RoundingPolicy.CONSERVATIVE_TO_TRADER   # rounds against us; for pre-trade estimates
```

`CONSERVATIVE_TO_TRADER` deserves a note. When *estimating* cost before a trade,
rounding in our favour produces an optimistic estimate, and optimistic cost
estimates are how a strategy appears profitable and is not. Estimates round
against us; settlement uses the exchange's actual rule.

### 4.6 TradingDay and the calendar port

A `TradingDay` that cannot verify it is a trading day is a lie the type tells.

The port lives in the shared kernel; S08 implements it in the Reference context.
`TradingDay` cannot be constructed without one:

```
TradingDay.of(date, calendar)      # raises if not a session
calendar.next_session(trading_day)
calendar.sessions_between(a, b)
```

`TradingDay + timedelta` does not exist. Session arithmetic goes through the
calendar or not at all. This matters more than it looks: the NSE/BSE expiry
regime changed on 2025-09-01, and any backtest spanning it that treats sessions
as calendar arithmetic is wrong in a way nothing will flag.

### 4.7 Invariants

```python
invariant(condition, "message", **context)   # raises InvariantViolation (a SafetyError)
```

A guard rather than `assert`, for one decisive reason: `python -O` strips
`assert`. An invariant that disappears under an optimisation flag is not an
invariant, and the one deployment where someone sets `-O` is exactly the one
where it mattered.

## 5. Database Changes

None. S03 defines types; S04 defines how they persist.

The persistence contract each type must satisfy is specified now so S04 inherits
it: `Money` → `BIGINT` + `CHAR(3)`; `Price` → `BIGINT` (µ-units);
`Quantity` → `INTEGER`; `TradingDay` → `DATE`; `InstrumentId` → `UUID`;
all timestamps → `TIMESTAMPTZ` in UTC.

## 6. Performance Budgets (ADR-036)

These primitives sit inside every loop in the platform. A backtest of one year of
1-minute bars across 50 instruments performs on the order of 10⁸ monetary
operations.

| Budget | Target | Why this number | Benchmark |
|---|---|---|---|
| `Money + Money` | **< 0.5 µs** | 10⁸ operations must not dominate a 60 s backtest budget (plan §12) | `bench_money_arithmetic` |
| `Money × int` | < 0.5 µs | same path | `bench_money_arithmetic` |
| `Money` construction from minor units | < 0.3 µs | constructed more often than operated on | `bench_money_construction` |
| `Price × Quantity → Money` | < 2 µs | includes an explicit rounding step | `bench_notional` |
| `Money.apply(Ratio)` | < 10 µs | `Decimal` path; per-trade, not per-tick | `bench_charges` |
| `Money.allocate`, 10 weights | < 20 µs | per-fill apportionment | `bench_allocate` |
| Parse `Money` from `"1234.56"` | < 5 µs | ingestion boundary only | `bench_parsing` |
| `TradingDay` compare / hash | < 0.3 µs | used as a dict key throughout | `bench_trading_day` |
| `Clock.now()` | < 1 µs | called per event | `bench_clock` |
| **1,000,000 `Money` additions** | **< 0.5 s** | the backtest-relevant aggregate | `bench_money_throughput` |
| `sys.getsizeof(Money)` | ≤ **64 bytes** | 10⁶ live instances must not dominate memory | `bench_memory` |

## 7. Folder Structure

```
backend/src/dhruva/shared/
├── money/
│   ├── __init__.py
│   ├── currency.py        Currency, minor-unit scale
│   ├── money.py           Money
│   ├── price.py           Price
│   ├── quantity.py        Quantity, SignedQuantity, Side
│   ├── ratio.py           Ratio, BasisPoints
│   └── rounding.py        RoundingPolicy and the named policies
├── identity.py            InstrumentId and other surrogate identifiers
├── time/
│   ├── __init__.py
│   ├── clock.py           Clock port, SystemClock, FrozenClock
│   ├── trading_day.py     TradingDay, TradingCalendar port
│   └── ranges.py          DateRange, TimeRange
├── events.py              DomainEvent base
└── invariants.py          invariant(), InvariantViolation

backend/tests/unit/shared/money/        (one module per type)
backend/tests/unit/shared/time/
backend/tests/benchmarks/test_primitives.py
```

## 8. Architecture Decisions (all approved at Design Review)

ADR-042 (money as integer minor units, superseding ADR-005) · ADR-043
(dimensional typing) · ADR-044 (explicit named rounding) · ADR-045 (unsigned
quantity, explicit side) · ADR-046 (calendar-verified trading day) · ADR-047
(exceptions only, no Result) · ADR-048 (rule R6, no float in monetary modules) ·
ADR-049 (mutation testing) · ADR-050 (shared kernel API-stable).

## 9. Measured Benchmark Results (ADR-036)

**Environment.** Intel Core i5-10300H @ 2.50 GHz, 2 vCPU, 3 GB RAM, Linux
6.8.0, **CPython 3.10.12**. The target runtime is 3.12; 3.11 and 3.12 carry
substantial interpreter speedups on exactly this kind of small-object arithmetic,
so every figure below is **conservative**.

**Methodology.** Microsecond-scale operations use best-of-batched-means: an inner
loop of N calls timed once, repeated R times, minimum taken. A per-iteration
`perf_counter` costs a meaningful fraction of what it measures, and a
per-iteration p99 reports the garbage collector rather than the code. The
million-addition figure is a single wall-clock measurement and also asserts the
arithmetic is correct, so a fast wrong answer cannot pass.

| Budget | Target | Measured | Status |
|---|---|---|---|
| `Money + Money` | < 0.50 µs | **0.456 µs** | PASS |
| `Money × int` | < 0.50 µs | **0.455 µs** | PASS |
| `Money` construction | < 0.30 µs | **0.374 µs** | **MISS (25%)** |
| `Price × Quantity → Money` | < 2.00 µs | **1.149 µs** | PASS |
| `Money.apply(Ratio)` | < 10.00 µs | **2.678 µs** | PASS |
| `Money.allocate`, 10 weights | < 20.00 µs | **8.215 µs** | PASS |
| `Money.parse` | < 5.00 µs | **2.230 µs** | PASS |
| `TradingDay` compare + hash | < 1.20 µs | **0.265 µs** | PASS |
| `SystemClock.now()` | < 1.00 µs | **0.293 µs** | PASS |
| `FrozenClock.now()` | < 1.00 µs | **0.055 µs** | PASS |
| `InstrumentId.deterministic` | < 10.00 µs | **5.851 µs** | PASS |
| 1,000,000 `Money` additions | < 0.50 s | **0.545 s** | **MISS (9%)** |
| `sizeof(Money)` | ≤ 64 B | **48 B** | PASS |

**11 of 13 pass. Two miss, both marginally, both recorded rather than dropped
(ADR-036).** They are the same measurement seen twice: construction dominates the
million-addition loop, since each addition builds a `Money`.

### What the benchmarks found

The first run was not marginal — it was **3.7× over budget**, and the cause was a
real defect rather than an unlucky threshold.

`invariant(condition, message, **context)` evaluates every argument eagerly. On
the addition path that meant building an f-string and calling `str()` twice on
**every single addition**, before checking anything. Rewriting the hot-path guards
as explicit `if ... raise` branches, and moving the failure message into a cold
method, took `Money + Money` from **1.856 µs to 0.456 µs** and the million-add
loop from **2.007 s to 0.545 s**.

That defect was invisible to every other gate. Coverage was 99%, types were
clean, the architecture conformed, and the tests all passed. Only a measured
budget surfaced it — which is the argument for ADR-036 in a single example.

## 10. Mutation Testing (ADR-049) — NOT COMPLETED

**Status: blocked in this environment. No score is reported, because no honest
score is available.**

Three attempts, each blocked for a different reason:

| Attempt | Outcome |
|---|---|
| `mutmut` 2.5.1 | Depends on `parso`, which cannot parse the `match` statement in `rounding.py`. 15 parse errors. Structurally blocked. |
| `mutmut` 3.6.0 | Parses correctly, but executes tests from a copied `mutants/` directory in a way that conflicts with the Python 3.10 compatibility shim this sandbox requires. |
| Purpose-built harness | Written and verified working — individual mutants are correctly killed — but each mutant requires a full test-suite run, and the environment enforces a 45-second ceiling per command with background processes terminated. |

Two genuine findings came out of the attempt, and both are kept:

1. **Hypothesis shrinking dominates mutation cost.** A killed mutant makes a
   property test *fail*, and Hypothesis then spends seconds minimising the
   counterexample — valuable for a human, pure overhead for a harness that only
   needs an exit code. Excluding the shrink phase in the `fast` profile cut
   per-mutant cost by more than an order of magnitude.
2. **An in-place mutation harness is dangerous.** The first version wrote mutants
   over the original source with a `try/finally` restore. A timeout killed the
   process before the `finally` ran, leaving a mutated, comment-stripped
   `money.py` on disk. It was caught within minutes by a failing test and
   recovered from git. The harness now mutates a **scratch copy of the tree**,
   which makes that failure mode impossible rather than merely unlikely.

**To run it on a machine without a command-time ceiling:**

```bash
cd DHRUVA
export PYTHONPATH="$PWD/backend/src"
python tools/mutation_harness.py \
    --package src/dhruva/shared/money \
    --tests tests/unit/shared/money/ --sample 40
python tools/mutation_harness.py \
    --package src/dhruva/shared/time \
    --tests tests/unit/shared/time/ --sample 40
```

Expected runtime: roughly 10–20 minutes per package at `--sample 40`; a couple of
hours exhaustive. Once a Python 3.12 host is available, `mutmut` replaces the
harness and the harness is deleted — it exists only because the intended tool
cannot run here.

**This is the one Definition-of-Done item S03 does not satisfy.** It is recorded
as TD-12 at HIGH priority and is a stated exception in the approval request rather
than a silent omission.

## 11. Documentation

`docs/DOMAIN.md` (719 lines) explains the reasoning behind every primitive,
including a Mermaid domain map and twelve worked examples of financial
programming mistakes this design prevents. ADR-042 … ADR-050 written; ADR-005
marked superseded; `docs/decisions.md` regenerated at 50 records.

## 12. Technical Debt Register (ADR-041)

| # | Item | Priority | Risk | Impact if unresolved | Trigger for removal | Planned subsystem |
|---|---|---|---|---|---|---|
| TD-12 | Mutation testing not executed | **HIGH** | Test suite may assert less than coverage implies | The primitives forty subsystems trust are unverified against the one gate designed to check assertions | A machine without a per-command time ceiling | Before **G0** sign-off |
| TD-13 | `Money` construction 0.374 µs vs 0.30 µs budget | MEDIUM | Low | ~25% over on the hottest constructor; compounds in long backtests | Re-measure on Python 3.12 | S04 |
| TD-14 | 1M additions 0.545 s vs 0.50 s budget | MEDIUM | Low | Same cause as TD-13, seen in aggregate | Re-measure on Python 3.12 | S04 |
| TD-01 | Canonical `uv.lock` not generated | HIGH | Supply chain | Hash-pinned lockfiles give the same guarantee meanwhile | Python 3.12 host | Before G1 |
| TD-02 | Validation runs on Python 3.10 with a shim | HIGH | Correctness | Source targets 3.12; behaviour differences would not be caught | Python 3.12 host | Before G1 |
| TD-15 | `TradingDay` bare constructor is reachable | LOW | Misuse | Calendar implementations need it; `of()` is the documented route | A language mechanism for friend-scoped construction | **ACCEPTED** |
| TD-16 | `Ratio` uses `Decimal`, not integers | LOW | Determinism | Decimal context sensitivity remains in the rate path, applied per trade rather than per tick | Evidence of a discrepancy or a hot-path need | **ACCEPTED** |
| TD-17 | Currency conversion unimplemented | LOW | Scope | Single-currency platform; `Currency` exists so the schema is ready | Multi-currency portfolio requirement | **ACCEPTED** |
| TD-05 | Container images not digest-pinned | MEDIUM | Supply chain | Mutable tags can drift | First deployed environment | S10 |
| TD-08 | Startup-to-ready and RSS budgets unmeasured | MEDIUM | Low | Meaningless until a database connection exists | Real dependency to connect to | S04 |
| TD-09 | Trace context does not cross process boundaries | MEDIUM | Observability | Correlation breaks at the bus | Event envelope exists | S05 |

**Eleven open, four accepted.** Three HIGH items — TD-01, TD-02 and TD-12 — share
a single trigger: access to a machine that is not this sandbox.

### How this could silently be wrong

- **Mutation testing has not run** (TD-12). Every claim about test *quality* in
  this subsystem rests on coverage and on reading, neither of which detects a
  test that executes without asserting.
- **Benchmarks measure a 3.10 interpreter.** The absolute figures are
  conservative, but the *ratios* between operations could shift on 3.12, and an
  optimisation that helps here might not help there.
- **`Decimal` remains in the rate path.** ADR-042 removed context sensitivity
  from `Money`; `Ratio` still has it. Applied per trade rather than per tick, so
  the exposure is small — but it is not zero, and it is recorded rather than
  forgotten.
- **The dimensional model is only as good as its adoption.** Nothing forces a
  future subsystem to use `Money` rather than an `int` of paise. Rule R6 bans
  floats inside the kernel; it does not compel the rest of the platform to use
  the kernel.
