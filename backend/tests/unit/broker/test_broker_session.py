"""The broker-credential payloads and the session expiry rule.

Two things are under test. The documents that go inside a sealed credential --
which must survive a round trip exactly, because a field lost here is a
credential that silently stops working -- and :func:`next_expiry_after`, which
decides whether an operator is told to log in again.

The expiry rule gets the most attention. Kite ends a session at 06:00 IST the
morning after it is created, and the failure mode of getting it wrong is
asymmetric: an expiry computed too late means the system believes it is
authenticated when the broker has already stopped listening, and every request
afterwards fails for a reason the operator cannot see from here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from dhruva.contexts.platform.domain.broker.session import (
    SESSION_EXPIRY_HOUR_IST,
    BrokerApplication,
    BrokerAuthState,
    BrokerSession,
    next_expiry_after,
    parse_broker_application,
    parse_broker_session,
    serialise_broker_application,
    serialise_broker_session,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import DataQualityError, InvariantViolation
from dhruva.shared.time.clock import FrozenClock

pytestmark = pytest.mark.unit

IST: Final = timedelta(hours=5, minutes=30)
ISSUED: Final = datetime(2026, 8, 8, 4, 0, tzinfo=UTC)  # 09:30 IST
EXPIRES: Final = datetime(2026, 8, 9, 0, 30, tzinfo=UTC)  # 06:00 IST next day

#: Obviously synthetic. Nothing here has the shape of real broker material, and
#: nothing here is or ever was a working credential.
IDENTIFIER: Final = "synthetic-application-identifier"
APPLICATION_MATERIAL: Final = "synthetic-application-material"
SESSION_MATERIAL: Final = "synthetic-session-material"


def _application() -> BrokerApplication:
    return BrokerApplication(
        identifier=SecretValue(IDENTIFIER, register=False),
        secret=SecretValue(APPLICATION_MATERIAL, register=False),
    )


def _session(**overrides: object) -> BrokerSession:
    defaults: dict[str, object] = {
        "token": SecretValue(SESSION_MATERIAL, register=False),
        "broker_user_id": "XX0000",
        "issued_at": ISSUED,
        "expires_at": EXPIRES,
    }
    return BrokerSession(**{**defaults, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ist_hour", "ist_minute", "expected_day"),
    [
        (9, 30, 9),
        (15, 45, 9),
        (23, 59, 9),
        (5, 59, 8),
        (3, 0, 8),
        (6, 0, 9),
        (6, 1, 9),
    ],
    ids=[
        "morning",
        "afternoon",
        "just-before-midnight",
        "one-minute-before-cutoff",
        "small-hours",
        "exactly-at-cutoff",
        "just-after-cutoff",
    ],
)
def test_a_session_expires_at_the_next_six_am_ist(
    ist_hour: int, ist_minute: int, expected_day: int
) -> None:
    """Including the cases either side of the cut-off, which is the whole risk.

    A login at 03:00 IST expires three hours later, *that morning*. Treating it
    as good until tomorrow would give it twenty-seven hours, and the extra
    twenty-four are hours in which every broker call fails.

    A login exactly at 06:00 IST gets tomorrow, not zero seconds: the rule is
    the *next* cut-off strictly after the issue time, and a session with a
    zero-length life would be issued and immediately reported expired.
    """
    issued = (datetime(2026, 8, 8, ist_hour, ist_minute, tzinfo=UTC) - IST).replace(tzinfo=UTC)

    expiry = next_expiry_after(issued)

    assert (expiry + IST).day == expected_day
    assert (expiry + IST).hour == SESSION_EXPIRY_HOUR_IST
    assert (expiry + IST).minute == 0
    assert expiry > issued


def test_an_expiry_is_always_in_the_future_of_its_issue() -> None:
    """The property behind the parametrised cases, over a full day of minutes."""
    midnight = datetime(2026, 8, 8, tzinfo=UTC)

    for minute in range(0, 24 * 60, 7):
        issued = midnight + timedelta(minutes=minute)
        assert next_expiry_after(issued) > issued


def test_a_naive_issue_time_is_refused() -> None:
    """ADR-006. A naive instant here would place the cut-off in an unknown zone."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        next_expiry_after(datetime(2026, 8, 8, 9, 30))  # noqa: DTZ001


def test_a_session_reports_expiry_against_an_injected_clock() -> None:
    """ADR-011: the report is reproducible rather than dependent on when it runs."""
    session = _session()

    assert not session.is_expired(FrozenClock(EXPIRES - timedelta(seconds=1)))
    assert session.is_expired(FrozenClock(EXPIRES))
    assert session.is_expired(FrozenClock(EXPIRES + timedelta(hours=1)))


# --------------------------------------------------------------------------- #
# The payloads
# --------------------------------------------------------------------------- #


def test_an_application_credential_round_trips() -> None:
    """Both halves, unchanged. Either being lost is a credential that cannot sign."""
    restored = parse_broker_application(serialise_broker_application(_application()))

    assert restored.identifier.reveal() == IDENTIFIER
    assert restored.secret.reveal() == APPLICATION_MATERIAL


def test_a_session_round_trips_including_its_expiry() -> None:
    """The expiry is the field a reader acts on, so it must survive exactly."""
    restored = parse_broker_session(serialise_broker_session(_session()))

    assert restored.token.reveal() == SESSION_MATERIAL
    assert restored.broker_user_id == "XX0000"
    assert restored.issued_at == ISSUED
    assert restored.expires_at == EXPIRES


def test_serialisation_is_byte_stable() -> None:
    """Sorted keys and no whitespace, so the same credential seals identically.

    Not merely tidiness: a payload whose byte form varied would make two
    otherwise-identical credentials produce different ciphertext, which removes
    the ability to tell "rotated" from "re-sealed the same thing".
    """
    first = serialise_broker_session(_session()).reveal()
    second = serialise_broker_session(_session()).reveal()

    assert first == second
    assert first.startswith('{"broker_user_id"')


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[]",
        '{"schema": 99, "identifier": "a", "secret": "b"}',
        '{"schema": 1, "secret": "b"}',
        '{"schema": 1, "identifier": "", "secret": "b"}',
    ],
    ids=["not-json", "not-an-object", "wrong-schema", "missing-field", "empty-field"],
)
def test_an_unreadable_application_payload_is_reported_not_guessed(payload: str) -> None:
    """A half-understood credential would be sent to the broker and rejected.

    The operator would then read a parsing bug as an expired secret and rotate a
    perfectly good one. Refusing here names the actual problem.
    """
    with pytest.raises(DataQualityError):
        parse_broker_application(SecretValue(payload, register=False))


def test_a_parse_failure_does_not_quote_the_document_it_failed_on() -> None:
    """The document being parsed is a decrypted secret.

    A JSON decoder's own message includes the offending text, which is why the
    underlying error is neither chained nor repeated. This asserts the outcome
    rather than the mechanism, because the mechanism is easy to reintroduce.
    """
    payload = f'{{"schema": 1, "identifier": "x", "secret": "{APPLICATION_MATERIAL}"'

    with pytest.raises(DataQualityError) as caught:
        parse_broker_application(SecretValue(payload, register=False))

    assert APPLICATION_MATERIAL not in str(caught.value)
    assert APPLICATION_MATERIAL not in repr(caught.value)


def test_a_session_payload_with_a_naive_instant_is_refused() -> None:
    """A naive expiry cannot be compared with an aware clock without guessing."""
    payload = (
        '{"schema": 1, "token": "t", "broker_user_id": "XX0000", '
        '"issued_at": "2026-08-08T04:00:00", "expires_at": "2026-08-09T00:30:00+00:00"}'
    )

    with pytest.raises(DataQualityError, match="naive"):
        parse_broker_session(SecretValue(payload, register=False))


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"token": SecretValue("", register=False)}, "session token is required"),
        ({"broker_user_id": ""}, "broker user id is required"),
        ({"expires_at": ISSUED - timedelta(seconds=1)}, "cannot expire before"),
        ({"expires_at": ISSUED}, "cannot expire before"),
        ({"issued_at": datetime(2026, 8, 8, 4, 0)}, "timezone-aware"),  # noqa: DTZ001
    ],
    ids=["no-token", "no-user-id", "expiry-before-issue", "zero-length", "naive-issue"],
)
def test_an_unusable_session_is_refused(overrides: dict[str, object], match: str) -> None:
    """Each one describes a session that could not be acted on if stored."""
    with pytest.raises(InvariantViolation, match=match):
        _session(**overrides)


def test_an_application_credential_needs_both_halves() -> None:
    """One without the other cannot compute the broker's checksum."""
    with pytest.raises(InvariantViolation, match="identifier is required"):
        replace(_application(), identifier=SecretValue("", register=False))
    with pytest.raises(InvariantViolation, match="secret is required"):
        replace(_application(), secret=SecretValue("", register=False))


def test_the_payload_types_carry_no_plaintext_in_their_repr() -> None:
    """``SecretValue`` masks itself; this asserts the containers do not undo it."""
    assert APPLICATION_MATERIAL not in repr(_application())
    assert IDENTIFIER not in repr(_application())
    assert SESSION_MATERIAL not in repr(_session())


def test_the_authentication_states_are_the_five_an_operator_can_be_in() -> None:
    """Closed, and each member is a different next action.

    Asserting the whole set means a sixth state cannot appear without somebody
    deciding what an operator should do about it.
    """
    assert {member.value for member in BrokerAuthState} == {
        "ENROLMENT_MISSING",
        "SESSION_MISSING",
        "SESSION_EXPIRED",
        "AUTHENTICATION_FAILED",
        "SUCCESS",
    }


def test_the_domain_module_names_no_broker() -> None:
    """ADR-003. These are properties of broker credentials, not of Zerodha.

    Parsed rather than grepped, and docstrings are excluded deliberately. The
    first version of this test failed on its own module's prose -- the class
    docstring says "Kite calls it the api_key", which is exactly the sentence a
    reader needs. A guard that can be tripped by an explanation gets silenced
    rather than obeyed; what must stay vendor-free is the *code*.
    """
    import ast  # noqa: PLC0415 - test-only import
    import pathlib  # noqa: PLC0415 - test-only import

    import dhruva.contexts.platform.domain.broker.session as module  # noqa: PLC0415

    tree = ast.parse(pathlib.Path(module.__file__ or "").read_text(encoding="utf-8"))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    words: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words.add(node.id)
        elif isinstance(node, ast.Attribute):
            words.add(node.attr)
        elif isinstance(node, ast.alias):
            words.add(node.name)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
        ):
            words.add(node.value)

    lowered = " ".join(words).lower()
    for vendor in ("zerodha", "kite", "upstox", "angel"):
        assert vendor not in lowered, f"{vendor} named in domain code"
