#!/usr/bin/env bash
#
# Canonical validation for S04 — Persistence Foundation.
#
# Produces the complete evidence package the Product Owner requires: full test
# output rather than a summary, benchmark numbers, migration logs, and the exact
# versions of every component involved.
#
# Requires: Python 3.12, Docker (or a reachable PostgreSQL with TimescaleDB), uv.
#
#   ./scripts/canonical_validation.sh
#
# Everything lands in docs/evidence/s04-<timestamp>/ and nothing is summarised —
# summarising is what this script exists to avoid.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE="${REPO_ROOT}/docs/evidence/s04-${STAMP}"
mkdir -p "${EVIDENCE}"

log() { printf '\n=== %s ===\n' "$1" | tee -a "${EVIDENCE}/00-run.log"; }
capture() { local name="$1"; shift; log "$name"; "$@" 2>&1 | tee "${EVIDENCE}/${name}.log"; return "${PIPESTATUS[0]}"; }

cd "${REPO_ROOT}/backend"

# --------------------------------------------------------------------------- #
# 1. Environment provenance. Recorded first, so every number below is
#    attributable to a specific stack rather than to "the canonical machine".
# --------------------------------------------------------------------------- #
log "environment"
{
  echo "captured_at_utc: ${STAMP}"
  echo "host_kernel:     $(uname -sr)"
  echo "cpu:             $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | xargs || sysctl -n machdep.cpu.brand_string 2>/dev/null || echo unknown)"
  echo "cores:           $(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo unknown)"
  echo "memory:          $(free -h 2>/dev/null | awk '/^Mem:/{print $2}' || echo unknown)"
  echo "python:          $(uv run python -V 2>&1)"
  echo "uv:              $(uv --version 2>&1)"
  echo "docker:          $(docker --version 2>&1 || echo 'not present')"
  echo "sqlalchemy:      $(uv run python -c 'import sqlalchemy; print(sqlalchemy.__version__)' 2>&1)"
  echo "alembic:         $(uv run python -c 'import alembic; print(alembic.__version__)' 2>&1)"
  echo "asyncpg:         $(uv run python -c 'import asyncpg; print(asyncpg.__version__)' 2>&1)"
  echo "pytest:          $(uv run pytest --version 2>&1 | head -1)"
} | tee "${EVIDENCE}/01-environment.log"

# PostgreSQL and TimescaleDB versions come from the running server, not from the
# image tag — the tag is what we asked for, the server is what we got.
if [ -n "${DHRUVA_TEST_DATABASE_URL:-}" ]; then
  log "database versions"
  uv run python - <<'PY' 2>&1 | tee "${EVIDENCE}/02-database-versions.log"
import asyncio, os, re
import asyncpg

async def main() -> None:
    url = re.sub(r"\+asyncpg", "", os.environ["DHRUVA_TEST_DATABASE_URL"])
    conn = await asyncpg.connect(url)
    print("postgresql:", await conn.fetchval("SHOW server_version"))
    ts = await conn.fetchval(
        "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
    )
    print("timescaledb:", ts or "NOT INSTALLED")
    print("isolation:", await conn.fetchval("SHOW transaction_isolation"))
    await conn.close()

asyncio.run(main())
PY
else
  echo "DHRUVA_TEST_DATABASE_URL unset; testcontainers will start one" \
    | tee "${EVIDENCE}/02-database-versions.log"
fi

# --------------------------------------------------------------------------- #
# 2. Quality gates. Run before the database work, so a gate failure is not
#    obscured by a long integration run.
# --------------------------------------------------------------------------- #
capture "10-ruff-check"   uv run ruff check .
capture "11-ruff-format"  uv run ruff format --check .
capture "12-mypy"         uv run mypy
capture "13-import-linter" uv run lint-imports --config .importlinter
( cd "${REPO_ROOT}" && capture "14-boundaries" uv run --project backend dhruva-check-boundaries )
( cd "${REPO_ROOT}" && capture "15-adr-guard"  uv run --project backend dhruva-adr-guard )

# --------------------------------------------------------------------------- #
# 3. Full test suite, verbose. Not summarised: every test outcome is recorded.
# --------------------------------------------------------------------------- #
capture "20-unit-suite" uv run pytest -v --no-header -p no:randomly

# --------------------------------------------------------------------------- #
# 4. Migrations: up -> down -> up, plus the autogenerate-is-empty check.
# --------------------------------------------------------------------------- #
capture "30-migration-upgrade"     uv run alembic upgrade head
capture "31-migration-downgrade"   uv run alembic downgrade base
capture "32-migration-reupgrade"   uv run alembic upgrade head
capture "33-migration-history"     uv run alembic history --verbose
capture "34-migration-current"     uv run alembic current --verbose
log "35-autogenerate-empty-diff"
uv run alembic revision --autogenerate -m "drift check" --rev-id drift_check 2>&1 \
  | tee "${EVIDENCE}/35-autogenerate-empty-diff.log"
DRIFT="$(find alembic/versions -name '*drift_check*' -print -quit)"
if [ -n "${DRIFT}" ]; then
  echo "--- generated migration body ---" | tee -a "${EVIDENCE}/35-autogenerate-empty-diff.log"
  cat "${DRIFT}" | tee -a "${EVIDENCE}/35-autogenerate-empty-diff.log"
  rm -f "${DRIFT}"
fi

# --------------------------------------------------------------------------- #
# 5. Integration suite, verbose. This is the evidence S04 is blocked on.
# --------------------------------------------------------------------------- #
capture "40-integration-suite" uv run pytest -v --no-header -p no:randomly -m integration

# --------------------------------------------------------------------------- #
# 6. Benchmarks. The stable-timing gate is opened because this machine can
#    resolve sub-microsecond budgets; the sandbox could not.
# --------------------------------------------------------------------------- #
export DHRUVA_CANONICAL_BENCHMARKS=1
capture "50-benchmarks" uv run pytest -v --no-header -p no:randomly -m benchmark

# --------------------------------------------------------------------------- #
# 7. Manifest.
# --------------------------------------------------------------------------- #
log "manifest"
{
  echo "S04 canonical validation evidence"
  echo "captured: ${STAMP}"
  echo "commit:   $(git -C "${REPO_ROOT}" rev-parse HEAD)"
  echo "branch:   $(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD)"
  echo
  for f in "${EVIDENCE}"/*.log; do
    printf '%-40s %s lines\n' "$(basename "$f")" "$(wc -l < "$f")"
  done
} | tee "${EVIDENCE}/99-manifest.log"

printf '\nEvidence written to: %s\n' "${EVIDENCE}"
printf 'Attach the whole directory; do not summarise it.\n'
