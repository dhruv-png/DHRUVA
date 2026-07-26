"""Typed, fail-fast process configuration.

The **only** module in the codebase that reads the environment (ADR-031,
boundary rule R5). Configuration is validated once, at a composition root, and
typed slices are injected into the components that need them. A component that
receives the whole :class:`Settings` object is a component that knows too much.

Fail-fast is the property that matters most here. A process with invalid
configuration must refuse to start, naming the offending field, rather than
starting successfully and failing at 09:16 on a trading day when something first
reads it.

Environment variable naming
---------------------------
Prefix ``DHRUVA_``, nested delimiter ``__``::

    DHRUVA_APP__ENVIRONMENT = production
    DHRUVA_DB__PASSWORD = ...
    DHRUVA_LOG__LEVEL = WARNING
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from dhruva.shared.config.environment import Environment
from dhruva.shared.config.secret import REDACTED_PLACEHOLDER, SecretValue
from dhruva.shared.errors import ConfigurationError, UnsafeConfigurationError

__all__ = [
    "AppSettings",
    "DatabaseSettings",
    "LogSettings",
    "RedisSettings",
    "Settings",
    "TracingSettings",
    "load_settings",
]


def _coerce_secret(value: Any) -> Any:
    """Wrap a plain string from the environment in a :class:`SecretValue`."""
    if isinstance(value, str):
        return SecretValue(value)
    return value


#: Pydantic-compatible secret field. The adapter lives here rather than on
#: :class:`SecretValue` itself so that the secret type stays dependency-free and
#: usable from ``domain`` layers, which must import no framework (ADR-001).
Secret = Annotated[
    SecretValue,
    BeforeValidator(_coerce_secret),
    PlainSerializer(lambda _: REDACTED_PLACEHOLDER, return_type=str),
]

Port = Annotated[int, Field(ge=1, le=65535)]

#: Substrings that mark a value as a development placeholder. A placeholder
#: reaching a deployed environment is more dangerous than a missing value,
#: because nothing else will stop it (FR-04).
_PLACEHOLDER_MARKERS: frozenset[str] = frozenset(
    {"change-me", "changeme", "placeholder", "example", "local_only", "your-", "xxxx"}
)


def _looks_like_a_placeholder(value: str) -> bool:
    """Return whether ``value`` looks like an unset development default."""
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


class _Group(BaseModel):
    """Base for the nested settings groups.

    ``extra="forbid"`` is deliberate: an unrecognised key is almost always a typo
    in a deployment manifest, and ignoring it silently means the operator
    believes they configured something they did not.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class AppSettings(_Group):
    """Identity and network binding of the running process."""

    name: str = "dhruva"
    environment: Environment = Environment.LOCAL
    debug: bool = False
    host: str = "127.0.0.1"
    port: Port = 8000


class LogSettings(_Group):
    """How this process emits logs.

    ``format`` is deliberately optional. Leaving it unset lets
    :meth:`Settings.resolved_log_format` derive it from the environment, so a
    deployed environment cannot accidentally be configured to emit
    human-readable logs that nothing can parse.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["json", "console"] | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _uppercase_level(cls, value: Any) -> Any:
        """Accept ``info`` as readily as ``INFO``; reject anything unknown."""
        return value.upper() if isinstance(value, str) else value


class DatabaseSettings(_Group):
    """PostgreSQL/TimescaleDB connection description.

    Defined at S02 so S04 inherits a validated description. S02 opens no
    connection and issues no query.
    """

    host: str = "localhost"
    port: Port = 5432
    name: str = "dhruva"
    user: str = "dhruva"
    password: Secret = Field(
        default_factory=lambda: SecretValue("dhruva_local_only", register=False)
    )
    pool_size: int = Field(default=10, ge=1, le=100)
    statement_timeout_ms: int = Field(default=30_000, ge=100)


class RedisSettings(_Group):
    """Redis connection description, used from S05."""

    host: str = "localhost"
    port: Port = 6379
    db: int = Field(default=0, ge=0, le=15)
    password: Secret | None = None


class TracingSettings(_Group):
    """OpenTelemetry configuration.

    The SDK is configured only at composition roots (ADR-040). When disabled,
    tracing is a no-op -- and the startup banner says so, because a no-op tracer
    is otherwise indistinguishable from a broken one (FR-19).
    """

    enabled: bool = False
    endpoint: str | None = None
    service_name: str = "dhruva"
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _endpoint_required_when_enabled(self) -> Self:
        """Refuse to claim tracing is on without somewhere to send spans."""
        if self.enabled and not self.endpoint:
            msg = "tracing is enabled but no exporter endpoint is configured"
            raise ValueError(msg)
        return self


class Settings(BaseSettings):
    """The complete, validated process configuration.

    Constructed once, by :func:`load_settings`, at a composition root. Never
    constructed by a context, and never read from a module-level global.
    """

    model_config = SettingsConfigDict(
        env_prefix="DHRUVA_",
        env_nested_delimiter="__",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        validate_default=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    otel: TracingSettings = Field(default_factory=TracingSettings)

    @property
    def environment(self) -> Environment:
        """Return the deployment environment, for convenience at call sites."""
        return self.app.environment

    def resolved_log_format(self) -> Literal["json", "console"]:
        """Return the renderer to use, deriving it from the environment if unset.

        Returns
        -------
        {"json", "console"}
            ``json`` in staging and production, ``console`` locally and in tests.

        Notes
        -----
        Derivation rather than a plain default means a deployed environment
        cannot silently end up emitting logs that no aggregator can parse.
        """
        if self.log.format is not None:
            return self.log.format
        return "json" if self.app.environment.is_deployed else "console"

    @model_validator(mode="after")
    def _reject_unsafe_deployed_configuration(self) -> Self:
        """Refuse to run a deployed environment with development-shaped values.

        Raises
        ------
        UnsafeConfigurationError
            If debug is on, a credential is still a placeholder, or the log
            format is forced to a human-readable renderer.

        Notes
        -----
        FR-04. Valid configuration that is *wrong* is more dangerous than invalid
        configuration, because every other check passes it.
        """
        if not self.app.environment.is_deployed:
            return self

        environment = str(self.app.environment)
        if self.app.debug:
            msg = "debug must be disabled outside development"
            raise UnsafeConfigurationError(msg, environment=environment, field="app.debug")

        if _looks_like_a_placeholder(self.db.password.reveal()):
            msg = "database password is still a development placeholder"
            raise UnsafeConfigurationError(msg, environment=environment, field="db.password")

        if self.log.format == "console":
            msg = "deployed environments must emit machine-readable logs"
            raise UnsafeConfigurationError(msg, environment=environment, field="log.format")

        return self


def load_settings(env_file: Path | None = None) -> Settings:
    """Load, validate and return the process configuration.

    Parameters
    ----------
    env_file
        Optional ``.env`` to read before the process environment. Environment
        variables always win, so a deployment cannot be overridden by a file that
        happened to be left in the image.

    Returns
    -------
    Settings
        Validated configuration.

    Raises
    ------
    ConfigurationError
        If any field is missing, malformed, or unsafe for its environment. The
        message names every offending field, because a startup failure that does
        not say which variable is wrong costs an operator far more than it saves.

    Notes
    -----
    This function is the single point at which the environment is read. Boundary
    rule R5 fails the build if any other module does so.
    """
    try:
        if env_file is not None:
            return Settings(_env_file=env_file)  # type: ignore[call-arg]  # pydantic-settings runtime kwarg
        return Settings()
    except UnsafeConfigurationError:
        raise
    except ValidationError as exc:
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        msg = "configuration is invalid"
        raise ConfigurationError(msg, fields=fields, details=details) from exc
