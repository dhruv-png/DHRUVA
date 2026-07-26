# ADR-049 — Mutation testing for the financial primitives

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Introduced by Amendment A15 during S03 implementation.

## Context

S03's test suite reports 99.3% line coverage across the monetary primitives. That
number is worth less than it appears.

Coverage measures which lines *ran*. It says nothing about whether anything was
*asserted* about them. A test that constructs a `Money`, calls `.allocate()`, and
checks only that the result has three elements will report full coverage of the
largest-remainder algorithm while catching none of its defects. Every subsequent
subsystem will inherit these primitives without re-verifying them, so the
distinction between "executed" and "verified" matters more here than anywhere
else in the platform.

ADR-030 caps the standing quality gates at six and requires a seventh to
demonstrate that it catches a class of defect the existing six structurally
cannot. That test is met. Mutation testing introduces deliberate faults — flipping
a comparison, altering a constant, removing a statement — and reports which faults
the suite fails to detect. No amount of coverage, typing, linting or architectural
enforcement can answer that question, because all six existing gates examine the
code rather than the tests.

## Decision

Mutation testing is introduced as the **seventh quality gate**, deliberately
narrow in scope.

**Scope.** `dhruva.shared.money` and `dhruva.shared.time` initially. These hold
arithmetic where a subtle fault produces a plausible wrong number rather than a
crash. Coverage extends to a subsystem only when that subsystem's own arithmetic
justifies it — S12 (Trading Cost Engine), S24 (Portfolio Engine) and S30
(Backtesting Engine) are the expected additions.

**Threshold.** A mutation score of **≥ 90%** on the covered packages. Surviving
mutants are either killed by a new test or recorded, with a reason, in the
subsystem's Technical Debt Register. A survivor that is genuinely equivalent —
a mutation producing semantically identical code — is documented rather than
chased.

**Cadence.** Not on every commit. Mutation testing takes minutes rather than
seconds, and a gate that makes the inner loop painful is a gate that gets
bypassed. It runs on a schedule and before each subsystem's approval, via
`make mutants`. The other six gates keep running on every commit.

**Tooling.** `mutmut`, chosen over `cosmic-ray` for a simpler operating model and
a smaller configuration surface. This is a reversible preference rather than an
architectural commitment; the score is what matters, not the tool.

## Rationale

The alternative — trusting line coverage — is the status quo this decision
rejects, and the reason is specific rather than general. This platform's failure
mode is not a crash; it is a plausible wrong number that reconciles against
nothing and is discovered months later. A test suite that runs every line of
`Money.allocate` without asserting that the parts sum to the whole would look
identical, on every existing gate, to one that does.

Restricting the scope is as deliberate as introducing the gate. Mutation testing
the FastAPI wiring or the boundary checker would cost minutes per run to
re-confirm things the other gates already establish. The value is concentrated
exactly where arithmetic meets money.

Running on a schedule rather than per-commit is a concession to the inner loop,
made explicitly rather than discovered later. A seven-gate pre-commit hook taking
four minutes would be disabled within a fortnight, and a disabled gate protects
nothing.

## Consequences

`mutmut` joins the development dependency set and `make mutants` joins the
Makefile. Neither affects the runtime dependency set.

Some surviving mutants will be equivalent and unkillable. Recording them with a
reason is the expected outcome, not a failure — and the record is more useful
than an artificially inflated score.

The threshold will initially be met by writing more assertions rather than more
tests. That is the point: mutation testing pushes a suite toward asserting
behaviour rather than exercising paths, which is the property that makes these
primitives trustworthy to forty later subsystems.

Where a surviving mutant reveals that a behaviour is genuinely unspecified rather
than merely untested, the correct response is to decide the behaviour and record
it — in the docstring, in `DOMAIN.md`, or in an ADR — before writing the test.
