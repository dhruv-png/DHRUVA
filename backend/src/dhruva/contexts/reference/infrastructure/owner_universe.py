"""Load the owner-approved universe from a reviewable static data file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dhruva.contexts.reference.application.watchlist import (
    ConfigureReferenceUniverseCommand,
    UniverseDefinition,
)
from dhruva.contexts.reference.domain.watchlist import InstrumentKind

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.shared.identity import AccountId

__all__ = ["OWNER_UNIVERSE_REVISION", "load_owner_universe"]

OWNER_UNIVERSE_REVISION = "owner-watchlist-2026-08-02"
_OWNER_UNIVERSE_PATH = Path(__file__).with_name("fixtures") / "owner_universe_v1.json"


@dataclass(frozen=True, slots=True)
class _OwnerUniverseRow:
    """Validated primitive shape loaded from the committed JSON fixture."""

    identity_key: str
    kind: str
    canonical_symbol: str
    company_name: str
    aliases: tuple[str, ...]
    former_names: tuple[str, ...]
    isin: str | None
    sector: str
    concentration_groups: tuple[str, ...]
    effective_from: str
    effective_to: str | None
    included_in_watchlist: bool
    futures_research_requested: bool


def load_owner_universe(
    account_id: AccountId,
    *,
    recorded_at: datetime,
) -> ConfigureReferenceUniverseCommand:
    """Return the exact approved watchlist plus the Nifty 50 benchmark."""
    payload: list[dict[str, Any]] = json.loads(_OWNER_UNIVERSE_PATH.read_text(encoding="utf-8"))
    definitions = tuple(_definition(_row(item)) for item in payload)
    return ConfigureReferenceUniverseCommand(
        account_id=account_id,
        definitions=definitions,
        recorded_at=recorded_at,
        source="owner_configuration",
        source_revision=OWNER_UNIVERSE_REVISION,
    )


def _definition(row: _OwnerUniverseRow) -> UniverseDefinition:
    """Convert one primitive row into the application input DTO."""
    return UniverseDefinition(
        identity_key=row.identity_key,
        kind=InstrumentKind(row.kind),
        canonical_symbol=row.canonical_symbol,
        company_name=row.company_name,
        aliases=row.aliases,
        former_names=row.former_names,
        isin=row.isin,
        sector=row.sector,
        concentration_groups=row.concentration_groups,
        effective_from=date.fromisoformat(row.effective_from),
        effective_to=date.fromisoformat(row.effective_to) if row.effective_to else None,
        included_in_watchlist=row.included_in_watchlist,
        futures_research_requested=row.futures_research_requested,
    )


def _row(payload: dict[str, Any]) -> _OwnerUniverseRow:
    """Freeze JSON collections so the loaded configuration is immutable."""
    return _OwnerUniverseRow(
        identity_key=str(payload["identity_key"]),
        kind=str(payload["kind"]),
        canonical_symbol=str(payload["canonical_symbol"]),
        company_name=str(payload["company_name"]),
        aliases=tuple(str(value) for value in payload["aliases"]),
        former_names=tuple(str(value) for value in payload["former_names"]),
        isin=str(payload["isin"]) if payload["isin"] is not None else None,
        sector=str(payload["sector"]),
        concentration_groups=tuple(str(value) for value in payload["concentration_groups"]),
        effective_from=str(payload["effective_from"]),
        effective_to=(
            str(payload["effective_to"]) if payload["effective_to"] is not None else None
        ),
        included_in_watchlist=bool(payload["included_in_watchlist"]),
        futures_research_requested=bool(payload["futures_research_requested"]),
    )
