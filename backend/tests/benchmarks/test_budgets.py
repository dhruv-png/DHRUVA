"""Measured performance budgets for S02 (ADR-036).

Every target declared in ``docs/subsystems/S02-core-runtime.md`` section 3.5 is
measured here, and the measured values are recorded in the release notes.

Absolute thresholds rather than run-to-run comparison: CI hardware varies enough
that relative comparison produces false alarms, and false alarms teach the author
to ignore the suite.

Thresholds are set with headroom over the declared budget so the suite is a
regression detector, not a hardware-speed detector. A breach means something
became materially slower, not that the runner was busy.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import time

import pytest

from dhruva.shared.config import SecretValue
from dhruva.shared.config.settings import load_settings
from dhruva.shared.context import bind_correlation, current_correlation_id
from dhruva.shared.logging import configure_logging, get_logger
from dhruva.shared.logging.processors import make_redactor
from dhruva.shared.observability import CheckOutcome, HealthRegistry, HealthStatus, MetricsRegistry

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]

#: Declared budget, in milliseconds, per the S02 design document.
#: Measurement note (ADR-036). Millisecond-scale budgets are tail measures
#: (p95/p99), because the tail is what an operator experiences. Microsecond-scale
#: budgets are best-of-batched-means, because at that scale a per-iteration
#: timer costs a large fraction of the thing being timed and a per-iteration
#: tail reports the garbage collector rather than the code. This distinction was
#: introduced after the first measurement pass showed 5x run-to-run variance on
#: the microsecond budgets under the test harness.
BUDGET_SETTINGS_LOAD_MS = 50.0
BUDGET_LOG_EMIT_US = 100.0
BUDGET_CONTEXT_BIND_US = 10.0
BUDGET_METRICS_RENDER_MS = 100.0
BUDGET_READY_MS = 100.0
BUDGET_REDACTION_OVERHEAD_RATIO = 1.25


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(int(len(ordered) * fraction), len(ordered) - 1)
    return ordered[index]


def _time_repeatedly(operation: object, iterations: int) -> list[float]:
    assert callable(operation)
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        operation()
        samples.append(time.perf_counter() - started)
    return samples


def _time_in_batches(operation: object, *, batch: int, repeats: int) -> float:
    """Return the best per-call time over ``repeats`` batches of ``batch`` calls.

    The right estimator for sub-10-microsecond work. Calling
    :func:`time.perf_counter` around each iteration costs a meaningful fraction
    of the thing being measured, and a per-iteration p99 mostly reports garbage
    collection and scheduler pre-emption rather than the code under test.

    Taking the *minimum* batch mean is the standard estimator for "how fast can
    this go" and is robust to that noise. It is deliberately not a tail measure:
    for a primitive invoked inside a request, the tail that matters is the
    request's, and that is measured separately at the endpoint level.
    """
    assert callable(operation)
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        for _ in range(batch):
            operation()
        best = min(best, (time.perf_counter() - started) / batch)
    return best


def test_settings_load_is_within_budget() -> None:
    """Startup is on the critical path of the human-attended Market Open Ritual."""
    samples = _time_repeatedly(load_settings, 20)
    p95_ms = _percentile(samples, 0.95) * 1000

    assert p95_ms < BUDGET_SETTINGS_LOAD_MS, f"settings load p95 {p95_ms:.1f}ms"


def test_context_bind_is_within_budget() -> None:
    """Bound once per request and once per tick batch."""

    def bind() -> None:
        with bind_correlation(correlation_id="dhv-bench"):
            current_correlation_id()

    per_call_us = _time_in_batches(bind, batch=2_000, repeats=20) * 1_000_000

    assert per_call_us < BUDGET_CONTEXT_BIND_US, f"context bind {per_call_us:.2f}us/call"


def test_log_emission_is_within_budget() -> None:
    """S10 may log hundreds of events per second; logging must not be the bottleneck.

    Output is redirected to an in-memory buffer so the measurement covers the
    processor chain and the renderer -- which is what we control -- rather than
    the speed of whatever the standard error stream happens to be attached to.
    """
    configure_logging(
        level="INFO", log_format="json", service="bench", version="0", environment="test"
    )
    log = get_logger("bench")
    SecretValue("bench-secret-value-abcdef")

    def emit() -> None:
        log.info("tick batch ingested", instrument_count=2847, exchange="NSE", lag_ms=4.2)

    with contextlib.redirect_stderr(io.StringIO()):
        per_call_us = _time_in_batches(emit, batch=1_000, repeats=10) * 1_000_000

    assert per_call_us < BUDGET_LOG_EMIT_US, f"log emit {per_call_us:.1f}us/call"


def test_redaction_overhead_is_within_budget() -> None:
    """A control that costs 3x will eventually be switched off for a hot path."""
    SecretValue("overhead-bench-secret-1234")
    event = {
        "event": "tick batch ingested",
        "instrument_count": 2847,
        "exchange": "NSE",
        "detail": {"segment": "NFO", "tokens": [1, 2, 3]},
    }
    with_scan = make_redactor(scan_values=True)
    without_scan = make_redactor(scan_values=False)

    baseline = _time_in_batches(
        lambda: without_scan(None, "info", dict(event)), batch=2_000, repeats=10
    )
    scanned = _time_in_batches(
        lambda: with_scan(None, "info", dict(event)), batch=2_000, repeats=10
    )
    ratio = scanned / baseline if baseline else 1.0

    assert ratio < BUDGET_REDACTION_OVERHEAD_RATIO, f"redaction overhead {ratio:.2f}x"


def test_metrics_render_is_within_budget() -> None:
    """Prometheus scrapes every 15s by default; a slow endpoint causes gaps."""
    registry = MetricsRegistry()
    for index in range(500):
        registry.counter(f"dhruva_bench_series_{index}_total", "Bench.").inc()

    samples = _time_repeatedly(registry.render, 50)
    p95_ms = _percentile(samples, 0.95) * 1000

    assert p95_ms < BUDGET_METRICS_RENDER_MS, f"metrics render p95 {p95_ms:.1f}ms"


def test_readiness_evaluation_is_within_budget() -> None:
    """Must stay well inside a 1s probe timeout with dependencies attached."""

    async def check() -> CheckOutcome:
        return CheckOutcome("dep", HealthStatus.HEALTHY, "ok")

    registry = HealthRegistry()
    for name in ("postgres", "redis", "kite", "feed"):
        registry.register(name, check)

    samples = _time_repeatedly(lambda: asyncio.run(registry.evaluate()), 200)
    p99_ms = _percentile(samples, 0.99) * 1000

    assert p99_ms < BUDGET_READY_MS, f"readiness p99 {p99_ms:.1f}ms"
