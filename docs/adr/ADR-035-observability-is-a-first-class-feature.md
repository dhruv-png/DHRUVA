# ADR-035 — Observability is a first-class feature: every runtime component ships logs, correlation, health, readiness, metrics and traces

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.3, section 4. Introduced by Amendment A12 on approval of S01.

## Context

The platform will run unattended for most of a trading day, ingesting a
high-volume feed whose failure mode is *silence* rather than an exception. A
WebSocket that stops delivering ticks looks exactly like a quiet market. A regime
model reading stale data produces confident, wrong answers.

Plan section 16 places observability at S41. That is far too late: by S41 there
will be forty subsystems, none of which were built to be observed, and
retrofitting instrumentation into them is both expensive and unreliable —
retrofitted instrumentation measures what is easy to reach, not what matters.

## Decision

Every runtime component introduced from S02 onward exposes six things, at the
moment it is introduced:

1. **Structured logs** — JSON outside `local`, through the shared logger only.
2. **Correlation IDs** — bound at every process edge and present on every log
   record and span emitted downstream of that edge.
3. **A health endpoint** (`/health`) — liveness. Answers "is this process
   running?" It performs no dependency checks and must never fail because a
   downstream is unavailable, or a restart loop results.
4. **A readiness endpoint** (`/ready`) — answers "can this process do its job right
   now?" It checks declared dependencies and returns a per-dependency breakdown.
   Readiness is where fail-closed lives (ADR-022): unknown counts as not ready.
5. **A metrics endpoint** (`/metrics`) — Prometheus exposition.
6. **Trace instrumentation** — OpenTelemetry spans across process edges and
   external calls.

The distinction between health and readiness is load-bearing and is not a
formality. Conflating them is the most common way an orchestrator is made to
restart a healthy process because a database was briefly slow.

For components without an HTTP surface — the ingest and worker processes — the
same six obligations are met through a small dedicated admin listener rather than
being waived. A background process that cannot be asked whether it is working is
precisely the process whose failure goes unnoticed.

S02 delivers the mechanism: a health/readiness registry that components register
checks into, the metrics registry, the tracing facade, and the first component
that exposes them.

## Rationale

Observability retrofitted is observability shaped by what was easy to instrument.
Building it first inverts that: each subsequent subsystem declares its own health
checks and metrics as part of its Definition of Done, while the author still knows
what "working" means for it.

The cost is real — S02 grows from four sessions to roughly six, and takes on
FastAPI, uvicorn and a metrics client earlier than the plan assumed. That cost is
paid once. The alternative is paying it forty times, worse, at S41.

S41 does not disappear. It becomes what it should have been: dashboards, SLOs,
alert rules and runbooks built on instrumentation that already exists, rather than
the creation of that instrumentation.

## Consequences

**S02's scope expands.** It now delivers a minimal `dhruva-api` process exposing
exactly `/health`, `/ready` and `/metrics` and nothing else. This is a real,
tested service rather than a placeholder, so it does not conflict with the Golden
Rules.

**Every subsequent subsystem's Definition of Done gains observability items**: the
health checks it contributes, the metrics it emits, and the spans it opens.

**Dependencies arrive earlier than planned**: FastAPI, uvicorn and a Prometheus
client at S02 rather than S35 and S41. Each is justified in the S02 design
document, as ADR-030 requires.

**A no-op tracer is indistinguishable from a broken one.** The startup banner must
state whether tracing is active, or misconfiguration will present as an absence of
data rather than as an error.
