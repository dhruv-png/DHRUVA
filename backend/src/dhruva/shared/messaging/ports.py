"""Transport ports (ADR-067).

The relay's exit is an :class:`EventPublisher`. Redis Streams is one adapter, an
in-memory recorder is another, and neither is privileged: a port whose only
implementation is Redis will be Redis-shaped, and the shape is only discovered
when a second adapter is written against it -- by which time callers depend on
the leak.

Nothing here imports a transport, and boundary rule R9 fails the build if domain
or strategy code imports one directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.shared.messaging.envelope import EventEnvelope

__all__ = ["AckToken", "Delivery", "EventPublisher", "EventStream", "PublishResult"]


class PublishResult(Protocol):
    """What a publisher reports back about one batch.

    Deliberately not a bare ``None``. The relay must know *which* envelopes were
    accepted, because a partially successful batch is the normal case when a
    broker degrades: marking the whole batch published would lose the remainder,
    and marking none of it would redeliver what already arrived.
    """

    @property
    def accepted(self) -> Sequence[EventEnvelope]:
        """Envelopes the transport accepted responsibility for."""
        ...

    @property
    def rejected(self) -> Sequence[tuple[EventEnvelope, Exception]]:
        """Envelopes the transport refused, each with the reason it gave."""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Hands envelopes to a transport.

    Implementations must be **idempotent under redelivery where the transport
    allows it**, and must never silently drop an envelope: an envelope that is
    neither accepted nor rejected is an envelope the relay will consider
    published while nothing holds it.

    Delivery is at-least-once (ADR-062). A publisher that raises after the
    transport has actually accepted a message causes a duplicate, which is
    correct and expected -- the alternative is silent loss, and losing an event
    about capital is strictly worse than repeating one.
    """

    async def publish(self, envelopes: Sequence[EventEnvelope]) -> PublishResult:
        """Publish a batch, reporting which envelopes were accepted.

        Must not raise for an ordinary transport failure: report the envelope as
        rejected with its reason instead, so the relay can schedule a retry
        rather than losing the whole batch to one bad message. Raising is
        reserved for a failure that makes the *publisher itself* unusable.
        """
        ...


#: What a consumer hands back to acknowledge a delivery.
#:
#: Deliberately opaque. It is whatever the adapter needs -- a Redis stream id, an
#: offset, a row number -- and a consumer that can *inspect* it can branch on it,
#: at which point the consumer is no longer portable between live delivery and
#: replay. ADR-067 forbids transport facts reaching a consumer, and typing this
#: as anything structured would be the leak arriving through the type system.
AckToken = Any


@dataclass(frozen=True, slots=True)
class Delivery:
    """One envelope, plus the opaque token that acknowledges it.

    A pair rather than a bare envelope, because acknowledgement is per-message
    and the consumer must be able to ack *this* one without knowing what a
    stream id is.
    """

    envelope: EventEnvelope
    ack_token: AckToken


@runtime_checkable
class EventStream(Protocol):
    """The consumer's entrance to the bus.

    Redis Streams and the replay engine are **peer** implementations (ADR-067).
    Neither is privileged, and a consumer written against this protocol cannot
    tell which one it has -- which is the whole of ADR-010's promise that one
    body of strategy code serves backtest, paper and live.

    Every implementation passes one shared conformance suite. Without it two
    adapters drift quietly, and the drift surfaces as a backtest that does not
    match production -- discovered after the strategy is trusted.
    """

    async def read(self, *, limit: int) -> Sequence[Delivery]:
        """Return up to ``limit`` deliveries, or an empty sequence when idle.

        Must not block indefinitely. A consumer decides its own polling cadence,
        and a stream that blocked would take that decision away -- and would make
        a replay's duration a function of the transport rather than of the data.
        """
        ...

    async def acknowledge(self, tokens: Sequence[AckToken]) -> int:
        """Confirm the deliveries identified by ``tokens``, returning how many.

        Called **after** the consumer's transaction commits, never before
        (ADR-065). Acknowledging first would drop the message on a crash between
        the two; acknowledging after risks a redelivery, which the ledger absorbs.
        """
        ...

    async def pending(self) -> int:
        """Return how many deliveries are read but not yet acknowledged.

        Part of the protocol rather than a Redis detail because it is the
        consumer-lag signal ADR-035 requires, and a replay adapter can answer it
        as truthfully as a broker can.
        """
        ...
