"""The error taxonomy is closed, coded, and safe to render.

Three properties are load-bearing and are asserted directly rather than left to
review: codes are unique, codes are stable across releases, and no error reveals
its context values through ``repr``.
"""

from __future__ import annotations

import inspect
import pickle

import pytest

from dhruva.shared import errors
from dhruva.shared.config import SecretValue
from dhruva.shared.errors import (
    ConfigurationError,
    DhruvaError,
    ErrorCode,
    ExternalServiceError,
    PreconditionUnknownError,
    SafetyError,
    StaleDataError,
    UnsafeConfigurationError,
    UpstreamTimeoutError,
)

#: Pinned snapshot of the taxonomy. Renaming a class or changing a code is a
#: breaking change to a public contract -- codes reach logs, API responses and,
#: from S41, alert rules. This test makes that change deliberate rather than
#: incidental. Update it in the same commit as the change, never afterwards.
EXPECTED_CODES: dict[str, str] = {
    "ConfigurationError": "DHR-CFG-001",
    "UnsafeConfigurationError": "DHR-CFG-002",
    "ValidationError": "DHR-VAL-001",
    "NotFoundError": "DHR-NFD-001",
    "ConflictError": "DHR-CFL-001",
    "PermissionDeniedError": "DHR-PRM-001",
    "ExternalServiceError": "DHR-EXT-001",
    "UpstreamUnavailableError": "DHR-EXT-002",
    "UpstreamTimeoutError": "DHR-EXT-003",
    "RateLimitedError": "DHR-EXT-004",
    "DataQualityError": "DHR-DQL-001",
    "StaleDataError": "DHR-DQL-002",
    "MissingDataError": "DHR-DQL-003",
    "SafetyError": "DHR-SAF-001",
    "PreconditionUnknownError": "DHR-SAF-002",
    "DegradedModeError": "DHR-SAF-003",
}


def _concrete_errors() -> dict[str, type[DhruvaError]]:
    return {
        name: obj
        for name, obj in inspect.getmembers(errors, inspect.isclass)
        if issubclass(obj, DhruvaError) and obj is not DhruvaError
    }


@pytest.mark.unit
def test_error_codes_are_pinned() -> None:
    """The taxonomy matches its snapshot exactly, in both directions."""
    actual = {name: str(cls.code) for name, cls in _concrete_errors().items()}
    assert actual == EXPECTED_CODES


@pytest.mark.unit
def test_error_codes_are_unique() -> None:
    """Two errors sharing a code make log filtering ambiguous."""
    codes = [str(cls.code) for cls in _concrete_errors().values()]
    assert len(codes) == len(set(codes))


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(EXPECTED_CODES))
def test_every_error_is_exported(name: str) -> None:
    """An error that is not exported cannot be caught by callers."""
    assert name in errors.__all__


@pytest.mark.unit
def test_context_is_structured_not_interpolated() -> None:
    """Context lands as fields, which is what makes logs queryable."""
    error = StaleDataError("tick data is stale", instrument_id="NIFTY", age_seconds=42)

    assert error.message == "tick data is stale"
    assert error.context == {"instrument_id": "NIFTY", "age_seconds": 42}
    assert "NIFTY" not in error.message


@pytest.mark.unit
def test_str_includes_the_code() -> None:
    """The code is what an operator greps for."""
    assert str(StaleDataError("tick data is stale")) == "[DHR-DQL-002] tick data is stale"


@pytest.mark.unit
def test_repr_never_reveals_context_values() -> None:
    """``repr`` is called implicitly by debuggers and some logging paths.

    A context value can be a credential, so ``repr`` exposes keys only. This is
    the quiet leak path that redaction by field name does not cover.
    """
    error = ConfigurationError(
        "bad config",
        api_secret="super-secret-value",  # noqa: S106 - a fake credential is the point of the test
        field="kite",
    )
    rendered = repr(error)

    assert "super-secret-value" not in rendered
    assert "api_secret" in rendered
    assert "field" in rendered


@pytest.mark.unit
def test_to_dict_is_the_deliberate_way_to_get_values() -> None:
    """Rendering for logs is explicit, so its call sites are reviewable."""
    payload = UpstreamTimeoutError("kite timed out", endpoint="/quote", elapsed_ms=3100).to_dict()

    assert payload == {
        "error": "UpstreamTimeoutError",
        "code": "DHR-EXT-003",
        "message": "kite timed out",
        "retryable": True,
        "context": {"endpoint": "/quote", "elapsed_ms": 3100},
    }


@pytest.mark.unit
def test_external_errors_are_retryable_and_others_are_not() -> None:
    """Callers read retryability from the error, not from the class name."""
    assert ExternalServiceError("upstream down").retryable
    assert not ConfigurationError("bad").retryable
    assert not SafetyError("blocked").retryable


@pytest.mark.unit
def test_safety_errors_are_their_own_branch() -> None:
    """ADR-022: 'we could not establish it was safe' is not an ordinary failure.

    The Risk Engine (S25) distinguishes these from failures, and the interface
    reports them honestly rather than as a generic error.
    """
    blocked = PreconditionUnknownError("ban list unavailable", scrip="RELIANCE")

    assert isinstance(blocked, SafetyError)
    assert not isinstance(blocked, ExternalServiceError)
    assert blocked.code.family == "SAF"


@pytest.mark.unit
def test_subclasses_narrow_without_breaking_except_clauses() -> None:
    """Catching a family must catch its members, or the taxonomy is decoration."""
    assert isinstance(UnsafeConfigurationError("unsafe"), ConfigurationError)
    assert isinstance(UpstreamTimeoutError("slow"), ExternalServiceError)
    assert isinstance(StaleDataError("stale"), errors.DataQualityError)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad", ["CFG-001", "DHR-CONFIG-001", "DHR-cfg-001", "DHR-CFG-1", "nonsense"]
)
def test_malformed_codes_are_rejected_at_construction(bad: str) -> None:
    """Failing at import time keeps a malformed code out of every log line."""
    with pytest.raises(ValueError, match="DHR-XXX-NNN"):
        ErrorCode(bad)


@pytest.mark.unit
def test_error_code_behaves_as_a_string() -> None:
    """Codes are serialised and compared as strings throughout the platform."""
    code = ErrorCode("DHR-DQL-002")

    assert code == "DHR-DQL-002"
    assert f"{code}" == "DHR-DQL-002"
    assert code.family == "DQL"


@pytest.mark.unit
def test_errors_survive_pickling_for_celery_transport() -> None:
    """Celery serialises exceptions across process boundaries (S05).

    An error that cannot be pickled turns a useful failure into an opaque one.
    """
    original = StaleDataError("stale", instrument_id="NIFTY", age_seconds=9)
    restored = pickle.loads(pickle.dumps(original))  # noqa: S301 - our own object, in-process

    assert str(restored) == str(original)


@pytest.mark.unit
def test_a_secret_in_error_context_does_not_leak_through_repr_or_str() -> None:
    """Defence in depth: the context value itself refuses to render."""
    error = ConfigurationError("auth failed", token=SecretValue("kite-token-abcdefgh"))

    assert "kite-token-abcdefgh" not in repr(error)
    assert "kite-token-abcdefgh" not in str(error)
    assert "kite-token-abcdefgh" not in str(error.to_dict())
