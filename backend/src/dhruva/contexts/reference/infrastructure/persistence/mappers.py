"""Pure model/record mappings for reference persistence (ADR-052)."""

from __future__ import annotations

from typing import Any

from dhruva.contexts.reference.infrastructure.persistence.models import (
    InstrumentIdentityRevisionModel,
    WatchlistMembershipRevisionModel,
)
from dhruva.contexts.reference.infrastructure.persistence.records import (
    IdentityRevisionRecord,
    MembershipRevisionRecord,
)

__all__ = [
    "identity_model_kwargs",
    "identity_record",
    "membership_model_kwargs",
    "membership_record",
]


def identity_model_kwargs(record: IdentityRevisionRecord) -> dict[str, Any]:
    """Return model constructor values for one identity revision."""
    return {
        "id": record.id,
        "instrument_id": record.instrument_id,
        "kind": record.kind,
        "canonical_symbol": record.canonical_symbol,
        "company_name": record.company_name,
        "aliases": list(record.aliases),
        "former_names": list(record.former_names),
        "isin": record.isin,
        "sector": record.sector,
        "concentration_groups": list(record.concentration_groups),
        "cash_exchange": record.cash_exchange,
        "cash_trading_symbol": record.cash_trading_symbol,
        "provider": record.provider,
        "instrument_token": record.instrument_token,
        "exchange_token": record.exchange_token,
        "futures_research_requested": record.futures_research_requested,
        "valid_from": record.valid_from,
        "valid_to": record.valid_to,
        "recorded_at": record.recorded_at,
        "source": record.source,
        "source_revision": record.source_revision,
    }


def identity_record(model: InstrumentIdentityRevisionModel) -> IdentityRevisionRecord:
    """Copy one identity model into an immutable primitive record."""
    return IdentityRevisionRecord(
        id=model.id,
        instrument_id=model.instrument_id,
        kind=model.kind,
        canonical_symbol=model.canonical_symbol,
        company_name=model.company_name,
        aliases=tuple(model.aliases),
        former_names=tuple(model.former_names),
        isin=model.isin,
        sector=model.sector,
        concentration_groups=tuple(model.concentration_groups),
        cash_exchange=model.cash_exchange,
        cash_trading_symbol=model.cash_trading_symbol,
        provider=model.provider,
        instrument_token=model.instrument_token,
        exchange_token=model.exchange_token,
        futures_research_requested=model.futures_research_requested,
        valid_from=model.valid_from,
        valid_to=model.valid_to,
        recorded_at=model.recorded_at,
        source=model.source,
        source_revision=model.source_revision,
    )


def membership_model_kwargs(record: MembershipRevisionRecord) -> dict[str, Any]:
    """Return model constructor values for one membership revision."""
    return {
        "id": record.id,
        "account_id": record.account_id,
        "instrument_id": record.instrument_id,
        "active_from": record.active_from,
        "active_to": record.active_to,
        "recorded_at": record.recorded_at,
        "source": record.source,
        "source_revision": record.source_revision,
    }


def membership_record(model: WatchlistMembershipRevisionModel) -> MembershipRevisionRecord:
    """Copy one membership model into an immutable primitive record."""
    return MembershipRevisionRecord(
        id=model.id,
        account_id=model.account_id,
        instrument_id=model.instrument_id,
        active_from=model.active_from,
        active_to=model.active_to,
        recorded_at=model.recorded_at,
        source=model.source,
        source_revision=model.source_revision,
    )
