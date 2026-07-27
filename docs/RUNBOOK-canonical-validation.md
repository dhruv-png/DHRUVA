# Runbook — Canonical Validation (Windows)

The supported path on Windows is **PowerShell**. Not WSL, not Git Bash.

`scripts/canonical_validation.sh` targets Linux CI. On Windows it fails with
`uv: command not found` because WSL is a separate filesystem and PATH — your uv
and Docker Desktop live on the Windows side, not inside the WSL distribution.
Installing them twice to satisfy a script is the wrong fix.

Use `scripts/canonical_validation.ps1`.

---

## The one thing that will bite you

**Alembic does not read `DHRUVA_TEST_DATABASE_URL`.**

`alembic/env.py` builds its URL from validated settings, because ADR-031 makes
settings the single source of process configuration:

```python
config.set_main_option("sqlalchemy.url", database_url(load_settings().db))
```

`DHRUVA_TEST_DATABASE_URL` is read **only** by the integration harness. Setting
it and running `alembic` directly connects as the default user `dhruva` and
fails authentication — which is exactly the reported symptom, and is a defect in
the harness rather than in your setup.

Alembic reads these instead:

| Variable | Default |
|---|---|
| `DHRUVA_DB__HOST` | `localhost` |
| `DHRUVA_DB__PORT` | `5432` |
| `DHRUVA_DB__NAME` | `dhruva` |
| `DHRUVA_DB__USER` | `dhruva` |
| `DHRUVA_DB__PASSWORD` | `dhruva_local_only` |

The double underscore is the nested delimiter; a single one will not work.

`tests/integration/conftest.py` now translates the one URL into these variables
automatically, so the harness and Alembic can no longer disagree. Only a
*manual* `alembic` invocation needs them set by hand.

---

## Option A — your local PostgreSQL (fastest)

TimescaleDB must be installed; three integration tests exercise hypertable DDL.

```powershell
cd C:\Users\dhruv\Projects\DHRUVA

# 1. A dedicated database. Never run this against one holding real data:
#    the harness truncates tables and runs `alembic downgrade base`.
psql -U postgres -c "CREATE DATABASE dhruva_test;"
psql -U postgres -d dhruva_test -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

# 2. Run it.
.\scripts\canonical_validation.ps1 -DatabaseUrl "postgresql+asyncpg://postgres:postgres@localhost:5432/dhruva_test"
```

If the `CREATE EXTENSION` fails, TimescaleDB is not installed in that server —
use Option B rather than skipping it. Plain PostgreSQL cannot verify what those
tests verify.

## Option B — Docker Desktop (no local setup)

```powershell
cd C:\Users\dhruv\Projects\DHRUVA
docker info                        # must succeed before continuing
.\scripts\canonical_validation.ps1
```

The harness starts `timescale/timescaledb:2.17.2-pg16` via testcontainers and
disposes of it afterwards. First run pulls the image.

---

## What the script does, in order

1. Environment provenance — recorded first, so every later number is attributable
2. Database target and server versions, read from the running server rather than
   the image tag
3. Quality gates: ruff, format, mypy, import-linter, boundaries R1–R8, ADR guard
4. Unit suite, verbose
5. Migrations: `upgrade head` → `downgrade base` → `upgrade head`
6. Schema-drift check, printing the generated migration body if not empty
7. Integration suite, verbose
8. Benchmarks with `DHRUVA_CANONICAL_BENCHMARKS=1`

Failures are **recorded and the run continues**. A stage that stops on first
failure produces an evidence package with a hole in it.

Everything lands in `docs\evidence\s04-<timestamp>\`. Attach it whole.

---

## Expected outcomes

| Stage | Success looks like | If it fails |
|---|---|---|
| Gates | `All checks passed`, `Success: no issues`, `4 kept, 0 broken`, `OK`, `intact` | Genuine regression — send the log, do not proceed |
| Unit suite | `937 passed, 5 skipped` (the 5 are intentional `skip` calls) | Send the log |
| `30-migration-upgrade` | `Running upgrade -> 0001_initial` | Auth failure means the `DHRUVA_DB__*` translation did not apply — check `02-database-target.log` |
| `31/32` down + re-up | Both complete without error | A failing downgrade is a real finding; ADR-055 requires it to work |
| Drift check | "No changes in schema detected" | If a body is printed, models and migrations disagree — that is a genuine defect |
| Integration suite | 12 tests run | **Some may fail. They have never executed anywhere.** That is them working |
| Benchmarks | Numbers printed | Two known misses (TD-13, TD-14) may pass on real hardware |

**On the integration suite specifically:** expect failures on first contact. A
suite that passes the first time it ever runs has usually not been asserting
much. Send the log either way.

---

## Afterwards

1. Append the numbers to `docs/PERFORMANCE_BASELINE.md` under a new **E2**
   section. Do not edit the E1 rows.
2. Close or re-scope TD-13, TD-14, TD-18 against real figures.
3. `uv lock` on 3.12 to close TD-01, if it now succeeds.
4. Re-run gates, confirm a clean tree, merge `--no-ff`, then tag `v0.4.0` **once**
   (ADR-051).
