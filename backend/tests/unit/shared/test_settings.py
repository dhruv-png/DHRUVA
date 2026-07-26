"""Configuration is typed, fail-fast, and refuses unsafe deployed values."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dhruva.shared.config import Environment, SecretValue
from dhruva.shared.config.settings import DatabaseSettings, Settings, load_settings
from dhruva.shared.errors import ConfigurationError, UnsafeConfigurationError


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited DHRUVA_* variables so tests do not affect each other."""
    for key in list(os.environ):
        if key.startswith("DHRUVA_"):
            monkeypatch.delenv(key, raising=False)


@pytest.mark.unit
def test_defaults_are_local_and_safe() -> None:
    """A developer with no configuration gets a working, local process."""
    settings = load_settings()

    assert settings.environment is Environment.LOCAL
    assert settings.app.port == 8000
    assert settings.resolved_log_format() == "console"


@pytest.mark.unit
def test_nested_variables_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``DHRUVA_APP__PORT`` maps to ``settings.app.port``."""
    monkeypatch.setenv("DHRUVA_APP__PORT", "9001")
    monkeypatch.setenv("DHRUVA_LOG__LEVEL", "warning")

    settings = load_settings()

    assert settings.app.port == 9001
    assert settings.log.level == "WARNING"


@pytest.mark.unit
def test_an_unknown_variable_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised key is almost always a typo in a deployment manifest.

    Ignoring it silently means the operator believes they configured something
    they did not.
    """
    monkeypatch.setenv("DHRUVA_APP__PORTT", "9001")

    with pytest.raises(ConfigurationError) as caught:
        load_settings()

    assert "portt" in str(caught.value.context["details"]).lower()


@pytest.mark.unit
def test_invalid_values_fail_fast_and_name_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """A startup failure that does not say which variable is wrong costs hours."""
    monkeypatch.setenv("DHRUVA_APP__PORT", "70000")

    with pytest.raises(ConfigurationError) as caught:
        load_settings()

    assert "app.port" in caught.value.context["fields"]


@pytest.mark.unit
def test_an_unrecognised_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misspelt level would otherwise be silently ignored by the framework."""
    monkeypatch.setenv("DHRUVA_LOG__LEVEL", "VERBOSE")

    with pytest.raises(ConfigurationError):
        load_settings()


@pytest.mark.unit
def test_deployed_environments_reject_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-04: valid configuration that is wrong is the dangerous kind."""
    monkeypatch.setenv("DHRUVA_APP__ENVIRONMENT", "production")
    monkeypatch.setenv("DHRUVA_APP__DEBUG", "true")
    monkeypatch.setenv("DHRUVA_DB__PASSWORD", "a-real-looking-production-password")

    with pytest.raises(UnsafeConfigurationError) as caught:
        load_settings()

    assert caught.value.context["field"] == "app.debug"


@pytest.mark.unit
def test_deployed_environments_reject_placeholder_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A development default reaching production is stopped by nothing else."""
    monkeypatch.setenv("DHRUVA_APP__ENVIRONMENT", "staging")

    with pytest.raises(UnsafeConfigurationError) as caught:
        load_settings()

    assert caught.value.context["field"] == "db.password"


@pytest.mark.unit
def test_deployed_environments_reject_human_readable_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Console logs in production are logs no aggregator can parse."""
    monkeypatch.setenv("DHRUVA_APP__ENVIRONMENT", "production")
    monkeypatch.setenv("DHRUVA_DB__PASSWORD", "a-real-looking-production-password")
    monkeypatch.setenv("DHRUVA_LOG__FORMAT", "console")

    with pytest.raises(UnsafeConfigurationError) as caught:
        load_settings()

    assert caught.value.context["field"] == "log.format"


@pytest.mark.unit
def test_a_valid_production_configuration_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The safety checks must not block a correct deployment."""
    monkeypatch.setenv("DHRUVA_APP__ENVIRONMENT", "production")
    monkeypatch.setenv("DHRUVA_DB__PASSWORD", "a-real-looking-production-password")

    settings = load_settings()

    assert settings.environment is Environment.PRODUCTION
    assert settings.resolved_log_format() == "json"


@pytest.mark.unit
def test_an_explicit_log_format_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Derivation is a default, not an override; a developer may still choose."""
    monkeypatch.setenv("DHRUVA_LOG__FORMAT", "json")

    assert load_settings().resolved_log_format() == "json"


@pytest.mark.unit
def test_log_format_is_derived_rather_than_defaulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Derivation stops a deployed environment being misconfigured independently."""
    monkeypatch.setenv("DHRUVA_APP__ENVIRONMENT", "staging")
    monkeypatch.setenv("DHRUVA_DB__PASSWORD", "a-real-looking-production-password")

    assert load_settings().resolved_log_format() == "json"


@pytest.mark.unit
def test_settings_are_frozen() -> None:
    """Mutable configuration is configuration that drifts at runtime."""
    settings = load_settings()

    with pytest.raises((TypeError, ValueError, AttributeError)):
        settings.app.port = 1234


@pytest.mark.unit
def test_secrets_never_serialise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dumping settings is a common debugging move; it must be safe."""
    monkeypatch.setenv("DHRUVA_DB__PASSWORD", "kite-production-password-9911")

    dumped = load_settings().model_dump_json()

    assert "kite-production-password-9911" not in dumped
    assert "redacted" in dumped


@pytest.mark.unit
def test_an_env_file_is_read_when_supplied(tmp_path: Path) -> None:
    """Developers keep local configuration in a file, not in their shell."""
    env_file = tmp_path / ".env"
    env_file.write_text("DHRUVA_APP__PORT=9876\n", encoding="utf-8")

    assert load_settings(env_file).app.port == 9876


@pytest.mark.unit
def test_the_environment_wins_over_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment must not be overridden by a file left in the image."""
    env_file = tmp_path / ".env"
    env_file.write_text("DHRUVA_APP__PORT=9876\n", encoding="utf-8")
    monkeypatch.setenv("DHRUVA_APP__PORT", "7777")

    assert load_settings(env_file).app.port == 7777


@pytest.mark.unit
def test_tracing_enabled_without_an_endpoint_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claiming tracing is on with nowhere to send spans is worse than off."""
    monkeypatch.setenv("DHRUVA_OTEL__ENABLED", "true")

    with pytest.raises(ConfigurationError):
        load_settings()


@pytest.mark.unit
def test_an_already_wrapped_secret_is_passed_through() -> None:
    """Construction in code supplies a SecretValue; the coercer must not re-wrap."""
    original = SecretValue("already-wrapped-credential", register=False)

    assert DatabaseSettings(password=original).password == original


@pytest.mark.unit
def test_settings_can_be_constructed_directly_for_tests() -> None:
    """Tests need a settings object without touching the process environment."""
    settings = Settings()

    assert settings.app.name == "dhruva"
