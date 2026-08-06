"""The quality gates declared in the plan are actually configured.

Configuration drifts silently. Someone under deadline pressure relaxes
``strict`` to unblock a merge, and eighteen months of typing discipline quietly
stops being enforced. These tests make the gates themselves part of the test
suite, so relaxing one is a visible, deliberate act that fails the build.

They assert on Master Project Plan sections 10.1 (Definition of Done),
13.3 (toolchain) and ADR-024 (strict typing).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from dhruva.tooling.boundaries import ALLOWED_CONTEXT_DEPENDENCIES


@pytest.fixture(scope="module")
def pyproject(repo_root: Path) -> dict[str, Any]:
    """Parse ``backend/pyproject.toml``."""
    data: dict[str, Any] = tomllib.loads(
        (repo_root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )
    return data


@pytest.mark.unit
def test_python_312_is_required(pyproject: dict[str, Any]) -> None:
    """ADR-024 fixes the language version; older runtimes lack the typing features."""
    assert pyproject["project"]["requires-python"] == ">=3.12"


@pytest.mark.unit
def test_mypy_is_strict(pyproject: dict[str, Any]) -> None:
    """ADR-024: no gradual-typing escape hatch."""
    mypy = pyproject["tool"]["mypy"]
    assert mypy["strict"] is True
    assert mypy["python_version"] == "3.12"
    assert mypy["warn_unreachable"] is True


@pytest.mark.unit
def test_bare_type_ignore_is_an_error(pyproject: dict[str, Any]) -> None:
    """ADR-024 requires every ignore to name its error code."""
    assert "ignore-without-code" in pyproject["tool"]["mypy"]["enable_error_code"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ("DTZ", "ADR-006: naive datetimes are banned"),
        ("FIX", "Golden Rules: no TODO or FIXME may survive a merge"),
        ("S", "security linting is on by default (ADR-020)"),
        ("ERA", "commented-out code is deleted, not archived in place"),
        ("D", "docstrings are a Definition of Done requirement"),
        ("T20", "print statements are not structured logging"),
        ("ASYNC", "ADR-024: no blocking calls on the event loop"),
    ],
)
def test_required_ruff_rule_families_are_selected(
    pyproject: dict[str, Any], rule: str, why: str
) -> None:
    """Each selected family maps to a specific decision in the plan."""
    assert rule in pyproject["tool"]["ruff"]["lint"]["select"], why


@pytest.mark.unit
def test_relative_imports_are_banned(pyproject: dict[str, Any]) -> None:
    """Absolute imports keep the boundary checker's analysis exact."""
    tidy = pyproject["tool"]["ruff"]["lint"]["flake8-tidy-imports"]
    assert tidy["ban-relative-imports"] == "all"


@pytest.mark.unit
def test_coverage_gate_matches_the_definition_of_done(pyproject: dict[str, Any]) -> None:
    """Plan section 10.1 sets 90% with branch coverage on."""
    assert pyproject["tool"]["coverage"]["report"]["fail_under"] == 90
    assert pyproject["tool"]["coverage"]["run"]["branch"] is True


@pytest.mark.unit
def test_pytest_is_configured_to_fail_loudly(pyproject: dict[str, Any]) -> None:
    """Warnings become errors and unknown markers are rejected.

    A deprecation warning ignored today is a broken build on an upgrade later.
    """
    options = pyproject["tool"]["pytest"]["ini_options"]
    assert "--strict-markers" in options["addopts"]
    assert "--strict-config" in options["addopts"]
    assert options["xfail_strict"] is True
    filters = options["filterwarnings"]

    assert filters[0] == "error", "warnings must be errors by default"
    for entry in filters[1:]:
        assert entry.startswith("ignore:"), (
            "every relaxation must be a narrowly targeted ignore, never a broad one"
        )
    assert len(filters) <= 4, (
        "the ignore list is growing; each entry is a warning class nobody sees again"
    )


@pytest.mark.unit
def test_declared_markers_cover_the_test_pyramid(pyproject: dict[str, Any]) -> None:
    """Plan section 14.1 names the layers; the markers must match."""
    declared = {m.split(":", 1)[0] for m in pyproject["tool"]["pytest"]["ini_options"]["markers"]}
    assert {"unit", "integration", "contract", "e2e", "slow"} <= declared


#: Every runtime dependency, mapped to the subsystem that introduced it and
#: justified in that subsystem's design document (ADR-030, ADR-032). Adding a
#: dependency means updating this map in the same commit -- deliberately, rather
#: than incidentally.
DEPENDENCY_PROVENANCE: dict[str, str] = {
    "pydantic": "S02 - typed configuration",
    "pydantic-settings": "S02 - environment loading (ADR-031)",
    "structlog": "S02 - structured logging with redaction (ADR-033)",
    "fastapi": "S02 - the observability component (ADR-035)",
    "uvicorn": "S02 - serving the observability component (ADR-035)",
    "prometheus-client": "S02 - metrics registry and exposition (ADR-035)",
    "opentelemetry-api": "S02 - tracing facade (ADR-040)",
    "sqlalchemy": "S04 - async ORM; models confined to infrastructure (ADR-052)",
    "alembic": "S04 - versioned migrations with declared reversibility (ADR-055)",
    "asyncpg": "S04 - the async PostgreSQL driver SQLAlchemy dispatches to",
    "tzdata": "S04 - zoneinfo has no system tz database on Windows, so "
    "`timezone = UTC` in alembic.ini cannot resolve without it (ADR-032)",
    "redis": "S05 - the Redis Streams client (ADR-002), confined to the messaging "
    "adapter by boundary rule R9 (ADR-068). A runtime dependency rather than a "
    "development one: the relay publishes through it in production",
    "celery": (
        "S05 - asynchronous job execution and scheduling runtime, "
        "confined to the infrastructure execution adapters."
    ),
    "cryptography": (
        "S06 - AES-256-GCM envelope encryption of stored credentials (ADR-020, "
        "ADR-070). Confined to the crypto adapter behind the KeyProvider port; "
        "no domain module imports it"
    ),
    "pyjwt": "S06 - signing and verifying access tokens (ADR-072), confined to "
    "the TokenIssuer adapter",
    "argon2-cffi": "S06 - hashing the secret a principal presents; ADR-033 "
    "forbids storing it recoverably",
    "httpx2": "MVP 1 - async transport for the narrow read-only Kite market-data "
    "adapter (ADR-076); provider HTTP types remain in Reference infrastructure",
}


@pytest.mark.unit
def test_every_runtime_dependency_is_pinned_and_accounted_for(
    pyproject: dict[str, Any],
) -> None:
    """A dependency with no recorded provenance is a subsystem started without a design.

    Exact pinning is required by ADR-032; the provenance map is required so that
    ``why is this here?`` has an answer that outlives the author's memory.
    """
    declared = pyproject["project"]["dependencies"]
    names = {spec.split("==")[0] for spec in declared}

    assert all("==" in spec for spec in declared), "ADR-032 requires exact pins"
    assert names == set(DEPENDENCY_PROVENANCE), "dependency set and provenance map disagree"


@pytest.mark.unit
def test_the_tracing_sdk_is_an_extra_not_a_library_dependency(
    pyproject: dict[str, Any],
) -> None:
    """ADR-040: library code imports the OpenTelemetry API; only composition roots use the SDK."""
    runtime = {spec.split("==")[0] for spec in pyproject["project"]["dependencies"]}
    tracing_extra = {
        spec.split("==")[0] for spec in pyproject["project"]["optional-dependencies"]["tracing"]
    }

    assert "opentelemetry-sdk" not in runtime
    assert "opentelemetry-sdk" in tracing_extra


@pytest.mark.unit
def test_the_deployment_lockfile_contains_every_runtime_dependency(
    repo_root: Path, pyproject: dict[str, Any]
) -> None:
    """`requirements.lock` is what gets installed, and it must not drift from pyproject.

    Two lockfiles are maintained deliberately. `uv.lock` is uv's own resolution
    and is updated by `uv add` / `uv lock`; `requirements.lock` is the
    hash-pinned artefact a deployment installs, and is produced by the
    `uv pip compile` command recorded in its own header. **`uv add` does not
    update the second one.**

    Nothing caught that before this test. The existing lockfile check asserts
    only that hashes are present, so a dependency added with `uv add` produced a
    green suite, a satisfied provenance map, and a deployment artefact that would
    not have installed the new package at all -- discovered on the deployment
    target rather than here.
    """
    lock = (repo_root / "backend" / "requirements.lock").read_text(encoding="utf-8")
    pinned = {line.split("==")[0].strip() for line in lock.splitlines() if "==" in line}
    declared = {spec.split("==")[0] for spec in pyproject["project"]["dependencies"]}

    missing = sorted(declared - pinned)
    assert not missing, (
        f"{missing} are declared in pyproject.toml but absent from requirements.lock. "
        f"`uv add` updates uv.lock only; regenerate both with the `uv pip compile` "
        f"commands recorded in the headers of requirements.lock and "
        f"requirements-dev.lock (ADR-032)."
    )


@pytest.mark.unit
def test_lockfiles_are_committed_and_hash_pinned(repo_root: Path) -> None:
    """ADR-032. A lockfile without hashes does not pin what it claims to pin."""
    for name in ("requirements.lock", "requirements-dev.lock"):
        content = (repo_root / "backend" / name).read_text(encoding="utf-8")
        assert "--hash=sha256:" in content, f"{name} is not hash-pinned"


@pytest.mark.unit
def test_python_version_is_declared_consistently(
    repo_root: Path, pyproject: dict[str, Any]
) -> None:
    """ADR-032 declares the version in three places; a test asserts they agree.

    Each is read by a different tool, and this assertion is cheaper than the
    afternoon lost to discovering they disagree.
    """
    pinned = (repo_root / ".python-version").read_text(encoding="utf-8").strip()
    ci = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert pinned == "3.12"
    assert pyproject["project"]["requires-python"] == ">=3.12"
    assert f"uv python install {pinned}" in ci


@pytest.mark.unit
def test_console_scripts_expose_the_architectural_controls(pyproject: dict[str, Any]) -> None:
    """Pre-commit and CI both invoke these by name."""
    scripts = pyproject["project"]["scripts"]
    assert scripts["dhruva-check-boundaries"] == "dhruva.tooling.boundaries:main"
    assert scripts["dhruva-adr-guard"] == "dhruva.tooling.adr_guard:main"
    # The dead-letter command is an operator's tool and a composition root
    # (ADR-064): it may build an engine from settings, which nothing in
    # `tooling` is allowed to do.
    assert scripts["dhruva-dlq"] == "dhruva.workers.dlq:main"


@pytest.mark.unit
def test_import_linter_covers_every_context(repo_root: Path) -> None:
    """A context missing from the layer contract would be silently unprotected."""
    config = (repo_root / "backend" / ".importlinter").read_text(encoding="utf-8")
    for context in ALLOWED_CONTEXT_DEPENDENCIES:
        assert f"dhruva.contexts.{context}" in config, f"'{context}' absent from .importlinter"


@pytest.mark.unit
def test_precommit_runs_the_same_gates_as_ci(repo_root: Path) -> None:
    """Local and CI enforcement must not diverge, or CI becomes a surprise."""
    precommit = (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    ci = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for gate in ("ruff", "mypy", "lint-imports", "dhruva-check-boundaries", "dhruva-adr-guard"):
        assert gate in precommit, f"'{gate}' missing from pre-commit"
        assert gate in ci, f"'{gate}' missing from CI"


#: The environment variable `requires_stable_timing` reads before it will run a
#: sub-microsecond budget (tests/benchmarks/test_primitives.py).
STABLE_TIMING_VARIABLE = "DHRUVA_CANONICAL_BENCHMARKS"


def _workflow_job(text: str, name: str) -> str:
    """Return one top-level job block from a workflow, by job key.

    Textual rather than parsed: PyYAML is not a dependency of this project, and
    adding one to assert on six lines of configuration would be a worse trade
    than reading the indentation the file already guarantees.
    """
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line == f"  {name}:")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  ") and not lines[index].startswith("   ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


@pytest.fixture(scope="module")
def ci_workflow(repo_root: Path) -> str:
    """Read the CI workflow once for the configuration assertions below."""
    return (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_authoritative_benchmark_job_can_measure_sub_microsecond_budgets(
    ci_workflow: str,
) -> None:
    """ADR-060 A3-A5 need Linux figures for budgets that skip without this flag.

    ADR-060 section 1 makes Linux CI the authoritative environment. The primitive
    budgets skip unless `requires_stable_timing` sees this variable, so without
    it the one environment permitted to close a budget never measures the
    budgets most often missed -- and TD-13 and TD-14 cannot close on anything
    this job produces.
    """
    job = _workflow_job(ci_workflow, "benchmarks")

    assert STABLE_TIMING_VARIABLE in job, (
        f"the benchmark job must set {STABLE_TIMING_VARIABLE}; "
        "without it ADR-060 A3-A5 cannot close"
    )


@pytest.fixture(scope="module")
def canonical_script(repo_root: Path) -> str:
    """Read the Windows canonical validation script."""
    return (repo_root / "scripts" / "canonical_validation.ps1").read_text(encoding="utf-8")


def _informational_stages(script: str) -> frozenset[str]:
    """Return the stage names the canonical script treats as non-gating."""
    match = re.search(r"\$InformationalStages = @\((?P<body>.*?)\)", script, re.DOTALL)
    assert match is not None, "$InformationalStages is not declared"
    return frozenset(re.findall(r"'([^']+)'", match.group("body")))


@pytest.mark.unit
def test_the_canonical_script_reports_a_gating_failure_to_the_shell(
    canonical_script: str,
) -> None:
    """A validation run that fails must not look green to whatever called it.

    The script previously had no `exit` at all, so PowerShell returned the status
    of its last statement. Strict mypy or the integration suite could fail, be
    recorded truthfully in the manifest, and still hand back 0.
    """
    assert canonical_script.rstrip().endswith("exit $script:ExitCode")


@pytest.mark.unit
def test_only_the_windows_benchmark_stage_is_informational(canonical_script: str) -> None:
    """ADR-060 section 2 exempts benchmarks on Windows. It exempts nothing else.

    This list is the one place a gate can be declassified, so it is the one place
    worth pinning. Widening it must fail here first, which is what makes it a
    deliberate act with an ADR behind it rather than a quiet edit.
    """
    assert _informational_stages(canonical_script) == frozenset({"50-benchmarks"})


@pytest.mark.unit
def test_every_stage_except_the_benchmarks_gates_the_shell_status(
    canonical_script: str,
) -> None:
    """The exemption is one stage wide, not a general licence to fail."""
    stages = frozenset(re.findall(r"Invoke-Captured '([^']+)'", canonical_script))
    gating = stages - _informational_stages(canonical_script)

    assert stages, "no stages found; the script's shape has changed"
    for required in (
        "12-mypy",
        "13-import-linter",
        "14-boundaries",
        "15-adr-guard",
        "20-unit-suite",
        "35-autogenerate-empty-diff",
        "40-integration-suite",
    ):
        assert required in gating, f"'{required}' no longer gates the shell status"


@pytest.mark.unit
def test_the_ordinary_test_job_does_not_claim_stable_timing(ci_workflow: str) -> None:
    """A shared runner is not a quiet machine outside the dedicated benchmark job.

    Setting the flag globally would turn measurement noise into red builds on the
    job that does gate a merge, which is how a budget stops being believed
    (ADR-060 R-060-4).
    """
    assert STABLE_TIMING_VARIABLE not in _workflow_job(ci_workflow, "backend")


#: The port the canonical script publishes its container on unless told otherwise.
#: Pinned because changing it silently would invalidate every runbook that names
#: it and every firewall rule somebody added for it.
DEFAULT_CONTAINER_PORT = 55432

#: PowerShell escape sequences. A backtick inside a double-quoted string escapes
#: the character after it, so ``"`netsh ..."`` is a newline followed by ``etsh``
#: rather than the command somebody meant to name.
_POWERSHELL_ESCAPES = frozenset("0abefnrtv`\"$'")


def _double_quoted_strings(script: str) -> list[str]:
    """Return the body of every double-quoted PowerShell string literal."""
    return re.findall(r'"((?:[^"`]|`.)*)"', script)


@pytest.mark.unit
def test_the_container_port_is_a_parameter_defaulting_to_the_documented_one(
    canonical_script: str,
) -> None:
    """The port must be supplyable, and must still default to the documented one.

    Windows reserves TCP ranges that move between reboots, so a fixed port is a
    run that cannot start. The alternative, discovered the hard way, is an
    operator editing the script and running the copy. Evidence produced that way
    describes a script that is not the committed one, which is the one property
    this evidence has to have.
    """
    assert re.search(
        rf"\[int\]\$ContainerPort\s*=\s*{DEFAULT_CONTAINER_PORT}\b", canonical_script
    ), "-ContainerPort must be a parameter defaulting to the documented port"


@pytest.mark.unit
def test_the_container_port_is_range_validated(canonical_script: str) -> None:
    """An impossible port must be refused at the call site, naming the value.

    PowerShell validates a parameter attribute before the script body runs, so
    the refusal arrives before Docker is contacted and before an evidence
    directory is created for a run that cannot happen.
    """
    assert "[ValidateRange(1, 65535)]" in canonical_script


@pytest.mark.unit
def test_the_script_never_chooses_a_port_by_itself(canonical_script: str) -> None:
    """A run that quietly moved would record a port nobody asked for.

    Worse, the next person to hit a collision would have no way to know it had
    happened before, because nothing would have failed.
    """
    for forbidden in ("Get-Random", "GetAvailablePort", "FindFreePort", "Port = 0"):
        assert forbidden not in canonical_script, (
            f"'{forbidden}' suggests the script picks a port itself; it must refuse instead"
        )


@pytest.mark.unit
def test_the_same_port_reaches_docker_and_the_connection_url(
    canonical_script: str,
) -> None:
    """One value, propagated -- not two that can drift apart.

    Everything downstream is derived from the URL: the DHRUVA_DB__* variables
    alembic reads, the migration stages, and the test session. A published port
    that disagreed with the URL would fail six stages in, with a connection
    error rather than a port error.
    """
    assert '-p "${ContainerPort}:5432"' in canonical_script
    assert "localhost:$ContainerPort/dhruva_test" in canonical_script


@pytest.mark.unit
def test_the_selected_port_is_recorded_in_the_evidence(canonical_script: str) -> None:
    """A run on a non-default port is a fact about that run.

    Both logs carry it, and they answer different questions: the environment log
    records what was *requested*, the database-target log what was actually
    published. They differ when a stale inherited URL is discarded and a
    container is started after the environment log was already written.
    """
    assert "container_port:  $ContainerPortNote" in canonical_script
    assert "container_port:           $effectivePort" in canonical_script


@pytest.mark.unit
def test_supplying_both_a_database_url_and_a_port_is_refused(
    canonical_script: str,
) -> None:
    """Either resolution would be a guess.

    Honouring the URL makes -ContainerPort silently do nothing; honouring the
    port would connect somewhere the caller never named. Refusing is the only
    answer that cannot be wrong.
    """
    assert re.search(
        r"if \(\$DatabaseUrl -and \$ContainerPortWasSupplied\) \{\s*\n\s*throw",
        canonical_script,
    ), "the script must refuse -DatabaseUrl together with -ContainerPort"


@pytest.mark.unit
def test_the_port_is_checked_before_the_image_is_pulled(canonical_script: str) -> None:
    """An unusable port should cost a second, not a multi-minute image download."""
    assert_index = canonical_script.index("Assert-PortIsBindable $ContainerPort")
    pull_index = canonical_script.index("docker pull $PostgresImage")

    assert assert_index < pull_index, "the port check must run before the pull"


@pytest.mark.unit
def test_an_unbindable_port_names_the_port_and_the_way_out(
    canonical_script: str,
) -> None:
    """Windows says "permission denied"; that is not what an operator needs.

    The failure has to name the port, say why it cannot be used, and give the
    flag that fixes it -- otherwise a working setup appears to break overnight
    for no reason anybody can act on.
    """
    match = re.search(r"function Assert-PortIsBindable.*?\n\}\n", canonical_script, re.DOTALL)
    assert match is not None, "Assert-PortIsBindable is not declared"
    body = match.group(0)

    assert "-ContainerPort" in body, "the failure must name the flag that fixes it"
    assert "excludedportrange" in body, "the failure must point at Windows reservations"
    assert "throw" in body, "an unusable port must stop the run, not warn"


@pytest.mark.unit
def test_no_double_quoted_string_nests_another_inside_a_subexpression(
    canonical_script: str,
) -> None:
    """PowerShell 5.1 will not parse it, and the script says so in a comment.

    The failure is a parse error before anything runs, so it cannot be caught by
    a run that got far enough to produce evidence. This test is the substitute
    for the PowerShell interpreter that the authoring environment lacks.
    """
    offenders = [body for body in _double_quoted_strings(canonical_script) if '$("' in body]

    assert offenders == [], f"nested double-quoted subexpression: {offenders}"


@pytest.mark.unit
def test_no_backtick_in_a_double_quoted_string_is_an_accidental_escape(
    canonical_script: str,
) -> None:
    """``"`netsh ..."`` is a newline and the word "etsh", not a command name.

    Quoting a command inside an error message is a natural thing to write and a
    silent corruption of the message the operator is supposed to act on.
    """
    offenders = [
        body
        for body in _double_quoted_strings(canonical_script)
        for index, character in enumerate(body)
        if character == "`" and index + 1 < len(body) and body[index + 1] not in _POWERSHELL_ESCAPES
    ]

    assert offenders == [], f"backtick used as a quote rather than an escape: {offenders}"
