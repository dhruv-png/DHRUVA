"""What a broker credential carries, and how it is written into a sealed blob.

Two payloads, one per purpose (ADR-077).

**Enrolment** holds the application's identifier and its secret. They are issued
together by the broker and rotated together by the owner, so they are one
lifecycle and belong in one sealed document. That is not the arrangement ADR-077
rejected: what it rejected was putting *enrolment and session* in one blob,
which would have given a daily login the power to overwrite a long-lived secret.

**Session** holds the token, the broker's own user id, when the session began
and when it stops working. The user id is not secret and could have lived in a
column; it is inside the sealed document anyway because splitting a small
payload across two storage mechanisms buys nothing and costs a migration.

Both are serialised as canonical JSON with sorted keys, which makes the sealed
bytes reproducible and lets a future field be added without the old rows
becoming unreadable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import DataQualityError
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from dhruva.shared.time.clock import Clock

__all__ = [
    "SESSION_EXPIRY_HOUR_IST",
    "BrokerApplication",
    "BrokerAuthState",
    "BrokerSession",
    "parse_broker_application",
    "parse_broker_session",
    "serialise_broker_application",
    "serialise_broker_session",
]

#: India Standard Time, written out rather than imported from a tz database.
#:
#: The only thing this offset is used for is the daily session cut-off, which is
#: defined by the broker in local time. IST has no daylight saving and has not
#: changed since 1945, so a fixed offset is exact here in a way it would not be
#: for a market calendar (ADR-046 owns that, and this is not that).
_IST: Final = timedelta(hours=5, minutes=30)

#: The hour, in IST, at which a broker session stops working.
#:
#: Kite Connect documents this as a regulatory requirement: an access token
#: "will expire at 6 AM on the next day". It is a property of the *broker*, so
#: the adapter supplies it; this constant is the default a caller inherits when
#: it has nothing better, and it is stated here so the meaning of an expiry is
#: readable without opening the vendor adapter.
SESSION_EXPIRY_HOUR_IST: Final = 6

_APPLICATION_SCHEMA: Final = 1
_SESSION_SCHEMA: Final = 1
_MAX_IDENTIFIER_LENGTH: Final = 128


class BrokerAuthState(StrEnum):
    """Where an account stands with one broker, as an operator experiences it.

    Closed, and each member is a different thing for the operator to *do*. That
    is the test a member has to pass to exist: ``SESSION_EXPIRED`` and
    ``SESSION_MISSING`` are separate because the first means "log in again, this
    worked yesterday" and the second means "you have never logged in", and an
    operator who confuses them looks for a bug that is not there.
    """

    #: No application credential is enrolled. Nothing can be attempted.
    ENROLMENT_MISSING = "ENROLMENT_MISSING"
    #: Enrolled, but no session has ever been established.
    SESSION_MISSING = "SESSION_MISSING"
    #: A session exists and its expiry has passed.
    SESSION_EXPIRED = "SESSION_EXPIRED"
    #: The broker refused the exchange. Distinct from a network failure, which
    #: is an error rather than a state: an operator retries one and fixes the
    #: other.
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    #: Enrolled, with a session that has not expired.
    SUCCESS = "SUCCESS"


@dataclass(frozen=True, slots=True)
class BrokerApplication:
    """The long-lived application credential an owner enrols once.

    ``identifier`` is the public half -- Kite calls it the api_key -- and is not
    secret in the cryptographic sense. It is still held as a ``SecretValue``
    rather than a ``str``, because a value that identifies which account is
    being traded is not something to have appearing in log lines by default, and
    because a caller that has to unwrap it has to decide to.
    """

    identifier: SecretValue
    secret: SecretValue

    def __post_init__(self) -> None:
        """Refuse a credential that could not authenticate anything."""
        invariant(bool(self.identifier.reveal()), "an application identifier is required")
        invariant(bool(self.secret.reveal()), "an application secret is required")
        invariant(
            len(self.identifier.reveal()) <= _MAX_IDENTIFIER_LENGTH,
            "application identifier is implausibly long",
            length=len(self.identifier.reveal()),
        )


@dataclass(frozen=True, slots=True)
class BrokerSession:
    """A live broker session: the token, whose it is, and when it dies.

    ``expires_at`` is stored rather than recomputed on read. The broker decides
    when a session ends, and a rule recomputed later from a changed constant
    would silently re-date sessions that were issued under the old one.
    """

    token: SecretValue
    broker_user_id: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        """Refuse a session that could not be used or could not be judged."""
        invariant(bool(self.token.reveal()), "a session token is required")
        invariant(bool(self.broker_user_id), "a broker user id is required")
        for name in ("issued_at", "expires_at"):
            value: datetime = getattr(self, name)
            invariant(
                value.tzinfo is not None and value.utcoffset() is not None,
                f"{name} must be timezone-aware",
                field=name,
            )
        invariant(
            self.expires_at > self.issued_at,
            "a session cannot expire before it was issued",
            issued_at=self.issued_at.isoformat(),
            expires_at=self.expires_at.isoformat(),
        )

    def is_expired(self, clock: Clock) -> bool:
        """Report whether this session has passed its expiry (ADR-011).

        Takes the clock rather than reading one, so a command's report of
        "expired" is reproducible in a test and in a replay.
        """
        return clock.now() >= self.expires_at


def next_expiry_after(issued_at: datetime, *, hour_ist: int = SESSION_EXPIRY_HOUR_IST) -> datetime:
    """Return the first ``hour_ist`` IST instant strictly after ``issued_at``.

    A session issued at 09:30 IST expires at 06:00 IST tomorrow; one issued at
    03:00 IST expires at 06:00 IST *today*, three hours later. Getting that
    second case wrong is how an overnight login is treated as good for a further
    twenty-seven hours.
    """
    invariant(
        issued_at.tzinfo is not None and issued_at.utcoffset() is not None,
        "issued_at must be timezone-aware",
    )
    local = issued_at.astimezone(UTC) + _IST
    candidate = local.replace(hour=hour_ist, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return (candidate - _IST).replace(tzinfo=UTC)


def serialise_broker_application(application: BrokerApplication) -> SecretValue:
    """Render an application credential as the plaintext that will be sealed."""
    payload = {
        "schema": _APPLICATION_SCHEMA,
        "identifier": application.identifier.reveal(),
        "secret": application.secret.reveal(),
    }
    return SecretValue(_canonical(payload), register=False)


def parse_broker_application(plaintext: SecretValue) -> BrokerApplication:
    """Read an application credential back out of an opened credential.

    Raises
    ------
    DataQualityError
        If the payload is not the document this module writes. Reported rather
        than guessed at: a half-understood credential would be sent to the
        broker and the rejection read as an expired secret.
    """
    payload = _decode(plaintext, expected_schema=_APPLICATION_SCHEMA, kind="application")
    return BrokerApplication(
        identifier=SecretValue(_string(payload, "identifier"), register=False),
        secret=SecretValue(_string(payload, "secret"), register=False),
    )


def serialise_broker_session(session: BrokerSession) -> SecretValue:
    """Render a session as the plaintext that will be sealed."""
    payload = {
        "schema": _SESSION_SCHEMA,
        "token": session.token.reveal(),
        "broker_user_id": session.broker_user_id,
        "issued_at": session.issued_at.astimezone(UTC).isoformat(),
        "expires_at": session.expires_at.astimezone(UTC).isoformat(),
    }
    return SecretValue(_canonical(payload), register=False)


def parse_broker_session(plaintext: SecretValue) -> BrokerSession:
    """Read a session back out of an opened credential.

    Raises
    ------
    DataQualityError
        If the payload is not the document this module writes.
    """
    payload = _decode(plaintext, expected_schema=_SESSION_SCHEMA, kind="session")
    return BrokerSession(
        token=SecretValue(_string(payload, "token"), register=False),
        broker_user_id=_string(payload, "broker_user_id"),
        issued_at=_instant(payload, "issued_at"),
        expires_at=_instant(payload, "expires_at"),
    )


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _decode(plaintext: SecretValue, *, expected_schema: int, kind: str) -> dict[str, Any]:
    try:
        payload = json.loads(plaintext.reveal())
    except ValueError:
        # The exception is deliberately not chained and its text is not
        # repeated: a JSON decoder quotes the document it failed on, and the
        # document here is a secret.
        message = f"the stored {kind} credential is not a readable document"
        raise DataQualityError(message, kind=kind) from None
    if not isinstance(payload, dict):
        message = f"the stored {kind} credential is not a document"
        raise DataQualityError(message, kind=kind)
    if payload.get("schema") != expected_schema:
        message = f"the stored {kind} credential uses an unsupported schema"
        raise DataQualityError(message, kind=kind, schema=repr(payload.get("schema")))
    return payload


def _string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        message = f"the stored credential is missing {field}"
        raise DataQualityError(message, field=field)
    return value


def _instant(payload: dict[str, Any], field: str) -> datetime:
    raw = _string(payload, field)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        message = f"the stored credential holds an unreadable {field}"
        raise DataQualityError(message, field=field) from None
    if parsed.tzinfo is None:
        message = f"the stored credential holds a naive {field}"
        raise DataQualityError(message, field=field)
    return parsed.astimezone(UTC)
