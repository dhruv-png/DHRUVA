"""Messaging adapters. Transport-specific code lives here and nowhere else.

Boundary rule R9 (ADR-068) fails the build if domain or strategy code imports
anything from this package: consumers reach a bus through the ports in
:mod:`dhruva.shared.messaging`, so the same code runs against live delivery and
against replay (ADR-067).
"""

from __future__ import annotations

from dhruva.contexts.platform.infrastructure.messaging.dead_letters import (
    DeadLetter,
    DeadLetterStore,
    RequeueReport,
)
from dhruva.contexts.platform.infrastructure.messaging.in_memory import (
    InMemoryAck,
    InMemoryBus,
    InMemoryPublisher,
    RecordedResult,
)
from dhruva.contexts.platform.infrastructure.messaging.redis_streams import (
    DEFAULT_GROUP_NAMESPACE,
    DEFAULT_MAXLEN,
    DEFAULT_NAMESPACE,
    ENVELOPE_FIELD,
    RedisAck,
    RedisEventStream,
    RedisPublishResult,
    RedisStreamPublisher,
    stream_key,
)
from dhruva.contexts.platform.infrastructure.messaging.relay import (
    DEFAULT_LEASE,
    OutboxRelay,
    RelayPass,
)
from dhruva.contexts.platform.infrastructure.messaging.replay import (
    OutboxReplayStream,
    ReplayAck,
)

__all__ = [
    "DEFAULT_GROUP_NAMESPACE",
    "DEFAULT_LEASE",
    "DEFAULT_MAXLEN",
    "DEFAULT_NAMESPACE",
    "ENVELOPE_FIELD",
    "DeadLetter",
    "DeadLetterStore",
    "InMemoryAck",
    "InMemoryBus",
    "InMemoryPublisher",
    "OutboxRelay",
    "OutboxReplayStream",
    "RecordedResult",
    "RedisAck",
    "RedisEventStream",
    "RedisPublishResult",
    "RedisStreamPublisher",
    "RelayPass",
    "ReplayAck",
    "RequeueReport",
    "stream_key",
]
