"""Provider-neutral domain values for deterministic computed features."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from shadow.domain.market import Instrument

FEATURE_IMPLEMENTATION_VERSION = "shadow.features.v1"


class FeatureError(ValueError):
    """Base class for an invalid feature contract or computation."""


class FeatureComputationError(FeatureError):
    """An explicit mathematical or configuration failure in feature computation."""

    def __init__(self, invariant: str, detail: str) -> None:
        self.invariant = invariant
        super().__init__(f"{invariant}: {detail}")


class FeatureName(StrEnum):
    """The small, explicit set of P0.2A close-price primitives."""

    SIMPLE_RETURN = "simple_return"
    ROLLING_MEAN = "rolling_mean"
    ROLLING_VARIANCE = "rolling_variance"
    ROLLING_STANDARD_DEVIATION = "rolling_standard_deviation"
    Z_SCORE = "z_score"


class FeatureInput(StrEnum):
    """Market value used by the initial feature kernel."""

    CLOSE = "close"


class FeatureState(StrEnum):
    """Whether a snapshot contains a usable numerical value."""

    WARMING_UP = "warming_up"
    READY = "ready"
    UNAVAILABLE = "unavailable"


class FeatureUnavailableReason(StrEnum):
    """A semantic reason a snapshot intentionally has no value."""

    INSUFFICIENT_HISTORY = "insufficient_history"
    ZERO_VARIANCE = "zero_variance"


def _canonical_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FeatureComputationError(field_name, "must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """One immutable feature result aligned to a completed market observation.

    ``source_dataset_id`` is the upstream declared dataset identity. It intentionally
    does not copy P0.1's whole-dataset fingerprint: appending later observations would
    otherwise rewrite the lineage field of an unchanged historical snapshot.
    """

    instrument: Instrument
    feature_name: FeatureName
    input_value: FeatureInput
    implementation_version: str
    value: Decimal | None
    observation_time: datetime
    availability_time: datetime
    window: int
    state: FeatureState
    unavailable_reason: FeatureUnavailableReason | None
    source_dataset_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise FeatureComputationError("instrument", "must be an Instrument")
        if not isinstance(self.feature_name, FeatureName):
            raise FeatureComputationError("feature_name", "must be a FeatureName")
        if not isinstance(self.input_value, FeatureInput):
            raise FeatureComputationError("input_value", "must be a FeatureInput")
        if not isinstance(self.state, FeatureState):
            raise FeatureComputationError("state", "must be a FeatureState")
        if self.unavailable_reason is not None and not isinstance(
            self.unavailable_reason, FeatureUnavailableReason
        ):
            raise FeatureComputationError(
                "unavailable_reason", "must be a FeatureUnavailableReason or None"
            )
        if (
            not self.implementation_version
            or self.implementation_version != self.implementation_version.strip()
        ):
            raise FeatureComputationError(
                "implementation_version", "must be a non-empty trimmed string"
            )
        if not self.source_dataset_id or self.source_dataset_id != self.source_dataset_id.strip():
            raise FeatureComputationError("source_dataset_id", "must be a non-empty trimmed string")
        if isinstance(self.window, bool) or not isinstance(self.window, int) or self.window <= 0:
            raise FeatureComputationError("window", "must be positive")
        observation_time = _canonical_utc(self.observation_time, "observation_time")
        availability_time = _canonical_utc(self.availability_time, "availability_time")
        object.__setattr__(self, "observation_time", observation_time)
        object.__setattr__(self, "availability_time", availability_time)
        if availability_time < observation_time:
            raise FeatureComputationError("availability_time", "must not precede observation_time")
        if self.state is FeatureState.READY:
            if self.value is None:
                raise FeatureComputationError("value", "ready snapshots require a value")
            if not isinstance(self.value, Decimal) or not self.value.is_finite():
                raise FeatureComputationError("value", "ready snapshots require a finite Decimal")
            if self.unavailable_reason is not None:
                raise FeatureComputationError(
                    "unavailable_reason", "ready snapshots cannot have an unavailable reason"
                )
        elif self.value is not None:
            raise FeatureComputationError("value", "non-ready snapshots must not contain a value")
        elif self.state is FeatureState.WARMING_UP:
            if self.unavailable_reason is not FeatureUnavailableReason.INSUFFICIENT_HISTORY:
                raise FeatureComputationError(
                    "unavailable_reason", "warm-up snapshots require insufficient_history"
                )
        elif self.unavailable_reason is not FeatureUnavailableReason.ZERO_VARIANCE:
            raise FeatureComputationError(
                "unavailable_reason", "P0.2A unavailable snapshots require zero_variance"
            )
