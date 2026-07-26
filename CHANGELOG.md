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

[Unreleased]: https://github.com/OWNER/dhruva/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/dhruva/releases/tag/v0.1.0
