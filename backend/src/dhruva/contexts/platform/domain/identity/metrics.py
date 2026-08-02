"""Bounded-cardinality identity security metrics (ADR-035, ADR-037)."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "AuthenticationOperation",
    "AuthorisationOperation",
    "IdentityMetrics",
    "SecurityOutcome",
]


class AuthenticationOperation(StrEnum):
    """The finite authentication operations exposed to metrics."""

    LOGIN = "login"
    REFRESH = "refresh"


class AuthorisationOperation(StrEnum):
    """The finite role-authority mutations exposed to metrics."""

    GRANT = "grant"
    REVOKE = "revoke"


class SecurityOutcome(StrEnum):
    """The only outcome labels: expected refusal is not infrastructure error."""

    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    ERROR = "error"


@runtime_checkable
class IdentityMetrics(Protocol):
    """Records security outcomes without accepting identity or secret material."""

    def authentication(
        self,
        operation: AuthenticationOperation,
        outcome: SecurityOutcome,
    ) -> None:
        """Count one login or refresh outcome."""
        ...

    def authorisation(
        self,
        operation: AuthorisationOperation,
        outcome: SecurityOutcome,
    ) -> None:
        """Count one permission grant or revoke outcome."""
        ...
