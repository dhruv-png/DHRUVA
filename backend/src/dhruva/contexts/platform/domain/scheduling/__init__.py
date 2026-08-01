"""Calendar-aware scheduling policy (ADR-066).

Pure domain. A job declares a session-relative moment; the calendar decides
whether today qualifies and what instant that is. Nothing here imports Celery,
a clock or a transport, so replacing the executor later is an adapter swap
rather than a rewrite of every schedule.
"""

from __future__ import annotations

from dhruva.contexts.platform.domain.scheduling.schedule import (
    MAX_OFFSET,
    SessionAnchor,
    TradingDaySchedule,
)

__all__ = ["MAX_OFFSET", "SessionAnchor", "TradingDaySchedule"]
