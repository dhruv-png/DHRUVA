"""Public API of the C3 ``analytics`` context.

This module is the **only** import surface other contexts may use. Importing
``dhruva.contexts.analytics.domain``, ``.application``, ``.infrastructure`` or
``.interfaces`` from another context is a build failure.

Re-export here the provider-neutral technical feature facts and pure calculator.
"""

from __future__ import annotations

from dhruva.contexts.analytics.application import technical_series_from_daily_bars
from dhruva.contexts.analytics.domain import (
    CANDIDATE_OUTCOME_REVISION,
    DEFAULT_EVALUATION_HORIZONS,
    TECHNICAL_FEATURE_REVISION,
    AdjustmentEvidence,
    BenchmarkBasis,
    BenchmarkRegime,
    FeatureName,
    FeatureStatus,
    FutureOutcome,
    OutcomeStatus,
    TechnicalBar,
    TechnicalFeature,
    TechnicalFeatureSet,
    TechnicalSeries,
    calculate_future_outcome,
    compute_technical_features,
    unavailable_technical_features,
)

__all__ = [
    "CANDIDATE_OUTCOME_REVISION",
    "DEFAULT_EVALUATION_HORIZONS",
    "TECHNICAL_FEATURE_REVISION",
    "AdjustmentEvidence",
    "BenchmarkBasis",
    "BenchmarkRegime",
    "FeatureName",
    "FeatureStatus",
    "FutureOutcome",
    "OutcomeStatus",
    "TechnicalBar",
    "TechnicalFeature",
    "TechnicalFeatureSet",
    "TechnicalSeries",
    "calculate_future_outcome",
    "compute_technical_features",
    "technical_series_from_daily_bars",
    "unavailable_technical_features",
]
