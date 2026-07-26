# ADR-045 — Quantity is unsigned; direction belongs exclusively to Side

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Proposed in the S03 design document and approved at Design Review.

## Context

The common shortcut is to encode direction in the sign of a quantity: negative
means sell. It works, costs nothing, and is understood by everyone on the team —
until someone forgets, and a position acquires the wrong sign.

That failure is quiet. A wrongly-signed position reconciles cleanly against
nothing, produces a plausible P&L, and is typically discovered when a real
position does not match the platform's view of it.

## Decision

`Quantity` rejects negative values outright. Direction is carried by `Side`, an
explicit enum with `BUY` and `SELL`.

Where a signed magnitude genuinely is the right model — a position delta, a net
change across fills — `SignedQuantity` exists and says so in its name. It is a
**distinct type**, not a mode of `Quantity`, so a function receiving one never has
to establish which it is holding.

`Side.sign` is the single sanctioned place where direction becomes a number.
Every other conversion goes through it rather than reimplementing the mapping.

Subtraction that would produce a negative quantity raises rather than clamping at
zero. Constructors are named for what they count: `shares()`, `contracts()`, and
`lots()` — the last requiring an explicit `lot_size`, because "3 lots" and "3
contracts" differ by a factor of 75 for NIFTY and the gap is a position error, not
a rounding one.

## Rationale

Clamping subtraction at zero was considered and rejected. Subtracting more than
is held is a logic error upstream, and returning zero would hide it at exactly the
moment it matters most.

Requiring `lot_size` with no default is the same reasoning applied to metadata: a
default would be a guess about an instrument this layer does not know, and a wrong
guess produces an order the exchange rejects at best.

## Consequences

Every order-construction site must state a side explicitly. That verbosity is the
decision working.

`Quantity` supports no `__neg__`. Negating a size is meaningless; reversing a
*direction* is `Side.opposite`.
