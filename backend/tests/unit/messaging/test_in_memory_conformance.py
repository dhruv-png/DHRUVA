"""The in-memory bus passes the transport conformance suite (ADR-067).

Runs the shared suite with no broker, so a violation of the port's contract is
caught in the inner loop rather than only in a container.

This is also what makes the suite worth anything. A conformance suite with one
implementation is a description of that implementation; with two it becomes a
contract, and the second one existing *before* the replay engine is written is
what stops the port quietly taking Redis's shape (ADR-067).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from dhruva.contexts.platform.infrastructure.messaging import InMemoryBus
from tests.transport_conformance import Bus, TransportConformance

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class TestInMemoryTransportConformance(TransportConformance):
    """Every assertion inherited, none overridden."""

    @pytest_asyncio.fixture
    async def bus(self) -> Bus:
        """One loopback acting as both ends, fresh for each test."""
        loopback = InMemoryBus()
        return Bus(publisher=loopback, stream=loopback)
