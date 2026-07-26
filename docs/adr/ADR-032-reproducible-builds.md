# ADR-032 — Reproducible builds: pinned dependencies, committed lockfile, documented environment

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.3, section 4. Introduced by Amendment A9 on approval of S01.

## Context

A build that cannot be reproduced cannot be debugged, cannot be audited, and
cannot be trusted to behave in production the way it behaved in test. For a
platform whose research results depend on numerical library versions, and whose
production behaviour will eventually move money, "it worked on my machine" is not
an acceptable failure mode.

S01 shipped with pinned dev-tool versions but no lockfile, because it had no
runtime dependencies to lock. S02 introduces the first ones.

## Decision

Every build is deterministic from a clean checkout.

1. **Dependencies are pinned.** Direct dependencies are pinned to exact versions
   in `pyproject.toml`. Transitive dependencies are pinned by lockfile.
2. **The lockfile is committed.** `backend/uv.lock` is versioned and reviewed. CI
   runs with `UV_FROZEN=1`, so a lockfile that disagrees with `pyproject.toml`
   fails the build rather than being silently regenerated.
3. **The Python version is declared in three places that must agree**:
   `.python-version`, `requires-python` in `pyproject.toml`, and the CI matrix. A
   test asserts they agree.
4. **The operating-system baseline is documented.** `docs/BUILD.md` records the
   supported OS, the container base image with its digest, and the reason for
   each.
5. **External tool versions are documented and pinned** — Docker images by tag and
   digest, GitHub Actions by major version, CLI tools by version.
6. **CI builds from a clean checkout** with no cached virtual environment
   restored into the build itself. Caches accelerate download, never resolution.

## Rationale

The strict alternative — vendoring dependencies or building from a fully hermetic
sandbox — buys marginal additional determinism at substantial ongoing cost, and
was rejected for a solo project. Lockfile plus frozen CI plus a digest-pinned base
image covers the failure modes that actually occur: a transitive dependency
publishing a breaking patch, a base image drifting under a mutable tag, and a
local environment diverging from CI.

Declaring the Python version in three places is redundant by design. Each is read
by a different tool, and a test that asserts their agreement is cheaper than the
afternoon lost to discovering they disagree.

## Consequences

Dependency upgrades become explicit, reviewable commits rather than ambient drift.
This is the intended effect and it is also friction: a security patch requires a
deliberate `uv lock --upgrade-package`, not merely a rebuild.

`docs/BUILD.md` becomes a maintained document with a real decay risk. It is added
to the Definition of Done checklist so that a change to the toolchain updates it
in the same commit (ADR-030).

Image digests must be refreshed periodically. The security workflow's weekly scan
is the trigger.
