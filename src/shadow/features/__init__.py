"""Deterministic feature-domain contracts and close-price primitives."""

from shadow.features.kernel import (
    calculate_rolling_mean,
    calculate_rolling_standard_deviation,
    calculate_rolling_variance,
    calculate_simple_returns,
    calculate_z_scores,
)
from shadow.features.models import (
    FEATURE_IMPLEMENTATION_VERSION,
    FeatureComputationError,
    FeatureError,
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
    FeatureUnavailableReason,
)

__all__ = [
    "FEATURE_IMPLEMENTATION_VERSION",
    "FeatureComputationError",
    "FeatureError",
    "FeatureInput",
    "FeatureName",
    "FeatureSnapshot",
    "FeatureState",
    "FeatureUnavailableReason",
    "calculate_rolling_mean",
    "calculate_rolling_standard_deviation",
    "calculate_rolling_variance",
    "calculate_simple_returns",
    "calculate_z_scores",
]
