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
| Status | **STEP 1 COMPLETE — awaiting Design Review (Step 2). No implementation started.** |

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

## 8. Proposed Architecture Decisions — FOR DESIGN REVIEW

**No implementation begins until these are approved.** One supersedes an
approved ADR and is flagged accordingly.

### ADR-042 — Money is an integer count of minor units *(supersedes ADR-005)*

**Proposed.** `Money` holds `int` minor units plus a `Currency`. `Decimal` is a
parsing and formatting type only. `Price` holds `int` micro-units at a fixed
6-decimal scale.

*Why it supersedes rather than amends:* ADR-005 states `Money` wraps `Decimal`,
and ADR-027 forbids editing an accepted record. ADR-042 restates ADR-005's still
valid clauses — no float, `BIGINT` minor units in storage, `Money` as the single
monetary type — so nothing is lost, and ADR-005 becomes
`Superseded by ADR-042` with its body intact as history.

*What forced it:* `Decimal`'s thread-local context makes results
environment-dependent, which is a determinism defect. And paise alone cannot
represent a currency-derivative tick of ₹0.0025.

### ADR-043 — Dimensional typing: Money, Price, Quantity and Ratio are distinct

**Proposed.** Operations are defined only where they are dimensionally
meaningful. `Money × Money` and `Money + Price` do not exist.

### ADR-044 — Rounding is always explicit and named after the rule it implements

**Proposed.** No default rounding mode anywhere. Policies named for the Indian
market rule they encode, including `CONSERVATIVE_TO_TRADER` for pre-trade
estimates.

### ADR-045 — Quantity is unsigned; direction is a separate `Side`

**Proposed.** `Quantity` rejects negatives. `Side` is an explicit enum.
`SignedQuantity` exists solely for position deltas. Eliminates the
"negative quantity means sell" convention, which is the kind of implicit rule
that survives right up until someone forgets it.

### ADR-046 — `TradingDay` cannot be constructed without a calendar

**Proposed.** The `TradingCalendar` port lives in the shared kernel; S08
implements it. No `timedelta` arithmetic on trading days.

### ADR-047 — Exceptions are the single error-signalling mechanism; no `Result` type

**Proposed.** Removes `Result` from S03's scope as listed in the plan. Two
idioms would mean a boundary question in every review for years.

### ADR-048 — Boundary rule R6: no `float` in the monetary modules

**Proposed.** The boundary checker fails the build if any module under
`dhruva.shared.money` references `float` — as an annotation, a call, or a
literal. Analytics may legitimately use floats for implied volatility and greeks;
settled cash never may.

## 9. Open Questions for the Product Owner

| # | Question | Why it matters now | My recommendation |
|---|---|---|---|
| Q1 | Should `Price` carry 6 decimal places universally, or a per-instrument scale? | Per-instrument is more precise but makes `Price` depend on S07, inverting the dependency. | **Fixed 6 dp.** Covers every Indian instrument with headroom; tick-size validation stays in S07 where the metadata lives. |
| Q2 | Should `Money` be currency-parameterised in the type system (`Money[INR]`)? | Catches cross-currency errors at type-check time rather than runtime. | **No.** No multi-currency trading is planned; the generic machinery would cost readability permanently for a runtime check that already exists. |
| Q3 | Should `Quantity` know about lot sizes? | F&O trades in lots; a bare integer invites "42 contracts" where 42 lots was meant. | **No, but** `Quantity` gets a `lots(n, lot_size)` named constructor so the conversion is explicit and greppable. Lot size itself stays in S07. |

---

*Step 1 complete. Steps 3 onward — implementation, review, testing, optimisation,
documentation, git history, summaries, debt register, lessons learned — follow
Design Review approval.*
