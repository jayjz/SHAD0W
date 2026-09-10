"""Provider-neutral contracts for deterministic strategy proposals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from shadow.domain.market import Instrument
from shadow.features import FEATURE_IMPLEMENTATION_VERSION, FeatureInput, FeatureName

MEAN_REVERSION_STRATEGY_ID = "mean_reversion_z_score"
MEAN_REVERSION_STRATEGY_VERSION = "shadow.strategies.mean_reversion.v1"


class StrategyContractError(ValueError):
    """An invalid strategy configuration or signal contract."""


class PositionState(StrEnum):
    """Non-authoritative lifecycle context supplied by later orchestration."""

    FLAT = "flat"
    HOLDING = "holding"


class SignalType(StrEnum):
    """A proposal; it is not an order, fill, or authorization."""

    LONG_ENTRY = "long_entry"
    EXIT = "exit"


class SignalReason(StrEnum):
    """The decision rule which made a proposal actionable."""

    ENTRY_THRESHOLD = "entry_threshold"
    EXIT_THRESHOLD = "exit_threshold"


def _canonical_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StrategyContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _nonempty_trimmed(value: str, field_name: str) -> None:
    if not value or value != value.strip():
        raise StrategyContractError(f"{field_name} must be a non-empty trimmed string")


@dataclass(frozen=True, slots=True)
class MeanReversionConfig:
    """Fixed parameters for the unvalidated close z-score hypothesis.

    ``maximum_feature_age`` is a data-freshness rule, not a holding horizon. P0.3
    deliberately does not model a maximum holding horizon because it has no
    authoritative position clock; P0.4 must supply that lifecycle context.
    """

    instrument: Instrument
    configuration_id: str
    rolling_window: int
    entry_threshold: Decimal
    exit_threshold: Decimal
    maximum_feature_age: timedelta
    feature_name: FeatureName = FeatureName.Z_SCORE
    feature_input: FeatureInput = FeatureInput.CLOSE
    feature_implementation_version: str = FEATURE_IMPLEMENTATION_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise StrategyContractError("instrument must be an Instrument")
        _nonempty_trimmed(self.configuration_id, "configuration_id")
        if (
            isinstance(self.rolling_window, bool)
            or not isinstance(self.rolling_window, int)
            or self.rolling_window <= 0
        ):
            raise StrategyContractError("rolling_window must be a positive integer")
        for threshold_name, threshold_value in (
            ("entry_threshold", self.entry_threshold),
            ("exit_threshold", self.exit_threshold),
        ):
            if not isinstance(threshold_value, Decimal) or not threshold_value.is_finite():
                raise StrategyContractError(f"{threshold_name} must be a finite Decimal")
        if self.entry_threshold >= self.exit_threshold:
            raise StrategyContractError("entry_threshold must be less than exit_threshold")
        if not isinstance(
            self.maximum_feature_age, timedelta
        ) or self.maximum_feature_age < timedelta(0):
            raise StrategyContractError("maximum_feature_age must be a non-negative timedelta")
        if self.feature_name is not FeatureName.Z_SCORE:
            raise StrategyContractError("feature_name must be z_score")
        if self.feature_input is not FeatureInput.CLOSE:
            raise StrategyContractError("feature_input must be close")
        if self.feature_implementation_version != FEATURE_IMPLEMENTATION_VERSION:
            raise StrategyContractError("feature_implementation_version is not supported")


@dataclass(frozen=True, slots=True)
class Signal:
    """Immutable strategy evidence for a later independent risk decision."""

    instrument: Instrument
    strategy_id: str
    strategy_version: str
    signal_type: SignalType
    decision_time: datetime
    availability_time: datetime
    feature_name: FeatureName
    feature_input: FeatureInput
    feature_implementation_version: str
    feature_window: int
    feature_observation_time: datetime
    feature_availability_time: datetime
    observed_feature_value: Decimal
    configuration_id: str
    entry_threshold: Decimal
    exit_threshold: Decimal
    maximum_feature_age: timedelta
    reason: SignalReason
    source_dataset_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise StrategyContractError("instrument must be an Instrument")
        for name, value in (
            ("strategy_id", self.strategy_id),
            ("strategy_version", self.strategy_version),
            ("feature_implementation_version", self.feature_implementation_version),
            ("configuration_id", self.configuration_id),
            ("source_dataset_id", self.source_dataset_id),
        ):
            _nonempty_trimmed(value, name)
        if not isinstance(self.signal_type, SignalType):
            raise StrategyContractError("signal_type must be a SignalType")
        if not isinstance(self.reason, SignalReason):
            raise StrategyContractError("reason must be a SignalReason")
        if not isinstance(self.feature_name, FeatureName) or not isinstance(
            self.feature_input, FeatureInput
        ):
            raise StrategyContractError("feature identity must use feature enums")
        if (
            isinstance(self.feature_window, bool)
            or not isinstance(self.feature_window, int)
            or self.feature_window <= 0
        ):
            raise StrategyContractError("feature_window must be a positive integer")
        if (
            not isinstance(self.observed_feature_value, Decimal)
            or not self.observed_feature_value.is_finite()
        ):
            raise StrategyContractError("observed_feature_value must be a finite Decimal")
        for threshold_name, threshold_value in (
            ("entry_threshold", self.entry_threshold),
            ("exit_threshold", self.exit_threshold),
        ):
            if not isinstance(threshold_value, Decimal) or not threshold_value.is_finite():
                raise StrategyContractError(f"{threshold_name} must be a finite Decimal")
        if self.entry_threshold >= self.exit_threshold:
            raise StrategyContractError("entry_threshold must be less than exit_threshold")
        if not isinstance(
            self.maximum_feature_age, timedelta
        ) or self.maximum_feature_age < timedelta(0):
            raise StrategyContractError("maximum_feature_age must be a non-negative timedelta")
        decision_time = _canonical_utc(self.decision_time, "decision_time")
        availability_time = _canonical_utc(self.availability_time, "availability_time")
        feature_observation_time = _canonical_utc(
            self.feature_observation_time, "feature_observation_time"
        )
        feature_availability_time = _canonical_utc(
            self.feature_availability_time, "feature_availability_time"
        )
        object.__setattr__(self, "decision_time", decision_time)
        object.__setattr__(self, "availability_time", availability_time)
        object.__setattr__(self, "feature_observation_time", feature_observation_time)
        object.__setattr__(self, "feature_availability_time", feature_availability_time)
        if availability_time != decision_time:
            raise StrategyContractError("availability_time must equal decision_time")
        if feature_availability_time > decision_time:
            raise StrategyContractError("feature_availability_time must not follow decision_time")
