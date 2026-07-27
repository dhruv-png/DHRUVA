<#
.SYNOPSIS
    Canonical validation for S04 - Persistence Foundation. Windows / PowerShell.

.DESCRIPTION
    Produces the complete evidence package: environment provenance, quality
    gates, full verbose test output, migration logs and benchmark numbers.
    Nothing is summarised - summarising is what this script exists to avoid.

    Requires: Python 3.12, uv, and either Docker Desktop running (the harness
    starts a TimescaleDB container) or a reachable PostgreSQL you point it at.

.PARAMETER DatabaseUrl
    Optional. An existing PostgreSQL with the TimescaleDB extension available,
    e.g. postgresql+asyncpg://postgres:postgres@localhost:5432/dhruva_test
    When omitted, Docker Desktop must be running and a container is started.

.EXAMPLE
    .\scripts\canonical_validation.ps1
    .\scripts\canonical_validation.ps1 -DatabaseUrl "postgresql+asyncpg://postgres:postgres@localhost:5432/dhruva_test"
#>
[CmdletBinding()]
param([string]$DatabaseUrl = $env:DHRUVA_TEST_DATABASE_URL)

$ErrorActionPreference = 'Continue'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Stamp    = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$Evidence = Join-Path $RepoRoot "docs\evidence\s04-$Stamp"
New-Item -ItemType Directory -Force -Path $Evidence | Out-Null

function Write-Stage([string]$Name) {
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
}

function Invoke-Captured([string]$Name, [scriptblock]$Command) {
    Write-Stage $Name
    $target = Join-Path $Evidence "$Name.log"
    & $Command 2>&1 | Tee-Object -FilePath $target
    $code = $LASTEXITCODE
    "exit_code: $code" | Add-Content $target
    if ($code -ne 0) { Write-Host "  -> exit $code (recorded, continuing)" -ForegroundColor Yellow }
    return $code
}

Push-Location (Join-Path $RepoRoot 'backend')
try {
    # --------------------------------------------------------------------- #
    # 1. Environment provenance, recorded first so every number below is
    #    attributable to a specific stack.
    # --------------------------------------------------------------------- #
    Write-Stage 'environment'
    $cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1)
    $env_log = @(
        "captured_at_utc: $Stamp"
        "os:              $((Get-CimInstance Win32_OperatingSystem).Caption)"
        "cpu:             $($cpu.Name)"
        "cores:           $($cpu.NumberOfLogicalProcessors)"
        "memory_gb:       $([math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1))"
        "python:          $(uv run python -V 2>&1)"
        "uv:              $(uv --version 2>&1)"
        "docker:          $(try { docker --version 2>&1 } catch { 'not present' })"
        "sqlalchemy:      $(uv run python -c 'import sqlalchemy; print(sqlalchemy.__version__)' 2>&1)"
        "alembic:         $(uv run python -c 'import alembic; print(alembic.__version__)' 2>&1)"
        "asyncpg:         $(uv run python -c 'import asyncpg; print(asyncpg.__version__)' 2>&1)"
        "pytest:          $((uv run pytest --version 2>&1) -split "`n" | Select-Object -First 1)"
    )
    $env_log | Tee-Object -FilePath (Join-Path $Evidence '01-environment.log')

    # --------------------------------------------------------------------- #
    # 2. Database target. Alembic builds its URL from settings (ADR-031), so the
    #    single documented URL is translated into the DHRUVA_DB__* variables
    #    settings understands. Without this, Alembic connects as the default
    #    user 'dhruva' and fails authentication.
    # --------------------------------------------------------------------- #
    if ($DatabaseUrl) {
        Write-Stage 'database target'
        $env:DHRUVA_TEST_DATABASE_URL = $DatabaseUrl
        $uri = [System.Uri]($DatabaseUrl -replace '\+asyncpg', '')
        $env:DHRUVA_DB__HOST = $uri.Host
        $env:DHRUVA_DB__PORT = $uri.Port
        $env:DHRUVA_DB__NAME = $uri.AbsolutePath.TrimStart('/')
        $userInfo = $uri.UserInfo -split ':'
        $env:DHRUVA_DB__USER = $userInfo[0]
        if ($userInfo.Count -gt 1) { $env:DHRUVA_DB__PASSWORD = $userInfo[1] }
        @(
            "DHRUVA_TEST_DATABASE_URL: $DatabaseUrl"
            "DHRUVA_DB__HOST:          $($env:DHRUVA_DB__HOST)"
            "DHRUVA_DB__PORT:          $($env:DHRUVA_DB__PORT)"
            "DHRUVA_DB__NAME:          $($env:DHRUVA_DB__NAME)"
            "DHRUVA_DB__USER:          $($env:DHRUVA_DB__USER)"
            "DHRUVA_DB__PASSWORD:      <set, not logged>"
        ) | Tee-Object -FilePath (Join-Path $Evidence '02-database-target.log')

        Invoke-Captured '03-database-versions' {
            uv run python -c @"
import asyncio, os, re, asyncpg
async def main():
    url = re.sub(r'\+asyncpg', '', os.environ['DHRUVA_TEST_DATABASE_URL'])
    c = await asyncpg.connect(url)
    print('postgresql:', await c.fetchval('SHOW server_version'))
    print('timescaledb:', await c.fetchval("SELECT extversion FROM pg_extension WHERE extname='timescaledb'") or 'NOT INSTALLED')
    print('isolation:', await c.fetchval('SHOW transaction_isolation'))
    await c.close()
asyncio.run(main())
"@
        } | Out-Null
    } else {
        'DHRUVA_TEST_DATABASE_URL unset; testcontainers will start one (Docker Desktop must be running)' |
            Tee-Object -FilePath (Join-Path $Evidence '02-database-target.log')
    }

    # --------------------------------------------------------------------- #
    # 3. Quality gates, before the database work so a gate failure is not
    #    buried under a long integration run.
    # --------------------------------------------------------------------- #
    Invoke-Captured '10-ruff-check'    { uv run ruff check . }              | Out-Null
    Invoke-Captured '11-ruff-format'   { uv run ruff format --check . }     | Out-Null
    Invoke-Captured '12-mypy'          { uv run mypy }                      | Out-Null
    Invoke-Captured '13-import-linter' { uv run lint-imports --config .importlinter } | Out-Null
    Push-Location $RepoRoot
    Invoke-Captured '14-boundaries' { uv run --project backend dhruva-check-boundaries } | Out-Null
    Invoke-Captured '15-adr-guard'  { uv run --project backend dhruva-adr-guard }        | Out-Null
    Pop-Location

    # --------------------------------------------------------------------- #
    # 4. Unit suite, verbose. Every outcome recorded.
    # --------------------------------------------------------------------- #
    Invoke-Captured '20-unit-suite' { uv run pytest -v --no-header -p no:randomly } | Out-Null

    # --------------------------------------------------------------------- #
    # 5. Migrations: up -> down -> up, plus the schema-drift check.
    # --------------------------------------------------------------------- #
    Invoke-Captured '30-migration-upgrade'   { uv run alembic upgrade head }   | Out-Null
    Invoke-Captured '31-migration-downgrade' { uv run alembic downgrade base } | Out-Null
    Invoke-Captured '32-migration-reupgrade' { uv run alembic upgrade head }   | Out-Null
    Invoke-Captured '33-migration-history'   { uv run alembic history --verbose } | Out-Null
    Invoke-Captured '34-migration-current'   { uv run alembic current --verbose } | Out-Null

    Write-Stage '35-autogenerate-empty-diff'
    $driftLog = Join-Path $Evidence '35-autogenerate-empty-diff.log'
    uv run alembic revision --autogenerate -m "drift check" --rev-id drift_check 2>&1 |
        Tee-Object -FilePath $driftLog
    $drift = Get-ChildItem 'alembic\versions' -Filter '*drift_check*' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($drift) {
        '--- generated migration body ---' | Add-Content $driftLog
        Get-Content $drift.FullName | Tee-Object -FilePath $driftLog -Append
        Remove-Item $drift.FullName -Force
    }

    # --------------------------------------------------------------------- #
    # 6. Integration suite. This is the evidence S04 is blocked on.
    # --------------------------------------------------------------------- #
    Invoke-Captured '40-integration-suite' {
        uv run pytest -v --no-header -p no:randomly -m integration
    } | Out-Null

    # --------------------------------------------------------------------- #
    # 7. Benchmarks. The stable-timing gate is opened because this machine can
    #    resolve sub-microsecond budgets; the development sandbox could not.
    # --------------------------------------------------------------------- #
    $env:DHRUVA_CANONICAL_BENCHMARKS = '1'
    Invoke-Captured '50-benchmarks' {
        uv run pytest -v --no-header -p no:randomly -m benchmark
    } | Out-Null

    # --------------------------------------------------------------------- #
    # 8. Manifest.
    # --------------------------------------------------------------------- #
    Write-Stage 'manifest'
    $manifest = @(
        'S04 canonical validation evidence'
        "captured: $Stamp"
        "commit:   $(git -C $RepoRoot rev-parse HEAD)"
        "branch:   $(git -C $RepoRoot rev-parse --abbrev-ref HEAD)"
        ''
    ) + (Get-ChildItem $Evidence -Filter '*.log' | ForEach-Object {
        '{0,-40} {1} lines' -f $_.Name, (Get-Content $_.FullName | Measure-Object -Line).Lines
    })
    $manifest | Tee-Object -FilePath (Join-Path $Evidence '99-manifest.log')

    Write-Host ""
    Write-Host "Evidence written to: $Evidence" -ForegroundColor Green
    Write-Host "Attach the whole directory; do not summarise it." -ForegroundColor Green
}
finally { Pop-Location }
