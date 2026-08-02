# D.H.R.U.V.A

**Dynamic Heuristic Regime Understanding & Volatility Analytics**
An institutional-grade quantitative analytics platform for the Indian equity and derivatives markets.

> **Stage 1 — Minimum Credible Product.** This repository contains analytics only.
> There is no execution capability, no broker order permission, and no code that can
> construct an order. That is a deliberate, enforced constraint (ADR-028), not an
> unfinished feature.

---

## What this is

Most retail tooling shows indicators. D.H.R.U.V.A is built around three things that
tooling usually omits:

1. **Regime detection** — classify the market state and condition every downstream
   read on it.
2. **Honest cost accounting** — an Indian-market cost engine accurate to the paisa,
   applied identically in research and in production.
3. **Explainability** — every output carries a structured evidence bundle. No
   unexplained numbers on screen.

## Governing documents

| Document | What it governs |
|---|---|
| [`docs/DHRUVA_MASTER_PROJECT_PLAN.md`](docs/DHRUVA_MASTER_PROJECT_PLAN.md) | Everything. Roadmap, gates, Definition of Done, risk register. **v1.4, approved 2026-07-26.** |
| [`docs/adr/`](docs/adr/) | Architecture decisions. Immutable once accepted (ADR-027). |
| [`docs/subsystems/`](docs/subsystems/) | Per-subsystem design documents. |
| [`docs/runbooks/`](docs/runbooks/) | How things fail and how to recover them. |
| [`docs/session-log.md`](docs/session-log.md) | What was done, decided, and is next. |

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
git clone <repo> && cd dhruva
make setup      # create the virtualenv, install all dependency groups
make hooks      # install pre-commit hooks
make up         # start PostgreSQL/TimescaleDB and Redis
make check      # run every gate CI runs
```

`make help` lists every target.

## Architecture in one paragraph

A **modular monolith** with nine bounded contexts whose boundaries are enforced at
build time, not by convention (ADR-001). Four processes — `api`, `ingest`,
`worker`, `scheduler` — share one codebase. Each context is layered
`interfaces → application → domain`, with `infrastructure` holding the adapters;
dependencies point inward only. Contexts talk to each other through one public
`api` module each, or by publishing domain events. Nothing is a microservice, and
everything is shaped so that it could become one.

```
backend/src/dhruva/
├── contexts/          # C1-C9, one package per bounded context
│   └── <context>/
│       ├── api.py             # the ONLY import surface other contexts may use
│       ├── domain/            # entities, value objects, ports. Zero I/O.
│       ├── application/       # use cases, orchestration
│       ├── infrastructure/    # adapters: DB, broker, bus
│       └── interfaces/        # routers, CLI, task entrypoints
├── shared/            # shared kernel: Money, TradingDay, Clock, events
├── tooling/           # the architectural controls, as executable code
├── api/ workers/ ingest/      # composition roots
```

## The controls

These run in pre-commit and in CI. They are why the architecture stays the
architecture.

| Control | Enforces |
|---|---|
| `ruff` | Style, plus banned constructs: naive datetimes, `print`, leftover `TODO`s, commented-out code |
| `mypy --strict` | ADR-024. A bare `# type: ignore` is an error |
| `lint-imports` | Clean Architecture layer ordering inside every context |
| `dhruva-check-boundaries` | Public-API-only access and the declared context dependency matrix (plan §5) |
| `dhruva-adr-guard` | Decision-log integrity and immutability (ADR-027) |
| `pytest --cov` | 90% coverage floor, branch coverage on |

## Where this is going

Stage 1 ends at **Gate G-MCP** (~Week 28): a go/no-go review on whether the regime
thesis holds. Stage 2 — intelligence, decision, execution, full dual-mode UI — is
not authorised until it passes. See plan §8.

## Licence

Proprietary. Personal decision-support tool. Not investment advice, and not offered
to third parties.
