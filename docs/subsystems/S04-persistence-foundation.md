# S04 — Persistence Foundation

| Field | Value |
|---|---|
| Subsystem | S04 |
| Phase | P0 — Platform Kernel |
| Stage | 1 (MCP) |
| Depends on | S02, S03 |
| Blocks | S05 and every subsystem that stores anything |
| Complexity | L → **XL** (see §2.4) |
| Estimate | 6 → **9 sessions** |
| Risk | **CRIT** |
| Status | **STEP 3 — IMPLEMENTATION** (design approved with two amendments, 2026-07-26) |

---

## 1. Overview

### 1.1 What this is

The layer that turns domain objects into rows and back, and the transaction
discipline that decides when it happens.

It is the first subsystem that touches state which **outlives the process**. S01
through S03 could be rolled back with `git checkout`. From S04 onward a rollback
may need to undo a schema change, and some schema changes cannot be undone at
all. That shift is the reason this subsystem is `CRIT`.

### 1.2 The architectural instruction

The Product Owner's directive is explicit and is the organising constraint of
this design:

```
        Domain Objects          ← knows nothing about storage
              ↑
        Mapping Layer           ← the only place both worlds are named
              ↑
        SQLAlchemy Models       ← knows nothing about the domain
              ↑
        Database
```

**SQLAlchemy models must never become the domain model.** Concretely, this rules
out two patterns that are otherwise common and would each be cheaper to build:

- **Active Record** — domain objects with `.save()`. Rejected outright; it makes
  every domain object depend on a session.
- **Imperative (classical) mapping** — SQLAlchemy's `map_imperatively()`, which
  attaches persistence to domain classes without those classes importing
  SQLAlchemy. This is the pattern *Architecture Patterns with Python* recommends,
  and it is genuinely tempting because it removes the mapping layer entirely.

  It is rejected here anyway, and §8 (ADR-052) sets out why: the domain object
  becomes an *instrumented* object at runtime, carrying a `_sa_instance_state`,
  lazy-loading behaviour, and identity-map semantics that the domain never asked
  for. `Money` would acquire hidden state. A test constructing a domain object
  would behave differently depending on whether a mapper had been configured.
  The domain would be persistence-*ignorant* in its imports and
  persistence-*coupled* in its behaviour, which is the harder kind of coupling to
  notice.

### 1.3 The tension this design has to resolve

A strict mapping layer costs a function call and an object allocation per row.
That is irrelevant for orders and instruments. It is **fatal** for market data:
S10 ingests on the order of 3,000 ticks per second and S11 aggregates millions of
bars.

Mapping every tick through a domain object would make the ingest path 10–50×
more expensive than it needs to be, to no benefit — a tick is not an aggregate,
has no invariants worth enforcing per row, and is never mutated.

**§8 proposes an explicit, bounded exception (ADR-054) rather than letting one
leak in later under deadline pressure.** Getting this wrong in either direction
is expensive: a domain-mapped tick path would not survive S10, and an
un-bounded exception would erode the whole architecture within a year.

## 2. Responsibilities

### 2.1 Owns

| Concern | Module |
|---|---|
| Engine, session factory, connection pooling | `platform/infrastructure/database/engine.py` |
| Async session lifecycle | `platform/infrastructure/database/session.py` |
| Unit of Work, transaction boundaries | `platform/infrastructure/database/unit_of_work.py` |
| Repository protocol and base | `platform/infrastructure/database/repository.py` |
| SQLAlchemy models (persistence only) | `<context>/infrastructure/models/` |
| Domain ↔ model mappers | `<context>/infrastructure/mappers/` |
| Domain-type SQL column types | `platform/infrastructure/database/types.py` |
| Alembic configuration and migrations | `backend/alembic/` |
| TimescaleDB DDL helpers | `platform/infrastructure/database/timescale.py` |
| Integration-test harness | `backend/tests/integration/conftest.py` |

### 2.2 Does not own

- **Any table for a specific business concept.** S04 provides the machinery and
  exactly one worked example. Instrument tables belong to S07, tick tables to
  S11, order tables to S33.
- **The event bus outbox.** Designed for in §4.7 so S05 inherits a transactional
  outbox rather than retrofitting one, but implemented by S05.
- **Read models and projections** → S11 onward.
- **Connection-level observability dashboards** → S41. S04 registers the metrics.

### 2.3 Explicitly rejected

**SQLite for tests.** It is the obvious way to make integration tests fast, and
it would make them lie. No `TIMESTAMPTZ` semantics, no `BIGINT` overflow
behaviour, no hypertables, different transaction isolation, different constraint
timing. A test suite that passes on SQLite and fails on PostgreSQL is worse than
a slower one that tells the truth (ADR-058).

### 2.4 Why the estimate grew from 6 to 9 sessions

1. The mapping layer is genuine additional work — a model, a mapper and mapper
   tests per aggregate, plus the column types for `Money`, `Price`, `TradingDay`
   and `InstrumentId`.
2. Losing SQLAlchemy's automatic dirty tracking means change tracking must be
   designed explicitly (§4.4). This is the subtlest part of the subsystem.
3. Migration safety (§4.6) and the rollback guarantees the Product Owner asked
   for require an expand/contract discipline and a tested down-path, not just
   `alembic revision --autogenerate`.

## 3. Functional Requirements

| # | Requirement | Verified by |
|---|---|---|
| FR-01 | No module under any `domain/` package imports `sqlalchemy` | **boundary rule R7** |
| FR-02 | Domain objects are constructible and testable with no database present | unit tests, no fixtures |
| FR-03 | Every aggregate has a model, a mapper, and a round-trip property test | property test per aggregate |
| FR-04 | `to_domain(from_domain(x)) == x` for every mapped type | property test |
| FR-05 | A `Money` round-trips through the database with exact minor units | integration test |
| FR-06 | A `Price` round-trips preserving all 8 decimal places | integration test |
| FR-07 | Repositories never commit, never roll back, never begin a transaction | static check + review |
| FR-08 | One use case, one Unit of Work, one transaction | integration test |
| FR-09 | An exception anywhere in a UoW rolls back everything | integration test |
| FR-10 | Nested UoW use is refused rather than silently joining | unit test |
| FR-11 | A session is never shared across concurrent tasks | integration test with `asyncio.gather` |
| FR-12 | Lazy loading raises rather than emitting a query | integration test |
| FR-13 | Every migration has a tested `downgrade`, or is explicitly marked irreversible | migration test harness |
| FR-14 | `alembic upgrade head` then `downgrade base` leaves an empty schema | integration test |
| FR-15 | Autogenerate produces an empty diff against head (models match migrations) | integration test |
| FR-16 | Every domain table carries `account_id NOT NULL` (ADR-004) | schema test |
| FR-17 | Bitemporal tables carry both `event_time` and `recorded_at` (ADR-007) | schema test |
| FR-18 | Hypertable, compression and retention DDL lives in migrations, not models | schema test |
| FR-19 | Integration tests run against real PostgreSQL + TimescaleDB | testcontainers |
| FR-20 | Integration tests are order-independent and leave no residue | run suite twice, shuffled |
| FR-21 | Domain events raised by an aggregate publish only after commit succeeds | integration test |
| FR-22 | Connection-pool exhaustion produces a typed error, not a hang | failure-injection test |

## 4. Architecture

### 4.1 The four layers, concretely

```
dhruva.contexts.<ctx>.domain.instrument.Instrument        ← frozen, pure Python
                    ▲
dhruva.contexts.<ctx>.infrastructure.mappers.InstrumentMapper
                    │   to_domain(row) -> Instrument
                    │   to_model(aggregate) -> InstrumentModel
                    ▼
dhruva.contexts.<ctx>.infrastructure.models.InstrumentModel   ← DeclarativeBase
                    ▲
                PostgreSQL
```

The mapper is **the only module that imports both**. That is what makes the
constraint checkable: rule R7 fails the build if a `domain/` module imports
SQLAlchemy, and the mapper package is the sanctioned place for the other
direction.

### 4.2 Domain column types

Persisting a `Money` as two loose columns invites someone to read the integer
without the currency. Custom `TypeDecorator`s keep the pairing intact:

| Domain type | SQL | Notes |
|---|---|---|
| `Money` | `BIGINT` + `CHAR(3)` | Composite; currency never separated from amount |
| `Price` | `BIGINT` | 8-dp scaled units, exactly as held in memory |
| `Quantity` | `INTEGER` | Non-negative constraint at the schema level too |
| `InstrumentId` / `AccountId` | `UUID` | Native type, not text |
| `TradingDay` | `DATE` | Reconstructed through the calendar on read |
| `Ratio` | `NUMERIC(18,8)` | Exact; never `DOUBLE PRECISION` |
| any instant | `TIMESTAMPTZ` | UTC, always (ADR-006) |

`TradingDay` is the interesting one. Reconstructing it requires a calendar, which
the mapper does not have and should not fetch. **Proposed:** the mapper produces
a plain `date` and the *repository* — which can be given a calendar at
construction — lifts it to a `TradingDay`. Raised in §9 as an open question,
because the alternative is a `TradingDay` that skips its own invariant on read.

### 4.3 Unit of Work

```python
async with uow_factory() as uow:              # BEGIN
    instrument = await uow.instruments.get(instrument_id)
    instrument.rename(new_symbol)              # pure domain operation
    await uow.instruments.update(instrument)   # staged, not written
    await uow.commit()                         # COMMIT, then publish events
# rollback on any exception, always
```

Rules, each with a reason:

- **The UoW owns the transaction. Repositories never commit.** A repository that
  commits makes every caller's atomicity guarantee depend on which repository
  methods it happened to call.
- **One use case, one UoW.** Not one per request — a request may run several.
- **Nesting is refused, not joined.** Silently joining an outer transaction means
  an inner `commit()` does nothing, and the caller cannot tell.
- **Events publish after commit, never inside it.** Publishing inside means a
  consumer can observe an event for a transaction that later rolls back.

### 4.4 Change tracking without the ORM's dirty checking

This is the cost of the mapping layer, and the part most likely to be got wrong.

SQLAlchemy's identity map detects mutation of an attached instance automatically.
Detached domain objects give up that mechanism, and the alternatives are:

| Option | Assessment |
|---|---|
| Compare against a snapshot taken at load | Automatic, but doubles memory per loaded aggregate and is a deep comparison on every commit |
| Version-number optimistic locking only | Detects conflicts, does not detect *what changed* |
| **Explicit `repository.update(aggregate)`** | **Proposed.** Verbose, and completely predictable |

Explicit is proposed because the failure mode of implicit tracking is *silence* —
a mutation that is never persisted, discovered later as missing data. The failure
mode of explicit tracking is a forgotten call, which a mapper round-trip test in
the use case will catch. Between a silent failure and a noisy one, take the noisy
one.

Aggregates additionally carry a `version` column for optimistic concurrency:
`UPDATE ... WHERE version = :loaded_version`, zero rows affected means someone
else won, and the caller gets a typed `ConflictError`.

### 4.5 Async session lifecycle

- `async_sessionmaker(expire_on_commit=False)`. Expiry-on-commit would trigger a
  lazy refresh after commit, which is both a query nobody asked for and an error
  on a detached object.
- **One session per Unit of Work.** Never shared across `asyncio` tasks —
  `AsyncSession` is not concurrency-safe, and sharing produces interleaved
  statement errors that are maddening to reproduce.
- **Lazy loading is disabled.** `lazy="raise"` on every relationship. In an async
  context a lazy load is an implicit IO in a place the author did not expect, and
  the N+1 it causes will not show up until production data volumes.
- Pool sizing comes from `DatabaseSettings` (defined in S02, unused until now).
  Exhaustion raises a typed error after a bounded wait rather than blocking
  forever (FR-22).

### 4.6 Migration safety and rollback

The Product Owner asked specifically for rollback guarantees. The honest position
has three parts.

**1. Expand / contract for anything destructive.** A column rename ships as:
add new → backfill → write both → read new → stop writing old → drop old, across
*separate releases*. Each step is independently reversible. A single-release
rename is not.

**2. Every migration declares its reversibility.** `downgrade()` is implemented
and tested, or the migration carries an explicit
`irreversible = "drops data: <reason>"` marker that the test harness recognises.
Silence is not permitted.

**3. Some things genuinely cannot be undone**, and the release notes must say so
rather than implying a clean `git checkout`. Dropping a column loses the data;
restoring the schema does not restore the values. From S04 onward, "rollback"
means *code plus schema*, and the two have different guarantees.

The migration test harness runs `upgrade head` → `downgrade base` → `upgrade head`
against a real database on every CI run, and separately asserts that
`--autogenerate` produces an empty diff, so models and migrations cannot drift.

### 4.7 TimescaleDB

Hypertables, continuous aggregates, compression and retention policies are
**DDL, not model configuration**. They live in migrations, where they are
versioned and reversible, rather than in a declarative model where they would be
invisible to Alembic.

Proposed conventions, to be exercised by S11 rather than S04:

- Chunk interval sized so one chunk fits comfortably in memory — for tick data,
  one day.
- Compression after 30 days, on a segment-by ordering of `instrument_id`.
- Continuous aggregates for 1m → 5m → 15m → 1h → 1d rollups.
- Retention enforced by policy, with cold data exported to Parquet first.

S04 delivers the helpers and one worked hypertable so the pattern is established;
S11 uses it in anger.

### 4.8 The transactional outbox

Domain events must not be lost if the process dies between commit and publish.
The outbox table is created here — written inside the same transaction as the
aggregate change — and drained by S05. Designing it now costs one table and saves
S05 from retrofitting delivery guarantees into an already-working bus.

## 5. Database Changes

The first real schema. S04 creates:

- `alembic_version` (Alembic's own).
- `outbox` — event id, type, payload, `occurred_at`, `published_at`, attempts.
- One worked example table demonstrating every convention: surrogate UUID primary
  key, `account_id NOT NULL`, `version` for optimistic locking, bitemporal
  `event_time`/`recorded_at`, `TIMESTAMPTZ` throughout, `Money` as
  `BIGINT` + `CHAR(3)`.
- RLS policies authored and permissive (ADR-004), so S44 activates rather than
  retrofits.

## 6. Performance Budgets (ADR-036)

Declared before implementation. The mapping-layer figures are the ones that
matter, because they are the price of the architecture and must be shown to be
affordable.

| Budget | Target | Why | Benchmark |
|---|---|---|---|
| Domain → model mapping | **< 5 µs** | Paid per row on every write | `bench_mapper` |
| Model → domain mapping | **< 5 µs** | Paid per row on every read | `bench_mapper` |
| Session acquisition from pool | < 1 ms | Per use case | `bench_session` |
| Single-row insert, committed | p95 < 5 ms | Ordinary write path | `bench_insert` |
| Load aggregate by id | p95 < 3 ms | Ordinary read path | `bench_load` |
| Bulk insert, 10,000 rows (Core path) | **< 500 ms** | The S10/S11 path; ORM would be 10–50× slower | `bench_bulk` |
| `upgrade head` on empty database | < 10 s | CI runs it on every integration run | `bench_migrate` |
| `upgrade head` → `downgrade base` | < 20 s | Rollback must be fast enough to actually use | `bench_migrate` |
| Integration suite, cold container | < 90 s | Slower than this and it gets skipped locally | measured in CI |

## 7. Folder Structure

```
backend/src/dhruva/contexts/platform/infrastructure/database/
├── engine.py          async engine, pool configuration
├── session.py         async_sessionmaker, session scope
├── unit_of_work.py    UnitOfWork, transaction boundary, event publication
├── repository.py      Repository protocol, SQLAlchemy base implementation
├── types.py           MoneyType, PriceType, TradingDayType, InstrumentIdType
├── timescale.py       hypertable / compression / retention DDL helpers
└── outbox.py          outbox model and writer

backend/src/dhruva/contexts/<ctx>/infrastructure/
├── models/            SQLAlchemy models — persistence shape only
└── mappers/           the only modules importing both worlds

backend/alembic/
├── env.py             async-aware, imports models for autogenerate
└── versions/

backend/tests/integration/
├── conftest.py        testcontainers PostgreSQL + TimescaleDB
├── test_migrations.py up/down/up, autogenerate-is-empty
├── test_unit_of_work.py
└── test_round_trips.py
```

## 8. Proposed Architecture Decisions — FOR DESIGN REVIEW

**No implementation begins until these are approved.**

### ADR-052 — Separate persistence models with an explicit mapping layer

**Proposed.** Domain objects and SQLAlchemy models are distinct classes. Mappers
are the only modules importing both. Imperative mapping is rejected: it leaves
the domain object instrumented at runtime, which is persistence coupling that
does not show up in an import graph.

### ADR-053 — The Unit of Work owns the transaction; repositories never commit

**Proposed.** One use case, one UoW, one transaction. Nesting refused rather than
joined. Events publish after commit.

### ADR-054 — Bulk timeseries writes bypass the ORM, within a stated boundary

**Proposed.** Tick and bar ingestion uses SQLAlchemy Core or `COPY`, not the
mapping layer. The boundary: the exception applies **only** to append-only,
immutable, non-aggregate timeseries rows with no invariants beyond column
constraints. Anything with an identity, a lifecycle or an invariant goes through
the mapping layer, whatever its volume.

*This is the decision most likely to erode.* Proposed with the boundary written
down precisely so that a future widening is a visible ADR rather than a
precedent.

### ADR-055 — Expand/contract migrations; reversibility is declared, never assumed

**Proposed.** Destructive changes ship across multiple releases. Every migration
implements and tests `downgrade()` or declares itself irreversible with a reason.
CI runs up → down → up against a real database.

### ADR-056 — Async session discipline

**Proposed.** One session per UoW, never shared across tasks,
`expire_on_commit=False`, `lazy="raise"` everywhere. Pool exhaustion raises a
typed error after a bounded wait.

### ADR-057 — Explicit change tracking with optimistic concurrency

**Proposed.** `repository.update(aggregate)` is required; no snapshot diffing, no
implicit dirty checking. Aggregates carry a `version` column; a lost update
raises `ConflictError`.

### ADR-058 — Integration tests use a real PostgreSQL with TimescaleDB

**Proposed.** No SQLite substitution. Testcontainers, function-scoped
transactional rollback for isolation, session-scoped container for speed.

### ADR-059 — Boundary rule R7: no persistence framework in the domain

**Proposed.** The build fails if any module under a `domain/` package imports
`sqlalchemy`, `alembic`, `asyncpg` or `psycopg`. R6 banned a numeric type from
the money layer; this bans a dependency direction from every domain layer.

## 8.1 Design Review amendments (approved 2026-07-26)

**Amendment 1 — ADR-054 boundary tightened.** The timeseries bypass must
terminate at a dedicated `TimeSeriesStorage` interface rather than remaining a
described convention. Seven conditions now define the exception, and boundary
rule **R8** makes it enforceable: the timeseries package may not import from any
`domain` package, so a business entity cannot be named on that path.

**Amendment 2 — `TradingDay` reconstruction (answers Q1).** Neither the mapper
nor the repository owns reconstruction. The flow is:

```
Database Row → Persistence DTO → Mapper → Reconstruction Factory → Domain Object
```

Repositories *orchestrate* reconstruction; they do not embed it. The mapper stays
a pure function between a model and a primitives-only record. A reconstruction
factory holds whatever domain services are needed — a `TradingCalendar` for
`TradingDay` — and turns a record into an aggregate.

This costs one more layer than my proposal and is better: it keeps the repository
focused on persistence, keeps the mapper pure and benchmarkable, and leaves
ADR-046 uncompromised, because reconstruction goes through the same calendar
verification as any other `TradingDay` construction.

**Q2, Q3, Q4** were approved as recommended: machinery plus one throwaway example
table, optimistic locking on every aggregate, and the outbox table created here.

## 9. Open Questions for the Product Owner

| # | Question | Why it matters | My recommendation |
|---|---|---|---|
| Q1 | Should the mapper or the repository reconstruct `TradingDay`? | `TradingDay` cannot exist without a calendar (ADR-046), but a mapper has no business holding one. | **Repository.** It is constructed with its dependencies and can hold a calendar; the mapper stays a pure function. The alternative — a `TradingDay` that skips validation on read — would put a hole in ADR-046. |
| Q2 | Should S04 include one real domain table, or only the machinery? | A machinery-only subsystem is untested against reality; a real table means S07 inherits decisions it did not make. | **Machinery plus one deliberately throwaway example**, deleted by S07. Proves the pattern end to end without pre-empting a later design. |
| Q3 | Optimistic locking on every aggregate, or only where contention is real? | A `version` column on everything costs a byte and a WHERE clause; omitting it means adding it later under load. | **Every aggregate.** The cost is negligible and retrofitting concurrency control to a live table is not. |
| Q4 | Should the outbox be created in S04 or deferred to S05? | Creating it now means S05 inherits delivery guarantees; deferring means S05 designs the bus first and retrofits durability. | **S04**, table only. One table now saves reworking a working bus later. |

---

*Step 1 complete. Awaiting Design Review before implementation.*
