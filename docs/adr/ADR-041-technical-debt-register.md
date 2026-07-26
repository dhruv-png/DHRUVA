# ADR-041 — Every subsystem maintains a Technical Debt Register

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.4, section 4. Introduced by Amendment A14 on approval of the S02 implementation.

## Context

Deferral is a legitimate and necessary engineering act. A solo project with a
fifteen-month horizon cannot do everything at the moment it first becomes
possible, and pretending otherwise produces either paralysis or dishonesty.

What makes deferral dangerous is not the deferral -- it is the forgetting. A
shortcut taken deliberately, with a reason, and a rough cost, is a decision. The
same shortcut six months later, with nobody remembering why, is a defect of
unknown origin. Risk R15 in the register is a bus factor of one, which makes the
gap between those two states shorter than usual.

## Decision

From S03 onward, every subsystem's design document carries a **Technical Debt
Register** with one row per deferred item and six columns:

| Column | Content |
|---|---|
| Item | The deferred improvement or known limitation |
| Rationale | Why deferring is the right call *now*, not merely the easier one |
| Effort | Rough implementation cost in sessions |
| Priority | ``HIGH`` / ``MEDIUM`` / ``LOW``, judged by consequence if never done |
| Milestone | The gate or subsystem by which it should be resolved |
| Status | ``OPEN`` / ``RESOLVED`` / ``ACCEPTED`` (accepted permanently) |

Two entries are mandatory where they apply: anything suppressed by a lint
``noqa``, and anything a test documents as a limitation rather than asserts as
correct. Both are deferrals wearing other clothes.

The register is reviewed at every gate. Items marked ``ACCEPTED`` are permanent
and require a one-line reason; unreviewed ``OPEN`` items older than two gates are
escalated to the Product Owner.

S02 carries a register as well, though the requirement formally begins at S03 --
it has real debt to record, and an empty precedent would be a poor one.

## Rationale

The alternative -- tracking debt in an issue tracker -- was considered and
rejected for this project specifically. An issue divorced from the design
document it belongs to loses the context that makes it decidable, and a solo
developer's issue tracker becomes a graveyard faster than a document does.
Keeping the register beside the design means the reader encounters the debt while
reading the thing it applies to.

Priority is judged by *consequence if never resolved* rather than by urgency.
Urgency is a property of the moment; consequence is a property of the item, and
only the second survives six months of not looking at it.

## Consequences

Design documents grow a section, and gate reviews grow an agenda item.

Some debt will be marked ``ACCEPTED`` and never resolved. That is a legitimate
outcome and is the point of having the column: a permanent trade-off recorded
once is better than the same argument re-litigated every time someone rediscovers
it.
