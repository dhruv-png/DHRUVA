"""Append-only provenance for matured candidate-ranking outcomes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date, datetime
    from uuid import UUID

    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "CANDIDATE_OUTCOME_SCHEMA_REVISION",
    "CandidateOutcome",
    "CandidateOutcomeAppendResult",
    "candidate_outcome_fingerprint",
]

CANDIDATE_OUTCOME_SCHEMA_REVISION: Final = "candidate-outcome-observation-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """One matured paper outcome; never a mutation of its ranking observation."""

    account_id: AccountId
    candidate_observation_id: UUID
    candidate_observation_sha256: str
    instrument_id: InstrumentId
    canonical_symbol: str
    signal_cutoff: datetime
    observable_through: date
    materialized_at: datetime
    horizon_sessions: int
    entry_date: date
    entry_price: Decimal
    exit_date: date
    exit_price: Decimal
    absolute_return: Decimal
    benchmark_return: Decimal
    excess_return: Decimal
    net_return: Decimal
    net_excess_return: Decimal
    maximum_adverse_excursion: Decimal
    maximum_favorable_excursion: Decimal
    holding_period_drawdown: Decimal
    realized_volatility: Decimal
    ranker_revision: str
    feature_revision: str
    evaluation_revision: str
    outcome_revision: str
    benchmark_symbol: str
    benchmark_basis: str
    execution_timing: str
    cost_bps: Decimal
    adjustment_status: str
    limitations: tuple[str, ...]
    stock_bar_revisions: tuple[str, ...]
    benchmark_bar_revisions: tuple[str, ...]
    outcome_sha256: str
    schema_revision: str = CANDIDATE_OUTCOME_SCHEMA_REVISION

    def __post_init__(self) -> None:
        """Require complete chronology, identities, and a self-verifying hash."""
        invariant(self.canonical_symbol.strip() != "", "outcome symbol cannot be blank")
        for value, field in (
            (self.signal_cutoff, "signal cutoff"),
            (self.materialized_at, "materialized at"),
        ):
            invariant(value.utcoffset() == timedelta(0), f"{field} must be UTC")
        invariant(self.horizon_sessions > 0, "outcome horizon must be positive")
        invariant(self.entry_date > self.signal_cutoff.date(), "entry is not after signal")
        invariant(self.exit_date >= self.entry_date, "outcome exit precedes entry")
        invariant(self.observable_through >= self.exit_date, "outcome is not observable")
        invariant(
            self.materialized_at.date() >= self.observable_through,
            "materialization is early",
        )
        invariant(self.cost_bps >= 0, "outcome cost cannot be negative")
        invariant(
            self.cost_bps == self.cost_bps.quantize(Decimal("0.0001")),
            "outcome cost supports at most four decimal places",
        )
        numeric = (
            self.entry_price,
            self.exit_price,
            self.absolute_return,
            self.benchmark_return,
            self.excess_return,
            self.net_return,
            self.net_excess_return,
            self.maximum_adverse_excursion,
            self.maximum_favorable_excursion,
            self.holding_period_drawdown,
            self.realized_volatility,
            self.cost_bps,
        )
        invariant(all(value.is_finite() for value in numeric), "outcome value must be finite")
        invariant(bool(_SHA256.fullmatch(self.candidate_observation_sha256)), "bad ranking hash")
        invariant(bool(self.stock_bar_revisions), "outcome needs stock bar provenance")
        invariant(bool(self.benchmark_bar_revisions), "outcome needs benchmark bar provenance")
        invariant(
            all(_SHA256.fullmatch(item) for item in self.stock_bar_revisions),
            "bad stock bar revision",
        )
        invariant(
            all(_SHA256.fullmatch(item) for item in self.benchmark_bar_revisions),
            "bad benchmark bar revision",
        )
        if self.outcome_sha256:
            invariant(bool(_SHA256.fullmatch(self.outcome_sha256)), "bad outcome hash")
            invariant(
                self.outcome_sha256 == candidate_outcome_fingerprint(self),
                "outcome hash mismatch",
            )


@dataclass(frozen=True, slots=True)
class CandidateOutcomeAppendResult:
    """Whether a matured fact was inserted or already existed identically."""

    outcome: CandidateOutcome
    created: bool


def candidate_outcome_fingerprint(outcome: CandidateOutcome) -> str:
    """Hash every identity, realized value, assumption, and source revision."""
    payload = {
        "account_id": str(outcome.account_id),
        "candidate_observation_id": str(outcome.candidate_observation_id),
        "candidate_observation_sha256": outcome.candidate_observation_sha256,
        "instrument_id": str(outcome.instrument_id),
        "canonical_symbol": outcome.canonical_symbol,
        "signal_cutoff": outcome.signal_cutoff.isoformat(),
        "observable_through": outcome.observable_through.isoformat(),
        "horizon_sessions": outcome.horizon_sessions,
        "entry_date": outcome.entry_date.isoformat(),
        "entry_price": _decimal(outcome.entry_price),
        "exit_date": outcome.exit_date.isoformat(),
        "exit_price": _decimal(outcome.exit_price),
        "absolute_return": _decimal(outcome.absolute_return),
        "benchmark_return": _decimal(outcome.benchmark_return),
        "excess_return": _decimal(outcome.excess_return),
        "net_return": _decimal(outcome.net_return),
        "net_excess_return": _decimal(outcome.net_excess_return),
        "maximum_adverse_excursion": _decimal(outcome.maximum_adverse_excursion),
        "maximum_favorable_excursion": _decimal(outcome.maximum_favorable_excursion),
        "holding_period_drawdown": _decimal(outcome.holding_period_drawdown),
        "realized_volatility": _decimal(outcome.realized_volatility),
        "ranker_revision": outcome.ranker_revision,
        "feature_revision": outcome.feature_revision,
        "evaluation_revision": outcome.evaluation_revision,
        "outcome_revision": outcome.outcome_revision,
        "benchmark_symbol": outcome.benchmark_symbol,
        "benchmark_basis": outcome.benchmark_basis,
        "execution_timing": outcome.execution_timing,
        "cost_bps": _decimal(outcome.cost_bps),
        "adjustment_status": outcome.adjustment_status,
        "limitations": list(outcome.limitations),
        "stock_bar_revisions": list(outcome.stock_bar_revisions),
        "benchmark_bar_revisions": list(outcome.benchmark_bar_revisions),
        "schema_revision": outcome.schema_revision,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value: Decimal) -> str:
    """Canonicalize storage-scale differences without losing exact value."""
    return format(value.normalize(), "f")
