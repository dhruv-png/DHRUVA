# Decision Index

Master index of every Architecture Decision Record. Generated content is not
acceptable here -- this file is reviewed, and reviewing it is how a decision that
should not have been made gets caught.

**50 decisions — 49 accepted, 1 superseded.** Records are immutable once accepted
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

None. A revision is the heavier instrument, reserved for changes that invalidate a
gate or a scope parameter (plan section 1.2). It requires written Product Owner
approval and a reissued Master Project Plan.

| AR | Date | Change | Plan version |
|---|---|---|---|
| — | — | — | — |
