# ADR-046 — TradingDay cannot be constructed without a calendar

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Proposed in the S03 design document and approved at Design Review.

## Context

NSE moved equity-derivative expiry from Thursday to Tuesday on 2025-09-01, and
BSE moved to Thursday. Any backtest spanning that date which computes "the next
expiry" by calendar arithmetic is wrong, and nothing in the data will flag it —
the results simply differ from reality in a way that looks like alpha.

More generally: a `TradingDay` that cannot verify it is a trading day is a lie
the type tells. A Sunday wearing that type propagates silently into every
downstream calculation.

## Decision

`TradingDay` is constructed through `TradingDay.of(date, calendar)`, which raises
if the date is not a session on that calendar.

`TradingDay + timedelta` does not exist. All session arithmetic goes through the
calendar: `next_session`, `previous_session`, `add_sessions`, `sessions_between`.

The `TradingCalendar` **port** lives in the shared kernel; the implementation
lives in the Reference context (S08). Everything may depend on the *idea* of a
calendar; only S08 knows the holidays.

Sessions are stored as timezone-aware **UTC instants**, not wall-clock times, and
carry a `SessionKind` distinguishing regular from Muhurat and exchange-declared
special sessions.

## Rationale

Storing UTC rather than local wall-clock time costs nothing today, because India
observes no daylight saving. It matters because storing wall-clock times would
bake that assumption into the schema, and an exchange that *does* observe DST
would require every stored session to be reinterpreted. UTC instants have one
meaning regardless.

`SessionKind` rather than inferring partial-ness from duration: a session is
partial because the exchange declared it so, not because it happens to be short,
and a regular session cut short by a halt is still a regular session.

## Consequences

Every subsystem needing session information takes a calendar dependency. That is
more wiring, and it is what makes the answer correct on both sides of
2025-09-01.

S08 must implement the port before any subsystem can construct a real
`TradingDay`. Until then, test calendars stand in — which is also how the port
gets exercised independently of the holidays.
