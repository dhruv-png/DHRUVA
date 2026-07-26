# ADR-030 — Documentation is updated in the same commit as the code it describes

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.2, section 4. Introduced by Amendments A7 and A8 on approval of S01.

## Context

The plan already requires documentation as a Definition of Done item, but it does
not say *when*. "Before the subsystem is approved" permits a final documentation
commit written days after the code, from memory — which is the least reliable
record available and the standard way documentation drifts from implementation.

The project also now has six quality gates, and an open question about whether
more should be added over time.

## Decision

Every subsystem updates, **in the same pull request as its implementation**:

* any ADRs its design implies;
* `docs/decisions.md`, regenerated if any ADR was added or superseded;
* `CHANGELOG.md`, under `[Unreleased]`;
* the README, where externally visible behaviour changed;
* the OpenAPI specification, where an endpoint changed.

Documentation is never a follow-up commit.

`CHANGELOG.md` follows Keep a Changelog with Semantic Versioning. Entries are
written for a reader who was not present — not as a restatement of the commit log,
which git already holds.

The standing quality gates are exactly six: ruff, `mypy --strict`, pytest,
import-linter, the boundary checker, and the ADR guard. A seventh is added only
when it can be shown to catch a class of defect the existing six miss.

## Rationale

Same-commit updates make the review question answerable. "Is this documentation
accurate?" is unanswerable in isolation; "does this documentation describe this
diff?" can be answered in seconds by anyone reading the pull request.

The alternative — a documentation pass at the end of each phase — was rejected
because it fails precisely when it matters most. Documentation debt accrues
fastest during the hard subsystems, which are exactly the ones a future reader
will need the documentation for.

The gate ceiling exists because tooling accretes by default. Every gate costs
feedback latency on every commit for the rest of the project, and a gate that
duplicates coverage another already provides is pure cost. Requiring a specific
defect class before adding one keeps the toolchain honest.

## Consequences

Pull requests are larger, and the template's checklist grows. Reviewing a
subsystem means reviewing its prose as well as its code, which is slower.

The compensating benefit is that `git log --follow` on any document explains not
just what changed but which code change caused it — and for a project whose
governing artefact is a plan document, that traceability is the point.

The six-gate ceiling means a proposal to add a gate must arrive with evidence.
That is a deliberate friction, and it will occasionally block a gate that would
have been mildly useful.
