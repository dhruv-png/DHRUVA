"""Shared kernel.

The vocabulary every bounded context speaks. Populated by S02 (Core Runtime) and
S03 (Domain Primitives).

Rules
-----
* ``shared`` imports nothing from ``dhruva.contexts``. Ever. Enforced by boundary
  rule R4 and by an import-linter contract.
* A type belongs here only if at least three contexts need it. Anything less is
  premature generalisation and belongs in the context that owns it.
* :mod:`dhruva.shared.config` is the only module permitted to read the
  environment (ADR-031, rule R5).
* No ``float`` appears under :mod:`dhruva.shared.money` (ADR-048, rule R6).

Contents
--------
:mod:`dhruva.shared.money`
    ``Money``, ``Price``, ``Quantity``, ``Ratio`` and the rounding policies. See
    ``docs/DOMAIN.md`` for why they are shaped this way.
:mod:`dhruva.shared.time`
    ``Clock``, ``TradingDay``, the ``TradingCalendar`` port, and half-open ranges.
:mod:`dhruva.shared.identity`
    Surrogate identifiers, independent of any broker.
:mod:`dhruva.shared.events`
    The ``DomainEvent`` base -- facts, not commands.
:mod:`dhruva.shared.errors`
    The closed error taxonomy.
:mod:`dhruva.shared.invariants`
    ``invariant()``, the guard that ``python -O`` cannot strip.
"""

from __future__ import annotations

from dhruva.shared.config import Environment, SecretValue
from dhruva.shared.context import (
    CorrelationContext,
    bind_correlation,
    copy_context_into,
    current_context,
    current_correlation_id,
    new_correlation_id,
)
from dhruva.shared.errors import DhruvaError, ErrorCode, InvariantViolation
from dhruva.shared.events import DomainEvent
from dhruva.shared.identity import AccountId, InstrumentId, SurrogateId
from dhruva.shared.invariants import invariant, unreachable
from dhruva.shared.money import Currency, Money, Price, Quantity, Ratio, Side, SignedQuantity
from dhruva.shared.time import (
    Clock,
    DateRange,
    FrozenClock,
    SessionKind,
    SystemClock,
    TimeRange,
    TradingCalendar,
    TradingDay,
    TradingSession,
)

__all__ = [
    "AccountId",
    "Clock",
    "CorrelationContext",
    "Currency",
    "DateRange",
    "DhruvaError",
    "DomainEvent",
    "Environment",
    "ErrorCode",
    "FrozenClock",
    "InstrumentId",
    "InvariantViolation",
    "Money",
    "Price",
    "Quantity",
    "Ratio",
    "SecretValue",
    "SessionKind",
    "Side",
    "SignedQuantity",
    "SurrogateId",
    "SystemClock",
    "TimeRange",
    "TradingCalendar",
    "TradingDay",
    "TradingSession",
    "bind_correlation",
    "copy_context_into",
    "current_context",
    "current_correlation_id",
    "invariant",
    "new_correlation_id",
    "unreachable",
]
