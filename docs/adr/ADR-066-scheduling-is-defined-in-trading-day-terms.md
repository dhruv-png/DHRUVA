# ADR-066 — Scheduled work is defined in trading-day terms, not cron

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

"Run at market open" expressed as `0 9 * * 1-5` is wrong on every exchange
holiday, wrong on the occasional Saturday session, and was wrong about expiry
conventions the day they changed on 2025-09-01.

## Decision

Beat schedules resolve through the S03 `TradingCalendar` port. A job declares a
**session-relative moment** -- `market_open + 5min`, `market_close - 15min`,
`session_end + 1h` -- and the calendar decides whether today qualifies and what
wall-clock time that is.

Cron expressions remain available only for work genuinely unrelated to trading
sessions, such as log rotation.

## Rationale

This is ADR-011 applied to scheduling. A calendar-resolved schedule is testable:
a frozen clock plus a stub calendar asserts that a job does not run on Diwali,
without waiting for Diwali.

A cron expression encodes a belief about the trading week into a string that no
test will ever contradict. Every such string is a latent defect that fires on a
holiday, which is precisely when nobody is watching.

## Consequences

Schedule resolution is domain logic and lives outside Celery, so replacing Celery
later is an adapter swap rather than a rewrite of every schedule.

Errors crossing the Celery boundary must remain picklable, as the plan already
requires of the ADR-038 taxonomy; a test asserts it.

A job whose session-relative moment does not occur today simply does not run.
That is the intended behaviour and must not be mistaken for a failed schedule.
