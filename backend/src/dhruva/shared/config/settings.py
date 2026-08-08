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

import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

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
    "CryptoSettings",
    "DatabaseSettings",
    "LogSettings",
    "MarketDataSettings",
    "NewsSettings",
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


#: Documented DOC 2.0 timespan forms: a positive count and a unit.
_TIMESPAN = re.compile(r"[1-9][0-9]{0,3}(min|h|d|w|m)")

#: A query expression travels as a URL parameter and has to stay readable in a
#: log line. Anything longer is a program, not a search.
_MAX_QUERY_OVERRIDE = 512


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


class CryptoSettings(_Group):
    """Master key for the credential vault, used from S06 (ADR-070, ADR-020).

    ADR-070 puts the master key in *process* configuration rather than in the
    Platform context's persisted configuration, for the obvious reason: the key
    that decrypts the vault must not be stored in the vault.

    ``master_key`` is **base64-encoded 32-byte key material**, not a passphrase.
    ADR-020 writes "master key from environment/KMS", and a key is what both
    sides of that alternative supply; deriving one from a passphrase here would
    invent a KDF that no decision records, and would make the environment
    variable's meaning differ from the KMS adapter's. Length and encoding are
    checked where the key is used rather than here, so that this module keeps
    importing no cryptographic library (boundary rule R5).

    The default is a placeholder that is *deliberately not* valid key material.
    A working default would mean every local database is encrypted under a key
    published in this repository, and the first deployment that forgot to set the
    variable would inherit it silently. Instead the placeholder is refused twice:
    by :meth:`Settings._reject_unsafe_deployed_configuration` in any deployed
    environment, and by the adapter itself the moment something tries to encrypt.
    """

    master_key: Secret = Field(
        default_factory=lambda: SecretValue("change-me-local-only", register=False)
    )


class AuthSettings(_Group):
    """Token signing and session lifetimes, used from S06 (ADR-072).

    Separate from :class:`CryptoSettings` because the two keys protect different
    things and rotate on different schedules. The vault master key protects data
    at rest and rotating it means re-wrapping every stored credential; the
    signing key protects tokens in flight and rotating it invalidates every
    outstanding access token and nothing else. One group holding both would
    invite an operator to rotate one while meaning the other.

    ``signing_key`` is symmetric key material for HS256. The default is a
    placeholder that is deliberately not usable, refused twice for the same
    reason ``crypto.master_key`` is: a working default means every deployment
    that forgot to set the variable signs tokens anyone reading this repository
    can forge.

    The lifetimes are here rather than hard-coded because ADR-072 makes each an
    operational decision with a stated default, and because a refresh token
    stores its own ``expires_at`` at issue -- so changing the setting affects new
    sessions and leaves outstanding ones exactly as they were.
    """

    signing_key: Secret = Field(
        default_factory=lambda: SecretValue("change-me-local-only", register=False)
    )

    #: Plan §15.1 fixes this at fifteen minutes. It is configurable because an
    #: incident may justify shortening it, and not because lengthening it is a
    #: normal thing to do -- ADR-072 bounds the damage of an unrevocable access
    #: token by exactly this number.
    access_token_seconds: int = Field(default=900, ge=60, le=3600)

    #: Long enough that nobody stores a password in a script to avoid
    #: re-authenticating, which is the failure ADR-072 names.
    refresh_token_days: int = Field(default=30, ge=1, le=365)


class NewsSettings(_Group):
    """How the news discovery pass queries its one approved provider.

    GDELT is the only configured source and it is anonymous, so there is no key,
    no token and no credential of any kind in this group. There is also no proxy
    setting: the recorded rate-limit response to GDELT is to send fewer requests,
    and a proxy would be a way of sending the same number from somewhere else.

    Every bound is deliberate. ``batch_size`` decides how many phrases share one
    request and therefore how many requests a pass makes; ``max_records``
    decides how much one request may return. Both have upper bounds because an
    unbounded value here is an unbounded demand on a free shared service, and
    lower bounds because a zero would produce a request that cannot answer
    anything.

    Watchlist-derived querying is the default and there is deliberately no
    boolean to turn it off. Setting ``query_override`` is what turns it off, and
    it does so structurally: an override runs as the pass's only query, and the
    watchlist plan is never built. Two independent switches encoding one
    decision is how a configuration ends up contradicting itself, with the code
    silently picking a winner.
    """

    #: The documented anonymous DOC 2.0 endpoint. Written out rather than
    #: imported because this module is a leaf: it may not import a context
    #: (boundary rule R4), and the adapter that owns the real constant is one.
    #: The two are kept honest by a test that asserts they agree.
    gdelt_endpoint: str = "https://api.gdeltproject.org/api/v2/doc/doc"

    #: An explicit expression for diagnostics or a deliberate one-off search.
    #: When set, the watchlist plan is not built and this is the only query.
    query_override: str | None = None

    #: Phrases per request. See ``domain.search.DEFAULT_BATCH_SIZE`` for why
    #: eight, and a test that keeps the two in agreement.
    batch_size: int = Field(default=8, ge=1, le=25)

    #: A documented DOC 2.0 timespan: a positive count and a unit.
    timespan: str = "1d"

    #: Articles one request may return. GDELT's documented ceiling is 250.
    max_records: int = Field(default=75, ge=1, le=250)

    #: Whole-request timeout. Long enough for a slow free service, short enough
    #: that an operator running this by hand is not left wondering.
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)

    #: How many archived items a read command prints before truncating.
    result_limit: int = Field(default=50, ge=1, le=500)

    #: How many days back from the cutoff a read command looks by default.
    lookback_days: int = Field(default=7, ge=1, le=365)

    @property
    def watchlist_queries_enabled(self) -> bool:
        """Return whether the pass builds its queries from the watchlist."""
        return self.query_override is None

    @field_validator("gdelt_endpoint")
    @classmethod
    def _require_documented_endpoint(cls, value: str) -> str:
        """Refuse anything but a plain https endpoint with no embedded query."""
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            msg = "the news endpoint must be an absolute https URL"
            raise ValueError(msg)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            msg = "the news endpoint must carry no query, fragment or credentials"
            raise ValueError(msg)
        return value

    @field_validator("query_override")
    @classmethod
    def _reject_an_empty_override(cls, value: str | None) -> str | None:
        """Refuse an override that is blank, unbalanced or absurdly long.

        An empty string is the dangerous case: it is what an unset environment
        variable looks like, and it would otherwise switch the pass out of
        watchlist mode and then ask the provider for nothing at all.
        """
        if value is None:
            return value
        if not value.strip():
            msg = "the news query override is empty; unset it to use the watchlist"
            raise ValueError(msg)
        if len(value) > _MAX_QUERY_OVERRIDE:
            msg = f"the news query override is longer than {_MAX_QUERY_OVERRIDE} characters"
            raise ValueError(msg)
        if value.count('"') % 2 or value.count("(") != value.count(")"):
            msg = "the news query override has unbalanced quotes or parentheses"
            raise ValueError(msg)
        return value

    @field_validator("timespan")
    @classmethod
    def _require_documented_timespan(cls, value: str) -> str:
        """Accept only the documented ``<count><unit>`` forms."""
        if not _TIMESPAN.fullmatch(value):
            msg = "timespan must be a positive count and a unit, such as 15min, 12h, 1d, 3w or 1m"
            raise ValueError(msg)
        return value


class MarketDataSettings(_Group):
    """Bounds for the bounded daily-history bootstrap refresh (MVP 1, AR-002).

    ``bootstrap_lookback_days`` is the whole of this group. It bounds how far
    back an operator-triggered refresh may ever request daily candles for the
    *current* market context -- this is bootstrap, not historical backfill, and
    the upper bound is the approved default itself: the field can be narrowed
    for a faster or cheaper run, never widened past twenty calendar days by
    configuration. A ``--all-history`` flag or an unbounded value here would be
    the same mistake wearing a different name.

    Twenty rather than six (the sessions a current market-context calculation
    needs) because weekends and Indian market holidays consume calendar days
    without producing a session, and the margin has to survive the worst
    realistic holiday clustering, not just an ordinary week.
    """

    bootstrap_lookback_days: int = Field(default=20, ge=1, le=20)


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
    crypto: CryptoSettings = Field(default_factory=CryptoSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    news: NewsSettings = Field(default_factory=NewsSettings)
    marketdata: MarketDataSettings = Field(default_factory=MarketDataSettings)
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

        if _looks_like_a_placeholder(self.crypto.master_key.reveal()):
            msg = "credential vault master key is still a development placeholder"
            raise UnsafeConfigurationError(msg, environment=environment, field="crypto.master_key")

        # A placeholder signing key is worse than a placeholder password: the
        # value is in this repository, so anybody who can read it can mint an
        # access token for any principal and any account (ADR-072).
        if _looks_like_a_placeholder(self.auth.signing_key.reveal()):
            msg = "token signing key is still a development placeholder"
            raise UnsafeConfigurationError(msg, environment=environment, field="auth.signing_key")

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
