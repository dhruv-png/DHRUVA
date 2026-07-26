# S01 — Repository, Tooling & CI Skeleton

| Field | Value |
|---|---|
| Subsystem | S01 |
| Phase | P0 — Platform Kernel |
| Stage | 1 (MCP) |
| Depends on | — |
| Blocks | S02 and everything downstream |
| Complexity | M |
| Estimate | 4 sessions |
| Risk | LOW |
| Status | **COMPLETE — awaiting approval** |

---

## 1. Overview

S01 lays the foundation every other subsystem is built on: the monorepo layout,
the Python toolchain, the bounded-context package skeleton, the continuous
integration pipeline, and the local development stack.

Its distinguishing feature is that the architecture is **executable**. Master
The Master Project Plan makes strong claims — dependencies point inward, contexts talk
only through public APIs, decisions are immutable — and S01 turns each of those
claims into a program that fails the build when it stops being true. Risk R13 in
the plan's register is "the modular monolith degrades into a mud ball despite
intent"; intent is not a control, so S01 ships the controls.

## 2. Responsibilities

**Owns**

- Monorepo structure and the boundary between its six areas
- Python toolchain: `uv`, ruff, mypy, pytest, import-linter, pre-commit
- The nine bounded-context package skeletons and three composition roots
- Two executable architectural controls: `dhruva.tooling.boundaries`, `dhruva.tooling.adr_guard`
- CI and security pipelines
- Local development data stack (Docker Compose)
- The ADR corpus and its integrity guarantee

**Explicitly does not own**

- Configuration, logging, error taxonomy, tracing → **S02**
- Domain primitives (`Money`, `TradingDay`, `Clock`) → **S03**
- Database schema, migrations, repositories → **S04**
- Any application process with a runtime entrypoint → **S02 onward**

## 3. Functional Requirements

| # | Requirement | Verified by |
|---|---|---|
| FR-01 | A clean clone reaches a green `make check` with two commands | `make setup && make check` |
| FR-02 | Nine bounded contexts exist, each with four layers and a public `api` module | `test_repository_layout.py` |
| FR-03 | Layer ordering is enforced within every context | `lint-imports`, 4 contracts |
| FR-04 | Cross-context access is possible only via `api`, and only where the §5 matrix allows | `dhruva-check-boundaries` |
| FR-05 | The context dependency graph is provably acyclic | `test_dependency_matrix_is_acyclic` |
| FR-06 | Every ADR from the approved plan exists as a file, and accepted records are immutable | `dhruva-adr-guard`, `test_adr_guard.py` |
| FR-07 | Strict typing is enforced with no escape hatch | `mypy --strict`, `test_toolchain_config.py` |
| FR-08 | Banned constructs fail the build: naive datetimes, `print`, `TODO`, commented-out code | ruff rule families `DTZ`, `T20`, `FIX`, `ERA` |
| FR-09 | Local Postgres/TimescaleDB and Redis start with one command | `make up` |
| FR-10 | No order-constructing or broker-connecting code exists (ADR-028) | `test_no_order_placing_code_exists` |
| FR-11 | Stage 2 contexts contain no logic (ADR-028) | `test_stage_two_contexts_carry_no_logic` |
| FR-12 | Local and CI gates cannot drift apart | `test_precommit_runs_the_same_gates_as_ci` |

## 4. Architecture

### 4.1 Repository areas

```
dhruva/
├── backend/     Python. The only area with logic at S01.
├── frontend/    Next.js. Placeholder README; built in Stage 1's UI slice.
├── infra/       Compose, Dockerfile, Postgres init.
├── docs/        Plan, ADRs, subsystem designs, runbooks, session log.
├── research/    Notebooks. Quarantined from production by ADR-025.
└── .github/     CI and security workflows, PR template.
```

### 4.2 Two controls, two different jobs

The plan makes two structurally different architectural claims, and they need
different enforcement mechanisms.

**Layer ordering** is a containment relationship — `domain` is below
`application` is below `interfaces`. import-linter's `layers` contract with
`containers` expresses this natively, in nine lines of configuration, for all
nine contexts.

**Context reachability** is not a containment relationship. The rule is "context
A may reach context B, but only through `B.api`, and only if the matrix permits
it." import-linter cannot express the "only through `B.api`" half without
enumerating every source-target pair — 72 hand-written contracts that would rot
on the first rename. So that rule gets a purpose-built AST checker whose entire
policy is one readable dictionary, `ALLOWED_CONTEXT_DEPENDENCIES`, which is a
direct transcription of plan §5.

Splitting the enforcement this way means each tool does what it is good at, and
the rule most likely to be broken in practice has the checker with the clearest
error messages.

### 4.3 Decision-log immutability

ADR-027 promises that accepted architecture is never silently modified. The
mechanism is a SHA-256 of each record's body — excluding the two lines that
legitimately change, `Status` and `Superseded by` — stored in
`docs/adr/checksums.json`.

`dhruva-adr-guard --update` registers new records but **refuses to overwrite an
existing entry**. That refusal is the enforcement: changing a decision requires
writing a new record, which is visible in review, rather than editing an old one,
which is not.

### 4.4 Why Compose ships data services only

The Golden Rules forbid placeholders. Four application containers that start,
find nothing to serve, and exit would be exactly that. Compose ships PostgreSQL
with TimescaleDB and Redis — both real, both needed from S04 and S05 — and the
application services arrive with the subsystems that give them work.

## 5. Database Changes

No schema. Alembic owns schema from S04 onward.

One initialisation script, `infra/postgres/init/01-extensions.sql`, installs the
extensions the schema will require — `timescaledb`, `uuid-ossp`, `pg_trgm`,
`pg_stat_statements` — and sets the database timezone to UTC so that a client
which forgets to set it still writes correct data (ADR-006).

## 6. API Contracts

No HTTP API. S01's public contracts are two command-line tools:

| Command | Exit 0 | Exit 1 |
|---|---|---|
| `dhruva-check-boundaries [--source-root PATH]` | Import graph conforms | One line per violation: `path:line: [Rn] message` |
| `dhruva-adr-guard [--adr-dir PATH] [--update]` | Decision log intact | One line per problem: `file: [Cn] message` |

Both resolve the repository root from the `.dhruva-root` marker, so they behave
identically from any working directory.

## 7. Folder Structure

```
backend/src/dhruva/
├── __init__.py            package docstring, re-exports __version__
├── __about__.py           single source of truth for the version
├── py.typed               PEP 561 marker
├── contexts/
│   └── <nine contexts>/
│       ├── api.py         the ONLY import surface other contexts may use
│       ├── domain/        entities, value objects, ports. Zero I/O.
│       ├── application/   use cases, orchestration
│       ├── infrastructure/ adapters
│       └── interfaces/    routers, CLI, task entrypoints
├── shared/                shared kernel (S03)
├── tooling/               boundaries.py, adr_guard.py
├── api/ workers/ ingest/  composition roots
backend/tests/{unit,integration}/
```

## 8. Implementation

| Artefact | Notes |
|---|---|
| `tooling/boundaries.py` | 152 statements. AST-based, four rules, resolves relative and star imports, deduplicates the ambiguity expansion of `from a.b import c`. |
| `tooling/adr_guard.py` | 198 statements. Five checks, checksum registry, `--update` that refuses to overwrite. |
| 9 × context packages | Docstrings state the context's ID, purpose, published events, permitted dependencies and delivery stage. |
| `pyproject.toml` | Runtime dependencies empty, and a test asserts it — a dependency appearing here means a subsystem started without a design document. |
| `.importlinter` | 4 contracts, all kept. |
| `ci.yml` | Gates ordered cheapest-first: ruff → mypy → layers → boundaries → ADR guard → tests. |
| `security.yml` | pip-audit, gitleaks, Trivy. Weekly schedule so an advisory surfaces without waiting for a commit. |
| 28 ADR files | Generated from plan §4, checksum-registered. |

## 9. Testing

**162 tests, 100% statement and branch coverage of `dhruva`.**

| File | Tests | Covers |
|---|---|---|
| `test_repository_layout.py` | 60 | Structure, layers, public APIs, required files, Stage-1 constraints |
| `test_context_boundaries.py` | 17 | The live import graph, matrix acyclicity, and one self-test per rule |
| `test_adr_guard.py` | 24 | The live corpus, and one self-test per check C1–C5 |
| `test_tooling_cli.py` | 12 | Both entry points: exit codes, messages, edge cases |
| `test_toolchain_config.py` | 19 | The gates themselves — strict mypy, ruff families, coverage floor, marker set |

**The pattern worth noting.** Each control is tested twice: once against the real
codebase, and once against a synthetic tree that deliberately breaks it. A green
control that *cannot* fail is worse than no control, because it is trusted.
`test_toolchain_config.py` exists for the same reason at one level up — it makes
relaxing a gate a visible act that fails the build, rather than a one-line commit
nobody notices.

## 10. Optimisation

S01 has no runtime path, so optimisation targets feedback latency, which for a
solo engineer compounds across every future session.

| Measure | Result |
|---|---|
| Full test suite | 12.8 s |
| Boundary check, 74 modules | < 0.2 s |
| ADR guard, 28 records | < 0.1 s |
| CI gate ordering | Cheapest-first: a style error fails in seconds, not after the test suite |
| Docker layer split | Dependencies resolve in a cached layer; a code edit rebuilds in seconds |
| CI concurrency | In-progress runs cancelled on push |

## 11. Documentation

| Document | Purpose |
|---|---|
| `README.md` | What this is, how to run it, the controls table |
| `CONTRIBUTING.md` | The lifecycle, the rules, how to change an approved decision |
| `backend/README.md` | Layer and context rules, dependency policy |
| `docs/adr/README.md` | ADR lifecycle, statuses, what warrants a record |
| `docs/decisions.md` | Index of all 28 decisions, plus a six-ADR reading order for newcomers |
| `docs/runbooks/local-development.md` | Setup, the gates, failure modes and recovery |
| `docs/session-log.md` | Resumability (risk R15) |
| `.github/pull_request_template.md` | The Definition of Done as a checklist |

## 12. Future Improvements

Deferred deliberately, each with the trigger that should bring it forward.

| Improvement | Trigger |
|---|---|
| `uv.lock` committed and `UV_FROZEN` enforced | First real dependency (S02) |
| A `dhruva-check-adr-referenced` hook: PRs touching a public API or a migration must cite an ADR | S04, when migrations begin |
| Mutation testing on the two controls | If a control ever fails to catch a real breach |
| Architecture fitness report on a schedule — coupling and boundary metrics over time | Quarterly, from G1 |
| Generate the C4 context diagram from `ALLOWED_CONTEXT_DEPENDENCIES` | When the matrix first changes |
| Dev container | If a second engineer joins |

### How this could silently be wrong

- **The boundary checker analyses imports, not calls.** Code could reach across a
  boundary through a runtime lookup — `importlib`, a registry, a string-keyed
  factory — and the checker would not see it. Accepted for now; the honest
  mitigation is that such indirection is itself a review smell.
- **`ALLOWED_CONTEXT_DEPENDENCIES` is a transcription.** If it ever drifts from
  plan §5, the checker will enforce the wrong architecture perfectly. Mitigation:
  the matrix is quoted in `docs/decisions.md` reading order, and any change to it
  requires an ADR.
- **The ADR guard hashes bodies, not meaning.** Someone could write a new ADR
  that contradicts an old one without marking the old one superseded. No tool
  catches that; the reading-order section in `decisions.md` is the mitigation.
- **The Stage-2 emptiness test checks structure, not intent.** A module could
  stay a docstring while the same logic appears under `shared/`. The
  `test_no_order_placing_code_exists` textual scan is the crude backstop.
