# Architecture Decision Records

Every major architectural decision in D.H.R.U.V.A is recorded here, one file per
decision, numbered monotonically and never renumbered.

**An accepted record is immutable.** Only its `Status` and `Superseded by` lines
may ever change. To change a decision, write a new record that supersedes the old
one and set the old one's status to `Superseded by ADR-nnn`. The original stays in
the repository as history. This is ADR-027, and it is enforced mechanically by
`dhruva-adr-guard` in pre-commit and CI, backed by `checksums.json`.

## What warrants a record

A decision is *major* -- and therefore requires a record -- if it does any of:

* constrains more than one bounded context;
* is costly to reverse;
* selects between viable alternatives;
* adds or removes a dependency on an external system;
* changes a data model, an API contract, or a security control.

## Adding a record

1. Copy `TEMPLATE.md` to `ADR-<next>-<kebab-slug>.md`.
2. Write it. Keep `Status: Proposed` while it is under discussion.
3. On acceptance set `Status: Accepted`, then run `dhruva-adr-guard --update`.
4. Commit the record and the updated `checksums.json` together.

## Statuses

| Status | Meaning |
|---|---|
| `Proposed` | Under discussion. Not binding. |
| `Accepted` | Binding. Immutable. |
| `Superseded by ADR-nnn` | Replaced. Retained as history. |
| `Deprecated` | No longer relevant; not replaced. |

## Heavier changes

A change that invalidates a **gate** or a **scope parameter** (plan section 1.2)
is an *Architecture Revision*, not an ADR. It requires written Product Owner
approval and a reissue of the Master Project Plan at the next version.
