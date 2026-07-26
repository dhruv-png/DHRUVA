## What and why

<!-- One paragraph. Link the subsystem design document. -->

Subsystem: S__
Design doc: `docs/subsystems/S__-____.md`

## Documentation synchronised in this PR (ADR-030)

- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] ADRs written for any major decision; `docs/decisions.md` regenerated
- [ ] README updated if externally visible behaviour changed
- [ ] OpenAPI updated if any endpoint changed
- [ ] `docs/session-log.md` entry added

## Commit plan (ADR-029)

Branch: `snn-<slug>`
Tag on merge: `v0.<nn>.0`

## Definition of Done (plan section 10.1)

- [ ] No placeholders, no `TODO`/`FIXME`, no unreachable `NotImplementedError`
- [ ] `make check` green locally
- [ ] Tests added: unit, and property tests for any numerical or monetary logic
- [ ] Failure-injection test added where the change touches I/O
- [ ] Documentation updated (module README, runbook, OpenAPI as applicable)
- [ ] Architectural decisions recorded as ADRs, checksums registered (ADR-027)

## Architecture

- [ ] Bounded-context boundaries respected; `ALLOWED_CONTEXT_DEPENDENCIES` unchanged, or changed **with** an ADR
- [ ] No naive `datetime`; time is injected (ADR-006, ADR-011)
- [ ] No float money (ADR-005)
- [ ] Ambiguous states fail closed (ADR-022)

## Stage gate (ADR-028)

- [ ] This change introduces **no** order-constructing or order-placing code

## How this could silently be wrong

<!-- Required for CRIT subsystems. The most useful section in the PR. -->
