"""Redaction is a control, and this file is the proof that it fires.

ADR-033 makes "no credential reaches a log sink" a control rather than a
convention. A redaction filter that has never been demonstrated to work is
security theatre, so these tests attack it from every direction a real leak
takes: named fields, interpolated messages, nested structures, exception
arguments, and bound context.
"""

from __future__ import annotations

import json

import pytest
import structlog

from dhruva.shared.config import REDACTED_PLACEHOLDER, SecretValue
from dhruva.shared.context import bind_correlation
from dhruva.shared.logging import configure_logging, get_logger, logging_is_configured

#: A fake credential, long enough to enter the value registry.
LEAKED = "kite-access-token-DEADBEEF-9911"


@pytest.fixture
def captured(capsys: pytest.CaptureFixture[str]) -> pytest.CaptureFixture[str]:
    """Configure JSON logging for the duration of a test."""
    configure_logging(
        level="DEBUG", log_format="json", service="test", version="0.0.0", environment="test"
    )
    capsys.readouterr()
    return capsys


def _records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    captured_output = capsys.readouterr().err.strip()
    return [json.loads(line) for line in captured_output.splitlines() if line.startswith("{")]


@pytest.mark.unit
def test_logging_reports_whether_it_is_configured(
    captured: pytest.CaptureFixture[str],
) -> None:
    """Bootstrap ordering depends on this being answerable."""
    get_logger("t").info("probe")

    assert logging_is_configured()
    assert _records(captured)


@pytest.mark.unit
@pytest.mark.parametrize(
    "field",
    [
        "password",
        "api_key",
        "apiKey",
        "access_token",
        "kite_api_secret",
        "authorization",
        "totp",
        "db_password_hash",
        "session_id",
        "private_key",
    ],
)
def test_sensitive_field_names_are_redacted(
    captured: pytest.CaptureFixture[str], field: str
) -> None:
    """Key-based redaction covers the common case of a named credential."""
    get_logger("t").info("attempting auth", **{field: LEAKED})

    record = _records(captured)[0]
    assert record[field] == REDACTED_PLACEHOLDER
    assert LEAKED not in json.dumps(record)


@pytest.mark.unit
def test_a_secret_interpolated_into_a_message_is_redacted(
    captured: pytest.CaptureFixture[str],
) -> None:
    """The leak that key-based redaction always misses.

    Nobody names the field ``password`` when they are formatting a string.
    """
    token = SecretValue(LEAKED)
    message = f"connecting with {token.reveal()}"
    get_logger("t").info(message)

    assert LEAKED not in json.dumps(_records(captured)[0])


@pytest.mark.unit
def test_a_secret_in_an_innocuously_named_field_is_redacted(
    captured: pytest.CaptureFixture[str],
) -> None:
    """Value-scanning catches what the field name does not advertise."""
    SecretValue(LEAKED)
    get_logger("t").info("request prepared", note=f"header set to {LEAKED}")

    assert LEAKED not in json.dumps(_records(captured)[0])


@pytest.mark.unit
def test_a_secret_nested_in_a_structure_is_redacted(
    captured: pytest.CaptureFixture[str],
) -> None:
    """Object graphs are logged whole far more often than anyone intends."""
    SecretValue(LEAKED)
    get_logger("t").info(
        "config dump",
        config={"broker": {"credentials": [{"value": LEAKED}], "host": "api.kite.trade"}},
    )

    rendered = json.dumps(_records(captured)[0])
    assert LEAKED not in rendered
    assert "api.kite.trade" in rendered, "redaction must not destroy ordinary content"


@pytest.mark.unit
def test_a_secret_in_an_exception_argument_is_redacted(
    captured: pytest.CaptureFixture[str],
) -> None:
    """Tracebacks render exception arguments, and arguments carry credentials."""
    SecretValue(LEAKED)

    def _reject() -> None:
        msg = f"auth rejected for token {LEAKED}"
        raise RuntimeError(msg)

    try:
        _reject()
    except RuntimeError:
        get_logger("t").exception("authentication failed")

    assert LEAKED not in json.dumps(_records(captured)[0])


@pytest.mark.unit
def test_redaction_survives_bound_context(captured: pytest.CaptureFixture[str]) -> None:
    """Context is merged by an earlier processor; redaction must still see it."""
    SecretValue(LEAKED)
    structlog.contextvars.bind_contextvars(api_key=LEAKED)
    try:
        get_logger("t").info("using bound context")
        assert LEAKED not in json.dumps(_records(captured)[0])
    finally:
        structlog.contextvars.clear_contextvars()


@pytest.mark.unit
def test_ordinary_content_is_untouched(captured: pytest.CaptureFixture[str]) -> None:
    """A redactor that destroys normal logs would simply be switched off."""
    get_logger("t").info(
        "subscription established", instrument_count=2847, exchange="NSE", latency_ms=12.5
    )

    record = _records(captured)[0]
    assert record["instrument_count"] == 2847
    assert record["exchange"] == "NSE"
    assert record["latency_ms"] == 12.5


@pytest.mark.unit
def test_short_values_do_not_trigger_redaction(captured: pytest.CaptureFixture[str]) -> None:
    """Redacting a four-character string out of every line would be worse."""
    SecretValue("abc")
    get_logger("t").info("state changed", state="abc", note="value abc observed")

    record = _records(captured)[0]
    assert record["note"] == "value abc observed"


@pytest.mark.unit
def test_secrets_inside_sequences_are_redacted(captured: pytest.CaptureFixture[str]) -> None:
    """Lists, tuples and sets are logged as readily as dicts.

    The field names here are deliberately innocuous. A field called ``tokens``
    would be redacted wholesale by the key pattern, which would pass this test
    without exercising the sequence traversal at all.
    """
    SecretValue(LEAKED)

    get_logger("t").info(
        "batch prepared", payload=[LEAKED, "safe"], pair=(LEAKED,), unique={LEAKED}
    )

    rendered = json.dumps(_records(captured)[0])
    assert LEAKED not in rendered
    assert "safe" in rendered, "redaction must be element-wise, not wholesale"


@pytest.mark.unit
def test_a_sensitive_field_name_redacts_the_whole_value(
    captured: pytest.CaptureFixture[str],
) -> None:
    """Key-based redaction is deliberately blunt.

    A field named ``tokens`` is replaced entirely rather than traversed. A false
    positive costs one redacted debug line; a false negative costs a rotation.
    """
    get_logger("t").info("batch prepared", tokens=["a-value", "another-value"])

    assert _records(captured)[0]["tokens"] == REDACTED_PLACEHOLDER


@pytest.mark.unit
def test_every_record_carries_service_identity(captured: pytest.CaptureFixture[str]) -> None:
    """A log line must identify the build that produced it."""
    get_logger("t").info("hello")

    record = _records(captured)[0]
    assert record["service"] == "test"
    assert record["version"] == "0.0.0"
    assert record["environment"] == "test"


@pytest.mark.unit
def test_timestamps_are_utc_with_an_explicit_offset(
    captured: pytest.CaptureFixture[str],
) -> None:
    """ADR-006. A log line read in another timezone must be unambiguous."""
    get_logger("t").info("hello")

    assert str(_records(captured)[0]["timestamp"]).endswith("+00:00")


@pytest.mark.unit
def test_correlation_appears_only_when_bound(captured: pytest.CaptureFixture[str]) -> None:
    """Nulls on every record are noise, and noise stops people reading logs."""
    get_logger("t").info("unbound")
    with bind_correlation(correlation_id="dhv-logging"):
        get_logger("t").info("bound")

    unbound, bound = _records(captured)
    assert "correlation_id" not in unbound
    assert bound["correlation_id"] == "dhv-logging"


@pytest.mark.unit
def test_console_format_is_available_for_humans(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Local development needs readable output, and it must redact too."""
    configure_logging(
        level="INFO", log_format="console", service="t", version="0", environment="local"
    )
    capsys.readouterr()
    SecretValue(LEAKED)

    get_logger("t").info("connecting", api_key=LEAKED)

    assert LEAKED not in capsys.readouterr().err
