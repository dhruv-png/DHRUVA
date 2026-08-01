"""Redis Streams passes the same transport conformance suite (ADR-067).

The same assertions as the in-memory run, against a real broker. Two adapters
passing one unchanged suite is the whole of what "live and replay are peers"
means operationally, and it is the only thing standing between this project and
a backtest that quietly stops matching production.

The ``bus`` fixture does the adapter-specific setup -- a key namespace, a
consumer group -- so that nothing in the shared suite has to know Redis exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from dhruva.contexts.platform.infrastructure.messaging import (
    RedisEventStream,
    RedisStreamPublisher,
)
from tests.transport_conformance import AGGREGATE_TYPE, Bus, TransportConformance

if TYPE_CHECKING:
    from redis.asyncio import Redis

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


class TestRedisTransportConformance(TransportConformance):
    """Every assertion inherited, none overridden.

    An override here would be this adapter declaring it is not a peer, which is
    exactly the drift ADR-067 exists to prevent. If one of the inherited
    assertions could not hold against Redis, that would be an architectural
    finding about the port -- not a licence to narrow the contract for one
    transport.
    """

    @pytest_asyncio.fixture(loop_scope="session")
    async def bus(self, redis_client: Redis, namespace: str) -> Bus:
        """Wire a publisher and a stream to a namespace nobody else is using."""
        stream = RedisEventStream(
            redis_client,
            aggregate_types=(AGGREGATE_TYPE,),
            group="conformance",
            consumer="worker-1",
            namespace=namespace,
        )
        await stream.ensure_group()
        return Bus(
            publisher=RedisStreamPublisher(redis_client, namespace=namespace, maxlen=1000),
            stream=stream,
        )
