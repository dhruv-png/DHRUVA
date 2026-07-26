# S02 — Core Runtime: Configuration, Logging, Errors, Tracing

| Field | Value |
|---|---|
| Subsystem | S02 |
| Phase | P0 — Platform Kernel |
| Stage | 1 (MCP) |
| Depends on | S01 |
| Blocks | S03 and everything downstream |
| Complexity | M |
| Estimate | 4 sessions |
| Risk | LOW (but see §12 — the redaction failure mode is HIGH) |
| Status | **STEP 1 OF 7 COMPLETE — architecture approved for implementation** |

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

**Explicitly does not own**

- `Money`, `Quantity`, `TradingDay`, `Clock`, `Result` → **S03**
- Database engine, sessions, Unit of Work → **S04**
- Event envelope and bus adapters → **S05**
- Secret *storage* and envelope encryption → **S06**. S02 owns how a secret is
  *handled in memory and kept out of logs*; S06 owns how it is persisted.
- Metrics and Prometheus wiring → **S41**. Deferred deliberately: there is nothing
  to measure until there is a runtime path, and a metrics registry built now would
  be a placeholder.

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

> This is a clarification of §5, not a departure from it, and is recorded as
> **ADR-031** so the distinction is not relitigated later.

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

backend/tests/unit/shared/
├── test_settings.py
├── test_secret_value.py
├── test_logging_redaction.py     ← includes the deliberate leak test
├── test_errors.py
├── test_context.py
└── test_bootstrap.py
```

## 8–12. Implementation, Testing, Optimisation, Documentation, Future Improvements

To be completed in Step 2 onward. Planned content:

- **New dependencies**, each justified: `pydantic`, `pydantic-settings`,
  `structlog`, `opentelemetry-api`, `opentelemetry-sdk`. The
  `test_runtime_dependencies_are_still_empty` assertion from S01 is replaced in
  the same commit by an explicit allowlist test, so the list stays deliberate.
- **New boundary rule R5** in `dhruva.tooling.boundaries`, with self-tests.
- **New ADRs**: ADR-031 (configuration is injected, never ambient), ADR-032
  (redaction is a tested control), ADR-033 (typed errors with stable codes),
  ADR-034 (correlation via contextvars), ADR-035 (OTel API in libraries, SDK at
  composition roots).
- **Testing**: property tests on `SecretValue` non-disclosure; async isolation
  tests for context leakage; the deliberate credential-leak test; a snapshot test
  pinning the error-code registry.

### How this could silently be wrong

Recorded now, before implementation, because these are the failure modes the
design must be built to expose:

- **Value-based redaction is best-effort.** A secret that is transformed before
  logging — base64-encoded, truncated, embedded in a URL — will not match the
  registry. Mitigation is that key-based redaction covers the common paths and
  that no code should be logging credentials at all; the value scan is a second
  net, not the first.
- **`contextvars` do not propagate into threads or process pools.** Any code that
  hands work to a `ThreadPoolExecutor` loses correlation silently. The bootstrap
  must provide a context-copying wrapper, and its absence will be invisible until
  someone reads a log without an ID.
- **Fail-fast configuration only covers what is declared.** A field with a
  plausible default that is wrong in production fails nothing. This is why FR-04
  exists — production must actively reject development-shaped values, not merely
  accept whatever it is given.
- **A no-op tracer is indistinguishable from a broken one.** If the SDK is
  misconfigured, spans vanish quietly. The startup banner must state whether
  tracing is active, so the absence of traces is diagnosable.
