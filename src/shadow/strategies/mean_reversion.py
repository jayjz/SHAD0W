"""One deterministic, unvalidated close z-score mean-reversion hypothesis."""

from __future__ import annotations

from datetime import UTC, datetime

from shadow.features import FeatureSnapshot, FeatureState
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


def _canonical_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StrategyContractError("decision_time must be timezone-aware")
    return value.astimezone(UTC)


def evaluate_mean_reversion(
    snapshot: FeatureSnapshot | None,
    config: MeanReversionConfig,
    position_state: PositionState,
    *,
    decision_time: datetime,
) -> Signal | None:
    """Propose an entry or exit only from legal, matching, ready z-score evidence.

    ``position_state`` is declared by future lifecycle orchestration and has no
    authority beyond selecting the relevant rule. This function creates neither an
    order nor a position and carries no mutable state between calls.
    """
    if not isinstance(config, MeanReversionConfig):
        raise StrategyContractError("config must be a MeanReversionConfig")
    if not isinstance(position_state, PositionState):
        raise StrategyContractError("position_state must be a PositionState")
    canonical_decision_time = _canonical_utc(decision_time)
    if snapshot is None:
        return None
    if (
        snapshot.instrument != config.instrument
        or snapshot.feature_name is not config.feature_name
        or snapshot.input_value is not config.feature_input
        or snapshot.implementation_version != config.feature_implementation_version
        or snapshot.window != config.rolling_window
        or snapshot.state is not FeatureState.READY
        or snapshot.value is None
        or snapshot.availability_time > canonical_decision_time
        or canonical_decision_time - snapshot.observation_time > config.maximum_feature_age
    ):
        return None

    if position_state is PositionState.FLAT and snapshot.value <= config.entry_threshold:
        signal_type = SignalType.LONG_ENTRY
        reason = SignalReason.ENTRY_THRESHOLD
    elif position_state is PositionState.HOLDING and snapshot.value >= config.exit_threshold:
        signal_type = SignalType.EXIT
        reason = SignalReason.EXIT_THRESHOLD
    else:
        return None

    return Signal(
        instrument=snapshot.instrument,
        strategy_id=MEAN_REVERSION_STRATEGY_ID,
        strategy_version=MEAN_REVERSION_STRATEGY_VERSION,
        signal_type=signal_type,
        decision_time=canonical_decision_time,
        availability_time=canonical_decision_time,
        feature_name=snapshot.feature_name,
        feature_input=snapshot.input_value,
        feature_implementation_version=snapshot.implementation_version,
        feature_window=snapshot.window,
        feature_observation_time=snapshot.observation_time,
        feature_availability_time=snapshot.availability_time,
        observed_feature_value=snapshot.value,
        configuration_id=config.configuration_id,
        entry_threshold=config.entry_threshold,
        exit_threshold=config.exit_threshold,
        maximum_feature_age=config.maximum_feature_age,
        reason=reason,
        source_dataset_id=snapshot.source_dataset_id,
    )
