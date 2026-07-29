# S05 — Event Bus & Job Runtime: Architecture and Design

- **Status:** Proposed — awaiting Design Review
- **Depends on:** S04 (Persistence Foundation), specifically the transactional outbox
- **Plan reference:** Master Project Plan §520
- **Proposed ADRs:** ADR-061 … ADR-069
- **Governing ADRs already binding on this subsystem:** 002, 007, 010, 011, 015, 018, 022, 035, 036, 038, 039, 053, 054, 056, 060

---

## 0. The constraint that shapes everything else

> *"Strategies should not know whether events originate from live market feeds or
> a replay engine."*

This is not a feature request; it is a constraint on every interface in S05, and
it is the reason several of the obvious designs are wrong. ADR-010 already
requires one execution kernel shared by backtest, paper and live. S05 is where
that promise is either kept or quietly broken, because S05 is where events start
*moving* — and movement is where "live" normally leaks in.

Section 12 is the compatibility analysis. It is the most important section in
this document and it is the one I would read first at review.

---

## 1. What S05 is for

S04 built the outbox **writer**. `OutboxWriter` stages an event inside the
caller's transaction so the event and the state change that caused it share one
fate. **Nothing reads it.** S05 builds the half that delivers.

The platform already *promises* five things, recorded at S04 approval:

| Promise | True today? |
|---|---|
| Transactional durability | ✅ yes |
| At-least-once delivery | ❌ nothing delivers |
| Stable, immutable `event_id` | ⚠️ column exists, no consumer relies on it |
| Per-aggregate ordering | ❌ nothing orders |
| Idempotent consumer contract | ❌ no consumers, no ledger |

**We made a delivery promise in S04 and this is where we pay for it.**

---

## 2. Finding from the review: the S04 outbox cannot carry the ADR-002 envelope

ADR-002 fixed the envelope in Phase 0: `event_id`, `event_type`, `event_version`,
`occurred_at`, `recorded_at`, `account_id`, `correlation_id`, `causation_id`,
`payload`. The table S04 created has six of those nine.

| Field | In `outbox` today |
|---|---|
| `event_id`, `event_type`, `occurred_at`, `recorded_at`, `account_id`, `payload` | present |
| **`event_version`** | **missing** |
| **`correlation_id`** | **missing** |
| **`causation_id`** | **missing** |

This is a gap between an approved decision and what was built, not a new design
choice. S05 closes it with an **expand-only migration `0003`** (ADR-055): three
nullable columns added, backfilled with defaults, then made `NOT NULL` in a later
contract step once no writer omits them.

I am flagging it rather than folding it silently into an implementation commit,
because it means **S04's outbox was never capable of satisfying ADR-002** and a
reviewer should know that before approving anything built on top of it.

---

## 3. Overall event-driven architecture

```
   ┌─────────────────── Producer side (S04, extended) ───────────────────┐
   │  Use case                                                           │
   │     repository.add(aggregate)                                       │
   │     outbox.stage(event)         ← ONE transaction, ONE fate         │
   └──────────────────────────────┬──────────────────────────────────────┘
                                  │ commit
                                  ▼
                        ┌──────────────────┐
                        │  outbox table    │  PostgreSQL — source of truth
                        └────────┬─────────┘
                                 │ SELECT … FOR UPDATE SKIP LOCKED
                                 ▼
                        ┌──────────────────┐
                        │   OutboxRelay    │  dedicated process (ADR-063)
                        └────────┬─────────┘
                                 │ EventPublisher  ← PORT, not Redis
                 ┌───────────────┼───────────────┐
                 ▼               ▼               ▼
        ┌────────────┐  ┌──────────────┐  ┌─────────────┐
        │ Redis      │  │ InMemory     │  │ Replay      │
        │ Streams    │  │ (tests)      │  │ (backtest)  │
        └─────┬──────┘  └──────────────┘  └──────┬──────┘
              │                                   │
              │  XREADGROUP                       │  ordered scan, as-of filtered
              ▼                                   ▼
        ┌──────────────────────────────────────────────┐
        │           EventStream  ← PORT                │
        │  A consumer sees envelopes. Nothing else.    │
        └────────────────────┬─────────────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  Consumer / ExecutionContext │
              │  processed_event ledger      │  (ADR-065)
              └──────────────────────────────┘
```

**Two ports, and they are the whole design.**

- `EventPublisher` — the relay's exit. Redis is one implementation.
- `EventStream` — the consumer's entrance. **Redis Streams and the replay engine
  are peers behind it.** A strategy that can name Redis has already failed the
  §0 constraint.

Everything else in S05 is an adapter or an operational concern.

---

## 4. Event envelope schema (proposed **ADR-061**)

Refines ADR-002; adds nothing that contradicts it.

| Field | Type | Purpose |
|---|---|---|
| `event_id` | UUID | Stable, immutable, assigned at creation. The deduplication key |
| `event_type` | str ≤128 | Namespaced: `marketdata.bar.closed` |
| `event_version` | int | Schema version of `payload` |
| `aggregate_type` | str | Which kind of thing this happened to |
| `aggregate_id` | UUID? | Which one. Null for events with no aggregate |
| `sequence` | int | Monotonic per producing instance. Ordering and replay anchor |
| `occurred_at` | datetime(tz) | **Event time** — when it was true in the market |
| `recorded_at` | datetime(tz) | **System time** — when we learned it (ADR-007) |
| `account_id` | UUID? | Tenant scope (ADR-004) |
| `correlation_id` | UUID | Ties everything caused by one external stimulus |
| `causation_id` | UUID? | The `event_id` of the direct parent |
| `payload` | JSON | Domain data, versioned by `event_version` |

### Why both `occurred_at` and `recorded_at`

This pair is ADR-007's bitemporality, and **it is the single mechanism that makes
honest backtesting possible.** A late-arriving correction has an `occurred_at` in
the past and a `recorded_at` of now. A backtest simulating T must see only events
with `recorded_at ≤ T`, or it trades on knowledge it did not have. Section 12.1.

### Why `causation_id` is separate from `correlation_id`

`correlation_id` answers *"what request is this part of?"* — flat, shared by
everything downstream. `causation_id` answers *"what directly caused this?"* —
a parent pointer forming a tree.

They are frequently conflated and the conflation is expensive here specifically
because of **ADR-018**: a signal must emit an `EvidenceBundle` naming its
contributing factors and data provenance. Provenance is a *causal chain*, not a
flat tag. With only `correlation_id` you can say "these forty events belong to
the 09:15 tick"; you cannot say "this order exists because of that signal, which
exists because of that bar". The second is what a regulator, and a user asking
"why did it trade?", actually want.

### Serialisation format (part of ADR-061)

**JSON, with domain primitives serialised by exact representation.**

```
Money    → {"minor_units": 123456, "currency": "INR"}
Price    → {"scaled_units": 123456780000, "scale": 8}
Quantity → {"units": 50}
TradingDay → {"date": "2026-07-28"}
datetime → RFC 3339 with explicit offset, always UTC
```

**No float appears in an envelope, ever.** A float here would silently undo
ADR-042 at the one point nobody inspects, and boundary rule R6 does not currently
reach serialisation code — see §17, TD-S05-1.

*Why JSON and not msgpack/protobuf/Avro:* the payloads are small and low-volume
(domain events, not ticks — ticks take the ADR-054 bypass and never enter the
bus). JSON is inspectable in `redis-cli`, in a DLQ dump, and in a log line. When
an event is stuck in a dead-letter queue at 09:20 on a trading day, being able to
read it without tooling is worth more than the bytes a binary codec saves.
Revisit if a bus payload ever becomes high-volume — that would be a design smell
in itself.

---

## 5. Versioning strategy (part of **ADR-061**)

- `event_version` starts at 1 and increments on any **incompatible** payload
  change.
- **Additive optional fields do not bump the version.** Consumers ignore unknown
  keys.
- Removing a field, renaming one, or changing its meaning **does** bump it.
- A consumer declares the versions it understands. An envelope at an unknown
  version is **routed to the DLQ, not skipped and not guessed** (ADR-022, fail
  closed). Guessing is how a schema change silently changes trading behaviour.
- Producers may emit two versions during a migration window; consumers upgrade
  first, producers second. This is the expand/contract of ADR-055 applied to
  wire format.

**Deliberately not chosen:** a schema registry. It is a second source of truth
and an operational dependency, and at this scale the version integer plus a
consumer-side check gives the same protection. Revisit if external consumers
appear.

---

## 6. Delivery guarantees (proposed **ADR-062**)

### At-least-once. Exactly-once is not offered.

The relay may publish and crash before marking the row published; it will publish
again on restart. **This is a consequence of the outbox, not a defect in it.** The
alternative — mark first, publish second — converts duplicate delivery into
silent loss, and losing an event about capital is strictly worse than repeating
one.

Exactly-once across a database and a broker requires distributed consensus this
project is not buying, and systems that claim it usually mean "at-least-once plus
deduplication", which is what §8 actually builds. Saying so plainly is the honest
engineering.

**At-most-once is rejected outright.** It trades correctness for latency the
platform does not need.

### Ordering: two different guarantees for two different purposes

This is where my first draft was wrong and the §0 constraint forces a better
answer.

| Mode | Guarantee | Mechanism |
|---|---|---|
| **Live delivery** | Per-`aggregate_id`, in `sequence` order. No cross-aggregate order | One Redis stream per aggregate-type; hash-partitioned consumers |
| **Replay** | **Total, deterministic**, reproducible byte-for-byte across runs | Ordered scan of the outbox by `(recorded_at, sequence)` |

A global live order would need a single stream and a single consumer — a
throughput ceiling bought for a guarantee almost nothing needs. But a *backtest*
needs determinism: the same inputs must produce the same trades, or a result
cannot be reproduced or defended.

The resolution: **live order is a subset of replay order.** Anything a strategy
may rely on live (per-aggregate sequence) is preserved in replay; replay
additionally fixes the interleaving that live leaves unspecified. A strategy
correct under live ordering is therefore correct under replay, but **a strategy
that accidentally depends on cross-aggregate interleaving will behave
differently between them** — see §12.4 for how that is caught rather than
discovered in production.

---

## 7. Consumer groups & Redis Streams design

One stream per aggregate type: `dhruva:events:{aggregate_type}`. One consumer
group per logical consumer: `dhruva:cg:{consumer_name}`.

- `XADD` with an explicit ID derived from `sequence` so the stream is idempotent
  under relay retry.
- `XREADGROUP` with `NOACK=false`; `XACK` **only after** the consumer's
  transaction commits.
- `XAUTOCLAIM` reclaims entries idle beyond a threshold — this is crash recovery
  for a dead consumer (§13).
- `MAXLEN ~ N` capped trimming. The stream is a *transport buffer*, not storage.
  **The outbox is the source of truth**; a wiped Redis loses nothing not yet
  published.

### Why Redis Streams, not Kafka / NATS / RabbitMQ

ADR-002 already decided this. Restated because S05 is where reversing it becomes
expensive.

| | Redis Streams | Kafka | NATS JetStream | RabbitMQ |
|---|---|---|---|---|
| Consumer groups + per-consumer ack | yes | yes | yes | yes |
| Replay from arbitrary offset | yes | yes | yes | no (queues drain) |
| Operational weight | **already deployed for caching** | ZK/KRaft, large | moderate | moderate |
| Ordering | per stream | per partition | per stream | per queue |
| Retention | capped by length/time | durable, long | durable | short |
| Right-sized for one-box Stage 1 | **yes** | no | yes | yes |

**The deciding argument is not throughput, it is that Redis is already in the
stack.** A second broker is a second thing to operate, monitor, back up, upgrade
and be woken by. At Stage 1 volumes — domain events, not ticks — Redis Streams
covers every requirement, and the `EventPublisher` port keeps Kafka a
substitution rather than a rewrite if S20+ volume justifies one.

RabbitMQ is eliminated on replay alone: queues drain, and §11 needs history.

---

## 8. Idempotency (proposed **ADR-065**)

A `processed_event` table, primary key `(consumer_group, event_id, run_id)`.

The consumer writes its ledger row **in the same transaction as its side
effects**. A duplicate violates the primary key, the whole unit of work rolls
back, and the side effect does not happen twice.

**Why not Redis-side deduplication:** it puts the dedup decision in a different
transaction from the work it protects. A crash between the two re-introduces
precisely the duplicate it was meant to prevent. This composes with S04's Unit of
Work (ADR-053); nothing else does.

**Why `run_id` is in the key** — this is a §0 consequence and my first draft got
it wrong. Keyed only by `(consumer_group, event_id)`, a backtest replaying the
same events a second time would find every row already present and **process
nothing**. The ledger would silently make the second backtest a no-op. `run_id`
scopes the ledger to one live session or one replay run. Live uses a constant
run; each replay allocates a fresh one.

**Retention:** ledger rows pruned after 30 days by a scheduled job (§10). Replay
runs prune on completion. Unbounded is not a retention policy.

ADR-015 already mandates idempotency on the order path with client-generated
keys; §8 is the general mechanism beneath it, and the two must not disagree —
see §14.

---

## 9. Retry, backoff, DLQ, poison messages (proposed **ADR-064**)

**Retry** — exponential backoff with full jitter, using the `attempts` and
`next_attempt_at` columns S04 already created:
`delay = min(base × 2^attempts, cap)`, jittered. Base 1 s, cap 60 s, **N = 5**.

Jitter is not decoration: without it, a broker outage produces a synchronised
retry storm at recovery, which is how an outage extends itself.

**The delay is computed against the injected `Clock` (ADR-011), never
`sleep()` against the wall clock** — §12.3.

**Dead-letter policy.** After N attempts an envelope moves to
`dhruva:dlq:{aggregate_type}` with its last error, attempt count and full
envelope. Three deliberate properties:

1. **A poison message never blocks the head.** It is moved aside, not retried in
   place forever. One malformed event must not stop the platform.
2. **Replay from the DLQ is explicit and human-initiated.** No automatic drain: a
   message reached the DLQ because retrying did not help, and retrying on a timer
   is a loop, not a recovery.
3. **DLQ depth is a monitored metric with a non-zero alert threshold.** A DLQ
   nobody watches is a silent data-loss mechanism with extra steps.

**Poison classification.** Failures split into *retryable* (broker unreachable,
timeout, lock contention) and *terminal* (unknown `event_version`, malformed
payload, failed validation). Terminal failures go straight to the DLQ **without
consuming five retries** — retrying a malformed payload five times is five
identical failures and a 60-second delay for nothing. This uses the S02 error
taxonomy (ADR-038), which already distinguishes these.

---

## 10. Celery, scheduled jobs, calendar awareness (proposed **ADR-066**)

Celery runs **scheduled and deferred work** — nightly reconciliation, ledger
pruning, corporate-action ingestion. It is **not** the event bus, and it is not
the relay (§11).

**"Run at market open" is not `0 9 * * 1-5`.** Indian markets close for holidays,
occasionally trade on a Saturday, and changed expiry conventions on 2025-09-01.
Beat schedules resolve through the S03 `TradingCalendar` port: a job declares a
*session-relative moment* (`market_open + 5min`, `market_close - 15min`,
`session_end + 1h`) and the calendar decides whether today qualifies and what
wall-clock time that is.

This is ADR-011 applied to scheduling, and it is testable: a frozen clock plus a
stub calendar asserts a job does not run on Diwali, without waiting for Diwali.

Errors crossing the Celery boundary must stay **picklable** — plan §442 already
requires it of the ADR-038 taxonomy, and a test asserts it.

---

## 11. Outbox relay (proposed **ADR-063**)

**A dedicated process, not a Celery beat task.** Three reasons in order of
weight:

1. **Latency.** Beat's practical granularity is seconds-to-minutes. The relay
   should drain within a second of commit. A one-minute beat makes every event a
   one-minute event.
2. **Starvation.** A relay running as a worker task competes with the pool it
   feeds. Under load — precisely when delivery matters most — the relay queues
   behind work its own events created.
3. **Concurrency control belongs in the query.** `FOR UPDATE SKIP LOCKED` makes N
   relay instances safe. That is a property of SQL, and should not depend on
   beat's singleton behaviour, which is an operational assumption.

**Loop:** claim a batch (`SKIP LOCKED`, ordered by `sequence`) → publish → mark
published → short sleep if empty. Ships with the health, readiness, metrics and
tracing surface ADR-035 requires of every runtime component.

**Publish ordering:** parallelised across aggregates, serialised within one. That
is exactly what §6 promises and no more.

---

## 12. Live / replay compatibility analysis ⭐

The section to read at review. Each item is a place where the obvious design
breaks backtesting, with the alternative I propose.

### 12.1 Look-ahead bias — the one that invalidates results silently

A replay that reads events by `occurred_at` alone will hand a strategy a
correction that *arrived* days later. The backtest then trades on knowledge it
did not have, and the result is not merely wrong — **it is optimistically
wrong**, which is the direction that gets capital committed.

**Proposal:** the `EventStream` port's replay adapter takes an `as_of` and is
*structurally incapable* of returning `recorded_at > as_of`. Not a filter a
caller may forget — a constructor parameter, enforced in the query, with a test
asserting a late-arriving correction is invisible before its `recorded_at`.

This is ADR-007 being cashed in, and it only works because S04 stores both
timestamps.

### 12.2 The idempotency ledger would make replays no-ops

Covered in §8. Without `run_id` the second run of a backtest processes nothing
and reports zero trades — a failure that looks like a *result*.

### 12.3 Wall-clock backoff makes replay take real time

A retry with a 60-second cap, replayed over a year of data, sleeps for
weeks. Worse, `sleep()` in a consumer path means backtest duration depends on
failure counts — nondeterministic wall-time for a deterministic computation.

**Proposal:** all delay computation goes through the injected `Clock`. In replay
the clock is virtual and advancing it is free. **No `asyncio.sleep` in any
retry or scheduling path** — a lint rule extending the ADR-011 ban, since
`datetime.now()` is already banned but `sleep` is not.

### 12.4 Ordering divergence between live and replay

§6 gives replay a *stronger* guarantee than live. A strategy accidentally
depending on cross-aggregate interleaving is correct in one and wrong in the
other — and the backtest will be the one that looks right.

**Proposal:** a `ShuffledReplay` test mode that permutes cross-aggregate order
within the bounds live permits, and asserts the strategy's decisions are
unchanged. A strategy failing this is relying on a guarantee it does not have.
This is cheap to build now and near-impossible to retrofit once strategies exist.

### 12.5 `recorded_at` must come from the Clock, not the database

If `recorded_at` defaults to PostgreSQL's `now()`, a replay cannot control it and
§12.1 becomes unenforceable. **Proposal:** `recorded_at` is set by the
application from the injected clock. The column keeps no server default, and a
test asserts a frozen clock produces the frozen value.

### 12.6 Correlation and causation IDs are nondeterministic

Randomly generated UUIDs differ per run, so two replays of identical input
produce different `EvidenceBundle` provenance (ADR-018) and cannot be diffed.

**Proposal:** in replay, derive them deterministically — UUID5 over
`(run_id, sequence)` — exactly as `InstrumentId.deterministic` already works in
S03. Strategy *decisions* must not depend on these values, and a test should
assert that; but reproducible *evidence* is worth the small cost.

### 12.7 Consumers must never import the transport

A strategy that imports `redis` or `celery` cannot run in a backtest process.

**Proposal:** proposed **boundary rule R9** — no module under `domain/` or
`strategy/` may import `redis`, `celery`, or any `infrastructure.messaging`
package. This is the same enforcement pattern as R7/R8 (ADR-059), and it is the
structural guarantee behind the §0 constraint. Convention will not hold here:
importing the client is always the convenient one-liner.

### 12.8 Live-only concepts leaking into the envelope

Consumer-group names, stream IDs, delivery counts and partition keys are
*transport* facts. If any reaches a consumer, replay must fabricate one.

**Proposal:** the envelope carries no transport field. `EventStream` yields
`(envelope, ack_token)` where `ack_token` is opaque and meaningless to a
strategy; the replay adapter's token is a no-op.

---

## 13. Failure and crash recovery

| Failure | Behaviour |
|---|---|
| Relay crashes mid-publish | Row not marked; republished on restart. Duplicate absorbed by §8 |
| Relay crashes after publish, before mark | Same. This *is* at-least-once |
| Redis wiped | Outbox is source of truth; unpublished rows republish. Published-but-unconsumed events are lost — **accepted, stated, and why the outbox is not trimmed until consumers have acked** |
| Consumer crashes mid-transaction | Postgres rolls back; entry stays pending; `XAUTOCLAIM` redelivers |
| Consumer crashes after commit, before `XACK` | Redelivered; ledger rejects the duplicate. Correct by construction |
| Postgres unavailable | Relay blocks and retries. **Fails closed** (ADR-022) — it does not publish without durable state |
| Poison message | DLQ after N, or immediately if terminal (§9) |
| Clock skew between processes | `occurred_at`/`recorded_at` come from the injected clock at the producer; consumers never compare their own clock to an envelope's |

---

## 14. Integration with S04

- **Outbox table** — extended by migration `0003` with the three missing ADR-002
  columns (§2), expand-only.
- **`OutboxWriter`** — unchanged interface; populates the new columns from the
  correlation context (ADR-039).
- **`UnitOfWork`** (ADR-053) — the consumer's ledger write and side effects share
  its transaction. No new transaction mechanism.
- **Repositories** — untouched. S05 adds no persistence pattern; the
  `processed_event` ledger is an ordinary table with an ordinary repository.
- **ADR-054 timeseries bypass** — **unchanged and unaffected.** Ticks do not go
  on the bus. If a future subsystem tries to publish per-tick events, that is a
  design error and §7's volume argument is the reason.
- **ADR-015 order idempotency** — the client-generated order key remains the
  order path's key. §8's ledger deduplicates *event delivery*; ADR-015
  deduplicates *broker dispatch*. Two layers, deliberately, because a duplicate
  event and a duplicate order need different remedies.

---

## 15. Preparing for S06 and backtesting

**S06 (Broker Integration).** Broker callbacks are the archetypal external
stimulus: they arrive out of order, duplicate, and must be reconciled with
internal state (ADR-017 makes broker state authoritative). S05 gives S06 the
`correlation_id`/`causation_id` chain to tie a fill back to the order and the
signal, the idempotency ledger for duplicate callbacks, and a DLQ for callbacks
that cannot be matched — which under ADR-022 must block rather than guess.

**Backtesting.** §12 is the preparation. Concretely, S05 leaves behind: an
`EventStream` port with live and replay adapters as peers; bitemporal
`as_of` enforcement; `run_id`-scoped idempotency; virtual-clock-driven retry and
scheduling; and boundary rule R9 making transport-free strategies structural.

**What S05 deliberately does not build:** the replay engine itself. It builds the
*port* the replay engine will implement and an in-memory adapter proving the port
is not Redis-shaped. Building a replay engine before any strategy exists would be
designing against an imagined consumer.

---

## 16. Observability, security, performance, scale

**Observability** (ADR-035). Metrics: outbox depth · unpublished age (the real
lag signal) · publish latency · relay throughput · DLQ depth · consumer lag ·
redelivery count · ledger hit rate. Logs: `event_id`, `event_type`,
`correlation_id`, `causation_id` on every line (ADR-039 propagates them).
Tracing: the trace context travels **in the envelope**, so a span at the consumer
is a child of the span at the producer — cross-process propagation is plan §452
and belongs here.

**Security.** Redis is not a trust boundary: AUTH + TLS, no public bind. Payloads
may carry account-scoped data, so redaction (ADR-037) applies to envelope logging
— an envelope must never be logged whole without passing the redactor. No secrets
in envelopes, ever (ADR-033). DLQ contents are as sensitive as the events, and
its access control must match. Multi-tenant: `account_id` on every envelope, and
consumers filter by it (ADR-004).

**Performance budgets**, declared before implementation (ADR-036), enforced on
Linux CI (ADR-060):

| Measurement | Budget |
|---|---|
| Envelope serialise | < 20 µs |
| Deserialise + validate | < 30 µs |
| Relay claim, batch of 100 | p95 < 10 ms |
| **Commit → published, end-to-end** | **p95 < 1 s** |
| Redis publish, single | p95 < 5 ms |
| Consumer ledger insert overhead | < 2 ms |
| Sustained relay throughput | ≥ 1,000 events/s |
| Replay throughput | ≥ 10,000 events/s |

Replay is budgeted an order of magnitude higher because a backtest over a year of
data is unusable otherwise — and because replay has no network in the path.

**Scalability.** Relay scales horizontally on `SKIP LOCKED`. Consumers scale
within a group. Streams shard by aggregate type. The known ceiling is the single
PostgreSQL writer, which is a Phase-2 concern, not Stage 1.

---

## 17. Deliberately deferred debt

| ID | Item | Why deferred |
|---|---|---|
| TD-S05-1 | Boundary rule R6 (no float) does not reach serialisation code | Extend R6 or add R10 during implementation; noted so it is not forgotten |
| TD-S05-2 | No schema registry | §5. The version integer suffices until external consumers exist |
| TD-S05-3 | Outbox pruning is time-based, not consumer-ack-based | Ack-based needs consumer progress tracking; scheduled prune with a generous window first |
| TD-S05-4 | The replay engine itself | §15. Port now, engine when a strategy exists |
| TD-S05-5 | No cross-region or multi-broker fan-out | Out of scope for Stage 1 |
| TD-S05-6 | DLQ replay is a CLI command, not a UI | UI is S3x |
| TD-S05-7 | `event_version` columns land nullable; the contract step is deferred | ADR-055 expand/contract; contract once no writer omits them |

---

## 18. Proposed ADRs

| ADR | Title |
|---|---|
| **061** | Event envelope: versioned, self-describing, JSON with exact domain representations |
| **062** | At-least-once delivery; per-aggregate ordering live, total deterministic ordering in replay |
| **063** | The outbox relay is a dedicated process, not a Celery beat task |
| **064** | Dead-letter policy: classified failures, bounded retries, explicit replay |
| **065** | Consumers deduplicate through a run-scoped ledger in their own transaction |
| **066** | Scheduled work is defined in trading-day terms, not cron |
| **067** | `EventStream` is a port; live and replay are peer adapters |
| **068** | Boundary rule R9: no strategy or domain module imports a transport |
| **069** | Replay is bitemporally correct by construction: `as_of` is structural, not a filter |

067–069 exist only because of the §0 constraint. Without it, S05 would need six
ADRs; the three extra are the price of a backtest you can trust, and they are
much cheaper now than retrofitted.

---

## 19. Packages and modules

```
backend/src/dhruva/
├─ shared/
│  └─ messaging/
│     ├─ envelope.py            EventEnvelope, versioning
│     ├─ serialisation.py       exact codecs for domain primitives
│     └─ ports.py               EventPublisher, EventStream, AckToken
└─ contexts/platform/
   ├─ domain/messaging/
   │  └─ delivery.py            retry policy, failure classification (no I/O)
   └─ infrastructure/messaging/
      ├─ relay.py               OutboxRelay
      ├─ redis_streams.py       publisher + stream adapter
      ├─ in_memory.py           test/dev adapter
      ├─ dlq.py                 dead-letter + replay command
      ├─ ledger.py              processed_event repository
      └─ scheduling/
         ├─ beat.py             calendar-aware schedule resolution
         └─ tasks.py            Celery wiring

backend/alembic/versions/
  0003_outbox_envelope_columns.py    event_version, correlation_id, causation_id
  0004_processed_event_ledger.py     idempotency ledger
```

`shared/messaging` holds the envelope because both producers and consumers need
it and neither owns it. Ports live beside it. Adapters live in infrastructure,
where R9 can see them.

### Dependency graph

```
        shared/money · time · identity · errors        (S03, API-stable)
                          │
                  shared/messaging                     envelope, ports
                     │          │
     domain/messaging│          │infrastructure/messaging
     (policy, pure)  │          │(relay, redis, celery, dlq, ledger)
                     │          │
                     └────┬─────┘
                          ▼
              platform/infrastructure/persistence      (S04: outbox, UoW)
                          ▼
                      PostgreSQL

   R9: domain/* and strategy/* → messaging *ports only*, never adapters
```

---

## 20. Open questions — I would rather you answer these than have me guess

1. **`N = 5` retries, 60 s cap, 30-day ledger retention.** Conventional
   defaults, not derived from your risk tolerance. If a broker callback should
   survive a 30-minute outage, N = 5 is wrong.
2. **Relay topology** — one process per deployment, or one per bounded context?
   My recommendation: one, until throughput evidence says otherwise.
3. **Should the DLQ be a Redis stream or a PostgreSQL table?** I have proposed
   Redis for symmetry. Postgres would make DLQ contents durable across a Redis
   wipe and queryable in SQL, at the cost of a second mechanism. **I lean
   Postgres on reflection** — a DLQ that a Redis flush can erase is a poor place
   for the events that most need attention. Flagging rather than silently
   changing §9.
4. **Is `run_id` acceptable in the ledger primary key** (§8), given it slightly
   complicates the live path for a backtesting benefit that arrives later?
5. **Should the outbox retain published rows indefinitely** for replay, or is
   replay sourced from a separate archive? Indefinite retention makes replay
   trivial and the table large. This decision shapes TD-S05-3.
6. **Confirm Redis Streams for Stage 1** — ADR-002 says yes; restated because
   S05 is where reversal becomes expensive.

---

## 21. Delivery order once approved

Each step is a stopping point with its own evidence, in small reviewable commits.

1. ADRs 061–069 recorded · migration `0003`
2. Envelope + serialisation — pure, no transport. Property tests: every domain
   primitive round-trips exactly; unknown version refused
3. Ports + in-memory adapter — proves the port is not Redis-shaped
4. Outbox relay — `SKIP LOCKED`, kill-mid-publish tests against real PostgreSQL
5. Redis Streams adapter — real Redis, consumer groups, `XAUTOCLAIM`
6. DLQ, classification, replay command
7. Idempotency ledger + migration `0004`
8. Celery + calendar-aware beat
9. Cross-process trace propagation
10. Replay adapter + `as_of` enforcement + `ShuffledReplay` conformance mode
11. Validation, documentation, release

Steps 2–4 are the substance. If they are right, 5–9 are adapters and wiring.
Step 10 is what makes §0 true rather than intended.
