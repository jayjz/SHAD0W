"""Behavioral invariants for the deterministic P0.2A feature kernel."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest

from shadow.data import dataset_fingerprint
from shadow.domain import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    DatasetMetadata,
    Instrument,
    MarketDataValidationError,
    Provenance,
    ValidationStatus,
)
from shadow.features import (
    FeatureComputationError,
    FeatureSnapshot,
    FeatureState,
    FeatureUnavailableReason,
    calculate_rolling_mean,
    calculate_rolling_standard_deviation,
    calculate_rolling_variance,
    calculate_simple_returns,
    calculate_z_scores,
)

FeatureCalculator = Callable[[Sequence[Bar], DatasetMetadata], tuple[FeatureSnapshot, ...]]


def _bars(
    closes: list[str], *, availability_times: list[datetime] | None = None
) -> tuple[list[Bar], DatasetMetadata]:
    start = datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
    interval = BarInterval(timedelta(minutes=1))
    instrument = Instrument("SPY")
    bars = [
        Bar(
            instrument=instrument,
            interval=interval,
            observation_time=start + timedelta(minutes=index),
            availability_time=(
                availability_times[index]
                if availability_times is not None
                else start + timedelta(minutes=index, seconds=1)
            ),
            availability_semantics=AvailabilitySemantics.PROVIDER_PUBLISHED,
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal("1"),
            provenance=Provenance("synthetic"),
        )
        for index, close in enumerate(closes)
    ]
    metadata = DatasetMetadata(
        source="synthetic",
        dataset_id="p02-close-fixture",
        instruments=(instrument,),
        interval=interval,
        coverage_start=start,
        coverage_end=start + timedelta(minutes=len(bars) - 1),
        validation_status=ValidationStatus.VALIDATED,
    )
    return bars, metadata


def test_simple_return_uses_the_documented_manual_formula() -> None:
    bars, metadata = _bars(["100", "110", "99"])

    snapshots = calculate_simple_returns(bars, metadata)

    assert snapshots[0].state is FeatureState.WARMING_UP
    assert snapshots[0].value is None
    assert snapshots[1].value == Decimal("0.1")
    assert snapshots[2].value == Decimal("-0.1")


def test_rolling_mean_uses_a_strict_full_window() -> None:
    bars, metadata = _bars(["1", "2", "3"])

    snapshots = calculate_rolling_mean(bars, metadata, window=3)

    assert [item.state for item in snapshots] == [
        FeatureState.WARMING_UP,
        FeatureState.WARMING_UP,
        FeatureState.READY,
    ]
    assert [item.value for item in snapshots] == [None, None, Decimal("2")]
    assert all(
        item.unavailable_reason is FeatureUnavailableReason.INSUFFICIENT_HISTORY
        for item in snapshots[:2]
    )


def test_population_variance_and_standard_deviation_have_explicit_known_values() -> None:
    bars, metadata = _bars(["1", "3"])

    variance = calculate_rolling_variance(bars, metadata, window=2)
    standard_deviation = calculate_rolling_standard_deviation(bars, metadata, window=2)

    # The population variance is ((1 - 2)^2 + (3 - 2)^2) / 2 == 1;
    # the sample-variance alternative would have been 2.
    assert variance[-1].value == Decimal("1")
    assert standard_deviation[-1].value == Decimal("1")


def test_z_score_uses_current_close_and_population_standard_deviation() -> None:
    bars, metadata = _bars(["1", "3"])

    snapshots = calculate_z_scores(bars, metadata, window=2)

    assert snapshots[0].state is FeatureState.WARMING_UP
    assert snapshots[-1].state is FeatureState.READY
    assert snapshots[-1].value == Decimal("1")


def test_zero_variance_is_explicitly_unavailable_not_non_finite() -> None:
    bars, metadata = _bars(["2", "2"])

    snapshot = calculate_z_scores(bars, metadata, window=2)[-1]

    assert snapshot.state is FeatureState.UNAVAILABLE
    assert snapshot.value is None
    assert snapshot.unavailable_reason is FeatureUnavailableReason.ZERO_VARIANCE


def test_feature_availability_is_the_latest_required_input_availability() -> None:
    late_first_availability = datetime(2024, 1, 2, 14, 40, tzinfo=UTC)
    second_availability = datetime(2024, 1, 2, 14, 32, tzinfo=UTC)
    bars, metadata = _bars(
        ["1", "3"], availability_times=[late_first_availability, second_availability]
    )

    snapshot = calculate_rolling_mean(bars, metadata, window=2)[-1]

    assert snapshot.availability_time == late_first_availability


@pytest.mark.parametrize(
    "calculator",
    [
        lambda bars, metadata: calculate_simple_returns(bars, metadata),
        lambda bars, metadata: calculate_rolling_mean(bars, metadata, window=2),
        lambda bars, metadata: calculate_rolling_variance(bars, metadata, window=2),
        lambda bars, metadata: calculate_rolling_standard_deviation(bars, metadata, window=2),
        lambda bars, metadata: calculate_z_scores(bars, metadata, window=2),
    ],
)
def test_appending_future_observations_cannot_change_existing_snapshots(
    calculator: FeatureCalculator,
) -> None:
    bars, metadata = _bars(["1", "3", "5"])
    future = replace(
        bars[-1],
        observation_time=datetime(2024, 1, 2, 14, 34, tzinfo=UTC),
        availability_time=datetime(2024, 1, 2, 14, 34, 1, tzinfo=UTC),
        open=Decimal("7"),
        high=Decimal("7"),
        low=Decimal("7"),
        close=Decimal("7"),
    )
    expanded_metadata = replace(metadata, coverage_end=future.observation_time)

    original = calculator(bars, metadata)
    expanded = calculator([*bars, future], expanded_metadata)

    assert original == expanded[: len(original)]


def test_repeated_computation_is_identical() -> None:
    bars, metadata = _bars(["1", "2", "5", "3"])

    assert calculate_z_scores(bars, metadata, window=3) == calculate_z_scores(
        bars, metadata, window=3
    )


def test_calculation_uses_its_fixed_decimal_context_not_the_callers_context() -> None:
    bars, metadata = _bars(["3", "1"])

    with localcontext() as low_precision_context:
        low_precision_context.prec = 6
        low_precision = calculate_simple_returns(bars, metadata)
    with localcontext() as high_precision_context:
        high_precision_context.prec = 60
        high_precision = calculate_simple_returns(bars, metadata)

    assert low_precision == high_precision


def test_calculation_does_not_change_p01_market_data_identity() -> None:
    bars, metadata = _bars(["1", "2", "3"])
    before = dataset_fingerprint(bars, metadata)

    calculate_rolling_mean(bars, metadata, window=2)

    assert dataset_fingerprint(bars, metadata) == before


def test_p01_sequence_validation_is_reused_without_sorting() -> None:
    bars, metadata = _bars(["1", "2"])

    with pytest.raises(MarketDataValidationError, match="chronological_order"):
        calculate_rolling_mean([bars[1], bars[0]], metadata, window=2)


def test_invalid_return_domain_and_window_fail_explicitly() -> None:
    bars, metadata = _bars(["0", "1"])

    with pytest.raises(FeatureComputationError, match="return_denominator"):
        calculate_simple_returns(bars, metadata)
    with pytest.raises(FeatureComputationError, match="window"):
        calculate_rolling_mean(bars, metadata, window=0)
