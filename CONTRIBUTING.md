# Contributing

This project has one maintainer and a fifteen-month horizon. The conventions below
exist so that the codebase stays resumable by a competent stranger — most likely a
future version of its author — after a six-month gap.

## The lifecycle

Every subsystem goes through all seven steps, to completion, before the next one
starts:

```
Architecture → Implementation → Review → Testing → Optimisation → Documentation → Approval
```

Nine subsystems at 80% is worse than seven at 100%. That failure mode is risk R01
in the plan, and it is the most likely way this project dies.

## Before you write code

1. Read the subsystem's entry in plan §6 and its dependencies.
2. Write `docs/subsystems/Snn-<name>.md` using the 12-section format.
3. If a decision is *major* by ADR-027's test, write the ADR first.

## While you write code

- Absolute imports only. No relative imports.
- No naive `datetime`. Time comes from an injected `Clock` (ADR-006, ADR-011).
- No float money. `Money` wraps `Decimal`; storage is integer paise (ADR-005).
- Domain layers import no framework and perform no I/O.
- Ambiguity fails closed (ADR-022). Every gate needs an explicit "unknown" branch.
- No placeholders, no `TODO`, no unreachable `NotImplementedError`. `ruff` enforces this.

## Before you open a pull request

```bash
make check
```

That runs lint, strict types, both architecture checks, the ADR guard, and the test
suite with the coverage gate — the same six gates, in the same order, as CI.

Then update the documentation **in the same commit** (ADR-030): ADRs,
`docs/decisions.md`, `CHANGELOG.md` under `[Unreleased]`, the README if behaviour
changed, and OpenAPI if an endpoint changed. Documentation is never a follow-up
commit.

## Commits, branches and tags

Trunk-based. **One subsystem, one branch, one pull request**, merged and pushed
before the next subsystem begins (ADR-029). A branch containing work from two
subsystems is a process defect, not a convenience.

Branch names are `snn-<slug>`: `s02-core-runtime`, `s07-instrument-master`.

Conventional Commits, scoped by context or subsystem area:

```
feat(platform): add environment-aware settings with fail-fast validation
fix(reference): correct expiry resolution across the 2025-09-01 regime change
test(platform): add deliberate credential-leak test for the log redactor
docs(adr): supersede ADR-002 with ADR-031
chore(ci): order quality gates cheapest-first
```

**Tags.** A completed subsystem bumps the minor version and is tagged `v0.<nn>.0`
where `<nn>` is the subsystem number — `v0.1.0` is S01, `v0.2.0` is S02. Gates are
tagged `gate-<id>`, for example `gate-g0`. `v1.0.0` is reserved for Gate G5, the
first release capable of touching live capital.

`main` is always deployable and always green.

## Every subsystem delivers a commit plan

Alongside the code, each subsystem produces: the branch name, its Conventional
Commit messages, a pull-request title, a pull-request summary, and a tag
recommendation. Nothing crosses a subsystem boundary uncommitted (ADR-029).

## Changing an approved decision

You do not edit the ADR. You write a new one that supersedes it, set the old one's
status to `Superseded by ADR-nnn`, and run `make adr-register`. The guard will
refuse to overwrite an existing checksum, and that refusal is the point.

Changes that invalidate a **gate** or a **scope parameter** are Architecture
Revisions: they need written Product Owner approval and a reissued plan.

## Stage 1 constraint

Until Gate G-MCP passes, no code that can construct or place an order may exist in
this repository (ADR-028). A test enforces it.
