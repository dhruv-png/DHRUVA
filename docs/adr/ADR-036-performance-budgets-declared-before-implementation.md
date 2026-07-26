# ADR-036 — Every subsystem declares measurable performance budgets before implementation

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.3, section 4. Introduced by Amendment A13 on approval of S01.

## Context

Plan section 12 sets platform-wide latency and throughput budgets — risk
authorisation under 100 ms, kill switch under one second, tick ingestion to
persistence under 500 ms. Those are the numbers that matter at the end. They say
nothing about whether the subsystem being built this week is on track to meet
them.

Performance discussions without numbers reduce to taste, and taste loses to
deadline pressure every time. A regression that is noticed as "feels slower" is a
regression that will be argued about rather than fixed.

## Decision

Every subsystem declares measurable performance targets in its design document,
**before implementation begins** — in Step 1 of the lifecycle, not Step 5.

Targets are drawn from whichever of these apply:

* startup time and time to readiness;
* steady-state memory, and memory growth over a simulated trading session;
* API latency at p50, p95, p99 — never the mean, which hides the tail that
  actually matters;
* computation throughput, expressed in the subsystem's own units (instruments per
  second, bars per second, options contracts priced per second);
* database query budgets: number of queries per operation, and worst-case rows
  examined;
* payload and allocation limits where they bear on the above.

Each target is **measured by a benchmark committed alongside the implementation**,
and the measured value is recorded in the subsystem's release notes. A target
without a benchmark is an aspiration.

Where a target cannot be met, that is recorded as a known limitation with its
measured value, not quietly dropped. A missed target that is documented is
information; a missed target that is forgotten is a defect waiting to surface at
Gate G2.

## Rationale

Declaring targets before implementation changes design decisions rather than
merely grading them. Knowing that options analytics must price a full chain in
under one second (plan section 12) rules out a per-strike round trip to the
database before that code is written, not after.

Benchmarks are required because measurement is the entire point. The alternative
considered — profiling only when something feels slow — was rejected because in a
system that runs unattended, "feels slow" is not a signal that reaches anyone.

Absolute thresholds are used rather than relative-to-previous-run comparisons,
because CI hardware varies enough that run-to-run comparison produces false alarms
and, worse, teaches the author to ignore them.

## Consequences

Every design document gains a performance-budget section, and every
implementation gains at least one benchmark. Benchmarks live in
`backend/tests/benchmarks/`, are marked `slow`, and are excluded from the default
test run so they do not slow the inner loop.

Some early budgets will be guesses. That is acceptable and expected: a guessed
budget that is measured and revised with a recorded reason is strictly better than
no budget, because the revision itself is evidence.

Budgets are revised only by amending the design document with the measured value
and the reason — never by silently lowering the number until it passes.
