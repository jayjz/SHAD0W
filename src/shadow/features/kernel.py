"""Deterministic close-price feature primitives over validated bar sequences."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from shadow.data import validate_bars
from shadow.domain.market import Bar, DatasetMetadata, Instrument
from shadow.features.models import (
    FEATURE_IMPLEMENTATION_VERSION,
    FeatureComputationError,
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
    FeatureUnavailableReason,
)

_CALCULATION_PRECISION = 34


def _calculation_context() -> Context:
    """Return an isolated, fixed context rather than using a caller's Decimal context."""
    return Context(prec=_CALCULATION_PRECISION, rounding=ROUND_HALF_EVEN)


def _canonical_value(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise FeatureComputationError(
            "non_finite_output", "calculation produced a non-finite value"
        )
    if value.is_zero():
        return Decimal(0)
    return Decimal(format(value.normalize(), "f"))


def _validate_window(window: int) -> None:
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise FeatureComputationError("window", "must be a positive integer")


def _feature_windows(bars: Sequence[Bar], window: int) -> Iterator[tuple[Bar, tuple[Bar, ...]]]:
    """Yield each bar and its trailing same-instrument window without sorting input."""
    histories: dict[Instrument, list[Bar]] = {}
    for bar in bars:
        history = histories.setdefault(bar.instrument, [])
        history.append(bar)
        yield bar, tuple(history[-window:])


def _availability(required_inputs: Sequence[Bar]) -> datetime:
    return max(item.availability_time for item in required_inputs)


def _snapshot(
    *,
    bar: Bar,
    required_inputs: Sequence[Bar],
    feature_name: FeatureName,
    window: int,
    metadata: DatasetMetadata,
    state: FeatureState,
    value: Decimal | None = None,
    unavailable_reason: FeatureUnavailableReason | None = None,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        instrument=bar.instrument,
        feature_name=feature_name,
        input_value=FeatureInput.CLOSE,
        implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        value=value,
        observation_time=bar.observation_time,
        availability_time=_availability(required_inputs),
        window=window,
        state=state,
        unavailable_reason=unavailable_reason,
        source_dataset_id=metadata.dataset_id,
    )


def _warming_snapshot(
    bar: Bar,
    required_inputs: Sequence[Bar],
    feature_name: FeatureName,
    window: int,
    metadata: DatasetMetadata,
) -> FeatureSnapshot:
    return _snapshot(
        bar=bar,
        required_inputs=required_inputs,
        feature_name=feature_name,
        window=window,
        metadata=metadata,
        state=FeatureState.WARMING_UP,
        unavailable_reason=FeatureUnavailableReason.INSUFFICIENT_HISTORY,
    )


def _population_statistics(values: Sequence[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    """Return population mean, variance (ddof=0), and standard deviation."""
    with localcontext(_calculation_context()):
        count = Decimal(len(values))
        mean = sum(values, Decimal(0)) / count
        variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / count
        standard_deviation = variance.sqrt()
        return (
            _canonical_value(mean),
            _canonical_value(variance),
            _canonical_value(standard_deviation),
        )


def calculate_simple_returns(
    bars: Sequence[Bar], metadata: DatasetMetadata
) -> tuple[FeatureSnapshot, ...]:
    """Calculate one-period simple close return, ``(P_t / P_t-1) - 1``.

    The first observation for each instrument is explicitly warming up. A zero prior
    close fails rather than being repaired or converted to a non-finite output.
    """
    validate_bars(bars, metadata)
    snapshots: list[FeatureSnapshot] = []
    for bar, required_inputs in _feature_windows(bars, window=2):
        if len(required_inputs) < 2:
            snapshots.append(
                _warming_snapshot(bar, required_inputs, FeatureName.SIMPLE_RETURN, 2, metadata)
            )
            continue
        prior_close = required_inputs[0].close
        if prior_close.is_zero():
            raise FeatureComputationError(
                "return_denominator", "simple return requires a non-zero prior close"
            )
        with localcontext(_calculation_context()):
            value = _canonical_value((bar.close / prior_close) - Decimal(1))
        snapshots.append(
            _snapshot(
                bar=bar,
                required_inputs=required_inputs,
                feature_name=FeatureName.SIMPLE_RETURN,
                window=2,
                metadata=metadata,
                state=FeatureState.READY,
                value=value,
            )
        )
    return tuple(snapshots)


def calculate_rolling_mean(
    bars: Sequence[Bar], metadata: DatasetMetadata, *, window: int
) -> tuple[FeatureSnapshot, ...]:
    """Calculate strict full-window population rolling means of bar close prices."""
    return _calculate_rolling_statistic(bars, metadata, window, FeatureName.ROLLING_MEAN, 0)


def calculate_rolling_variance(
    bars: Sequence[Bar], metadata: DatasetMetadata, *, window: int
) -> tuple[FeatureSnapshot, ...]:
    """Calculate strict full-window population rolling close variance (ddof=0)."""
    return _calculate_rolling_statistic(bars, metadata, window, FeatureName.ROLLING_VARIANCE, 1)


def calculate_rolling_standard_deviation(
    bars: Sequence[Bar], metadata: DatasetMetadata, *, window: int
) -> tuple[FeatureSnapshot, ...]:
    """Calculate strict full-window population rolling close standard deviation."""
    return _calculate_rolling_statistic(
        bars, metadata, window, FeatureName.ROLLING_STANDARD_DEVIATION, 2
    )


def _calculate_rolling_statistic(
    bars: Sequence[Bar],
    metadata: DatasetMetadata,
    window: int,
    feature_name: FeatureName,
    statistic_index: int,
) -> tuple[FeatureSnapshot, ...]:
    _validate_window(window)
    validate_bars(bars, metadata)
    snapshots: list[FeatureSnapshot] = []
    for bar, required_inputs in _feature_windows(bars, window):
        if len(required_inputs) < window:
            snapshots.append(
                _warming_snapshot(bar, required_inputs, feature_name, window, metadata)
            )
            continue
        value = _population_statistics([item.close for item in required_inputs])[statistic_index]
        snapshots.append(
            _snapshot(
                bar=bar,
                required_inputs=required_inputs,
                feature_name=feature_name,
                window=window,
                metadata=metadata,
                state=FeatureState.READY,
                value=value,
            )
        )
    return tuple(snapshots)


def calculate_z_scores(
    bars: Sequence[Bar], metadata: DatasetMetadata, *, window: int
) -> tuple[FeatureSnapshot, ...]:
    """Calculate strict full-window close z-scores using population standard deviation.

    A zero standard deviation produces an explicit unavailable snapshot rather than
    an infinity, NaN, or fabricated numerical value.
    """
    _validate_window(window)
    validate_bars(bars, metadata)
    snapshots: list[FeatureSnapshot] = []
    for bar, required_inputs in _feature_windows(bars, window):
        if len(required_inputs) < window:
            snapshots.append(
                _warming_snapshot(bar, required_inputs, FeatureName.Z_SCORE, window, metadata)
            )
            continue
        mean, _, standard_deviation = _population_statistics(
            [item.close for item in required_inputs]
        )
        if standard_deviation.is_zero():
            snapshots.append(
                _snapshot(
                    bar=bar,
                    required_inputs=required_inputs,
                    feature_name=FeatureName.Z_SCORE,
                    window=window,
                    metadata=metadata,
                    state=FeatureState.UNAVAILABLE,
                    unavailable_reason=FeatureUnavailableReason.ZERO_VARIANCE,
                )
            )
            continue
        with localcontext(_calculation_context()):
            value = _canonical_value((bar.close - mean) / standard_deviation)
        snapshots.append(
            _snapshot(
                bar=bar,
                required_inputs=required_inputs,
                feature_name=FeatureName.Z_SCORE,
                window=window,
                metadata=metadata,
                state=FeatureState.READY,
                value=value,
            )
        )
    return tuple(snapshots)
