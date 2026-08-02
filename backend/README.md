# Backend

Python 3.12, strict typing, async-first at I/O boundaries.

## Layout

```
src/dhruva/
├── contexts/<context>/{api.py,domain,application,infrastructure,interfaces}
├── shared/       # shared kernel (S03)
├── tooling/      # architectural controls: boundaries.py, adr_guard.py
├── api/          # FastAPI composition root         (S02+)
├── workers/      # Celery composition root          (S05+)
└── ingest/       # ingestion composition root       (S10+)
tests/{unit,integration}/
```

## Layer rules

Dependencies point inward. `domain` imports nothing but `dhruva.shared` and the
standard library. `interfaces` never imports `infrastructure` — wiring is the
composition root's job. Enforced by `.importlinter`.

## Context rules

A context reaches another only through that context's `api` module, and only if
the dependency appears in `ALLOWED_CONTEXT_DEPENDENCIES` (a transcription of plan
§5). Enforced by `dhruva-check-boundaries`.

## Dependencies

Runtime dependencies are pinned in `pyproject.toml`, `uv.lock` and the exported
requirements lockfiles. Each arrives with the subsystem that justifies it and a
provenance comment: FastAPI and structlog with S02, SQLAlchemy and Alembic with
S04, Celery and Redis with S05, and cryptography, PyJWT and argon2 with S06.
Install from the lock with `uv sync --all-groups --frozen`; do not add an
unreviewed package directly to the environment.

The v0.6.0 HTTP baseline is FastAPI `0.133.1` with transitive Starlette `1.3.1`.
The development test client is `httpx2==2.9.1`, Starlette's supported successor
to the legacy `httpx` package.

## Commands

Run from the repository root: `make lint`, `make types`, `make boundaries`,
`make adr`, `make test`, `make cov`, or `make check` for all of them.
