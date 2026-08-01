"""Transport-agnostic messaging vocabulary (ADR-061, ADR-067).

The envelope and its codecs live in the shared kernel because both producers and
consumers need them and neither owns them. Nothing here knows what a transport
is: Redis Streams and the replay engine are peer adapters built on top (ADR-067),
and boundary rule R9 keeps strategy and domain code away from both.
"""

from __future__ import annotations

from dhruva.shared.messaging.codecs import (
    UnencodableValueError,
    decode_value,
    encode_value,
)
from dhruva.shared.messaging.envelope import (
    INITIAL_EVENT_VERSION,
    EventEnvelope,
    UnsupportedEventVersionError,
    canonical_json,
)
from dhruva.shared.messaging.ports import (
    AckToken,
    Delivery,
    EventPublisher,
    EventStream,
    PublishResult,
)

__all__ = [
    "INITIAL_EVENT_VERSION",
    "AckToken",
    "Delivery",
    "EventEnvelope",
    "EventPublisher",
    "EventStream",
    "PublishResult",
    "UnencodableValueError",
    "UnsupportedEventVersionError",
    "canonical_json",
    "decode_value",
    "encode_value",
]
