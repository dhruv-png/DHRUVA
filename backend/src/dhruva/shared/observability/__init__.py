"""Observability: tracing, health, readiness and metrics.

ADR-035 makes these first-class rather than a later enhancement. Every runtime
component ships all six obligations at the moment it is introduced.

Tracing uses the **OpenTelemetry API** in library code and configures the **SDK
only at composition roots** (ADR-040). The API is a no-op facade when no SDK is
present, so tests and library consumers pay nothing, and there is no reason to
wrap a facade in a second facade of our own.
"""

from __future__ import annotations

from opentelemetry import trace

from dhruva.shared.observability.health import (
    CheckOutcome,
    HealthRegistry,
    HealthStatus,
    ReadinessReport,
)
from dhruva.shared.observability.metrics import METRIC_NAME_PATTERN, MetricsRegistry

__all__ = [
    "METRIC_NAME_PATTERN",
    "CheckOutcome",
    "HealthRegistry",
    "HealthStatus",
    "MetricsRegistry",
    "ReadinessReport",
    "get_tracer",
    "tracing_is_active",
]


def get_tracer(name: str) -> trace.Tracer:
    """Return a tracer for ``name``.

    Returns a no-op tracer unless a composition root has configured an SDK, which
    is the correct default for library code and for tests.
    """
    return trace.get_tracer(name)


def tracing_is_active() -> bool:
    """Return whether a real tracer provider is installed.

    A no-op tracer is otherwise indistinguishable from a broken one: spans simply
    vanish. The startup banner reports this value so that misconfiguration
    presents as a visible fact rather than as an absence of data (FR-19).
    """
    provider = trace.get_tracer_provider()
    return type(provider).__name__ not in {"NoOpTracerProvider", "ProxyTracerProvider"}
