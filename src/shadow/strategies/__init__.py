"""Explicit strategy hypotheses that emit provider-neutral signals only."""

from shadow.strategies.mean_reversion import evaluate_mean_reversion
from shadow.strategies.models import (
    MEAN_REVERSION_STRATEGY_ID,
    MEAN_REVERSION_STRATEGY_VERSION,
    MeanReversionConfig,
    PositionState,
    Signal,
    SignalReason,
    SignalType,
    StrategyContractError,
)

__all__ = [
    "MEAN_REVERSION_STRATEGY_ID",
    "MEAN_REVERSION_STRATEGY_VERSION",
    "MeanReversionConfig",
    "PositionState",
    "Signal",
    "SignalReason",
    "SignalType",
    "StrategyContractError",
    "evaluate_mean_reversion",
]
