"""The observability component satisfies every ADR-035 obligation."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import dhruva.api.__main__ as entrypoint
from dhruva.api.app import CORRELATION_HEADER, create_app
from dhruva.shared.config.settings import Settings
from dhruva.shared.observability import CheckOutcome, HealthRegistry, HealthStatus, MetricsRegistry
from dhruva.shared.runtime import RuntimeContext


@pytest.fixture
def context() -> RuntimeContext:
    """Build an isolated runtime context, without touching the environment."""
    return RuntimeContext(
        settings=Settings(),
        health=HealthRegistry(),
        metrics=MetricsRegistry(),
        service="dhruva-api-test",
    )


@pytest.fixture
def client(context: RuntimeContext) -> Iterator[TestClient]:
    """Return a client over an isolated application instance."""
    with TestClient(create_app(context)) as test_client:
        yield test_client


@pytest.mark.unit
def test_health_is_alive_and_names_the_build(client: TestClient) -> None:
    """Liveness identifies the process, so a probe log is actionable."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    assert response.json()["service"] == "dhruva-api-test"


@pytest.mark.unit
def test_health_never_consults_dependencies(context: RuntimeContext, client: TestClient) -> None:
    """A health endpoint that checks the database turns slowness into a restart loop.

    This is the single most consequential distinction in ADR-035, so it is
    asserted directly: health stays 200 while readiness reports the failure.
    """

    async def broken() -> CheckOutcome:
        return CheckOutcome("postgres", HealthStatus.UNHEALTHY, "down")

    context.health.register("postgres", broken)

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503


@pytest.mark.unit
def test_ready_reports_a_per_dependency_breakdown(
    context: RuntimeContext, client: TestClient
) -> None:
    """An operator must see which dependency failed, not merely that one did."""

    async def ok() -> CheckOutcome:
        return CheckOutcome("redis", HealthStatus.HEALTHY, "pong")

    async def bad() -> CheckOutcome:
        return CheckOutcome("kite", HealthStatus.UNHEALTHY, "session expired")

    context.health.register("redis", ok)
    context.health.register("kite", bad)

    payload = client.get("/ready").json()

    assert payload["ready"] is False
    failing = [c["name"] for c in payload["checks"] if c["status"] == "unhealthy"]
    assert failing == ["kite"]


@pytest.mark.unit
def test_ready_is_200_when_no_dependencies_are_declared(client: TestClient) -> None:
    """A process with nothing to depend on can serve immediately."""
    assert client.get("/ready").status_code == 200


@pytest.mark.unit
def test_metrics_serves_prometheus_exposition(client: TestClient) -> None:
    """Scrapers require the exposition format and its content type."""
    client.get("/health")

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "dhruva_platform_http_requests_total" in response.text


@pytest.mark.unit
def test_requests_are_counted_by_endpoint_and_status(client: TestClient) -> None:
    """Metrics must distinguish a failing endpoint from a busy one."""
    client.get("/health")
    client.get("/health")

    body = client.get("/metrics").text

    assert 'endpoint="/health"' in body
    assert 'status="200"' in body


@pytest.mark.unit
def test_a_correlation_id_is_generated_when_absent(client: TestClient) -> None:
    """Work without an identifier cannot be traced."""
    correlation = client.get("/health").headers[CORRELATION_HEADER]

    assert correlation.startswith("dhv-")


@pytest.mark.unit
def test_an_inbound_correlation_id_is_honoured(client: TestClient) -> None:
    """Honouring an inbound id is what makes a request traceable across processes."""
    response = client.get("/health", headers={CORRELATION_HEADER: "dhv-upstream-123"})

    assert response.headers[CORRELATION_HEADER] == "dhv-upstream-123"


@pytest.mark.unit
def test_no_business_surface_is_exposed(client: TestClient) -> None:
    """S02 delivers three endpoints and nothing else.

    Interactive docs are disabled: this component has no business API to
    document, and an open schema endpoint is surface with no purpose.
    """
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


@pytest.mark.unit
def test_a_failing_check_does_not_leak_a_connection_string(
    context: RuntimeContext, client: TestClient
) -> None:
    """FR-20. The readiness payload is served over HTTP."""

    async def leaky() -> CheckOutcome:
        msg = "could not connect to postgres://dhruva:hunter2@db:5432/dhruva"
        raise ConnectionError(msg)

    context.health.register("postgres", leaky)

    assert "hunter2" not in client.get("/ready").text


@pytest.mark.unit
def test_the_entrypoint_wires_bootstrap_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m dhruva.api`` must actually serve the app it bootstraps.

    Wiring is where composition roots go wrong, and it is invisible to every
    other test, so it is asserted here rather than trusted.
    """
    served: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        served["app"] = app
        served.update(kwargs)

    monkeypatch.setattr(entrypoint.uvicorn, "run", fake_run)
    entrypoint.main()

    assert served["app"] is not None
    assert served["access_log"] is False, "uvicorn must not duplicate the correlation middleware"
    assert served["log_config"] is None, "structlog owns logging; uvicorn must not reconfigure it"
