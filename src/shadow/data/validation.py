"""Fail-closed validation for chronological market-data collections."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import Bar, DatasetMetadata, Quote


@dataclass(frozen=True, slots=True)
class DatasetValidationReport:
    """Successful validation evidence for one immutable input collection."""

    record_count: int
    observation_kind: str


def _validate_common(
    observations: Sequence[Bar | Quote],
    metadata: DatasetMetadata,
    observation_kind: str,
) -> DatasetValidationReport:
    if not observations:
        raise MarketDataValidationError("dataset_records", "must contain at least one observation")

    previous_time: datetime | None = None
    identities: set[tuple[object, ...]] = set()
    for index, observation in enumerate(observations):
        if previous_time is not None and observation.observation_time < previous_time:
            raise MarketDataValidationError(
                "chronological_order",
                "observations must be supplied in nondecreasing observation_time",
                index,
            )
        previous_time = observation.observation_time
        if observation.instrument not in metadata.instruments:
            raise MarketDataValidationError(
                "instrument_scope",
                f"{observation.instrument.identifier!r} is absent from metadata",
                index,
            )
        if observation.provenance.source != metadata.source:
            raise MarketDataValidationError(
                "provenance_source", "observation source must equal metadata source", index
            )
        if not metadata.coverage_start <= observation.observation_time <= metadata.coverage_end:
            raise MarketDataValidationError(
                "coverage", "observation_time is outside declared metadata coverage", index
            )
        identity: tuple[object, ...]
        if isinstance(observation, Bar):
            identity = (
                observation.instrument.identifier,
                observation.interval.microseconds,
                observation.observation_time,
            )
        else:
            identity = (observation.instrument.identifier, observation.observation_time)
        if identity in identities:
            raise MarketDataValidationError(
                "duplicate_observation", "duplicate observation identity", index
            )
        identities.add(identity)
    return DatasetValidationReport(
        record_count=len(observations), observation_kind=observation_kind
    )


def validate_bars(bars: Sequence[Bar], metadata: DatasetMetadata) -> DatasetValidationReport:
    """Validate bars without sorting, deduplicating, or repairing input."""
    if metadata.interval is None:
        raise MarketDataValidationError("dataset_interval", "bar metadata must declare an interval")
    for index, bar in enumerate(bars):
        if bar.interval != metadata.interval:
            raise MarketDataValidationError(
                "interval_consistency", "bar interval must equal metadata interval", index
            )
    return _validate_common(bars, metadata, "bar")


def validate_quotes(quotes: Sequence[Quote], metadata: DatasetMetadata) -> DatasetValidationReport:
    """Validate quotes without sorting, deduplicating, or repairing input."""
    if metadata.interval is not None:
        raise MarketDataValidationError(
            "dataset_interval", "quote metadata must not declare a bar interval"
        )
    return _validate_common(quotes, metadata, "quote")
