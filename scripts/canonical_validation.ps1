<#
.SYNOPSIS
    Canonical validation for S04 - Persistence Foundation. Windows / PowerShell.

.DESCRIPTION
    Produces the complete evidence package: environment provenance, quality
    gates, full verbose test output, migration logs and benchmark numbers.
    Nothing is summarised - summarising is what this script exists to avoid.

    Requires: Python 3.12, uv, and a database. Either supply one with
    -DatabaseUrl, or leave it off and let this script start a TimescaleDB
    container (Docker Desktop must be running).

    A database is mandatory, not optional. Stages 30-35 invoke `alembic` as
    standalone processes, outside pytest -- they cannot borrow a container that
    only exists for the lifetime of the test session. Whatever database is used,
    this script provisions it before any stage runs and every stage shares it.

.PARAMETER DatabaseUrl
    An existing PostgreSQL with the TimescaleDB extension available, e.g.
    postgresql+asyncpg://postgres:postgres@localhost:5432/dhruva_test
    Omit it to have a disposable container started and torn down for you.

    Use a dedicated database. This script truncates tables and runs
    `alembic downgrade base`.

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

#: TimescaleDB, not plain PostgreSQL: hypertable DDL is part of what is verified.
$PostgresImage = 'timescale/timescaledb:2.17.2-pg16'
#: A non-default port, so a container never collides with a local PostgreSQL.
$ContainerPort = 55432
$ContainerName = "dhruva-canonical-$Stamp"
$StartedContainer = $false

#: Stage name -> exit code. The manifest turns this into a verdict, so that a
#: run in which everything failed cannot be mistaken for a run that happened.
$Verdicts = [ordered]@{}

function Write-Stage([string]$Name) {
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
}

function Write-Log([string]$Name, [object]$Content) {
    # UTF-8 explicitly. PowerShell 5.1's Tee-Object writes UTF-16LE, which makes
    # the evidence awkward to read anywhere except Windows.
    $target = Join-Path $Evidence "$Name.log"
    $Content | Out-File -FilePath $target -Encoding utf8
    $Content | Out-Host
    return $target
}

function Invoke-Captured([string]$Name, [scriptblock]$Command) {
    Write-Stage $Name
    $target = Join-Path $Evidence "$Name.log"
    $output = & $Command 2>&1
    $code = $LASTEXITCODE
    $output | Out-File -FilePath $target -Encoding utf8
    $output | Out-Host
    "exit_code: $code" | Add-Content $target -Encoding utf8
    $script:Verdicts[$Name] = $code
    if ($code -ne 0) { Write-Host "  -> exit $code (recorded, continuing)" -ForegroundColor Yellow }
    return $code
}

function Start-CanonicalDatabase {
    <#
        Start a disposable TimescaleDB and return its URL. The container is owned
        by this script rather than by pytest, because the migration stages run
        before pytest starts and need the same database the tests will use.
    #>
    Write-Stage 'starting database container'

    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker is not responding. Start Docker Desktop, or pass -DatabaseUrl to use an existing PostgreSQL."
    }

    Write-Host "  pulling $PostgresImage (first run only, this can take a few minutes)"
    docker pull $PostgresImage 2>&1 | Out-Host

    docker run -d --name $ContainerName `
        -e POSTGRES_USER=dhruva_test `
        -e POSTGRES_PASSWORD=dhruva_test `
        -e POSTGRES_DB=dhruva_test `
        -p "${ContainerPort}:5432" `
        $PostgresImage 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not start container $ContainerName." }
    $script:StartedContainer = $true

    # Readiness, done properly.
    #
    # `docker exec pg_isready` is not a sufficient test, and trusting it is what
    # produced `ConnectionError: unexpected connection_lost() call` during SSL
    # negotiation. The postgres entrypoint runs initdb, then starts a
    # *temporary* server with `listen_addresses=''` to execute the init scripts,
    # then stops it, then starts the real one. During that middle phase the
    # temporary server answers on the unix socket, so `pg_isready` reports
    # success -- while nothing is listening on TCP. Docker has already published
    # the port, so a host connection is accepted and immediately dropped, which
    # asyncpg reports as the connection being lost mid-handshake.
    #
    # The discriminator is the log line. The temporary server binds no TCP
    # address and therefore never logs one; only the real server does.
    Write-Host "  waiting for the database to accept TCP connections"
    $ready = $false
    foreach ($attempt in 1..90) {
        $logs = (docker logs $ContainerName 2>&1) -join "`n"
        if ($logs -match 'listening on IPv4 address') {
            docker exec $ContainerName pg_isready -q -U dhruva_test -d dhruva_test 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        }
        if ($attempt % 10 -eq 0) { Write-Host "  still initialising ($($attempt * 2)s)" }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        docker logs $ContainerName 2>&1 | Select-Object -Last 30 | Out-Host
        throw "Database did not begin listening on TCP within 180 seconds. Container log above."
    }

    # No CREATE EXTENSION here. The image installs timescaledb into template1, so
    # POSTGRES_DB inherits it at creation, and the image's own init script also
    # creates it -- which is why even `CREATE EXTENSION IF NOT EXISTS` failed
    # with a duplicate key on pg_extension_name_index: IF NOT EXISTS checks the
    # catalogue before the concurrent init has committed, then collides with it.
    #
    # The extension is *verified* instead, by the connection probe in stage 03,
    # which reports its version and exits non-zero when it is absent. Verifying
    # a precondition is the right move regardless; creating one that the image
    # already guarantees was redundant work that could only ever fail.

    Write-Host "  container $ContainerName ready on port $ContainerPort" -ForegroundColor Green
    return "postgresql+asyncpg://dhruva_test:dhruva_test@localhost:$ContainerPort/dhruva_test"
}

function Stop-CanonicalDatabase {
    if (-not $script:StartedContainer) { return }
    Write-Stage 'removing database container'
    docker rm -f $ContainerName 2>&1 | Out-Null
}

Push-Location (Join-Path $RepoRoot 'backend')
try {
    # --------------------------------------------------------------------- #
    # 0. Remove a poisoned .pytest_cache.
    #
    #    A corrupted cache directory has twice killed pytest *after* the last
    #    test but *before* the summary, taking the FAILURES section with it --
    #    so three real failures were recorded as verdicts with no diagnostics.
    #
    #    `-p no:cacheprovider` is NOT sufficient on its own: pytest stats this
    #    directory during collection regardless of whether the cache plugin is
    #    loaded, so a corrupt one aborts the run with a bare permission error
    #    before a single test is collected. Reproduced on Windows (WinError 5)
    #    and on Linux (EACCES) from the same directory, so removal has to
    #    actually succeed -- silently continuing past a failure here buys a
    #    confusing failure ten seconds later.
    # --------------------------------------------------------------------- #
    Remove-Item -Recurse -Force '.pytest_cache' -ErrorAction SilentlyContinue
    if (Test-Path '.pytest_cache') {
        throw (
            "backend\.pytest_cache exists and could not be removed. pytest stats it " +
            "during collection even with -p no:cacheprovider, so the run would fail " +
            "with a permission error before collecting anything. Close any editor or " +
            "terminal holding it, then remove it: " +
            "Remove-Item -Recurse -Force backend\.pytest_cache"
        )
    }

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
    Write-Log '01-environment' $env_log | Out-Null

    # --------------------------------------------------------------------- #
    # 2. Database target, provisioned before any stage that needs one.
    # --------------------------------------------------------------------- #
    if (-not $DatabaseUrl) { $DatabaseUrl = Start-CanonicalDatabase }

    Write-Stage 'database target'
    $env:DHRUVA_TEST_DATABASE_URL = $DatabaseUrl
    # Alembic never reads DHRUVA_TEST_DATABASE_URL. It builds its URL from
    # settings (ADR-031), so the one documented URL is translated here into the
    # DHRUVA_DB__* variables settings understands -- note the *double*
    # underscore. Without this, alembic/env.py falls through to the defaults in
    # shared/config/settings.py and connects as user 'dhruva'.
    $uri = [System.Uri]($DatabaseUrl -replace '\+asyncpg', '')
    $env:DHRUVA_DB__HOST = $uri.Host
    $env:DHRUVA_DB__PORT = $uri.Port
    $env:DHRUVA_DB__NAME = $uri.AbsolutePath.TrimStart('/')
    $userInfo = $uri.UserInfo -split ':'
    $env:DHRUVA_DB__USER = $userInfo[0]
    if ($userInfo.Count -gt 1) { $env:DHRUVA_DB__PASSWORD = $userInfo[1] }
    # Turn "no database" into a hard collection error instead of twelve skips.
    $env:DHRUVA_REQUIRE_DATABASE = '1'

    # Computed outside the string: PowerShell 5.1 will not parse a double-quoted
    # string nested inside a $() subexpression of another double-quoted string.
    if ($StartedContainer) {
        $provisionedBy = "this script (container $ContainerName)"
    } else {
        $provisionedBy = 'supplied via -DatabaseUrl'
    }
    $redactedUrl = $DatabaseUrl -replace ':[^:@/]+@', ':<redacted>@'

    Write-Log '02-database-target' @(
        "provisioned_by:           $provisionedBy"
        "DHRUVA_TEST_DATABASE_URL: $redactedUrl"
        "DHRUVA_DB__HOST:          $($env:DHRUVA_DB__HOST)"
        "DHRUVA_DB__PORT:          $($env:DHRUVA_DB__PORT)"
        "DHRUVA_DB__NAME:          $($env:DHRUVA_DB__NAME)"
        "DHRUVA_DB__USER:          $($env:DHRUVA_DB__USER)"
        "DHRUVA_DB__PASSWORD:      <set, not logged>"
        "DHRUVA_REQUIRE_DATABASE:  1"
    ) | Out-Null

    # Prove the database is reachable *before* running six stages against it.
    # Failing here names the problem; failing later buries it in a stack trace.
    #
    # The probe is written to a file and executed as a file. Passing Python
    # source to `python -c` through PowerShell means the source crosses the
    # native-command argument parser, which strips the inner double quotes --
    # `c.fetchval("SELECT ...")` arrived at the interpreter as
    # `c.fetchval(SELECT ...)` and died with `'(' was never closed`. A here-string
    # does not prevent this; the mangling happens at the call boundary, after
    # PowerShell has finished with the string. A file has no such boundary.
    $probeScript = Join-Path $Evidence '03-database-versions.py'
    @'
"""Connection probe: prove the database is reachable and correctly provisioned."""
import asyncio
import os
import re
import sys

import asyncpg

EXTENSION_QUERY = "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"

#: How long to keep retrying a *transient* connection failure.
CONNECT_DEADLINE_SECONDS = 90.0

#: Failures that mean "not up yet" rather than "wrong". A server that is still
#: starting accepts the socket and closes it again, which asyncpg surfaces as
#: ConnectionError("unexpected connection_lost() call") from inside SSL
#: negotiation -- an unhelpful message for an entirely ordinary race.
TRANSIENT = (
    ConnectionError,
    OSError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
)


async def connect_with_retry(url: str) -> asyncpg.Connection:
    """Connect, retrying only failures that a wait could plausibly cure.

    A wrong password or a missing database is not retried: sitting in a loop for
    ninety seconds re-sending bad credentials turns an instant, clearly worded
    failure into a slow, vague one.
    """
    deadline = asyncio.get_event_loop().time() + CONNECT_DEADLINE_SECONDS
    delay = 0.5
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncpg.connect(url)
        except TRANSIENT as error:
            if asyncio.get_event_loop().time() >= deadline:
                print(
                    f"FATAL: no usable connection after {attempt} attempts "
                    f"over {CONNECT_DEADLINE_SECONDS:.0f}s. Last error: {error!r}"
                )
                raise
            print(f"  attempt {attempt}: {type(error).__name__} -- retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)


async def main() -> int:
    url = re.sub(r"\+asyncpg", "", os.environ["DHRUVA_TEST_DATABASE_URL"])
    connection = await connect_with_retry(url)
    try:
        print("postgresql: ", await connection.fetchval("SHOW server_version"))
        extension = await connection.fetchval(EXTENSION_QUERY)
        print("timescaledb:", extension or "NOT INSTALLED")
        print("isolation:  ", await connection.fetchval("SHOW transaction_isolation"))
        print("database:   ", await connection.fetchval("SELECT current_database()"))
        print("user:       ", await connection.fetchval("SELECT current_user"))
    finally:
        await connection.close()

    if not extension:
        # Three integration tests exercise hypertable DDL, which does not exist
        # without the extension. Continuing would produce failures that look like
        # defects in the persistence layer.
        print("FATAL: the timescaledb extension is not installed in this database.")
        return 1
    return 0


sys.exit(asyncio.run(main()))
'@ | Out-File -FilePath $probeScript -Encoding utf8

    $probe = Invoke-Captured '03-database-versions' { uv run python $probeScript }
    if ($probe -ne 0) {
        $advice = "See 03-database-versions.log."
        if (-not $StartedContainer) {
            # The overwhelmingly common cause: a URL was supplied for a database
            # that is not running. Supplying one suppresses the container this
            # script would otherwise have started -- so pointing -DatabaseUrl at
            # port $ContainerPort, which is the port the container uses, asks for
            # a database nobody started.
            $advice = (
                "Nothing answered at $($env:DHRUVA_DB__HOST):$($env:DHRUVA_DB__PORT). " +
                "-DatabaseUrl was supplied, which tells this script you are providing the " +
                "database yourself and suppresses the container it would otherwise start. " +
                "To have one started for you, rerun with no arguments: " +
                ".\scripts\canonical_validation.ps1"
            )
        }
        throw "Database probe failed. $advice Every stage below shares this dependency, so the run stops here rather than producing six copies of the same failure."
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
    # -p no:cacheprovider: the .pytest_cache directory has repeatedly become
    # unwritable mid-run (WinError 5 / WinError 183), aborting collection for a
    # reason that has nothing to do with the code under test. The cache buys
    # --lf and --ff, neither of which a full canonical run uses.
    Invoke-Captured '20-unit-suite' {
        uv run pytest -v --no-header -p no:randomly -p no:cacheprovider
    } | Out-Null

    # --------------------------------------------------------------------- #
    # 5. Migrations: up -> down -> up, plus the schema-drift check.
    # --------------------------------------------------------------------- #
    Invoke-Captured '30-migration-upgrade'   { uv run alembic upgrade head }   | Out-Null
    Invoke-Captured '31-migration-downgrade' { uv run alembic downgrade base } | Out-Null
    Invoke-Captured '32-migration-reupgrade' { uv run alembic upgrade head }   | Out-Null
    Invoke-Captured '33-migration-history'   { uv run alembic history --verbose } | Out-Null
    Invoke-Captured '34-migration-current'   { uv run alembic current --verbose } | Out-Null

    Invoke-Captured '35-autogenerate-empty-diff' {
        uv run alembic revision --autogenerate -m "drift check" --rev-id drift_check
    } | Out-Null
    $driftLog = Join-Path $Evidence '35-autogenerate-empty-diff.log'
    $drift = Get-ChildItem 'alembic\versions' -Filter '*drift_check*' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($drift) {
        # A generated body means the mapped metadata and the migrated schema
        # disagree. The body is the diff; it is recorded verbatim and the file
        # removed so it cannot be mistaken for an intended migration.
        '--- generated migration body: SCHEMA DRIFT DETECTED ---' | Add-Content $driftLog -Encoding utf8
        Get-Content $drift.FullName | Add-Content $driftLog -Encoding utf8
        Remove-Item $drift.FullName -Force
        $Verdicts['35-autogenerate-empty-diff'] = 1
    }

    # --------------------------------------------------------------------- #
    # 6. Integration suite. This is the evidence S04 is blocked on.
    # --------------------------------------------------------------------- #
    Invoke-Captured '40-integration-suite' {
        uv run pytest -v --no-header -p no:randomly -p no:cacheprovider -m integration
    } | Out-Null

    # --------------------------------------------------------------------- #
    # 7. Benchmarks. The stable-timing gate is opened because this machine can
    #    resolve sub-microsecond budgets; the development sandbox could not.
    # --------------------------------------------------------------------- #
    $env:DHRUVA_CANONICAL_BENCHMARKS = '1'
    Invoke-Captured '50-benchmarks' {
        uv run pytest -v --no-header -p no:randomly -p no:cacheprovider -m benchmark
    } | Out-Null

    # --------------------------------------------------------------------- #
    # 8. Manifest, with a verdict per stage.
    #
    #    The previous manifest listed line counts, which is why a run where
    #    every database stage failed and every integration test skipped could be
    #    described as "the stages execute". Line counts measure output, not
    #    outcome. Exit codes measure outcome.
    # --------------------------------------------------------------------- #
    Write-Stage 'manifest'
    $failed = @($Verdicts.GetEnumerator() | Where-Object { $_.Value -ne 0 })
    $manifest = @(
        'S04 canonical validation evidence'
        "captured: $Stamp"
        "commit:   $(git -C $RepoRoot rev-parse HEAD)"
        "branch:   $(git -C $RepoRoot rev-parse --abbrev-ref HEAD)"
        ''
        'stage verdicts'
        '--------------'
    ) + ($Verdicts.GetEnumerator() | ForEach-Object {
        '{0,-40} {1} (exit {2})' -f $_.Key, $(if ($_.Value -eq 0) { 'PASS' } else { 'FAIL' }), $_.Value
    }) + @(
        ''
        $(if ($failed.Count -eq 0) {
            'OVERALL: PASS - every stage exited 0.'
        } else {
            "OVERALL: FAIL - $($failed.Count) stage(s) failed: $($failed.Key -join ', ')"
        })
        ''
        'files'
        '-----'
    ) + (Get-ChildItem $Evidence -Filter '*.log' | ForEach-Object {
        '{0,-40} {1} lines' -f $_.Name, (Get-Content $_.FullName | Measure-Object -Line).Lines
    })
    Write-Log '99-manifest' $manifest | Out-Null

    Write-Host ""
    Write-Host "Evidence written to: $Evidence" -ForegroundColor Green
    if ($failed.Count -eq 0) {
        Write-Host "OVERALL: PASS" -ForegroundColor Green
    } else {
        Write-Host "OVERALL: FAIL - $($failed.Count) stage(s): $($failed.Key -join ', ')" -ForegroundColor Red
    }
    Write-Host "Attach the whole directory; do not summarise it." -ForegroundColor Green
}
finally {
    Stop-CanonicalDatabase
    Pop-Location
}
