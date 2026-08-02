# Build Environment

Reproducibility contract for D.H.R.U.V.A (ADR-032). A build that cannot be
reproduced cannot be debugged, cannot be audited, and cannot be trusted to behave
in production the way it behaved in test.

## Declared versions

| Component | Version | Declared in |
|---|---|---|
| Python | **3.12** | `.python-version`, `backend/pyproject.toml` (`requires-python`), `.github/workflows/ci.yml` |
| uv | ≥ 0.9 | `docs/BUILD.md`, CI via `astral-sh/setup-uv@v6` |
| ruff | 0.16.0 | `backend/pyproject.toml` dev group, `.pre-commit-config.yaml` |
| mypy | 1.18.2 | `backend/pyproject.toml` dev group |
| import-linter | 2.4 | `backend/pyproject.toml` dev group |
| pytest | 8.4.2 | `backend/pyproject.toml` dev group |
| FastAPI | 0.133.1 | `backend/pyproject.toml` runtime dependencies |
| Starlette | 1.3.1 | `backend/uv.lock` (FastAPI transitive dependency) |
| httpx2 | 2.9.1 | `backend/pyproject.toml` dev group (Starlette test client) |

A test — `test_python_version_is_declared_consistently` — asserts the three Python
declarations agree. Each is read by a different tool, and the assertion is cheaper
than the afternoon lost to discovering they disagree.

## Operating system baseline

| Context | Baseline | Why |
|---|---|---|
| CI | `ubuntu-24.04` (pinned, not `ubuntu-latest`) | `latest` silently changes underneath a green build |
| Container runtime | `python:3.12-slim-bookworm` | Debian stable; slim keeps the attack surface small |
| Database | `timescale/timescaledb:2.17.2-pg16` | Pinned tag; TimescaleDB is required from S04 |
| Cache / event bus | `redis:7.4-alpine` | Pinned tag; Streams durability requires appendonly |
| Development | Linux or macOS, x86-64 or arm64 | No platform-specific code; CI runs x86-64 only |

**Image digests.** Tags are mutable. Before the first deployed environment (S10),
every image reference gains an `@sha256:` digest. Tracked in the S02 technical
debt register.

## Dependency locking

Direct dependencies are pinned exactly in `backend/pyproject.toml`. Transitive
dependencies are pinned, with hashes, in two committed lockfiles:

| File | Contents | Install |
|---|---|---|
| `backend/requirements.lock` | Runtime + all extras | `uv pip install --require-hashes -r backend/requirements.lock` |
| `backend/requirements-dev.lock` | Development toolchain | `uv pip install --require-hashes -r backend/requirements-dev.lock` |

Regenerate after any dependency change:

```bash
cd backend
uv lock
uv pip compile pyproject.toml --python-version 3.12 --universal \
    --generate-hashes --all-extras -o requirements.lock
uv pip compile pyproject.toml --python-version 3.12 --universal \
    --generate-hashes --group dev -o requirements-dev.lock
```

Commit `pyproject.toml`, `uv.lock` and both exported lockfiles in the same commit.
Install the canonical environment with `uv sync --all-groups --frozen`. For a
deliberate narrow upgrade, use `uv lock --upgrade-package <name>` before
regenerating both exports; review the normalized package/version delta before
commit.

Audit the exact hash-pinned deployment set, without installing the local editable
project into the audit target:

```bash
uv run --project backend pip-audit --strict --desc --require-hashes \
    --disable-pip --requirement backend/requirements.lock
```

Generate a release CycloneDX SBOM from that same lock:

```bash
uv run --project backend pip-audit --strict --require-hashes --disable-pip \
    --requirement backend/requirements.lock --format cyclonedx-json \
    --output docs/releases/vX.Y.Z.sbom.cdx.json
```

## CI guarantees

- Builds run from a **clean checkout**; no virtual environment is restored into
  the build. Caches accelerate download, never resolution.
- Container images are built from the repository root with no build-time network
  access beyond the package index.
- Security scanning runs `pip-audit`, `gitleaks` over **full history**, and Trivy
  against the built image, weekly as well as per-commit.

## Verification-environment constraints

Development validation currently runs on Python 3.10 because 3.12 cannot be
obtained in the sandbox. The source targets 3.12; the gap is bridged by a
sandbox-only compatibility shim that is **not part of this repository** and never
ships. Three consequences are recorded as debt, all resolved by validating on 3.12:

1. `UP047` (PEP 695 type parameters) is suppressed on one signature.
2. `asyncio.TimeoutError` is named alongside `TimeoutError` in one except clause.
3. Benchmark figures are conservative — 3.11 and 3.12 are materially faster.

## Reproducing a build

```bash
git clone <repo> && cd dhruva
git checkout v0.2.0
make setup
make check
```

Any divergence from a green `make check` at a tagged commit is a reproducibility
defect and should be reported as such.
