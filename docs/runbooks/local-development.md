# Runbook — Local Development

Covers: first-time setup, the quality gates, and what to do when one of them
fails. Written to be usable by someone returning to this repository after a long
gap (risk R15).

## Prerequisites

| Tool | Version | Why |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | ≥ 0.9 | Dependency and Python-version management |
| Docker + Compose v2 | current | PostgreSQL/TimescaleDB and Redis |
| git | ≥ 2.40 | — |

Python itself is not a prerequisite: `uv` fetches 3.12.

## First run

```bash
git clone <repo> && cd dhruva
make setup     # venv + all dependency groups          (~1 min)
make hooks     # install pre-commit hooks              (~10 s)
make up        # start Postgres and Redis              (~30 s first time)
make check     # every gate CI runs                    (~30 s)
```

Expected end state: `make check` green, `docker compose ps` showing both services
healthy.

## The gates

Run individually while working; `make check` runs all of them in CI's order.

| Command | Checks | Typical |
|---|---|---|
| `make lint` | ruff check and format | 1 s |
| `make types` | mypy strict (ADR-024) | 5 s |
| `make boundaries` | Layer contracts + context boundaries | 3 s |
| `make adr` | Decision-log integrity (ADR-027) | < 1 s |
| `make test` | 162 tests | 13 s |
| `make cov` | Tests with the 90% coverage floor | 14 s |

## Failure modes

### `boundaries: N violation(s)` with `[R1]`

A context imported another context's internals.

```
dhruva/contexts/analytics/application/x.py:12: [R1] context 'analytics' imports
'dhruva.contexts.marketdata.domain.bar', reaching into 'marketdata' internals.
```

**Fix.** Export what you need from `dhruva/contexts/marketdata/api.py` and import
that instead. If the type genuinely belongs to three or more contexts, it belongs
in `dhruva/shared/` — but apply that test honestly; premature promotion to the
shared kernel is how the kernel becomes a second mud ball.

### `boundaries: ... [R2]`

The dependency is not in the declared matrix.

**Fix.** Either it is a mistake — invert it with a port, as Risk does with
`PositionReader` — or the architecture is genuinely changing, in which case write
an ADR and update `ALLOWED_CONTEXT_DEPENDENCIES` in the same commit. Never update
the matrix alone; the matrix is the plan, and the plan is not edited quietly.

### `adr-guard: ... [C5] body has changed since it was accepted`

Someone edited an accepted decision. This is exactly what ADR-027 forbids.

**Fix.** Revert the file (`git checkout -- docs/adr/ADR-nnn-*.md`). If the
decision really is changing, write a new record, set the old one's status to
`Superseded by ADR-nnn`, and run `make adr-register`. Only `Status` and
`Superseded by` lines may change in an accepted record.

### `adr-guard: ... [C5] accepted but not registered`

A new record was added without registering its checksum.

**Fix.** `make adr-register`, then commit the record and `checksums.json`
together.

### `lint-imports: ... BROKEN`

A layer was violated — most often `domain` importing something with I/O.

**Fix.** The domain layer declares a **port** (an abstract interface); the
infrastructure layer implements it; the composition root wires them. If that feels
heavy for the case at hand, the logic probably belongs in `application`, not
`domain`.

### `ruff ... DTZ005`

A naive `datetime` was constructed. ADR-006 stores UTC and ADR-011 injects time.

**Fix.** Take a `Clock` dependency. Never call `datetime.now()` outside a clock
adapter — it defeats deterministic tests and the shared execution kernel.

### `ruff ... FIX002` / `ERA001`

A `TODO` or commented-out code survived. Golden Rules: finish it or delete it.
Commented-out code is what version control is for.

### `mypy ... [ignore-without-code]`

A bare `# type: ignore`. ADR-024 requires the error code and a linked issue:
`# type: ignore[arg-type]  # see #123`.

### Docker: `port is already allocated`

Something else holds 5432 or 6379.

**Fix.** Override in `infra/.env`: `POSTGRES_PORT=55432`, `REDIS_PORT=56379`.

### Database is wedged

Local data is disposable until S04 ships migrations:

```bash
make down
docker volume rm dhruva_postgres-data dhruva_redis-data
make up
```

## Adding a dependency

1. Confirm the subsystem design document justifies it.
2. `uv add --project backend <package>` (or `--group dev`).
3. Commit `pyproject.toml` and `uv.lock` together.
4. If it is the first runtime dependency, update
   `test_runtime_dependencies_are_still_empty` in the same commit — deliberately,
   not incidentally.

## Adding a subsystem

1. Write `docs/subsystems/Snn-<name>.md` first, using the 12-section format.
2. Write any ADRs the design implies, before the code.
3. Branch `snn-<slug>`.
4. Build it through all seven lifecycle steps.
5. `make check` green, then open a PR against the template checklist.
6. Add a `docs/session-log.md` entry.

## Stage 1 constraint

Until Gate G-MCP passes, no code that can construct or place an order may exist in
this repository (ADR-028). Two tests enforce it. If you find yourself needing to
disable one of them, that is the signal to stop and hold the G-MCP review.
