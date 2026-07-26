# ADR-029 — Version control is part of the Definition of Done; no subsystem accumulates uncommitted

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.2, section 4. Introduced by Amendment A6 on approval of S01.

## Context

S01 completed as 131 files with no version control history at all. That is a
tolerable state for exactly one subsystem and an indefensible one for two. The
project has a bus factor of one (risk R15) and a fifteen-month horizon, and its
roadmap already treats the subsystem as the atomic unit of work — but nothing yet
made that unit atomic in the repository.

## Decision

An approved subsystem is committed to a dedicated branch, merged to `main` via
pull request, and pushed **before the next subsystem begins**.

Every subsystem delivers, as part of its output:

* a branch name, `snn-<slug>`;
* one or more Conventional Commit messages;
* a pull-request title;
* a pull-request summary;
* a tag recommendation.

`main` is tagged `v0.<nn>.0` at each subsystem completion, where `<nn>` is the
subsystem number. Gates are tagged `gate-<id>` until execution exists; `v1.0.0` is
reserved for Gate G5.

Where the working environment has authenticated Git access, the operations are
performed directly. Where it does not, the exact commands are supplied in a form
that can be pasted without modification or interpretation — no placeholders left
for the reader to resolve beyond a single remote URL set once.

## Rationale

Two of the three highest-exposure risks in the register are made materially worse
by uncommitted work. An unpushed subsystem is one disk failure from never having
existed (R15). A branch holding three subsystems is unreviewable, unrevertable,
and impossible to bisect, which turns a small defect into an archaeology exercise
and accelerates scope collapse (R01).

Committing at the approval boundary also makes the history match the plan: the
roadmap's unit is the subsystem, the gate criteria are stated per subsystem, and
now `git log` agrees. `git show v0.20.0` becomes a precise answer to "what
existed at Gate G-MCP", which no amount of documentation reproduces as reliably.

The alternative considered was committing continuously during development, which
is normal practice on a team. It was rejected here only for the *approval*
boundary, not for working commits: the rule is that nothing crosses a subsystem
boundary uncommitted, and working commits within a branch remain encouraged.

## Consequences

The Definition of Done gains a version-control section. A subsystem that is
otherwise finished but uncommitted is `IN PROGRESS`, not `DONE`, regardless of how
green its gates are.

Tags become load-bearing: they are the mechanism by which a gate is verifiable
after the fact, so a mis-tagged commit is a real defect rather than cosmetic.

There is a cost. Producing a commit plan, a PR title and a PR summary for every
subsystem is perhaps twenty minutes of work each time, and on a solo project the
pull request has no second reader. That cost is accepted deliberately: the PR body
is the durable record of *why* a subsystem looks the way it does, and the future
reader it is written for is the author, eighteen months from now.
