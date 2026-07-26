"""Bootstrap brings the runtime up in the order the design requires."""

from __future__ import annotations

import json
import os

import pytest

from dhruva.__about__ import __version__
from dhruva.shared.config import Environment, SecretValue
from dhruva.shared.errors import UnsafeConfigurationError
from dhruva.shared.logging import get_logger, logging_is_configured
from dhruva.shared.runtime import bootstrap


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from inherited DHRUVA_* variables."""
    for key in list(os.environ):
        if key.startswith("DHRUVA_"):
            monkeypatch.delenv(key, raising=False)


@pytest.mark.unit
def test_bootstrap_returns_a_wired_context(capsys: pytest.CaptureFixture[str]) -> None:
    """A composition root gets configuration and both registries, once."""
    context = bootstrap(service="dhruva-test")
    capsys.readouterr()

    assert context.service == "dhruva-test"
    assert context.settings.environment is Environment.LOCAL
    assert context.health.registered == ()
    assert context.metrics.registered == ()


@pytest.mark.unit
def test_logging_is_configured_before_anything_can_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Step 2 of the sequence. Nothing before it is permitted to emit output."""
    bootstrap(service="dhruva-test")
    capsys.readouterr()

    assert logging_is_configured()


@pytest.mark.unit
def test_the_startup_banner_states_the_facts_an_operator_needs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Including whether tracing is active (FR-19).

    A no-op tracer is otherwise indistinguishable from a broken one: spans simply
    vanish, and the absence of data looks like the absence of traffic.
    """
    monkeypatch.setenv("DHRUVA_APP__ENVIRONMENT", "staging")
    monkeypatch.setenv("DHRUVA_DB__PASSWORD", "a-real-looking-staging-password")

    bootstrap(service="dhruva-test")

    banner = next(
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{") and "runtime started" in line
    )
    assert banner["service"] == "dhruva-test"
    assert banner["version"] == __version__
    assert banner["environment"] == "staging"
    assert banner["tracing_active"] is False
    assert banner["log_format"] == "json"


@pytest.mark.unit
def test_a_secret_created_after_bootstrap_is_still_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Step 3 of the sequence, verified.

    The registry is snapshotted per record rather than per configuration, so a
    credential constructed later in startup is still scrubbed.
    """
    bootstrap(service="dhruva-test")
    capsys.readouterr()

    token = SecretValue("kite-token-created-after-bootstrap")
    get_logger("t").info("using token", note=f"value {token.reveal()}")

    assert "kite-token-created-after-bootstrap" not in capsys.readouterr().err


@pytest.mark.unit
def test_bootstrap_refuses_unsafe_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast: the process must not start, rather than fail at 09:16."""
    monkeypatch.setenv("DHRUVA_APP__ENVIRONMENT", "production")
    monkeypatch.setenv("DHRUVA_APP__DEBUG", "true")
    monkeypatch.setenv("DHRUVA_DB__PASSWORD", "a-real-looking-production-password")

    with pytest.raises(UnsafeConfigurationError):
        bootstrap(service="dhruva-test")


@pytest.mark.unit
def test_a_tracing_misconfiguration_is_warned_about_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configured-but-inactive tracing must present as a fact, not as silence."""
    monkeypatch.setenv("DHRUVA_OTEL__ENABLED", "true")
    monkeypatch.setenv("DHRUVA_OTEL__ENDPOINT", "http://collector:4318")

    bootstrap(service="dhruva-test")

    assert "spans will be discarded" in capsys.readouterr().err


@pytest.mark.unit
def test_the_context_is_immutable(capsys: pytest.CaptureFixture[str]) -> None:
    """Wiring is decided once, at startup, and not rearranged at runtime."""
    context = bootstrap(service="dhruva-test")
    capsys.readouterr()

    with pytest.raises((AttributeError, TypeError)):
        context.service = "other"  # type: ignore[misc]  # asserting immutability
