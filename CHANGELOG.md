# Changelog

All notable changes to D.H.R.U.V.A are recorded here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are written for a reader who was not present. `git log` already holds the
commit messages; this file explains what changed and why it mattered (ADR-030).

**Version scheme.** A completed subsystem bumps the minor version and is tagged
`v0.<nn>.0`, where `<nn>` is the subsystem number. Gates are tagged `gate-<id>`.
`v1.0.0` is reserved for Gate G5 — the first release capable of touching live
capital (ADR-026, ADR-029).

## [Unreleased]

Nothing yet.

## [0.3.0] — 2026-07-26 — S03 Domain Primitives & Shared Kernel

The vocabulary every later subsystem speaks. Forty-three subsystems remain and
every one will import from here, so this is the layer where correctness is worth
the most and churn costs the most.

### Added

- **`Money`** — an exact integer count of minor units, never a float. Exact
  addition, lossless allocation by largest remainder, rate application with a
  named policy.
- **`Price`** — an integer at a **fixed 8-decimal scale**. This is the change
  that matters most: NSE currency derivatives tick at ₹0.0025, and a price at
  paise scale would have silently rounded every one of them, invisibly, until a
  position failed to reconcile.
- **`Quantity` / `SignedQuantity` / `Side`** — quantity is unsigned; direction
  never lives in a sign. `lots()` requires an explicit `lot_size`, because the
  gap between "3 lots" and "3 contracts" is a 25-fold position error.
- **`Ratio`** — one type with percent, basis-point and fraction constructors.
- **`RoundingPolicy`** — named for the market rule, not the mathematical mode.
  `STT_NEAREST_RUPEE` can be checked against a SEBI circular; `ROUND_HALF_UP`
  cannot. `CONSERVATIVE_TO_TRADER` exists because optimistic cost estimates are
  how a strategy appears profitable and is not.
- **`TradingDay` / `TradingCalendar` / `TradingSession`** — a trading day cannot
  be constructed without a calendar, and there is no `timedelta` arithmetic.
  NSE moved expiry from Thursday to Tuesday on 2025-09-01; a backtest spanning
  that date that computes expiry by date arithmetic is wrong and nothing flags it.
- **`Clock`** — injected, never read from the wall clock, forward-only when frozen.
- **`InstrumentId` / `AccountId`** — UUIDs we mint. A test walks the module AST
  and fails if any executable reference to a broker identifier appears.
- **`DomainEvent`** — facts, not commands. Transport metadata belongs to S05's
  envelope, so a fact means the same thing replayed as it did on the wire.
- **`DateRange` / `TimeRange`** — half-open, so consecutive ranges tile without
  double-counting a boundary.
- **Boundary rule R6** — no float under `shared/money`, in any of its three
  shapes, while permitting the `isinstance` guards that reject floats.
- **`docs/DOMAIN.md`** — 719 lines explaining *why*, with a domain map and twelve
  worked examples of financial bugs this design prevents.

### Changed

- Hot-path guards rewritten from `invariant(cond, msg, **ctx)` to explicit
  branches. The helper evaluates its arguments eagerly, so every addition was
  building an f-string and two `str()` calls before checking anything.
  **`Money + Money`: 1.856 µs → 0.456 µs. One million additions: 2.007 s →
  0.545 s.** Invisible to every gate except a measured budget.

### Decisions

ADR-042 (supersedes ADR-005) · ADR-043 · ADR-044 · ADR-045 · ADR-046 · ADR-047 ·
ADR-048 · ADR-049 · ADR-050. **The shared kernel is API-stable from this release.**

### Known limitations

Mutation testing did not execute in the build environment and is recorded as
TD-12 at HIGH priority. Two performance budgets miss marginally (25% and 9%),
both measured on Python 3.10 against a 3.12 target, both recorded rather than
dropped.

## [0.2.0] — 2026-07-26 — S02 Core Runtime

The platform's cross-cutting capabilities, and its first runtime component.

### Added

- **Typed, fail-fast configuration** (`dhruva.shared.config`). Validated once at a
  composition root and injected as typed slices. Deployed environments actively
  *reject* development-shaped values — debug enabled, placeholder credentials,
  human-readable log format — because valid configuration that is wrong is more
  dangerous than invalid configuration, which every other check would catch.
- **Boundary rule R5.** The build fails if any module outside `shared.config`
  reads `os.environ`, `os.getenv` or `dotenv`. Detects all six spellings. This is
  what makes ADR-010's shared execution kernel possible: a strategy cannot behave
  differently in backtest and live if it cannot see the environment.
- **`SecretValue`** — a credential that refuses to render itself through `str`,
  `repr`, f-strings, format specifications, `%`-formatting, JSON or pickle. Format
  specs are ignored rather than honoured, so `f"{secret:.4}"` cannot become a
  prefix oracle. Hashing raises: a hashable secret becomes a dict key, and dict
  keys end up in logs.
- **Structured logging with two-strategy redaction.** By field name *and* by
  registered value, applied last in the processor chain so it also catches
  secrets merged in from context and exception arguments — which is where real
  leaks come from. Verified by a deliberate credential-leak suite.
- **Closed error taxonomy** — eight families, stable `DHR-XXX-NNN` codes pinned by
  snapshot test, structured context as fields rather than interpolated text. The
  `SAF` family gives "we could not establish it was safe, so we stopped" its own
  identity, so the Risk Engine can distinguish a refusal from a failure.
- **Correlation context** — correlation, causation and account identifiers via
  `contextvars`, inherited by nested binds so end-to-end tracing survives layering.
- **Health, readiness and metrics registries**, and the `dhruva-api` component
  exposing `/health`, `/ready` and `/metrics`. Health performs no dependency
  checks by design: an endpoint that consults the database turns a slow database
  into a restart loop. Readiness fails closed — a check that cannot be evaluated
  counts as not ready.
- **Ordered `bootstrap()`.** Settings, then logging, then secret registration,
  then tracing, then a banner stating whether tracing is actually active. The
  ordering is the design: reversing steps two and three would leave a window in
  which a credential could be logged in the clear.
- **Reproducible-build artefacts** — hash-pinned lockfiles, `docs/BUILD.md`, and a
  test asserting the three Python version declarations agree.
- **Performance budget suite** — six measured budgets, run separately from the
  inner loop.

### Changed

- `bind_correlation` rewritten from a `@contextmanager` generator to a class-based
  context manager: **17.7 µs → 2.0 µs** per call. It sits on the hot path of every
  request and every tick batch.
- The log redactor now caches its secret snapshot against a registry version
  counter instead of rebuilding two sets per record: **71.9 µs → 36.7 µs** per
  emission.

Neither optimisation would have been found without ADR-036's requirement to
measure before implementing.

### Decisions

ADR-031 (configuration placement), ADR-032 (reproducible builds), ADR-033
(secrets), ADR-034 (release governance), ADR-035 (observability first), ADR-036
(performance budgets), ADR-037 (redaction as a tested control), ADR-038 (error
taxonomy), ADR-039 (correlation), ADR-040 (OpenTelemetry split), ADR-041
(technical debt register).

### Known limitations

Eight open and three accepted items in the S02 technical debt register. The two
`HIGH` items — canonical `uv.lock`, and validation on Python 3.12 — share a single
trigger: access to a 3.12 host.

## [0.1.0] — 2026-07-26 — S01 Repository, Tooling & CI Skeleton

First subsystem. Establishes the foundation every later subsystem is built on,
and — more importantly — turns the approved architecture into controls that fail
the build when it stops holding.

### Added

- **Monorepo skeleton.** Six areas: `backend`, `frontend`, `infra`, `docs`,
  `research`, `.github`. Nine bounded-context packages, each with four Clean
  Architecture layers and a single public `api` module. Three composition roots.
- **`dhruva.tooling.boundaries`** — an AST-based checker enforcing four
  architectural rules: public-API-only access between contexts, the declared
  dependency matrix from plan §5, no inward dependency on composition roots, and
  a leaf-only shared kernel. Resolves relative, star and plain-`import` forms, so
  the rules cannot be evaded by changing import style.
- **`dhruva.tooling.adr_guard`** — decision-log integrity and immutability
  (ADR-027). SHA-256 of each record's body, excluding only the two lines that
  legitimately change. `--update` registers new records and refuses to overwrite
  an existing one; that refusal *is* the enforcement mechanism.
- **Toolchain**: uv, ruff (with `DTZ`, `T20`, `FIX`, `ERA`, `S`, `ASYNC`, `D`
  rule families selected against specific plan decisions), `mypy --strict`,
  pytest, import-linter, pre-commit.
- **CI and security pipelines.** Gates ordered cheapest-first so a style error
  fails in seconds rather than after the test suite. Weekly dependency, secret and
  container scans.
- **Local data stack.** PostgreSQL 16 with TimescaleDB, Redis 7.4, both with
  health checks. Container timezone pinned to UTC so any accidental local-time
  dependency fails loudly in development rather than silently in production.
- **28 Architecture Decision Records**, generated from the approved plan and
  checksum-registered.
- **162 tests, 100% statement and branch coverage** of `dhruva`. Each control is
  tested twice: against the real codebase, and against synthetic trees that
  deliberately break it.

### Notable decisions

- Context reachability is enforced by a purpose-built checker rather than by
  import-linter, which cannot express "only via `B.api`" without 72 hand-written
  contracts that would rot on the first rename. Layer ordering stays with
  import-linter, which expresses it natively.
- Docker Compose ships data services only. Four application containers with
  nothing to serve would be placeholders, and the Golden Rules forbid those.
- Runtime dependencies are empty, and a test asserts it — a dependency appearing
  there means a subsystem was started without a design document justifying it.

### Known limitations

Documented in full in `docs/subsystems/S01-repository-tooling-ci.md`:

- The boundary checker analyses imports, not calls. Runtime indirection —
  `importlib`, a registry, a string-keyed factory — is invisible to it.
- `ALLOWED_CONTEXT_DEPENDENCIES` is a transcription of plan §5. If it drifts, the
  checker will enforce the wrong architecture perfectly.
- The ADR guard hashes bodies, not meaning. A new ADR that contradicts an old one
  without superseding it passes every check.

[Unreleased]: https://github.com/dhruv-png/DHRUVA/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/dhruv-png/DHRUVA/releases/tag/v0.3.0
[0.2.0]: https://github.com/dhruv-png/DHRUVA/releases/tag/v0.2.0
[0.1.0]: https://github.com/dhruv-png/DHRUVA/releases/tag/v0.1.0
