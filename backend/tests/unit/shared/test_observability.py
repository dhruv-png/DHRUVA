"""Health, readiness and metrics behave the way ADR-035 requires."""

from __future__ import annotations

import asyncio
import time

import pytest

from dhruva.shared.observability import (
    CheckOutcome,
    HealthRegistry,
    HealthStatus,
    MetricsRegistry,
    get_tracer,
    tracing_is_active,
)
from dhruva.shared.observability.metrics import DEFAULT_LATENCY_BUCKETS


async def _healthy() -> CheckOutcome:
    return CheckOutcome("postgres", HealthStatus.HEALTHY, "connected")


async def _degraded() -> CheckOutcome:
    return CheckOutcome("redis", HealthStatus.DEGRADED, "high latency")


async def _unhealthy() -> CheckOutcome:
    return CheckOutcome("kite", HealthStatus.UNHEALTHY, "session expired")


async def _raises() -> CheckOutcome:
    msg = "connection refused to postgres://user:hunter2@db:5432"
    raise ConnectionError(msg)


async def _hangs() -> CheckOutcome:
    await asyncio.sleep(10)
    return CheckOutcome("slow", HealthStatus.HEALTHY)


@pytest.mark.unit
def test_an_empty_registry_is_ready() -> None:
    """A process with no declared dependencies can serve immediately."""
    report = asyncio.run(HealthRegistry().evaluate())

    assert report.status is HealthStatus.HEALTHY
    assert report.is_ready


@pytest.mark.unit
def test_degraded_is_still_ready() -> None:
    """Degraded is a warning, not a refusal.

    Taking a process out of rotation because a dependency is slow converts a
    degradation into an outage.
    """
    registry = HealthRegistry()
    registry.register("redis", _degraded)

    report = asyncio.run(registry.evaluate())

    assert report.status is HealthStatus.DEGRADED
    assert report.is_ready


@pytest.mark.unit
def test_any_unhealthy_dependency_makes_the_process_not_ready() -> None:
    """The aggregate is the worst outcome, not an average."""
    registry = HealthRegistry()
    registry.register("postgres", _healthy)
    registry.register("kite", _unhealthy)

    report = asyncio.run(registry.evaluate())

    assert report.status is HealthStatus.UNHEALTHY
    assert not report.is_ready


@pytest.mark.unit
def test_a_check_that_raises_counts_as_unhealthy() -> None:
    """ADR-022: unknown fails closed. A check that cannot answer is not fine."""
    registry = HealthRegistry()
    registry.register("postgres", _raises)

    report = asyncio.run(registry.evaluate())

    assert report.status is HealthStatus.UNHEALTHY
    assert not report.is_ready


@pytest.mark.unit
def test_a_failing_check_does_not_leak_its_exception_message() -> None:
    """FR-20. ``/ready`` is served over HTTP and messages carry connection strings."""
    registry = HealthRegistry()
    registry.register("postgres", _raises)

    report = asyncio.run(registry.evaluate())

    detail = report.checks[0].detail
    assert "hunter2" not in detail
    assert "ConnectionError" in detail


@pytest.mark.unit
def test_a_hanging_check_times_out_rather_than_hanging_the_probe() -> None:
    """A hanging check times out rather than hanging the readiness probe.

    A check that never returns would otherwise hang readiness itself, which is
    the exact failure readiness exists to prevent.
    """
    registry = HealthRegistry(timeout_seconds=0.05)
    registry.register("slow", _hangs)

    report = asyncio.run(registry.evaluate())

    assert report.status is HealthStatus.UNHEALTHY
    assert "did not answer" in report.checks[0].detail


@pytest.mark.unit
def test_checks_run_concurrently() -> None:
    """Total latency must be the slowest check, not the sum.

    Otherwise the endpoint drifts out of its p95 budget as dependencies are added.
    """

    async def slow() -> CheckOutcome:
        await asyncio.sleep(0.05)
        return CheckOutcome("x", HealthStatus.HEALTHY)

    registry = HealthRegistry()
    for name in ("a", "b", "c", "d"):
        registry.register(name, slow)

    started = time.perf_counter()
    asyncio.run(registry.evaluate())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15, "checks appear to be running sequentially"


@pytest.mark.unit
def test_duplicate_registration_is_rejected() -> None:
    """Silently replacing a check means a dependency stops being verified."""
    registry = HealthRegistry()
    registry.register("postgres", _healthy)

    with pytest.raises(ValueError, match="already registered"):
        registry.register("postgres", _healthy)


@pytest.mark.unit
def test_the_report_renders_a_per_dependency_breakdown() -> None:
    """An operator needs to know *which* dependency is the problem."""
    registry = HealthRegistry()
    registry.register("postgres", _healthy)
    registry.register("kite", _unhealthy)

    report = asyncio.run(registry.evaluate())
    payload = report.to_dict()

    assert payload["ready"] is False
    assert {check.name for check in report.checks} == {"postgres", "kite"}
    assert len(payload["checks"]) == 2  # type: ignore[arg-type]  # to_dict returns object values


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["ticks_total", "dhruva_ticks", "DHRUVA_MARKETDATA_TICKS_TOTAL", "dhruva-marketdata-ticks"],
)
def test_metric_names_must_follow_the_convention(name: str) -> None:
    """A convention a tool cannot enforce is a convention that decays."""
    with pytest.raises(ValueError, match="dhruva_<context>_<name>_<unit>"):
        MetricsRegistry().counter(name, "doc")


@pytest.mark.unit
def test_a_conforming_metric_registers_and_renders() -> None:
    """The happy path, end to end through exposition."""
    registry = MetricsRegistry()
    counter = registry.counter("dhruva_marketdata_ticks_total", "Ticks ingested.")
    counter.inc(5)

    rendered = registry.render().decode()

    assert "dhruva_marketdata_ticks_total" in rendered
    assert registry.registered == ("dhruva_marketdata_ticks_total",)
    assert registry.registry is not None, "the collector must be reachable for S41 wiring"


@pytest.mark.unit
def test_duplicate_metric_registration_is_rejected() -> None:
    """prometheus_client would otherwise report this far from its cause."""
    registry = MetricsRegistry()
    registry.gauge("dhruva_platform_queue_depth", "Depth.")

    with pytest.raises(ValueError, match="already registered"):
        registry.gauge("dhruva_platform_queue_depth", "Depth.")


@pytest.mark.unit
def test_registries_are_isolated_from_each_other() -> None:
    """Owning the registry is what makes tests order-independent."""
    first, second = MetricsRegistry(), MetricsRegistry()
    first.counter("dhruva_platform_events_total", "Events.")

    second.counter("dhruva_platform_events_total", "Events.")

    assert second.registered == ("dhruva_platform_events_total",)


@pytest.mark.unit
def test_histogram_buckets_bracket_the_platform_budgets() -> None:
    """Percentiles are only as good as the buckets around them.

    Plan section 12 judges risk authorisation at 100 ms; the default prometheus
    buckets put no boundary there.
    """
    assert 0.1 in DEFAULT_LATENCY_BUCKETS
    assert min(b for b in DEFAULT_LATENCY_BUCKETS if b >= 0.25) < 0.3


@pytest.mark.unit
def test_tracing_is_a_no_op_until_a_composition_root_configures_it() -> None:
    """Library code and tests must pay nothing for tracing they did not ask for."""
    assert not tracing_is_active()

    with get_tracer("test").start_as_current_span("probe") as span:
        assert span is not None
