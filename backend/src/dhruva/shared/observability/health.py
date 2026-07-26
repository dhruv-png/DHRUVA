"""Health and readiness.

The distinction between the two is load-bearing and is not a formality (ADR-035).

**Health** answers "is this process running?" It performs no dependency checks
and must never fail because a downstream is unavailable. A health endpoint that
consults the database turns a slow database into a restart loop, which turns a
degraded system into an outage.

**Readiness** answers "can this process do its job right now?" It checks declared
dependencies and reports a per-dependency breakdown. Readiness is where
fail-closed lives (ADR-022): a check that cannot be evaluated counts as **not
ready**, never as ready.

Later subsystems register their own checks here rather than editing the API
component -- S10 registers feed freshness, S23 registers broker session validity,
S24 registers reconciliation currency.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

__all__ = [
    "CheckOutcome",
    "HealthRegistry",
    "HealthStatus",
    "ReadinessReport",
]

#: A check that has not answered within this budget is treated as failed. The
#: readiness endpoint must stay well inside a typical one-second probe timeout
#: even when a dependency is hanging (performance budget, S02 design section 3.5).
DEFAULT_CHECK_TIMEOUT_SECONDS: Final = 2.0


class HealthStatus(StrEnum):
    """The outcome of a single check, or of the aggregate.

    Attributes
    ----------
    HEALTHY
        The dependency answered and is usable.
    DEGRADED
        The dependency answered but is impaired. The process is still ready:
        degraded is a warning, not a refusal.
    UNHEALTHY
        The dependency is unusable, or could not be evaluated. ADR-022 collapses
        "broken" and "unknown" into the same outcome deliberately -- a check that
        cannot answer must not be assumed to be fine.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"

    @property
    def is_ready(self) -> bool:
        """Return whether this outcome permits serving traffic."""
        return self is not HealthStatus.UNHEALTHY


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """The result of evaluating one dependency check.

    Attributes
    ----------
    name
        Dependency name, for example ``postgres`` or ``kite_session``.
    status
        The outcome.
    detail
        Human-readable explanation. Must never contain a credential -- it is
        served over HTTP by ``/ready`` (FR-20).
    duration_ms
        How long the check took, so a slow dependency is visible before it fails.
    """

    name: str
    status: HealthStatus
    detail: str = ""
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Aggregate readiness across every registered check.

    Attributes
    ----------
    status
        The worst outcome among the checks. ``HEALTHY`` when there are none.
    checks
        Per-dependency breakdown, so an operator sees *which* dependency is the
        problem rather than only that something is.
    """

    status: HealthStatus
    checks: tuple[CheckOutcome, ...] = field(default_factory=tuple)

    @property
    def is_ready(self) -> bool:
        """Return whether the process should receive traffic."""
        return self.status.is_ready

    def to_dict(self) -> dict[str, object]:
        """Render for the ``/ready`` response body."""
        return {
            "status": str(self.status),
            "ready": self.is_ready,
            "checks": [
                {
                    "name": check.name,
                    "status": str(check.status),
                    "detail": check.detail,
                    "duration_ms": round(check.duration_ms, 2),
                }
                for check in self.checks
            ],
        }


#: A readiness check: an async callable returning its own outcome.
CheckFunction = Callable[[], Awaitable[CheckOutcome]]


class HealthRegistry:
    """Where components declare the dependencies they need to be ready.

    Instantiated once per process at the composition root and passed to the
    components that contribute checks. Deliberately not a module-level singleton:
    an ambient global registry cannot be isolated in tests, and a readiness
    system that cannot be tested is a readiness system nobody trusts.
    """

    def __init__(self, *, timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS) -> None:
        """Initialise an empty registry.

        Parameters
        ----------
        timeout_seconds
            Per-check budget. A check exceeding it is reported ``UNHEALTHY``
            rather than being awaited indefinitely -- a hanging check would
            otherwise hang the readiness probe, which is the failure mode
            readiness exists to prevent.
        """
        self._checks: dict[str, CheckFunction] = {}
        self._timeout_seconds = timeout_seconds

    def register(self, name: str, check: CheckFunction) -> None:
        """Register a dependency check.

        Parameters
        ----------
        name
            Unique dependency name.
        check
            Async callable returning a :class:`CheckOutcome`.

        Raises
        ------
        ValueError
            If ``name`` is already registered. Silently replacing a check would
            mean a dependency stops being verified without anyone noticing.
        """
        if name in self._checks:
            msg = f"health check {name!r} is already registered"
            raise ValueError(msg)
        self._checks[name] = check

    @property
    def registered(self) -> tuple[str, ...]:
        """Return the names of every registered check, in registration order."""
        return tuple(self._checks)

    async def _run_one(self, name: str, check: CheckFunction) -> CheckOutcome:
        """Evaluate one check, converting timeouts and exceptions to outcomes.

        Notes
        -----
        The timeout clause names both ``TimeoutError`` and
        ``asyncio.TimeoutError``. On the Python 3.12 target these are the same
        object and the pair is redundant; it is written this way so that
        verification on the older sandbox interpreter exercises the same branch
        rather than silently skipping it. Remove the second name once validation
        runs on 3.12 (tracked in the S02 technical debt register).
        """
        started = time.perf_counter()
        try:
            outcome = await asyncio.wait_for(check(), timeout=self._timeout_seconds)
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 - see note below
            elapsed = (time.perf_counter() - started) * 1000
            return CheckOutcome(
                name=name,
                status=HealthStatus.UNHEALTHY,
                detail=f"check did not answer within {self._timeout_seconds:.1f}s",
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            return CheckOutcome(
                name=name,
                status=HealthStatus.UNHEALTHY,
                detail=f"check raised {type(exc).__name__}",
                duration_ms=elapsed,
            )
        elapsed = (time.perf_counter() - started) * 1000
        return CheckOutcome(
            name=outcome.name or name,
            status=outcome.status,
            detail=outcome.detail,
            duration_ms=elapsed,
        )

    async def evaluate(self) -> ReadinessReport:
        """Run every registered check concurrently and aggregate the result.

        Returns
        -------
        ReadinessReport
            Aggregate status and per-dependency breakdown.

        Notes
        -----
        Checks run concurrently so that total latency is the slowest check rather
        than their sum -- which is what keeps the endpoint inside its p95 budget
        as the number of dependencies grows.

        The exception detail deliberately reports only the exception *type*, never
        its message. A message can contain a connection string, and this payload
        is served over HTTP (FR-20).
        """
        if not self._checks:
            return ReadinessReport(status=HealthStatus.HEALTHY, checks=())

        outcomes = await asyncio.gather(
            *(self._run_one(name, check) for name, check in self._checks.items())
        )
        ordered = tuple(outcomes)
        if any(outcome.status is HealthStatus.UNHEALTHY for outcome in ordered):
            aggregate = HealthStatus.UNHEALTHY
        elif any(outcome.status is HealthStatus.DEGRADED for outcome in ordered):
            aggregate = HealthStatus.DEGRADED
        else:
            aggregate = HealthStatus.HEALTHY
        return ReadinessReport(status=aggregate, checks=ordered)
