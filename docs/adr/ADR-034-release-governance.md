# ADR-034 — Release governance: every subsystem ships changelog, ADRs, tag, release notes, migration and rollback

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.3, section 4. Introduced by Amendment A11 on approval of S01.

## Context

ADR-029 made version control part of the Definition of Done: a subsystem is
committed, tagged and pushed before the next begins. That establishes *that* a
release happens, but not *what a release contains*.

The gap matters most at the moment it is least convenient. When a subsystem
misbehaves in production at 09:20 on a trading day, the question is not what the
commits said — it is what changed, what has to be undone, and in what order.
Writing that down afterwards is writing it from memory, under pressure.

## Decision

A subsystem is not complete until all six release artefacts exist:

| Artefact | Location | Content |
|---|---|---|
| Changelog entry | `CHANGELOG.md` | What changed and why it mattered, for a reader who was not present |
| ADR updates | `docs/adr/`, `docs/decisions.md` | Every major decision the subsystem made |
| Version tag | annotated git tag `v0.<nn>.0` | Signed where signing is configured |
| Release notes | `docs/releases/v0.<nn>.0.md` | Summary, gate results, known limitations, dependency changes |
| Migration notes | in the release notes | Schema, configuration and operational changes required to move forward; "none" stated explicitly rather than omitted |
| Rollback instructions | in the release notes | The exact steps to return to the previous tag, including what is **not** reversible |

Rollback instructions must state honestly what cannot be undone. A destructive
migration, a consumed broker token, a published event — each is irreversible, and
a rollback section that implies otherwise is worse than none.

## Rationale

The two artefacts that carry disproportionate weight are migration and rollback
notes, and they are the two most often skipped, because at authoring time the
answer usually is "none". Writing "none" explicitly is not ceremony: it is the
difference between *verified none* and *nobody checked*.

Release notes are separated from the changelog because they serve different
readers. The changelog is cumulative and narrative; release notes are a
point-in-time operational record, including gate results and dependency deltas,
which a changelog should not carry.

The alternative — release notes only at gates rather than at subsystems — was
rejected because a gate spans up to thirteen subsystems, and by then the details
that make rollback instructions correct have been forgotten.

## Consequences

Every subsystem costs an additional document. For subsystems with no operational
surface, it is short, and it says "no migration required; rollback is `git
checkout` of the previous tag" — which is exactly the information a future
operator needs to stop worrying.

`docs/releases/` becomes the operational history of the platform, and the place to
look when a regression must be bisected by behaviour rather than by commit.
