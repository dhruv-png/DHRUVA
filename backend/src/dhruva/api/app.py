"""The observability component.

The first runtime component in the platform, and deliberately the smallest one
that can exist: three endpoints, no business surface. Business endpoints arrive
at S35.

It exists now rather than later because ADR-035 requires every runtime component
to ship its observability obligations at the moment it is introduced, and because
building the mechanism against a real consumer is the only way to know the
mechanism works.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from dhruva.__about__ import __version__
from dhruva.shared.context import bind_correlation
from dhruva.shared.logging import get_logger
from dhruva.shared.observability.metrics import PROMETHEUS_CONTENT_TYPE
from dhruva.shared.runtime import RuntimeContext

__all__ = ["CORRELATION_HEADER", "create_app"]

#: Inbound correlation identifier. Honoured when supplied so a request can be
#: traced across process boundaries; generated when absent.
CORRELATION_HEADER: Final = "X-Correlation-ID"

_log = get_logger(__name__)


def create_app(context: RuntimeContext) -> FastAPI:
    """Build the observability application.

    Parameters
    ----------
    context
        The runtime context produced by
        :func:`~dhruva.shared.runtime.bootstrap`.

    Returns
    -------
    FastAPI
        An application exposing ``/health``, ``/ready`` and ``/metrics``.

    Notes
    -----
    A factory rather than a module-level ``app`` object, so tests construct an
    isolated instance with their own registries. A module-level app would read
    configuration at import time, which is precisely what ADR-031 forbids.
    """
    app = FastAPI(
        title="D.H.R.U.V.A observability",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    requests = context.metrics.counter(
        "dhruva_platform_http_requests_total",
        "HTTP requests served by the observability component.",
        labels=("endpoint", "status"),
    )
    latency = context.metrics.histogram(
        "dhruva_platform_http_request_duration_seconds",
        "Latency of the observability endpoints.",
        labels=("endpoint",),
    )

    @app.middleware("http")
    async def _correlation_middleware(request, call_next):  # type: ignore[no-untyped-def]  # starlette hook
        """Bind a correlation identifier for the lifetime of the request."""
        inbound = request.headers.get(CORRELATION_HEADER)
        with bind_correlation(correlation_id=inbound) as correlation:
            with latency.labels(endpoint=request.url.path).time():
                response = await call_next(request)
            requests.labels(endpoint=request.url.path, status=str(response.status_code)).inc()
            if correlation.correlation_id is not None:
                response.headers[CORRELATION_HEADER] = correlation.correlation_id
            return response

    @app.get("/health", response_class=JSONResponse)
    async def health() -> JSONResponse:
        """Liveness. Always 200 while the process is running.

        Performs **no** dependency checks, deliberately. A health endpoint that
        consults the database turns a slow database into a restart loop, which
        turns a degraded system into an outage (ADR-035).
        """
        return JSONResponse({"status": "alive", "service": context.service, "version": __version__})

    @app.get("/ready", response_class=JSONResponse)
    async def ready() -> JSONResponse:
        """Readiness, with a per-dependency breakdown.

        Returns 200 when every dependency is healthy or merely degraded, and 503
        when any is unhealthy -- including any that could not be evaluated, since
        ADR-022 makes unknown fail closed.
        """
        report = await context.health.evaluate()
        if not report.is_ready:
            _log.warning(
                "readiness check failed",
                status=str(report.status),
                failing=[c.name for c in report.checks if not c.status.is_ready],
            )
        return JSONResponse(report.to_dict(), status_code=200 if report.is_ready else 503)

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> Response:
        """Prometheus exposition."""
        return Response(content=context.metrics.render(), media_type=PROMETHEUS_CONTENT_TYPE)

    return app
