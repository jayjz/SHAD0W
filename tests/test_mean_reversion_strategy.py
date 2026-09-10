"""Decision-boundary tests for the P0.3 mean-reversion hypothesis."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shadow.domain import Instrument
from shadow.features import (
    FEATURE_IMPLEMENTATION_VERSION,
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
    FeatureUnavailableReason,
)
from shadow.strategies import (
    MEAN_REVERSION_STRATEGY_ID,
    MEAN_REVERSION_STRATEGY_VERSION,
    MeanReversionConfig,
    PositionState,
    Signal,
    SignalReason,
    SignalType,
    StrategyContractError,
    evaluate_mean_reversion,
)

TIME = datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
INSTRUMENT = Instrument("SPY")


def _config(**changes: object) -> MeanReversionConfig:
    values: dict[str, object] = {
        "instrument": INSTRUMENT,
        "configuration_id": "mean-reversion-example-v1",
        "rolling_window": 20,
        "entry_threshold": Decimal("-2"),
        "exit_threshold": Decimal("0"),
        "maximum_feature_age": timedelta(minutes=1),
    }
    values.update(changes)
    return MeanReversionConfig(**values)  # type: ignore[arg-type]


def _snapshot(
    value: Decimal | None = Decimal("-2"),
    *,
    state: FeatureState = FeatureState.READY,
    availability_time: datetime = TIME,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        instrument=INSTRUMENT,
        feature_name=FeatureName.Z_SCORE,
        input_value=FeatureInput.CLOSE,
        implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        value=value,
        observation_time=TIME,
        availability_time=availability_time,
        window=20,
        state=state,
        unavailable_reason=(
            None
            if state is FeatureState.READY
            else FeatureUnavailableReason.INSUFFICIENT_HISTORY
            if state is FeatureState.WARMING_UP
            else FeatureUnavailableReason.ZERO_VARIANCE
        ),
        source_dataset_id="synthetic-mean-reversion-fixture",
    )


def _evaluate(
    snapshot: FeatureSnapshot | None, position_state: PositionState = PositionState.FLAT
) -> Signal | None:
    return evaluate_mean_reversion(snapshot, _config(), position_state, decision_time=TIME)


def test_entry_occurs_exactly_at_the_configured_threshold() -> None:
    signal = _evaluate(_snapshot(Decimal("-2")))

    assert signal is not None
    assert signal.signal_type is SignalType.LONG_ENTRY
    assert signal.reason is SignalReason.ENTRY_THRESHOLD


def test_entry_occurs_beyond_threshold_but_not_inside_it() -> None:
    assert _evaluate(_snapshot(Decimal("-2.1"))) is not None
    assert _evaluate(_snapshot(Decimal("-1.9"))) is None


def test_warming_up_and_unavailable_features_cannot_produce_actionable_signals() -> None:
    assert _evaluate(_snapshot(None, state=FeatureState.WARMING_UP)) is None
    assert _evaluate(_snapshot(None, state=FeatureState.UNAVAILABLE)) is None


def test_future_or_stale_feature_evidence_cannot_produce_an_earlier_signal() -> None:
    future_snapshot = _snapshot(availability_time=TIME + timedelta(seconds=1))
    stale_snapshot = replace(_snapshot(), observation_time=TIME - timedelta(minutes=2))

    assert _evaluate(future_snapshot) is None
    assert _evaluate(stale_snapshot) is None


def test_exit_obeys_its_configured_threshold_and_requires_holding_context() -> None:
    signal = _evaluate(_snapshot(Decimal("0")), PositionState.HOLDING)

    assert signal is not None
    assert signal.signal_type is SignalType.EXIT
    assert signal.reason is SignalReason.EXIT_THRESHOLD
    assert _evaluate(_snapshot(Decimal("-0.1")), PositionState.HOLDING) is None
    assert _evaluate(_snapshot(Decimal("0")), PositionState.FLAT) is None


def test_repeated_evaluation_and_feature_prefix_are_stable() -> None:
    original = _snapshot()
    future = _snapshot(Decimal("-3"), availability_time=TIME + timedelta(minutes=1))

    assert _evaluate(original) == _evaluate(original)
    assert [_evaluate(item) for item in (original,)] == [
        _evaluate(item) for item in (original, future)[:1]
    ]


def test_signal_preserves_reconstructable_evidence_without_execution_artifacts() -> None:
    signal = _evaluate(_snapshot())

    assert signal is not None
    assert signal.instrument == INSTRUMENT
    assert signal.strategy_id == MEAN_REVERSION_STRATEGY_ID
    assert signal.strategy_version == MEAN_REVERSION_STRATEGY_VERSION
    assert signal.decision_time == TIME
    assert signal.availability_time == TIME
    assert signal.feature_name is FeatureName.Z_SCORE
    assert signal.feature_window == 20
    assert signal.feature_observation_time == TIME
    assert signal.feature_availability_time == TIME
    assert signal.observed_feature_value == Decimal("-2")
    assert signal.configuration_id == "mean-reversion-example-v1"
    assert signal.entry_threshold == Decimal("-2")
    assert signal.exit_threshold == Decimal("0")
    assert signal.maximum_feature_age == timedelta(minutes=1)
    assert signal.source_dataset_id == "synthetic-mean-reversion-fixture"
    assert set(signal.__dataclass_fields__) == {
        "instrument",
        "strategy_id",
        "strategy_version",
        "signal_type",
        "decision_time",
        "availability_time",
        "feature_name",
        "feature_input",
        "feature_implementation_version",
        "feature_window",
        "feature_observation_time",
        "feature_availability_time",
        "observed_feature_value",
        "configuration_id",
        "entry_threshold",
        "exit_threshold",
        "maximum_feature_age",
        "reason",
        "source_dataset_id",
    }


def test_configuration_is_immutable_and_rejects_invalid_semantics() -> None:
    config = _config()

    with pytest.raises(AttributeError):
        config.entry_threshold = Decimal("-3")  # type: ignore[misc]
    with pytest.raises(StrategyContractError, match="entry_threshold"):
        _config(entry_threshold=Decimal("0"), exit_threshold=Decimal("0"))
    with pytest.raises(StrategyContractError, match="rolling_window"):
        _config(rolling_window=0)
    with pytest.raises(StrategyContractError, match="feature_name"):
        _config(feature_name=FeatureName.ROLLING_MEAN)


def test_wrong_instrument_feature_version_or_missing_feature_fails_closed() -> None:
    assert _evaluate(None) is None
    assert _evaluate(replace(_snapshot(), instrument=Instrument("QQQ"))) is None
    assert _evaluate(replace(_snapshot(), implementation_version="other.v1")) is None


def test_decision_time_must_be_explicit_and_timezone_aware() -> None:
    with pytest.raises(StrategyContractError, match="timezone-aware"):
        evaluate_mean_reversion(
            _snapshot(), _config(), PositionState.FLAT, decision_time=datetime(2024, 1, 2)
        )
