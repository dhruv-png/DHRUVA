"""Shared kernel.

Types and capabilities that genuinely belong to every context. Populated by S02
(Core Runtime) and S03 (Domain Primitives).

Rules
-----
* ``shared`` imports nothing from ``dhruva.contexts``. Ever. Enforced by boundary
  rule R4 and by an import-linter contract.
* A type belongs here only if at least three contexts need it. Anything less is
  premature generalisation and belongs in the context that owns it.
* :mod:`dhruva.shared.config` is the only module permitted to read the
  environment (ADR-031, boundary rule R5).

Available now
-------------
:mod:`dhruva.shared.errors`
    The closed error taxonomy. Every error carries a stable code and structured
    context.
:mod:`dhruva.shared.config`
    :class:`~dhruva.shared.config.environment.Environment` and
    :class:`~dhruva.shared.config.secret.SecretValue`.
:mod:`dhruva.shared.context`
    Correlation, causation and account identifiers, propagated via contextvars.
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
from dhruva.shared.errors import DhruvaError, ErrorCode

__all__ = [
    "CorrelationContext",
    "DhruvaError",
    "Environment",
    "ErrorCode",
    "SecretValue",
    "bind_correlation",
    "copy_context_into",
    "current_context",
    "current_correlation_id",
    "new_correlation_id",
]
