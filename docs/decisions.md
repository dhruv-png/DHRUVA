# Decision Index

Master index of every Architecture Decision Record. Generated content is not
acceptable here -- this file is reviewed, and reviewing it is how a decision that
should not have been made gets caught.

**76 decisions — 75 accepted, 1 superseded.** Records are immutable once accepted
(ADR-027); see [`adr/README.md`](adr/README.md) for the lifecycle.

| # | Decision | Status |
|---|---|---|
| [ADR-001](ADR-001-modular-monolith-not-microservices.md) | Modular monolith, not microservices | Accepted |
| [ADR-002](ADR-002-kafka-deferred-redis-streams-is-the-v1-event.md) | Kafka deferred; Redis Streams is the v1 event backbone | Accepted |
| [ADR-003](ADR-003-provider-agnostic-market-data-via-ports-and.md) | Provider-agnostic market data via ports and adapters | Accepted |
| [ADR-004](ADR-004-multi-tenant-ready-schema-from-day-one.md) | Multi-tenant-ready schema from day one | Accepted |
| [ADR-005](ADR-005-money-is-decimal-storage-is-integer-paise.md) | Money is `Decimal`; storage is integer paise | Superseded by ADR-042 |
| [ADR-006](ADR-006-all-timestamps-stored-utc-all-display-asia-kolkata.md) | All timestamps stored UTC; all display Asia/Kolkata; `TradingDay` is a domain type | Accepted |
| [ADR-007](ADR-007-bitemporal-data-for-anything-a-backtest-reads.md) | Bitemporal data for anything a backtest reads | Accepted |
| [ADR-008](ADR-008-prices-stored-unadjusted-adjustment-applied-at-read.md) | Prices stored unadjusted; adjustment applied at read time | Accepted |
| [ADR-009](ADR-009-internal-surrogate-instrument-id-tradingsymbol-is.md) | Internal surrogate `instrument_id`; `tradingsymbol` is never a key | Accepted |
| [ADR-010](ADR-010-one-execution-kernel-shared-by-backtest-paper-and.md) | One execution kernel shared by backtest, paper, and live | Accepted |
| [ADR-011](ADR-011-time-is-injected-never-read-from-the-wall-clock.md) | Time is injected, never read from the wall clock | Accepted |
| [ADR-012](ADR-012-the-risk-engine-is-a-mandatory-unbypassable-pre.md) | The Risk Engine is a mandatory, unbypassable pre-trade gate | Accepted |
| [ADR-013](ADR-013-the-cost-engine-is-effective-dated-and-shared-by.md) | The Cost Engine is effective-dated and shared by all three execution modes | Accepted |
| [ADR-014](ADR-014-orders-and-fills-are-an-append-only-immutable.md) | Orders and fills are an append-only immutable ledger | Accepted |
| [ADR-015](ADR-015-idempotency-on-the-entire-order-path.md) | Idempotency on the entire order path | Accepted |
| [ADR-016](ADR-016-hard-unbypassable-order-rate-governor.md) | Hard, unbypassable order-rate governor | Accepted |
| [ADR-017](ADR-017-broker-state-is-the-source-of-truth-internal-state.md) | Broker state is the source of truth; internal state is a hypothesis | Accepted |
| [ADR-018](ADR-018-explainability-is-a-data-contract-not-a-ui-feature.md) | Explainability is a data contract, not a UI feature | Accepted |
| [ADR-019](ADR-019-simple-mode-and-professional-mode-are-two.md) | Simple Mode and Professional Mode are two compositions of one API | Accepted |
| [ADR-020](ADR-020-security-by-default-encrypted-broker-credentials-no.md) | Security by default: encrypted broker credentials, no secrets in the database in plaintext | Accepted |
| [ADR-021](ADR-021-daily-market-open-ritual-is-a-designed-feature.md) | Daily "Market Open Ritual" is a designed feature | Accepted |
| [ADR-022](ADR-022-fail-closed.md) | Fail closed | Accepted |
| [ADR-023](ADR-023-monorepo.md) | Monorepo | Accepted |
| [ADR-024](ADR-024-python-3-12-strict-typing-async-first-at-i-o.md) | Python 3.12, strict typing, async-first at I/O boundaries | Accepted |
| [ADR-025](ADR-025-research-notebooks-are-not-production.md) | Research notebooks are not production | Accepted |
| [ADR-026](ADR-026-no-live-capital-before-gate-g5.md) | No live capital before Gate G5 | Accepted |
| [ADR-027](ADR-027-every-major-architectural-decision-is-recorded-as.md) | Every major architectural decision is recorded as an ADR; approved architecture is never silently modified | Accepted |
| [ADR-028](ADR-028-mcp-first-delivery-execution-subsystems-are-gated.md) | MCP-first delivery; execution subsystems are gated on demonstrated analytics value | Accepted |
| [ADR-029](ADR-029-version-control-is-part-of-the-definition-of.md) | Version control is part of the Definition of Done; no subsystem accumulates uncommitted | Accepted |
| [ADR-030](ADR-030-documentation-is-updated-in-the-same-commit-as.md) | Documentation is updated in the same commit as the code it describes | Accepted |
| [ADR-031](ADR-031-process-configuration-lives-in-the-shared-kernel.md) | Process configuration lives in the shared kernel; the Platform context owns only persisted configuration | Accepted |
| [ADR-032](ADR-032-reproducible-builds.md) | Reproducible builds: pinned dependencies, committed lockfile, documented environment | Accepted |
| [ADR-033](ADR-033-no-secret-exists-in-the-repository.md) | No secret exists in the repository; secrets arrive only from the configuration provider | Accepted |
| [ADR-034](ADR-034-release-governance.md) | Release governance: every subsystem ships changelog, ADRs, tag, release notes, migration and rollback | Accepted |
| [ADR-035](ADR-035-observability-is-a-first-class-feature.md) | Observability is a first-class feature: every runtime component ships logs, correlation, health, readiness, metrics and traces | Accepted |
| [ADR-036](ADR-036-performance-budgets-declared-before-implementation.md) | Every subsystem declares measurable performance budgets before implementation | Accepted |
| [ADR-037](ADR-037-log-redaction-is-a-tested-control.md) | Log redaction is a tested control with two independent strategies | Accepted |
| [ADR-038](ADR-038-errors-are-a-closed-taxonomy-with-stable-codes.md) | Errors are a closed taxonomy with stable machine-readable codes | Accepted |
| [ADR-039](ADR-039-correlation-propagates-through-contextvars.md) | Correlation propagates through contextvars, and threads require an explicit wrapper | Accepted |
| [ADR-040](ADR-040-opentelemetry-api-in-libraries-sdk-at-roots.md) | OpenTelemetry API in library code; the SDK only at composition roots | Accepted |
| [ADR-041](ADR-041-technical-debt-register.md) | Every subsystem maintains a Technical Debt Register | Accepted |
| [ADR-042](ADR-042-money-is-an-integer-count-of-minor-units.md) | Money is an integer count of minor units; Price carries a fixed high-precision scale | Accepted |
| [ADR-043](ADR-043-dimensional-typing-of-domain-quantities.md) | Dimensional typing: Money, Price, Quantity and Ratio are distinct types | Accepted |
| [ADR-044](ADR-044-rounding-is-always-explicit-and-named.md) | Rounding is always explicit and named after the rule it implements | Accepted |
| [ADR-045](ADR-045-quantity-is-unsigned-direction-is-side.md) | Quantity is unsigned; direction belongs exclusively to Side | Accepted |
| [ADR-046](ADR-046-trading-day-requires-a-calendar.md) | TradingDay cannot be constructed without a calendar | Accepted |
| [ADR-047](ADR-047-exceptions-only-no-result-type.md) | Exceptions are the single error-signalling mechanism; no Result type | Accepted |
| [ADR-048](ADR-048-no-float-in-the-monetary-modules.md) | Boundary rule R6: no float in the monetary modules | Accepted |
| [ADR-049](ADR-049-mutation-testing-for-the-financial-primitives.md) | Mutation testing for the financial primitives | Accepted |
| [ADR-050](ADR-050-shared-kernel-is-api-stable.md) | The shared kernel is API-stable from S03 | Accepted |
| [ADR-051](ADR-051-release-tags-are-immutable.md) | Release tags are immutable | Accepted |
| [ADR-052](ADR-052-separate-persistence-models-and-mappers.md) | Separate persistence models with an explicit mapping layer | Accepted |
| [ADR-053](ADR-053-unit-of-work-owns-the-transaction.md) | The Unit of Work owns the transaction; repositories never commit | Accepted |
| [ADR-054](ADR-054-timeseries-bypass-behind-a-dedicated-interface.md) | Bulk timeseries writes bypass the ORM behind a dedicated TimeSeriesStorage interface | Accepted |
| [ADR-055](ADR-055-expand-contract-migrations.md) | Expand/contract migrations; every migration declares its reversibility and operational impact | Accepted |
| [ADR-056](ADR-056-async-session-discipline.md) | Async session discipline: bounded, deterministic, never global | Accepted |
| [ADR-057](ADR-057-explicit-change-tracking.md) | Explicit change tracking with optimistic concurrency | Accepted |
| [ADR-058](ADR-058-integration-tests-use-a-real-database.md) | Integration tests run against real PostgreSQL with TimescaleDB | Accepted |
| [ADR-059](ADR-059-boundary-rules-r7-and-r8.md) | Boundary rules R7 and R8: the domain imports no persistence, the timeseries path imports no domain | Accepted |
| [ADR-060](ADR-060-benchmark-budgets-are-enforced-on-the-deployment-target.md) | Benchmark budgets are enforced on the deployment target, not the development machine | Accepted |
| [ADR-061](ADR-061-event-envelope-is-versioned-and-transport-agnostic.md) | The event envelope is versioned, self-describing and transport-agnostic | Accepted |
| [ADR-062](ADR-062-at-least-once-delivery-and-ordering-guarantees.md) | At-least-once delivery; per-aggregate ordering live, total deterministic ordering in replay | Accepted |
| [ADR-063](ADR-063-outbox-relay-is-a-dedicated-process.md) | The outbox relay is a dedicated process, not a Celery beat task | Accepted |
| [ADR-064](ADR-064-dead-letter-policy.md) | Dead-letter policy: classified failures, bounded retries, explicit replay | Accepted |
| [ADR-065](ADR-065-run-scoped-idempotency-ledger.md) | Consumers deduplicate through a run-scoped ledger written in their own transaction | Accepted |
| [ADR-066](ADR-066-scheduling-is-defined-in-trading-day-terms.md) | Scheduled work is defined in trading-day terms, not cron | Accepted |
| [ADR-067](ADR-067-eventstream-is-a-port-live-and-replay-are-peers.md) | EventStream is a port; live and replay are peer adapters | Accepted |
| [ADR-068](ADR-068-boundary-rule-r9-no-transport-in-domain-or-strategy.md) | Boundary rule R9: no strategy or domain module imports a transport | Accepted |
| [ADR-069](ADR-069-replay-is-bitemporally-correct-by-construction.md) | Replay is bitemporally correct by construction: as_of is structural, not a filter | Accepted |
| [ADR-070](ADR-070-envelope-encryption-behind-a-key-provider-port.md) | Envelope encryption with per-record data keys behind a KeyProvider port | Accepted |
| [ADR-071](ADR-071-the-audit-log-is-append-only-enforced-by-the-database.md) | The audit log is append-only, enforced by the database, and publishes AuditRecorded | Accepted |
| [ADR-072](ADR-072-access-tokens-are-stateless-refresh-tokens-are-stored-and-rotated.md) | Access tokens are short-lived and stateless; refresh tokens are stored, rotated on use, and revocable | Accepted |
| [ADR-073](ADR-073-authorisation-defaults-to-deny.md) | Authorisation defaults to deny; order placement is a separate permission gated on enrolled 2FA | Accepted |
| [ADR-074](ADR-074-rls-policies-are-authored-and-exercised-from-the-start.md) | Row-level security policies are authored *and exercised* from the start, permissive in v1 | Accepted |
| [ADR-075](ADR-075-reference-identity-and-watchlist-are-bitemporal-revisions.md) | Reference identity and watchlist membership are bitemporal revisions | Accepted |
| [ADR-076](ADR-076-kite-market-data-uses-a-narrow-direct-http-adapter.md) | Kite market data uses a narrow direct HTTP adapter | Accepted |
| [ADR-077](ADR-077-credential-purpose-separates-secret-lifecycles.md) | Bind a credential to its purpose, not just its broker | Accepted |

## Reading order for someone new

If you have twenty minutes and want to understand why the codebase looks the way
it does, read these six in order:

1. **ADR-001** — why a modular monolith and not services
2. **ADR-010** — why backtest, paper and live share one execution kernel
3. **ADR-007** — why every record carries two timestamps
4. **ADR-012** — why the risk gate is structurally unbypassable
5. **ADR-022** — why ambiguity blocks trading rather than proceeding
6. **ADR-028** — why there is no execution code in this repository yet
7. **ADR-029** — why nothing crosses a subsystem boundary uncommitted
8. **ADR-035** — why observability is built at S02 rather than at S41
9. **ADR-037** — why log redaction has two independent strategies
10. **ADR-042** — why money is an integer, and why paise were not enough
11. **ADR-043** — why `Money + Price` does not compile

## Architecture Revisions

Two approved revisions are recorded below. A revision is the heavier instrument,
reserved for changes that invalidate a gate or a scope parameter (plan section
1.2); it requires written Product Owner approval and, when the plan itself
changes, a reissued Master Project Plan.

| AR | Date | Change | Plan version |
|---|---|---|---|
| **AR-001b** | 2026-07-29 | **The Unit of Work stages events into the outbox; it does not publish them.** Amends the scope of ADR-053, which said events publish after commit. `add_event()` now writes an `OutboxRow` inside the caller's transaction; `_publish`, `_discard_events` and the publisher constructor argument are removed. Approved in writing by the Product Owner after an evidence review. | v1.6 (no plan change) |
| **AR-002** | 2026-08-02 | **DHRUVA becomes a private, two-user, end-of-day swing-research and paper-trading product.** The institutional S07–S46 delivery sequence and Gate G-MCP staging are indefinitely deferred as the active roadmap. Four product milestones now deliver unified equity/futures/news data; scanning, backtesting and isolated paper portfolios; news intelligence and optional validated ML ranking; then the private dashboard, alerts and operations. Options, live orders and public SaaS remain out of scope. The owner approved Zerodha market data up to ₹500/month, zero recurring news/inference spend, prospective point-in-time news archiving and local sentiment with a deterministic fallback. | Personal MVP v1.0 (reissued active plan) |

**Why AR-001b was necessary.** ADR-053's post-commit publication left a window in
which the transaction was durable and the event existed only in process memory: a
crash there lost the event while keeping the change that caused it — the
dual-write problem the outbox exists to solve, sitting beside the outbox. A
publisher failure also raised out of `commit()` for work that had succeeded, so a
retrying caller would apply a non-idempotent use case twice.

ADR-053 itself is unmodified, as ADR-027 requires of an accepted record. Its
transaction-ownership decision stands unchanged; only the event-publication
clause is superseded, and this row is where that is recorded.

**Why AR-002 was necessary.** The original plan optimised for a broad institutional
platform and sequenced forty remaining subsystems behind an analytics-thesis gate.
The approved product is narrower in users, instruments and execution risk, but it
needs a usable end-to-end research workflow sooner. The immutable `v0.6.0`
foundation and its controls remain; only the active scope, milestone sequence and
provider budget change. The historical plan is retained with a prominent deferral
notice, and `DHRUVA_PERSONAL_MVP_PLAN.md` is the required reissued plan.
