"""Prometheus identity security metrics with closed label vocabularies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from dhruva.contexts.platform.domain.identity.metrics import (
    AuthenticationOperation,
    AuthorisationOperation,
    SecurityOutcome,
)

if TYPE_CHECKING:
    from prometheus_client import Counter

    from dhruva.shared.observability import MetricsRegistry

__all__ = [
    "AUTHENTICATION_METRIC",
    "AUTHORISATION_METRIC",
    "PrometheusIdentityMetrics",
]

AUTHENTICATION_METRIC: Final = "dhruva_platform_authentication_attempts_total"
AUTHORISATION_METRIC: Final = "dhruva_platform_authorisation_mutations_total"


class PrometheusIdentityMetrics:
    """Two counters whose labels cannot carry caller-controlled values."""

    __slots__ = ("_authentication", "_authorisation")

    def __init__(self, registry: MetricsRegistry) -> None:
        """Register the S06 counters in the process-owned metrics registry."""
        self._authentication: Counter = registry.counter(
            AUTHENTICATION_METRIC,
            "Authentication attempts by operation and bounded outcome.",
            labels=("operation", "outcome"),
        )
        self._authorisation: Counter = registry.counter(
            AUTHORISATION_METRIC,
            "Permission mutations by operation and bounded outcome.",
            labels=("operation", "outcome"),
        )

    def authentication(
        self,
        operation: AuthenticationOperation,
        outcome: SecurityOutcome,
    ) -> None:
        """Count one login or refresh without accepting a subject or credential."""
        self._authentication.labels(operation=operation.value, outcome=outcome.value).inc()

    def authorisation(
        self,
        operation: AuthorisationOperation,
        outcome: SecurityOutcome,
    ) -> None:
        """Count one grant or revoke without accepting actor, tenant, role or permission."""
        self._authorisation.labels(operation=operation.value, outcome=outcome.value).inc()
