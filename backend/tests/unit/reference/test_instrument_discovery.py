"""Daily discovery keeps cash analysis while deriving futures from current facts."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dhruva.contexts.reference.application.instrument_discovery import (
    DiscoverOwnerInstruments,
    DiscoverOwnerInstrumentsCommand,
)
from dhruva.contexts.reference.domain.instrument_master import (
    FuturesAvailabilityStatus,
    FuturesContractStatus,
    InstrumentMasterSnapshot,
)
from dhruva.contexts.reference.infrastructure.owner_universe import load_owner_universe
from dhruva.contexts.reference.infrastructure.zerodha_instruments import (
    parse_instrument_master,
)
from dhruva.shared.errors import StaleDataError
from dhruva.shared.identity import AccountId

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

MARKET_DATE = date(2026, 8, 2)
FETCHED_AT = datetime(2026, 8, 2, 6, 30, tzinfo=UTC)
FIXTURE = Path(__file__).parents[2] / "fixtures" / "zerodha" / "instruments_sanitized.csv"


class FakeInstrumentMasterSource:
    """Return one arranged immutable provider snapshot."""

    def __init__(self, snapshot: InstrumentMasterSnapshot) -> None:
        self.snapshot = snapshot
        self.requested: list[date] = []

    async def fetch(self, *, market_date: date) -> InstrumentMasterSnapshot:
        """Capture the requested effective date and return the snapshot."""
        self.requested.append(market_date)
        return self.snapshot


def _snapshot() -> InstrumentMasterSnapshot:
    payload = FIXTURE.read_bytes()
    return InstrumentMasterSnapshot(
        provider="zerodha",
        market_date=MARKET_DATE,
        fetched_at=FETCHED_AT,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        raw_csv=payload,
        entries=parse_instrument_master(payload),
    )


def _command() -> DiscoverOwnerInstrumentsCommand:
    universe = load_owner_universe(
        AccountId.deterministic("owner-family"),
        recorded_at=FETCHED_AT,
    )
    return DiscoverOwnerInstrumentsCommand(
        definitions=universe.definitions,
        market_date=MARKET_DATE,
    )


async def test_exact_owner_universe_resolves_cash_and_dynamic_futures() -> None:
    """All cash identities survive while only evidenced futures become available."""
    source = FakeInstrumentMasterSource(_snapshot())

    discovery = await DiscoverOwnerInstruments(source).execute(_command())

    by_symbol = {item.canonical_symbol: item for item in discovery.resolutions}
    assert len(discovery.resolutions) == 21
    assert all(item.cash is not None for item in discovery.resolutions)
    assert by_symbol["NAM-INDIA"].cash is not None
    assert by_symbol["NAM-INDIA"].cash.trading_symbol == "NAM-INDIA"
    assert by_symbol["M&M"].cash is not None
    assert by_symbol["M&M"].cash.trading_symbol == "M&M"
    assert tuple(contract.expiry for contract in by_symbol["ADANIENT"].futures.contracts) == (
        date(2026, 8, 27),
        date(2026, 9, 24),
        date(2026, 10, 29),
    )
    assert len(by_symbol["NIFTY 50"].futures.contracts) == 3
    assert len(by_symbol["M&M"].futures.contracts) == 1
    assert by_symbol["HAL"].futures.status is FuturesAvailabilityStatus.CURRENTLY_UNAVAILABLE
    assert by_symbol["HAL"].cash is not None
    assert source.requested == [MARKET_DATE]


async def test_options_expired_contracts_and_invalid_lots_never_create_futures() -> None:
    """Only current positive-lot NFO-FUT rows count as eligibility evidence."""
    discovery = await DiscoverOwnerInstruments(FakeInstrumentMasterSource(_snapshot())).execute(
        _command()
    )
    by_symbol = {item.canonical_symbol: item for item in discovery.resolutions}

    assert all(
        contract.expiry >= MARKET_DATE for contract in by_symbol["ADANIENT"].futures.contracts
    )
    assert by_symbol["ETERNAL"].futures.contracts == ()
    assert by_symbol["HAL"].futures.contracts == ()
    expired = tuple(
        observation
        for observation in by_symbol["ADANIENT"].futures_observations
        if observation.status is FuturesContractStatus.EXPIRED
    )
    assert len(expired) == 1
    assert expired[0].contract.expiry == date(2026, 7, 30)
    assert not expired[0].selected_for_availability


async def test_ambiguous_contract_month_degrades_only_that_underlying() -> None:
    """Two mappings for one expiry are explicit unavailability, not a guessed contract."""
    snapshot = _snapshot()
    existing = next(item for item in snapshot.entries if item.instrument_token == 300001)
    duplicate = replace(
        existing,
        instrument_token=900001,
        exchange_token=900002,
        trading_symbol="ADANIENT26AUGFUTX",
    )
    arranged = replace(snapshot, entries=(*snapshot.entries, duplicate))

    discovery = await DiscoverOwnerInstruments(FakeInstrumentMasterSource(arranged)).execute(
        _command()
    )
    by_symbol = {item.canonical_symbol: item for item in discovery.resolutions}

    assert by_symbol["ADANIENT"].futures.status is FuturesAvailabilityStatus.CURRENTLY_UNAVAILABLE
    assert "ambiguous" in by_symbol["ADANIENT"].futures.reason
    assert by_symbol["M&M"].futures.status is FuturesAvailabilityStatus.AVAILABLE


async def test_provider_token_turnover_does_not_change_contract_identity() -> None:
    """A token is an effective attribute, never the stable contract key."""
    first_snapshot = _snapshot()
    old = next(item for item in first_snapshot.entries if item.instrument_token == 300001)
    replacement = replace(old, instrument_token=800001, exchange_token=800002)
    second_snapshot = replace(
        first_snapshot,
        entries=tuple(replacement if item is old else item for item in first_snapshot.entries),
    )

    first = await DiscoverOwnerInstruments(FakeInstrumentMasterSource(first_snapshot)).execute(
        _command()
    )
    second = await DiscoverOwnerInstruments(FakeInstrumentMasterSource(second_snapshot)).execute(
        _command()
    )
    first_contract = next(
        item for item in first.resolutions if item.canonical_symbol == "ADANIENT"
    ).futures.contracts[0]
    second_contract = next(
        item for item in second.resolutions if item.canonical_symbol == "ADANIENT"
    ).futures.contracts[0]

    assert first_contract.contract_id == second_contract.contract_id
    assert first_contract.instrument_token != second_contract.instrument_token


async def test_master_assigned_to_another_date_is_rejected_as_stale() -> None:
    """A cached older master cannot silently claim to be today's provider state."""
    stale = replace(_snapshot(), market_date=date(2026, 8, 1))

    with pytest.raises(StaleDataError, match="different trading date"):
        await DiscoverOwnerInstruments(FakeInstrumentMasterSource(stale)).execute(_command())
