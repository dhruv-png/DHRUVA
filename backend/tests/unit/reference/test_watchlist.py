"""Canonical instrument and membership invariants."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
    InstrumentKind,
    WatchlistInstrument,
    WatchlistMembershipRevision,
)
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit

ACCOUNT = AccountId.deterministic("owner-family")
INSTRUMENT = InstrumentId.deterministic("reference", "nse-equity-mahindra-and-mahindra")
RECORDED = datetime(2026, 8, 2, 12, tzinfo=UTC)


def _identity(**overrides: object) -> InstrumentIdentityRevision:
    values: dict[str, object] = {
        "instrument_id": INSTRUMENT,
        "kind": InstrumentKind.EQUITY,
        "canonical_symbol": "M&M",
        "company_name": "Mahindra & Mahindra Limited",
        "aliases": ("Mahindra & Mahindra",),
        "former_names": (),
        "isin": None,
        "sector": "Automobiles",
        "concentration_groups": (),
        "cash_mapping": CashInstrumentMapping(exchange="NSE", trading_symbol="M&M"),
        "futures_research_requested": True,
        "valid_from": date(2026, 8, 2),
        "valid_to": None,
        "recorded_at": RECORDED,
        "source": "owner_configuration",
        "source_revision": "owner-watchlist-2026-08-02",
    }
    return InstrumentIdentityRevision(**{**values, **overrides})  # type: ignore[arg-type]


def _membership(**overrides: object) -> WatchlistMembershipRevision:
    values: dict[str, object] = {
        "account_id": ACCOUNT,
        "instrument_id": INSTRUMENT,
        "active_from": date(2026, 8, 2),
        "active_to": None,
        "recorded_at": RECORDED,
        "source": "owner_configuration",
        "source_revision": "owner-watchlist-2026-08-02",
    }
    return WatchlistMembershipRevision(**{**values, **overrides})  # type: ignore[arg-type]


def test_punctuation_is_part_of_the_canonical_symbol() -> None:
    """The owner-provided ampersand survives identity and cash mapping."""
    identity = _identity()

    assert identity.canonical_symbol == "M&M"
    assert identity.cash_mapping.trading_symbol == "M&M"


@pytest.mark.parametrize("symbol", ["M AND M", "nam india", "-BAD", "BAD_SYMBOL"])
def test_invalid_equity_symbols_are_refused(symbol: str) -> None:
    """No invented normalization can enter canonical identity."""
    with pytest.raises(InvariantViolation, match="invalid canonical NSE symbol"):
        _identity(
            canonical_symbol=symbol,
            cash_mapping=CashInstrumentMapping(exchange="NSE", trading_symbol="M&M"),
        )


def test_nifty_index_identity_accepts_its_canonical_space() -> None:
    """Index identity is separate from equity symbol syntax."""
    identity = _identity(
        kind=InstrumentKind.INDEX,
        canonical_symbol="NIFTY 50",
        cash_mapping=CashInstrumentMapping(exchange="NSE", trading_symbol="NIFTY 50"),
    )

    assert identity.kind is InstrumentKind.INDEX


def test_provider_tokens_are_optional_but_never_partial() -> None:
    """The owner list expresses intent; the provider refresh resolves tokens."""
    assert _identity().cash_mapping.instrument_token is None

    with pytest.raises(InvariantViolation, match="tokens form a pair"):
        CashInstrumentMapping(
            exchange="NSE",
            trading_symbol="M&M",
            provider="zerodha",
            instrument_token=123,
        )


def test_aliases_are_case_insensitively_unique_and_sorted() -> None:
    """Entity linking receives deterministic aliases without collisions."""
    with pytest.raises(InvariantViolation, match="duplicates"):
        _identity(aliases=("SBI", "sbi"))
    with pytest.raises(InvariantViolation, match="sorted"):
        _identity(aliases=("Zed", "Alpha"))


def test_identity_and_membership_windows_are_inclusive() -> None:
    """An end date remains active for that session and closes after it."""
    final_day = date(2026, 8, 31)
    identity = _identity(valid_to=final_day)
    membership = _membership(active_to=final_day)

    assert identity.effective_on(final_day)
    assert membership.active_on(final_day)
    assert not identity.effective_on(date(2026, 9, 1))
    assert not membership.active_on(date(2026, 9, 1))


def test_recorded_time_must_be_utc_and_aware() -> None:
    """Reference history cannot carry an ambiguous knowledge instant."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        _identity(
            recorded_at=datetime(2026, 8, 2, 12)  # noqa: DTZ001 -- defect under test
        )


def test_join_refuses_different_stable_instruments() -> None:
    """A membership cannot be accidentally labelled with another identity."""
    with pytest.raises(InvariantViolation, match="same instrument"):
        WatchlistInstrument(
            identity=_identity(),
            membership=_membership(
                instrument_id=InstrumentId.deterministic("reference", "different")
            ),
        )
