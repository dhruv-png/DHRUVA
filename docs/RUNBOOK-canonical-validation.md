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

`tests/integration/conftest.py` translates the one URL into these variables
automatically, so the harness and Alembic can no longer disagree. Only a
*manual* `alembic` invocation needs them set by hand.

### Why that translation was not enough on its own

Stages 30–35 run `alembic` as **standalone processes, outside pytest**. A
container started by testcontainers exists only inside the pytest process, for
the lifetime of the test session — so in the first version of this script, a run
without `-DatabaseUrl` reached the migration stages with no database in
existence and no `DHRUVA_DB__*` set, fell through to the settings defaults, and
failed as user `dhruva`. The translation in `conftest.py` was never consulted,
because pytest had not started yet.

**A database is now mandatory and the script provisions it before any stage
runs.** When `-DatabaseUrl` is omitted the script starts and owns the container
itself, and every stage — Alembic and pytest alike — shares that one database.

---

## Which option to use

**Run it with no arguments unless you have a specific reason not to.** Supplying
`-DatabaseUrl` tells the script you are providing the database yourself, and it
will not start one. Pointing `-DatabaseUrl` at `localhost:55432` — the port the
script's own container uses — asks for a database nobody started, and the probe
stops the run with `ConnectionRefusedError`.

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

The script starts `timescale/timescaledb:2.17.2-pg16` on port **55432** — not
5432, so it cannot collide with your local PostgreSQL — waits for
`pg_isready`, and removes the container in a `finally` block even if the run
throws. First run pulls the image, which takes a few minutes.

It does **not** create the TimescaleDB extension on this path. The image
installs it into `template1`, so `POSTGRES_DB` inherits it, and the image's own
init script creates it as well. Racing that init made even
`CREATE EXTENSION IF NOT EXISTS` fail with a duplicate key on
`pg_extension_name_index` — `IF NOT EXISTS` reads the catalogue before the
concurrent init commits, then collides with it. The extension is verified by the
stage 03 probe instead, which is the more useful check regardless.

---

## What the script does, in order

1. Environment provenance — recorded first, so every later number is attributable
2. Database provisioning (container, or the URL you supplied), then a
   **connection probe**: server version, TimescaleDB extension version, default
   isolation level, read from the running server rather than assumed from the
   image tag
3. Quality gates: ruff, format, mypy, import-linter, boundaries R1–R8, ADR guard
4. Unit suite, verbose
5. Migrations: `upgrade head` → `downgrade base` → `upgrade head`
6. Schema-drift check, printing the generated migration body if not empty
7. Integration suite, verbose
8. Benchmarks with `DHRUVA_CANONICAL_BENCHMARKS=1`
9. Manifest with a **PASS/FAIL verdict per stage** and an overall verdict

Failures are **recorded and the run continues**. A stage that stops on first
failure produces an evidence package with a hole in it. The one exception is the
connection probe at step 2: if the database is unreachable, every stage below it
would fail for the same reason and the run stops there instead of producing six
copies of the same stack trace.

Everything lands in `docs\evidence\s04-<timestamp>\`. Attach it whole. Read
`99-manifest.log` first — it now states the outcome, not just the line counts.

### Two verdicts, and which one is the exit status

The manifest states both, because they answer different questions.

- **`OVERALL`** counts every stage. A Windows benchmark miss shows up here, and
  it should: a reviewer has to see it.
- **`GATING`** counts every stage except the ones ADR-060 §2 records as
  informational on Windows — today that is `50-benchmarks` and nothing else.
  This is what the script returns as its shell exit status.

So a run whose only failure is `50-benchmarks` prints `OVERALL: FAIL`, prints
`GATING: PASS`, and **exits 0**. That is the state ADR-060 anticipates: the
figures are recorded, the branch is still mergeable, and Linux CI remains the
authoritative environment for the budget itself. Any other stage failing exits
1, as does a run that aborts before every stage has reported.

### Two guards against a run that looks like it worked

- **`DHRUVA_REQUIRE_DATABASE=1`** is set by the script. Under it, an unreachable
  database is a hard collection error. Without it, the integration suite skips
  quietly — and twelve `SKIPPED` lines in a log read exactly like a stage that
  executed. They are not evidence of anything.
- **`-p no:cacheprovider`** on every pytest invocation, **and** the run refuses
  to start if `backend\.pytest_cache` cannot be removed. That directory has
  become corrupt on this repository on both Windows (`WinError 5`, `WinError
  183`) and Linux (`EACCES`), and `no:cacheprovider` alone is not enough: pytest
  stats it during collection whether or not the cache plugin is loaded, so a
  corrupt one aborts the run before a single test is collected. If the script
  stops here, close whatever holds the directory and remove it:

  ```powershell
  Remove-Item -Recurse -Force backend\.pytest_cache
  ```

---

## Expected outcomes

| Stage | Success looks like | If it fails |
|---|---|---|
| Gates | `All checks passed`, `Success: no issues`, `4 kept, 0 broken`, `OK`, `intact` | Genuine regression — send the log, do not proceed |
| Unit suite | `937 passed, 5 skipped` (the 5 are intentional `skip` calls) | Send the log |
| `03-database-versions` | `postgresql: 16.x`, `timescaledb: 2.17.x`, `isolation: read committed`, plus the database and user. A few `attempt N: ... retrying` lines are normal on a cold container | `NOT INSTALLED` for TimescaleDB means the extension is missing — use Option B. The run stops here by design |
| `30-migration-upgrade` | `Running upgrade -> 0001_initial` | Auth failure as user `dhruva` means the `DHRUVA_DB__*` translation did not apply — check `02-database-target.log`, which now records every variable that was set |
| `31/32` down + re-up | Both complete without error | A failing downgrade is a real finding; ADR-055 requires it to work |
| Drift check | "No changes in schema detected" | If a body is printed, models and migrations disagree — that is a genuine defect |
| Integration suite | **12 tests run — not skipped.** Any `SKIPPED` here now means the run is invalid | **Some may fail. They have never executed anywhere.** That is them working |
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
