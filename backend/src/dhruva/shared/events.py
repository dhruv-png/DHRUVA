"""Domain events.

A domain event is **a fact that occurred**. Past tense, immutable, and carrying
only what is needed to understand what happened.

What a domain event is not
--------------------------
It is not a command (``PlaceOrder``), not a workflow step, and not a data
transfer object. ``OrderFilled`` is a fact; ``FillOrder`` is an instruction, and
instructions belong in the application layer.

The distinction matters because facts can be replayed and instructions cannot. An
append-only ledger of facts (ADR-014) reconstructs state at any point; a log of
instructions only tells you what someone intended.

Envelope versus fact
--------------------
This class carries the *fact*. Transport metadata -- correlation identifiers,
delivery attempts, consumer offsets -- belongs to the event envelope that S05's
bus wraps around it (ADR-002). Keeping them apart means a fact means the same
thing whether it arrived over Redis Streams, was replayed from the ledger, or was
constructed in a test.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from dhruva.shared.invariants import invariant

__all__ = ["DomainEvent"]


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base class for facts the domain records.

    Subclasses add the fields describing their particular fact and nothing else::

        @dataclass(frozen=True, slots=True, kw_only=True)
        class RegimeChanged(DomainEvent):
            instrument_id: InstrumentId
            previous: RegimeLabel
            current: RegimeLabel

    Attributes
    ----------
    event_id
        Unique per occurrence. Generated if not supplied, so a consumer can
        deduplicate without the producer having to think about it.
    occurred_at
        When the fact became true in the world -- **not** when it was recorded.
        The two differ whenever data arrives late, which is most of the time in
        market data, and conflating them is how lookahead bias enters a backtest
        (ADR-007).

    Notes
    -----
    ``kw_only`` is deliberate. Events accumulate fields over years, and positional
    construction turns every addition into a breaking change at every call site.

    ``event_type`` is derived from the class name rather than stored, so it cannot
    drift from the class it names.
    """

    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime

    def __post_init__(self) -> None:
        """Reject a naive ``occurred_at``.

        An event whose timestamp has no timezone has no defined position in the
        sequence of events, which defeats the purpose of recording it (ADR-006).
        """
        invariant(
            self.occurred_at.tzinfo is not None and self.occurred_at.utcoffset() is not None,
            "event occurred_at must be timezone-aware",
            event_type=self.event_type,
            value=self.occurred_at.isoformat(),
        )

    @property
    def event_type(self) -> str:
        """Return the routing name of this event, derived from the class name.

        Derived rather than declared so it cannot disagree with the class. A
        stored ``event_type`` that says ``OrderFilled`` on an ``OrderRejected``
        instance is the kind of defect that survives review.
        """
        return type(self).__name__
