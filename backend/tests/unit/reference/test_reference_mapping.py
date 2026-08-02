"""Reference revisions cross domain, record and model boundaries losslessly."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
    InstrumentKind,
    WatchlistMembershipRevision,
)
from dhruva.contexts.reference.infrastructure.persistence.factories import (
    IdentityRevisionFactory,
    MembershipRevisionFactory,
)
from dhruva.contexts.reference.infrastructure.persistence.mappers import (
    identity_model_kwargs,
    membership_model_kwargs,
)
from dhruva.contexts.reference.infrastructure.persistence.models import ReferenceBase
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit

IDENTITY_FACTORY = IdentityRevisionFactory()
MEMBERSHIP_FACTORY = MembershipRevisionFactory()
INSTRUMENT = InstrumentId(uuid4())
ACCOUNT = AccountId(uuid4())
RECORDED = datetime(2026, 8, 2, 12, tzinfo=UTC)


def _identity(*, recorded_at: datetime = RECORDED) -> InstrumentIdentityRevision:
    return InstrumentIdentityRevision(
        instrument_id=INSTRUMENT,
        kind=InstrumentKind.EQUITY,
        canonical_symbol="NAM-INDIA",
        company_name="Nippon Life India Asset Management Limited",
        aliases=("NAM India", "Nippon India AMC"),
        former_names=(),
        isin="INF204K01VT2",
        sector="Financial Services",
        concentration_groups=(),
        cash_mapping=CashInstrumentMapping(
            exchange="NSE",
            trading_symbol="NAM-INDIA",
            provider="zerodha",
            instrument_token=12345,
            exchange_token=67890,
        ),
        futures_research_requested=True,
        valid_from=date(2026, 8, 2),
        valid_to=None,
        recorded_at=recorded_at,
        source="owner_configuration",
        source_revision="v1",
    )


def _membership(*, recorded_at: datetime = RECORDED) -> WatchlistMembershipRevision:
    return WatchlistMembershipRevision(
        account_id=ACCOUNT,
        instrument_id=INSTRUMENT,
        active_from=date(2026, 8, 2),
        active_to=None,
        recorded_at=recorded_at,
        source="owner_configuration",
        source_revision="v1",
    )


def test_identity_domain_record_round_trip_is_lossless() -> None:
    """Punctuation, aliases, tokens and concentration metadata all survive."""
    original = _identity()

    assert IDENTITY_FACTORY.reconstruct(IDENTITY_FACTORY.deconstruct(original)) == original


def test_membership_domain_record_round_trip_is_lossless() -> None:
    """Tenant and both temporal axes survive reconstruction."""
    original = _membership()

    assert MEMBERSHIP_FACTORY.reconstruct(MEMBERSHIP_FACTORY.deconstruct(original)) == original


def test_source_revision_identity_is_stable_across_provider_retry_time() -> None:
    """A later retry cannot mint another persistence identity for one revision."""
    first = IDENTITY_FACTORY.deconstruct(_identity())
    retry = IDENTITY_FACTORY.deconstruct(_identity(recorded_at=RECORDED + timedelta(hours=1)))

    assert first.id == retry.id


def test_model_kwargs_cover_every_record_field() -> None:
    """A new primitive cannot be silently omitted at the ORM boundary."""
    identity = IDENTITY_FACTORY.deconstruct(_identity())
    membership = MEMBERSHIP_FACTORY.deconstruct(_membership())

    assert set(identity_model_kwargs(identity)) == set(identity.__dataclass_fields__)
    assert set(membership_model_kwargs(membership)) == set(membership.__dataclass_fields__)


def test_reference_metadata_contains_all_archive_and_watchlist_tables() -> None:
    """Alembic sees every reference table and cannot propose dropping one."""
    assert set(ReferenceBase.metadata.tables) == {
        "cash_instrument_mapping_revision",
        "futures_contract",
        "futures_contract_revision",
        "instrument_identity_revision",
        "instrument_master_snapshot",
        "instrument_resolution_revision",
        "reference_instrument",
        "watchlist_membership_revision",
    }


def test_membership_account_is_non_nullable() -> None:
    """Tenant ownership remains mechanically discoverable by the RLS guard."""
    column = ReferenceBase.metadata.tables["watchlist_membership_revision"].c.account_id

    assert not column.nullable
