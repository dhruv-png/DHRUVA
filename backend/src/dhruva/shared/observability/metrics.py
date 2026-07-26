"""Metrics registry and exposition.

Wraps :mod:`prometheus_client` rather than exposing it directly, for two reasons
that both matter more than the small indirection costs.

First, **naming**. Plan section 13.2 fixes the convention
``dhruva_<context>_<name>_<unit>``. A wrapper can enforce it; a convention cannot.
A metric named inconsistently is a metric nobody finds when they need it.

Second, **isolation**. ``prometheus_client`` defaults to a process-global
registry, which makes tests order-dependent and makes a duplicate registration
an unrecoverable error. Owning the registry means a test can create its own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

__all__ = ["METRIC_NAME_PATTERN", "PROMETHEUS_CONTENT_TYPE", "MetricsRegistry"]

#: ``dhruva_<context>_<name>_<unit>``: lowercase, underscore-separated, at least
#: three segments after the prefix is stripped.
METRIC_NAME_PATTERN: Final = re.compile(r"^dhruva_[a-z0-9]+(?:_[a-z0-9]+)+$")

#: Content type for the ``/metrics`` response.
PROMETHEUS_CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

#: Latency buckets in seconds, chosen against the platform's budgets (plan
#: section 12): risk authorisation p99 < 100 ms, API reads p95 < 300 ms. Default
#: prometheus buckets waste resolution below 5 ms and above 1 s, where this
#: platform has no decisions to make.
DEFAULT_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)


class MetricsRegistry:
    """A process's metrics, with naming enforced at registration.

    Instantiated once at the composition root and passed to components. Not a
    module-level singleton, for the same reason as
    :class:`~dhruva.shared.observability.health.HealthRegistry`: an ambient
    global cannot be isolated in tests.
    """

    def __init__(self) -> None:
        """Create an empty registry owning its own prometheus collector."""
        self._registry = CollectorRegistry()
        self._names: set[str] = set()

    @property
    def registry(self) -> CollectorRegistry:
        """Return the underlying collector registry."""
        return self._registry

    @property
    def registered(self) -> tuple[str, ...]:
        """Return every registered metric name, sorted."""
        return tuple(sorted(self._names))

    def _validate(self, name: str) -> str:
        """Enforce the naming convention and reject duplicates.

        Raises
        ------
        ValueError
            If the name breaks the convention or is already registered.
            Registering the same name twice is a programming error that
            prometheus_client would otherwise report far from its cause.
        """
        if not METRIC_NAME_PATTERN.match(name):
            msg = (
                f"metric name {name!r} must match 'dhruva_<context>_<name>_<unit>', "
                f"lowercase and underscore-separated (plan section 13.2)"
            )
            raise ValueError(msg)
        if name in self._names:
            msg = f"metric {name!r} is already registered"
            raise ValueError(msg)
        self._names.add(name)
        return name

    def counter(self, name: str, documentation: str, labels: Sequence[str] = ()) -> Counter:
        """Register a monotonically increasing counter."""
        return Counter(self._validate(name), documentation, list(labels), registry=self._registry)

    def gauge(self, name: str, documentation: str, labels: Sequence[str] = ()) -> Gauge:
        """Register a gauge, for values that go up and down."""
        return Gauge(self._validate(name), documentation, list(labels), registry=self._registry)

    def histogram(
        self,
        name: str,
        documentation: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_LATENCY_BUCKETS,
    ) -> Histogram:
        """Register a histogram, for latency and size distributions.

        Notes
        -----
        Buckets default to :data:`DEFAULT_LATENCY_BUCKETS`, which are tuned to
        this platform's stated budgets rather than to the prometheus defaults.
        Percentiles are only as good as the buckets around them, and the defaults
        put no boundary near 100 ms -- the threshold the risk engine is judged on.
        """
        return Histogram(
            self._validate(name),
            documentation,
            list(labels),
            registry=self._registry,
            buckets=tuple(buckets),
        )

    def render(self) -> bytes:
        """Render the registry in Prometheus exposition format."""
        return generate_latest(self._registry)
