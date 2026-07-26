# S02 — Core Runtime: Configuration, Logging, Errors, Tracing

| Field | Value |
|---|---|
| Subsystem | S02 |
| Phase | P0 — Platform Kernel |
| Stage | 1 (MCP) |
| Depends on | S01 |
| Blocks | S03 and everything downstream |
| Complexity | M → **L** (rescoped by ADR-035) |
| Estimate | 4 → **6 sessions** (rescoped by ADR-035) |
| Risk | LOW (but see §12 — the redaction failure mode is HIGH) |
| Status | **STEP 2 — IMPLEMENTATION** (design approved 2026-07-26; revised for ADR-031 … ADR-036) |

---

## 1. Overview

S02 gives the platform the four cross-cutting capabilities every subsequent
subsystem assumes exist: **typed configuration**, **structured logging**, a
**typed error taxonomy**, and **request-scoped correlation with tracing**.

These are the least glamorous and most load-bearing modules in the codebase.
Every one of the remaining 44 subsystems will import them, and every one of them
is expensive to change later — not because the code is complex, but because by
S10 there will be several hundred call sites.

Two things distinguish this design from a conventional "add logging and settings"
subsystem:

1. **There is no ambient global configuration.** Configuration is read exactly
   once, at a composition root, validated, and injected as typed values. No module
   anywhere reads an environment variable. This is what makes S26's shared
   execution kernel (ADR-010) possible: a strategy cannot behave differently in
   backtest and live because it cannot see the environment.
2. **Redaction is a tested control, not a convention.** The platform will hold
   credentials that can move money (ADR-020). A log-redaction filter that has
   never been proven to fire is a security theatre. S02 ships a deliberate
   credential-leak test that fails the build if a secret can reach a log sink.

## 2. Responsibilities

**Owns**

| Capability | Module |
|---|---|
| Environment model and settings schema | `dhruva.shared.config` |
| Structured logging, processors, redaction | `dhruva.shared.logging` |
| Error taxonomy and error codes | `dhruva.shared.errors` |
| Correlation / causation / account context | `dhruva.shared.context` |
| Tracing facade and span helpers | `dhruva.shared.observability` |
| Composition-root bootstrap sequence | `dhruva.shared.runtime` |
| Health and readiness check registry | `dhruva.shared.observability.health` |
| Metrics registry and exposition | `dhruva.shared.observability.metrics` |
| The first runtime component: `/health`, `/ready`, `/metrics` | `dhruva.api` |

> **Scope expanded by ADR-035.** Observability is no longer deferred to S41.
> S02 now delivers both the mechanism — a registry that later subsystems declare
> checks and metrics into — and the first component that exposes it. That
> component is a real, tested HTTP service with exactly three endpoints, not a
> placeholder.

**Explicitly does not own**

- `Money`, `Quantity`, `TradingDay`, `Clock`, `Result` → **S03**
- Database engine, sessions, Unit of Work → **S04**
- Event envelope and bus adapters → **S05**
- Secret *storage* and envelope encryption → **S06**. S02 owns how a secret is
  *handled in memory and kept out of logs*; S06 owns how it is persisted.
- Dashboards, SLO definitions, alert rules and runbooks → **S41**. S02 ships the
  instrumentation; S41 builds the operational layer on top of it. The split moved
  here from "S41 builds everything" when ADR-035 was accepted.
- Business endpoints of any kind → **S35**. The `dhruva-api` process delivered here
  serves three observability endpoints and nothing else.

## 3. Functional Requirements

| # | Requirement | Verified by |
|---|---|---|
| FR-01 | Settings load from environment and `.env`, with a `DHRUVA_` prefix and nested delimiter | unit + integration |
| FR-02 | Invalid or missing required configuration fails at startup with a message naming the field, never at first use | `test_settings_fail_fast` |
| FR-03 | The environment is a closed enum: `local`, `test`, `staging`, `production` | unit |
| FR-04 | Production configuration rejects development defaults — debug on, permissive CORS, placeholder secrets | `test_production_rejects_unsafe_defaults` |
| FR-05 | Secrets are held in a `SecretStr`-like type whose `repr`, `str` and JSON forms never reveal the value | property test |
| FR-06 | Logs are JSON in non-local environments, human-readable in `local` | unit |
| FR-07 | Every log record carries `correlation_id` where one is bound | unit |
| FR-08 | A global redaction processor removes secrets by key name **and** by value match | **deliberate leak test** |
| FR-09 | Every error derives from `DhruvaError` and carries a stable, machine-readable code | unit + registry test |
| FR-10 | Error codes are unique across the taxonomy and stable across releases | `test_error_codes_are_unique_and_stable` |
| FR-11 | Correlation context propagates across `async` boundaries and does not leak between tasks | async isolation test |
| FR-12 | Tracing is a no-op unless a composition root configures an exporter | unit |
| FR-13 | No module outside `shared.config` reads `os.environ` | **new boundary rule R5** |
| FR-14 | Timestamps in logs are UTC and ISO-8601 with an explicit offset (ADR-006) | unit |
| FR-15 | `/health` returns 200 whenever the process is running, and performs **no** dependency checks | unit |
| FR-16 | `/ready` returns a per-dependency breakdown; an unknown dependency counts as not ready (ADR-022) | unit |
| FR-17 | `/metrics` serves Prometheus exposition format with the platform's naming convention | unit |
| FR-18 | Subsystems register health checks and metrics through a registry rather than editing the API | unit |
| FR-19 | The startup banner states whether tracing is active, so misconfiguration is visible | unit |
| FR-20 | No secret appears in any endpoint response, including `/ready` failure detail | **deliberate leak test** |
| FR-21 | `.python-version`, `requires-python` and the CI matrix agree (ADR-032) | `test_python_version_is_consistent` |
| FR-22 | No `.env` file is tracked; `.env.example` contains no realistic value (ADR-033) | `test_secret_hygiene` |

## 3.5 Performance Budgets (ADR-036)

Declared before implementation. Each is measured by a benchmark in
`backend/tests/benchmarks/`, and measured values are recorded in the release notes
for `v0.2.0`.

| Budget | Target | Why this number | Benchmark |
|---|---|---|---|
| Settings load and validation | **< 50 ms** | Startup is on the critical path of the daily Market Open Ritual (ADR-021), which is human-attended | `bench_settings_load` |
| Process start → `/ready` returns 200 | **< 2 s** cold | An orchestrator restart must not extend a market-hours outage | `bench_bootstrap` |
| `/health` latency | **p99 < 5 ms** | Liveness probes run every few seconds; anything slower distorts probe budgets | `bench_health_endpoint` |
| `/ready` latency, all checks healthy | **p95 < 50 ms**, **p99 < 100 ms** | Must stay well inside a 1 s probe timeout even with dependency checks | `bench_ready_endpoint` |
| `/metrics` render, 500 series | **p95 < 100 ms** | Prometheus default scrape interval is 15 s; a slow endpoint causes gaps | `bench_metrics_render` |
| Log record emission, JSON, redaction on | **p99 < 100 µs** | Tick ingestion (S10) may log at hundreds of events per second; logging must not become the bottleneck | `bench_log_emit` |
| Redaction overhead vs. no redaction | **< 25%** | The control must be cheap enough that nobody is tempted to disable it | `bench_log_emit` |
| Correlation bind + unbind | **p99 < 10 µs** | Bound once per request and per tick batch | `bench_context_bind` |
| Steady-state RSS, api process idle | **< 120 MB** | Target deployment is a single modest VM shared with Postgres and Redis | `bench_memory_footprint` |
| RSS growth over 100k log records | **< 5 MB** | Detects the classic leak: unbounded context or an ever-growing secret registry | `bench_memory_footprint` |

Two of these are deliberately aggressive. The redaction overhead ceiling exists
because a security control that costs 3× will eventually be switched off "just for
this one hot path". The log-emission budget exists because S10 will be the first
place where logging volume could plausibly become a throughput problem, and
discovering that at S10 would be too late to change the design cheaply.

## 4. Architecture

### 4.1 Placement, and why it is not in a context

All six modules live in `dhruva.shared`, not in the Platform context (C9).

The reason is the boundary rule already enforced by S01: `dhruva.shared` is a leaf
and may be imported by every context, whereas a context may only be imported by
contexts the matrix permits. Errors, logging and correlation are needed by *every*
layer of *every* context, including `domain`. Putting them in C9 would require
every context to declare a dependency on C9, which would make the matrix almost
fully connected and drain it of meaning.

The plan's §5 assignment of "config" to C9 stands for *account-scoped, persisted*
configuration — feature flags, user preferences — which S06 and later subsystems
will own. Process configuration is a different thing and belongs in the kernel.

> Recorded as **ADR-031**, approved as a clarification of §5 rather than a
> redesign of it, so the distinction is not relitigated at S06.

### 4.2 Configuration: read once, inject typed values

```
        environment / .env
                │
                ▼
    ┌───────────────────────────┐
    │  Settings (pydantic)      │   validated once, at process start
    │   ├── app: AppSettings    │
    │   ├── log: LogSettings    │
    │   ├── db:  DatabaseSettings│  (schema only at S02; used from S04)
    │   ├── redis: RedisSettings │  (schema only at S02; used from S05)
    │   └── otel: TracingSettings│
    └───────────────────────────┘
                │  injected
                ▼
      composition root (api / workers / ingest)
                │  passes typed slices
                ▼
      application services, adapters
```

Contexts receive `LogSettings` or `DatabaseSettings`, never `Settings`. A module
that needs the whole settings object is a module that knows too much.

**The rule that makes this real:** a new boundary-checker rule, **R5**, fails the
build if any module outside `dhruva.shared.config` imports `os.environ`,
`os.getenv`, or `dotenv`. Without R5, "inject configuration" is a convention that
survives until the first inconvenient Tuesday.

### 4.3 Logging: structlog, with redaction as a processor

Processor chain, in order:

```
  merge_contextvars      ← correlation_id, causation_id, account_id
  add_log_level
  add_timestamp(UTC, ISO-8601)
  add_service_context    ← service name, version, environment
  REDACT                 ← by key name and by registered secret value
  format_exc_info
  JSONRenderer | ConsoleRenderer
```

Redaction is deliberately placed **last before rendering**, so it also catches
secrets that earlier processors merged in from context or from exception
arguments — which is exactly where real leaks come from.

Two redaction strategies, because either alone is insufficient:

- **By key.** Any field whose name matches a sensitive pattern (`password`,
  `token`, `secret`, `api_key`, `access_token`, `authorization`, `totp`, …) is
  replaced with `«redacted»`.
- **By value.** Secrets constructed through the platform's secret type register
  their value in a process-local set; the processor scans rendered values for
  those strings. This catches the case that key-based redaction always misses:
  a secret interpolated into a message string or an exception argument.

Value-scanning has a cost, and it is bounded deliberately: only values from the
registry, only strings of at least 8 characters, and the registry is small
(single-digit entries).

### 4.4 Errors: a closed taxonomy with stable codes

```
DhruvaError                      code, message, context, is_retryable
├── ConfigurationError           DHR-CFG-*
├── ValidationError              DHR-VAL-*
├── NotFoundError                DHR-NFD-*
├── ConflictError                DHR-CFL-*
├── PermissionError              DHR-PRM-*
├── ExternalServiceError         DHR-EXT-*
│   ├── UpstreamUnavailableError
│   ├── UpstreamTimeoutError
│   └── RateLimitedError
├── DataQualityError             DHR-DQL-*
│   ├── StaleDataError
│   └── MissingDataError
└── SafetyError                  DHR-SAF-*      ← ADR-022, fail-closed
    ├── PreconditionUnknownError
    └── DegradedModeError
```

Three properties matter more than the shape:

- **Stable codes.** `DHR-DQL-001` means the same thing in 2028. A test asserts
  uniqueness and pins the current set, so renaming one is a visible act.
- **Structured context, not string formatting.** `raise StaleDataError(instrument_id=..., age_seconds=...)`,
  never an f-string. The context lands in the log record as fields, which is what
  makes logs queryable.
- **`SafetyError` is its own branch.** ADR-022 requires ambiguity to fail closed.
  Giving "we do not know, so we stopped" its own error family means the Risk
  Engine (S25) can distinguish it from an ordinary failure and the UI can say so
  honestly rather than reporting a generic error.

### 4.5 Correlation and tracing

`contextvars` carry `correlation_id`, `causation_id` and `account_id`. They bind at
the process edge — an HTTP request, a Celery task, a tick batch — and propagate
automatically across `await` boundaries, including into `asyncio.TaskGroup`
children.

Tracing uses the **OpenTelemetry API** in library code and the **SDK only at
composition roots**. The API is a no-op facade when no SDK is configured, so tests
and library consumers pay nothing, and there is no need to invent a `Tracer` port
of our own — wrapping a facade in another facade adds a layer and removes nothing.

### 4.6 Bootstrap sequence

`dhruva.shared.runtime.bootstrap()` is the single ordered entry point every
composition root calls:

```
1. load settings                 → fail fast on invalid configuration
2. configure logging             → nothing before this point may log
3. register known secret values  → redaction active from here
4. configure tracing             → no-op unless an exporter is set
5. log a startup banner          → service, version, environment, git sha
6. return the Settings object
```

The ordering is the design. Configuring logging before settings means startup
errors are unstructured; registering secrets after logging is configured but
before anything uses them means there is no window in which a secret could be
logged in the clear.

## 5. Database Changes

None. S02 defines `DatabaseSettings` and `RedisSettings` as validated schema so
that S04 and S05 inherit a configured, tested connection description — but S02
opens no connection and creates no table.

## 6. API Contracts

No HTTP endpoints. S02's contracts are Python surfaces:

| Surface | Contract |
|---|---|
| `bootstrap(env_file: Path \| None = None) -> Settings` | Ordered startup; raises `ConfigurationError` on invalid config |
| `get_logger(name: str) -> BoundLogger` | The only sanctioned way to obtain a logger |
| `bind_correlation(correlation_id, causation_id, account_id) -> ContextToken` | Binds request scope; token restores on exit |
| `DhruvaError(code, message, **context)` | Base of the taxonomy; `.to_dict()` renders safely |
| `SecretValue(str)` | Never reveals itself in `repr`, `str`, or serialisation |

An error-code-to-HTTP-status mapping is defined here as data but applied at the
edge in S35. Domain errors do not know about HTTP.

## 7. Folder Structure

```
backend/src/dhruva/shared/
├── __init__.py            re-exports the small public surface
├── config/
│   ├── __init__.py
│   ├── environment.py     Environment enum + predicates
│   ├── secret.py          SecretValue and the redaction registry
│   └── settings.py        Settings and its nested models
├── errors/
│   ├── __init__.py
│   ├── base.py            DhruvaError, ErrorCode
│   └── taxonomy.py        the closed set of error classes
├── logging/
│   ├── __init__.py
│   ├── processors.py      redaction, service context, UTC timestamps
│   └── setup.py           configure_logging(), get_logger()
├── context.py             contextvars + bind_correlation()
├── observability.py       tracer accessor, span helpers
└── runtime.py             bootstrap()

backend/src/dhruva/shared/observability/
├── __init__.py            tracer accessor, span helpers
├── health.py              health/readiness registry, check protocol
└── metrics.py             metric registry, exposition

backend/src/dhruva/api/
├── __init__.py
├── app.py                 minimal FastAPI app: /health, /ready, /metrics only
└── __main__.py            uvicorn entrypoint

backend/tests/unit/shared/
├── test_settings.py
├── test_secret_value.py
├── test_logging_redaction.py     ← includes the deliberate leak test
├── test_errors.py
├── test_context.py
├── test_health_registry.py
├── test_metrics.py
└── test_bootstrap.py
backend/tests/unit/api/
└── test_observability_endpoints.py
backend/tests/benchmarks/            ← marked `slow`, excluded from the inner loop
```

## 8. Implementation

| Module | Statements | What it owns |
|---|---|---|
| `shared/errors/` | 71 | Eight-family closed taxonomy, stable codes, structured context (ADR-038) |
| `shared/config/environment.py` | 17 | Closed environment enum with security-relevant predicates |
| `shared/config/secret.py` | 44 | `SecretValue` — non-disclosing credential type (ADR-033) |
| `shared/config/settings.py` | 99 | Typed settings, fail-fast validation, unsafe-deployment rejection (ADR-031) |
| `shared/context.py` | 55 | Correlation/causation/account propagation (ADR-039) |
| `shared/logging/` | 84 | structlog chain with two-strategy redaction (ADR-037) |
| `shared/observability/` | 113 | Health/readiness registry, metrics registry, tracing facade (ADR-035, ADR-040) |
| `shared/runtime.py` | 23 | Ordered `bootstrap()` |
| `api/` | 46 | The observability component: `/health`, `/ready`, `/metrics` |
| `tooling/boundaries.py` | +26 | Boundary rule **R5** |

**Seven new dependencies**, each justified above and pinned exactly. The
OpenTelemetry SDK is an optional extra, not a runtime dependency, and a test
asserts the split (ADR-040).

**Two optimisations found by the budget suite**, both real rather than cosmetic:

1. `bind_correlation` was a `@contextmanager` generator. The generator machinery
   dominated a call on the hot path of every request and tick batch. Rewritten as
   a class-based context manager: **17.7 µs → 2.0 µs**.
2. The redactor rebuilt its secret snapshot on **every log record** — two set
   constructions per record. Cached against a registry version counter:
   **71.9 µs → 36.7 µs** per emission.

Neither would have been found without ADR-036's requirement to measure.

## 9. Testing

**335 tests, 99.9% statement coverage, 99.5% branch coverage.** Six benchmarks
run separately.

| Suite | Focus |
|---|---|
| `test_errors.py` | Code uniqueness, pinned snapshot, `repr` non-disclosure, pickling |
| `test_secret_value.py` | Seven rendering paths, pickle/hash refusal, property test |
| `test_settings.py` | Fail-fast, unsafe-deployment rejection, env-file precedence |
| `test_context.py` | Nesting, restoration on exception, async isolation, thread limitation |
| `test_logging_redaction.py` | **The deliberate credential-leak suite** |
| `test_observability.py` | Health/readiness semantics, timeout, concurrency, naming |
| `test_observability_endpoints.py` | All three endpoints, correlation, no-leak assertions |
| `test_bootstrap.py` | Ordering, banner contents, post-bootstrap secret registration |
| `test_secret_hygiene.py` | Repository-level ADR-033 invariants |
| `test_tooling_cli.py` | R5 across six spellings, plus its exemptions |

The leak suite is the one that matters. It attacks redaction from every direction
a real leak takes — named fields, interpolated messages, nested structures,
exception arguments, bound context — and asserts that ordinary content survives.

## 10. Optimisation — Measured Budget Compliance

| Budget | Target | Measured | Status |
|---|---|---|---|
| Settings load + validation | < 50 ms | **0.89 ms** (p95) | PASS |
| Correlation bind + unbind | < 10 µs | **2.01 µs**/call | PASS |
| Log emit, JSON, redaction on | < 100 µs | **36.7 µs**/call | PASS |
| Redaction overhead vs. off | < 1.25× | **1.02×** | PASS |
| Metrics render, 500 series | < 100 ms | **7.62 ms** (p95) | PASS |
| Readiness eval, 4 checks | < 100 ms | **0.45 ms** (p99) | PASS |

**All six pass.** Figures are from Python 3.10; the 3.12 target is materially
faster, so these are conservative.

**Measurement methodology, revised during implementation.** Millisecond-scale
budgets use tail measures (p95/p99), because the tail is what an operator
experiences. Microsecond-scale budgets use best-of-batched-means, because a
per-iteration `perf_counter` costs a large fraction of the thing being timed and
a per-iteration tail reports the garbage collector rather than the code. The
first measurement pass showed 5× run-to-run variance before this change. Recorded
here rather than silently applied, as ADR-036 requires.

Two budgets — startup-to-ready and steady-state RSS — are deferred to S04, when
there is a database connection to make them meaningful. Recorded in §13.

## 11. Documentation

ADR-037 … ADR-041 written; `docs/decisions.md` regenerated (41 records);
`docs/BUILD.md` created; `CHANGELOG.md` and `docs/releases/v0.2.0.md` written;
`Makefile` gains `bench` and `lock`; session log updated.

## 12. Future Improvements

| Improvement | Trigger |
|---|---|
| Trace context propagation across process boundaries | S05, when the event envelope exists |
| `/ready` result caching under probe pressure | If probe frequency ever shows in the metrics |
| Structured log sampling for high-volume paths | S10, if tick logging approaches its budget |
| OpenTelemetry log correlation (trace_id on records) | When the SDK is first configured in a deployed environment |

## 13. Technical Debt Register (ADR-041)

| # | Item | Rationale for deferral | Effort | Priority | Milestone | Status |
|---|---|---|---|---|---|---|
| TD-01 | Migrate to canonical `uv.lock` + `uv sync --frozen` | `uv lock` needs a 3.12 interpreter the sandbox cannot download. Hash-pinned `uv pip compile` output gives the same guarantee meanwhile. | 0.5 | **HIGH** | First 3.12 host | OPEN |
| TD-02 | Validate on Python 3.12 and remove the compatibility shim | 3.12 build artefact unreachable here. Source already targets 3.12; the shim never ships. | 0.5 | **HIGH** | Before G1 | OPEN |
| TD-03 | Remove `UP047` suppression (PEP 695 type parameters) | Adopting the syntax now would make the module unparseable by the verification interpreter, i.e. untested. | 0.1 | MEDIUM | With TD-02 | OPEN |
| TD-04 | Remove `asyncio.TimeoutError` from the timeout except clause | Redundant on 3.12; present so the sandbox exercises the same branch rather than skipping it. | 0.1 | LOW | With TD-02 | OPEN |
| TD-05 | Pin container images by `@sha256:` digest | Tags are mutable, but no deployed environment exists yet, so drift has no consequence today. | 0.5 | MEDIUM | S10 (first deploy) | OPEN |
| TD-06 | Value-based redaction cannot see a transformed secret (base64, truncated, in a URL) | No general solution exists. Key-matching is the first net; this is the second. Cost of chasing it exceeds the residual risk. | — | MEDIUM | — | **ACCEPTED** |
| TD-07 | `contextvars` do not reach threads or process pools | Intrinsic to the mechanism. `copy_context_into` is the remedy and is tested; the limitation is asserted so it is documented behaviour. | — | LOW | — | **ACCEPTED** |
| TD-08 | Startup-to-ready and RSS budgets unmeasured | Both are meaningless until there is a real dependency to connect to. | 0.5 | MEDIUM | S04 | OPEN |
| TD-09 | Trace context does not cross process boundaries | Requires the event envelope, which does not exist until S05. | 1.0 | MEDIUM | S05 | OPEN |
| TD-10 | `/ready` has no result caching | Premature without evidence of probe pressure. Adding a cache would also add a staleness question nobody has asked yet. | 0.5 | LOW | S41 | OPEN |
| TD-11 | `configure_logging` carries a `PLR0913` suppression | Six independent keyword-only settings; grouping them into an object adds a type without adding meaning. | — | LOW | — | **ACCEPTED** |

Eight open, three accepted. TD-01 and TD-02 are the only `HIGH` items and share
a single trigger: access to a Python 3.12 host.

### How this could silently be wrong

- **Value-based redaction is best-effort** (TD-06). A transformed secret escapes.
- **`contextvars` do not propagate into threads** (TD-07). Work handed to a pool
  loses correlation with no error.
- **Fail-fast covers only what is declared.** A field with a plausible but wrong
  default fails nothing — which is why FR-04 requires deployed environments to
  *reject* development-shaped values rather than merely accept what they are given.
- **A no-op tracer is indistinguishable from a broken one.** Mitigated by the
  startup banner reporting `tracing_active`, but only if someone reads it.
- **Benchmarks measure a 3.10 interpreter.** Figures are conservative rather than
  wrong, but the *ratios* between operations could shift on 3.12.
