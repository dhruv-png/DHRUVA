"""Historical universes and corporate-action semantics fail closed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from dhruva.contexts.reference.api import (
    HistoricalUniverseDefinition,
    HistoricalUniverseMember,
    HistoricalUniverseMembershipRevision,
    MembershipReason,
    ReturnBasis,
    SourceDiligenceStatus,
    SurvivorshipStatus,
    UniverseKind,
)
from dhruva.contexts.reference.domain.corporate_actions import return_basis_supported
from dhruva.contexts.reference.domain.historical_universe import (
    assess_survivorship,
    historical_universe_fingerprint,
)
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit

ACCOUNT = AccountId.deterministic("owner-family")
KNOWN = datetime(2020, 1, 2, 12, tzinfo=UTC)
STOCK = InstrumentId.deterministic("reference", "historical-stock")


def _definition(*, licensing_confirmed: bool = False) -> HistoricalUniverseDefinition:
    return HistoricalUniverseDefinition(
        account_id=ACCOUNT,
        universe_id="licensed-india-equities-v1",
        label="Licensed India Equities",
        kind=UniverseKind.HISTORICAL_MARKET_UNIVERSE,
        known_at=KNOWN,
        source="licensed-fixture",
        source_revision="dataset-v1",
        source_status=SourceDiligenceStatus.OWNER_APPROVAL_REQUIRED,
        historical_membership_available=True,
        removals_included=True,
        delistings_included=True,
        pit_known_at_available=True,
        instrument_lifecycle_available=True,
        licensing_confirmed=licensing_confirmed,
    )


def _membership() -> HistoricalUniverseMembershipRevision:
    return HistoricalUniverseMembershipRevision(
        account_id=ACCOUNT,
        universe_id="licensed-india-equities-v1",
        instrument_id=STOCK,
        effective_from=date(2018, 1, 1),
        effective_to=date(2020, 6, 30),
        known_at=KNOWN,
        source="licensed-fixture",
        source_revision="dataset-v1",
        reason=MembershipReason.SOURCE_REPORTED,
        source_member_key="security-1",
        is_delisted=True,
    )


def test_current_selection_and_unlicensed_history_cannot_claim_survivorship_safety() -> None:
    """Rows alone are insufficient: coverage, PIT, lifecycle, and rights all gate safety."""
    current = HistoricalUniverseDefinition(
        account_id=ACCOUNT,
        universe_id="current-owner-watchlist",
        label="Current owner watchlist",
        kind=UniverseKind.CURRENT_OWNER_WATCHLIST,
        known_at=KNOWN,
        source="owner-selection",
        source_revision="owner-v1",
        source_status=SourceDiligenceStatus.DOCUMENTATION_REVIEWED,
        historical_membership_available=False,
        removals_included=False,
        delistings_included=False,
        pit_known_at_available=False,
        instrument_lifecycle_available=False,
        licensing_confirmed=True,
    )

    assert assess_survivorship(current).status is SurvivorshipStatus.CURRENT_SELECTION_ONLY
    assert not assess_survivorship(current).survivorship_safe
    assert assess_survivorship(_definition()).status is SurvivorshipStatus.PIT_KNOWN_AT_AVAILABLE
    assert not assess_survivorship(_definition()).survivorship_safe
    assert assess_survivorship(_definition(licensing_confirmed=True)).survivorship_safe


def test_membership_effectivity_keeps_removed_and_delisted_security_queryable_in_past() -> None:
    """An ended/delisted membership remains a historical fact before its end date."""
    member = _membership()

    assert member.active_on(date(2019, 1, 1))
    assert member.is_delisted
    assert not member.active_on(date(2021, 1, 1))


def test_universe_fingerprint_includes_identity_mapping_revision() -> None:
    """A symbol/mapping correction changes evidence identity without changing security id."""
    membership = _membership()
    first = (
        HistoricalUniverseMember(
            membership=membership,
            canonical_symbol="OLD",
            company_name="Example Limited",
            identity_source_revision="mapping-v1",
        ),
    )
    changed = (replace(first[0], canonical_symbol="NEW", identity_source_revision="mapping-v2"),)

    first_hash = historical_universe_fingerprint(_definition(), date(2019, 1, 1), KNOWN, first)
    assert first_hash == historical_universe_fingerprint(
        _definition(), date(2019, 1, 1), KNOWN, first
    )
    assert first_hash != historical_universe_fingerprint(
        _definition(), date(2019, 1, 1), KNOWN, changed
    )


def test_total_return_requires_both_adjustment_and_dividend_evidence() -> None:
    """UNKNOWN cannot become adjusted, and dividends cannot be invented."""
    assert not return_basis_supported(
        ReturnBasis.UNKNOWN, adjustment_verified=True, dividends_complete=True
    )
    assert not return_basis_supported(
        ReturnBasis.PRICE_ADJUSTED,
        adjustment_verified=False,
        dividends_complete=True,
    )
    assert not return_basis_supported(
        ReturnBasis.TOTAL_RETURN,
        adjustment_verified=True,
        dividends_complete=False,
    )
    assert return_basis_supported(
        ReturnBasis.TOTAL_RETURN,
        adjustment_verified=True,
        dividends_complete=True,
    )
