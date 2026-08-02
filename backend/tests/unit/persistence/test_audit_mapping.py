"""The audit record's four-layer mapping, round-tripped without a database.

Domain <-> record <-> model kwargs, and the model's columns against the record's
fields. The database round trip -- and every part of the append-only guarantee --
is an integration concern (ADR-058, ADR-071); what is verified here is that
nothing is lost or invented at a layer boundary.

Losslessness matters more on this table than on most. An audit record cannot be
corrected: the database refuses an ``UPDATE``, so a field that came back subtly
wrong is wrong permanently, and the only remedy is a compensating row that says
the first one was mistaken. A mapping defect here does not produce a bug to fix;
it produces evidence to disclaim.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.contexts.platform.domain.audit import AuditAction, AuditOutcome, AuditRecord
from dhruva.contexts.platform.infrastructure.persistence.factories import AuditFactory
from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_audit_log_model_kwargs,
    to_audit_log_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import AuditLogModel
from dhruva.contexts.platform.infrastructure.persistence.records import AuditLogRecord
from dhruva.shared.errors import DataQualityError
from dhruva.shared.identity import AccountId

pytestmark = pytest.mark.unit

FACTORY: Final = AuditFactory()
ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
CORRELATION: Final = UUID("22222222-2222-2222-2222-222222222222")
OCCURRED: Final = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)
ROW_ID: Final = UUID("33333333-3333-3333-3333-333333333333")


def _record(**overrides: object) -> AuditRecord:
    defaults: dict[str, object] = {
        "actor": "operator@dhruva.local",
        "action": AuditAction.CREDENTIAL_READ,
        "subject": "credential:zerodha",
        "outcome": AuditOutcome.SUCCEEDED,
        "occurred_at": OCCURRED,
        "recorded_at": OCCURRED,
        "account_id": ACCOUNT,
        "correlation_id": CORRELATION,
    }
    return AuditRecord(**{**defaults, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #


#: Arbitrary text with a non-blank prefix, rather than ``st.text().filter(...)``.
#: The record refuses a blank actor, so a filter would be the obvious way to
#: satisfy it -- and a filter that rejects a large share of what the strategy
#: generates trips Hypothesis's ``filter_too_much`` health check, which under
#: ``filterwarnings = ["error"]`` is a failure that appears and disappears
#: between runs. Mapping instead of filtering discards nothing.
_non_blank = st.text(max_size=200).map(lambda tail: f"a{tail}")


@given(
    actor=_non_blank,
    subject=_non_blank,
    action=st.sampled_from(list(AuditAction)),
    outcome=st.sampled_from(list(AuditOutcome)),
    delay_seconds=st.integers(min_value=0, max_value=10**6),
)
def test_domain_to_record_to_domain_is_lossless(
    actor: str,
    subject: str,
    action: AuditAction,
    outcome: AuditOutcome,
    delay_seconds: int,
) -> None:
    """Every field survives both directions, for every action and outcome.

    ``actor`` and ``subject`` are generated rather than fixed because they are
    the two free-text fields, and free text is where an encoding assumption
    hides -- a name with a combining character, a subject with a newline in it.
    Neither should be treated specially, and this is what says so.
    """
    original = _record(
        actor=actor,
        subject=subject,
        action=action,
        outcome=outcome,
        recorded_at=OCCURRED + timedelta(seconds=delay_seconds),
    )

    assert FACTORY.reconstruct(FACTORY.deconstruct(original, ROW_ID)) == original


def test_the_enums_are_stored_as_their_values_not_their_names() -> None:
    """``StrEnum`` makes the two easy to confuse and only one is the contract.

    ``AuditAction.ORDER_ACTION.value`` is ``"order_action"``; its ``name`` is
    ``"ORDER_ACTION"``. A table holding a mixture is a table no query can filter,
    and because rows are never updated the mixture would be permanent.
    """
    flattened = FACTORY.deconstruct(_record(action=AuditAction.ORDER_ACTION), ROW_ID)

    assert flattened.action == "order_action"
    assert flattened.outcome == "succeeded"


def test_record_to_model_kwargs_covers_every_column() -> None:
    """A field added to the record but forgotten in the mapper would be silent."""
    flattened = FACTORY.deconstruct(_record(), ROW_ID)

    kwargs = to_audit_log_model_kwargs(flattened)

    assert set(kwargs) == {f.name for f in flattened.__dataclass_fields__.values()}


def test_the_model_and_the_record_describe_the_same_row() -> None:
    """Catches a column added to the table and not to the record, or the reverse.

    Either omission is silent: the row saves, and the missing value is simply
    never read back. On this table the value would also be unrecoverable, since
    nothing may go back and fill it in.
    """
    columns = {column.name for column in AuditLogModel.__table__.columns}

    assert columns == {f.name for f in AuditLogRecord.__dataclass_fields__.values()}


def test_a_model_maps_to_a_record_field_for_field() -> None:
    """The read direction, which the round-trip test above does not exercise."""
    flattened = FACTORY.deconstruct(_record(), ROW_ID)
    model = AuditLogModel(**to_audit_log_model_kwargs(flattened))

    assert to_audit_log_record(model) == flattened


# --------------------------------------------------------------------------- #
# What the table must not have
# --------------------------------------------------------------------------- #


def test_the_table_has_no_version_column() -> None:
    """Optimistic concurrency protects an ``UPDATE``, and there is no ``UPDATE``.

    ADR-057 puts a ``version`` on every aggregate, and this is the one table it
    does not apply to. A version column here would imply an update path that the
    migration's triggers make impossible -- and would be the first thing someone
    reached for when adding one.
    """
    assert "version" not in {column.name for column in AuditLogModel.__table__.columns}


def test_the_table_has_no_free_form_column() -> None:
    """The record's anti-secret property, asserted against the schema.

    An earlier draft of the migration carried ``metadata JSONB``. The domain
    object had already argued at length why a free-form field is the wrong thing
    to put on the one table written to be read widely (ADR-037), and the column
    was the same defect arriving through the schema instead. This is what makes
    reintroducing it a visible act.
    """
    offenders = [
        column.name
        for column in AuditLogModel.__table__.columns
        if column.type.__class__.__name__ in {"JSON", "JSONB", "HSTORE", "ARRAY"}
    ]

    assert not offenders, f"audit_log gained a column that can hide a secret: {offenders}"


def test_every_column_is_not_null() -> None:
    """ADR-071 names each field as carried, not as optional.

    ``account_id`` in particular: ADR-004 requires it on every domain table and
    ADR-071 repeats it, and a nullable one would leave rows that RLS cannot scope
    when S44 activates it.
    """
    nullable = [column.name for column in AuditLogModel.__table__.columns if column.nullable]

    assert nullable == []


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #


def test_the_record_carries_no_domain_types() -> None:
    """The record is the layer with no invariants, so a mapper needs no domain."""
    for name, annotation in AuditLogRecord.__annotations__.items():
        assert str(annotation) in {"UUID", "str", "datetime"}, (
            f"{name} is a domain type; records carry primitives only"
        )


# --------------------------------------------------------------------------- #
# Reading a row the domain does not recognise
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["action", "outcome"])
def test_an_unknown_stored_value_fails_on_read(field: str) -> None:
    """A value outside the enum is bad data, and it fails where it is read.

    On a table with no update path this is the plausible corruption: not an edit,
    but an insert from a writer that knew a value this one does not. Returning it
    as an unrecognised string would put it into a compliance export with nothing
    to mark it as unreadable, which is the outcome worth failing to avoid.

    ``DataQualityError`` rather than ``InvariantViolation``: nothing about the
    caller's arguments was wrong, and the distinction is what tells an operator
    whether to look at the code or at the row.
    """
    flattened = FACTORY.deconstruct(_record(), ROW_ID)
    if field == "action":
        corrupted = replace(flattened, action="?")
    else:
        corrupted = replace(flattened, outcome="?")

    with pytest.raises(DataQualityError):
        FACTORY.reconstruct(corrupted)


def test_the_failure_names_the_row_and_the_field() -> None:
    """An operator cannot fix the row, so the error has to be enough on its own."""
    flattened = FACTORY.deconstruct(_record(), ROW_ID)
    corrupted = replace(flattened, action="exfiltration")

    with pytest.raises(DataQualityError) as raised:
        FACTORY.reconstruct(corrupted)

    assert str(ROW_ID) in str(raised.value.context["row_id"])
    assert raised.value.context["stored"] == "exfiltration"


def test_a_reconstructed_record_is_not_reusing_the_row_identity() -> None:
    """The surrogate key stays in persistence; the value object never learns it.

    A domain record that carried its row id would let a caller assemble a
    "corrected" record with the same identity and hand it to the store -- and the
    only reason that fails today is the database. Keeping the key out of the
    value object means it fails one layer earlier, with nothing to express.
    """
    original = _record()
    flattened = FACTORY.deconstruct(original, uuid4())

    assert FACTORY.reconstruct(flattened) == original


def _as_dict(record: AuditLogRecord) -> dict[str, object]:
    """Return a slotted frozen record's fields as a mutable mapping."""
    return {name: getattr(record, name) for name in record.__dataclass_fields__}
