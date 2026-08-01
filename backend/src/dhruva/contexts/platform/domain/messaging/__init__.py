"""Delivery policy. Pure domain logic, no transport and no I/O (ADR-064)."""

from __future__ import annotations

from dhruva.contexts.platform.domain.messaging.dead_letters import (
    RequeueRefusal,
    RequeueVerdict,
    may_requeue,
)
from dhruva.contexts.platform.domain.messaging.delivery import (
    MAX_DELIVERY_ATTEMPTS,
    Disposition,
    RetryPolicy,
    classify,
)

__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "Disposition",
    "RequeueRefusal",
    "RequeueVerdict",
    "RetryPolicy",
    "classify",
    "may_requeue",
]
