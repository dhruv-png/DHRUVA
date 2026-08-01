# S06 — Dependency plan

> **Status:** Prepared, not executed. Every step below is blocked on the
> canonical Windows environment, on Design Review, or on both. Nothing here has
> been applied to `pyproject.toml`.
>
> Companion to `docs/design/S06-identity-secrets-audit.md` §14 question 1.

---

## Why this is a separate document

A dependency addition in this repository is **atomic across four artefacts**, and
three of them are enforced by test:

| Artefact | Enforced by |
|---|---|
| `backend/pyproject.toml` — exact `==` pin (ADR-032) | `test_every_runtime_dependency_is_pinned_and_accounted_for` |
| `DEPENDENCY_PROVENANCE` in `tests/unit/test_toolchain_config.py` | the same test — the set and the map must agree exactly |
| `backend/requirements.lock` | `test_the_deployment_lockfile_contains_every_runtime_dependency` |
| `backend/uv.lock`, `backend/requirements-dev.lock` | `UV_FROZEN=1` in CI |

Applying any one of them alone turns the suite red. S05 learned this the
expensive way: `uv add` maintains `uv.lock` only, which produced a green suite, a
satisfied provenance map, and a deployment artefact that would not have installed
the new package at all. The test above exists because of it.

So the three libraries S06 needs cannot be added incrementally, and cannot be
added at all from the Linux sandbox — lockfile regeneration is work on the
canonical environment.

**Correction recorded while preparing this.** The repository previously said
"all four lockfiles" (in `docs/releases/v0.5.0.md` and in the S06 design). There
are **three**, and only two of them are produced by `uv pip compile`; `uv.lock`
is uv's own resolution. Git history confirms no fourth has ever been tracked.
Both documents are corrected in the same commit as this one.

---

## The three libraries

Each is justified by an accepted decision, but **each is assumed by an ADR that
is still proposed**, so none may be added before Design Review.

| Library | For | Justified by | Assumed by |
|---|---|---|---|
| `cryptography` | AES-256-GCM envelope encryption of stored credentials | ADR-020 (accepted): "envelope-encrypted (per-record data key, master key from environment/KMS)" | proposed ADR-070, which picks the cipher |
| `pyjwt` | Signing and verifying access tokens | Plan §15.1 (approved): "JWT access (15 min) + rotating refresh token" | proposed ADR-072 |
| `argon2-cffi` | Hashing the secret a principal presents at authentication | Plan §15.1 AuthN row; ADR-033 forbids storing it recoverably | proposed ADR-072 |

**Latest versions on PyPI as of 2026-08-01**, resolved from the sandbox and
**not yet verified against Python 3.12 on Windows**:

- `cryptography==50.0.0`
- `pyjwt==2.13.0`
- `argon2-cffi==25.1.0`

Treat these as starting points for `uv add`, not as pins to copy. `cryptography`
and `argon2-cffi` are both compiled extensions, so the resolution that matters is
the one your environment produces.

---

## Exact commands, in order

Run from `backend/`. Steps 1–3 are one atomic change; do not commit between them.

**1. Add the runtime dependencies.**

```
uv add "cryptography==50.0.0" "pyjwt==2.13.0" "argon2-cffi==25.1.0"
```

**2. Regenerate the two deployment lockfiles.** These are the canonical commands,
copied from each file's own header — `uv add` does **not** update them.

```
uv pip compile pyproject.toml --python-version 3.12 --universal --generate-hashes --all-extras -o requirements.lock
uv pip compile pyproject.toml --python-version 3.12 --universal --generate-hashes --group dev -o requirements-dev.lock
```

**3. Record provenance.** Add to `DEPENDENCY_PROVENANCE` in
`tests/unit/test_toolchain_config.py`, matching the existing entries' style:

```python
    "cryptography": (
        "S06 - AES-256-GCM envelope encryption of stored credentials (ADR-020, "
        "ADR-070). Confined to the crypto adapter behind the KeyProvider port; "
        "no domain module imports it"
    ),
    "pyjwt": "S06 - signing and verifying access tokens (ADR-072), confined to "
    "the TokenIssuer adapter",
    "argon2-cffi": "S06 - hashing the secret a principal presents; ADR-033 "
    "forbids storing it recoverably",
```

And to the comment block above `dependencies` in `pyproject.toml`, following the
S02/S04/S05 pattern:

```toml
  # S06 -- Identity, Secrets Vault & Audit:
  #   cryptography   AES-256-GCM envelope encryption (ADR-020, ADR-070)
  #   pyjwt          access-token signing and verification (ADR-072)
  #   argon2-cffi    password hashing; never reversible (ADR-033)
```

**4. Verify before committing.**

```
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy --strict src tests
uv run lint-imports
uv run dhruva-check-boundaries
uv run dhruva-adr-guard
uv run pytest tests/unit -q
```

The two tests that would catch a partial application are
`test_every_runtime_dependency_is_pinned_and_accounted_for` and
`test_the_deployment_lockfile_contains_every_runtime_dependency`. Both should be
green before the commit, and both should have been red before step 3.

---

## Integration points

Where each library is permitted to appear, and where it must not.

| Library | Permitted in | Must not appear in |
|---|---|---|
| `cryptography` | `contexts/platform/infrastructure/crypto/` — the `KeyProvider` adapter | any `domain/` or `application/` module |
| `pyjwt` | `contexts/platform/infrastructure/` — the `TokenIssuer` adapter | any `domain/` module; any interfaces module (which receives a verified principal, not a token) |
| `argon2-cffi` | the same infrastructure adapter that stores and verifies the presented secret | any `domain/` module |

The ports themselves — `KeyProvider` and `TokenIssuer` — live in
`contexts/platform/domain/identity/` and import none of the three. That is the
entire point of ADR-070's port argument: the domain must not know whether a key
is unwrapped locally or by a KMS.

### Finding: no boundary rule enforces the crypto separation

Boundary rules R1–R9 do not cover this. R7 keeps persistence frameworks out of
the domain and R9 keeps transports out of domain and strategy, but **nothing
would fail the build if a domain module imported `cryptography` directly.** The
port separation above is currently a convention.

That matters more here than it would elsewhere: a domain module that imports a
cipher is a domain module that cannot be exercised without key material, which is
the same class of failure R9 exists to prevent for transports.

Adding a rule R10 — *no domain module imports a cryptographic or token library* —
would close it, following exactly the pattern ADR-068 used for R9, including
being written before the package it constrains exists. **A new boundary rule is
an ADR-level change and requires your approval**, so it is recorded here rather
than implemented.

---

## What is blocked, and on what

| Step | Blocked on |
|---|---|
| Adding any of the three libraries | Design Review accepting ADR-070 and ADR-072 |
| Lockfile regeneration | The canonical Windows environment |
| Boundary rule R10 | Your approval — it is a new rule, not an implementation detail |
| Pinning verified versions | An actual resolution on Python 3.12 |

## Unrelated debt noticed while preparing this

`pytest-benchmark==5.1.0` is a declared development dependency that **nothing
imports**. All 29 `benchmark`-marked tests time themselves; none uses the
plugin's fixture. Removing it is a one-line change to `pyproject.toml` plus the
same lockfile regeneration, so it is cheapest to do in the same pass as the
additions above rather than as its own trip through the canonical environment.
