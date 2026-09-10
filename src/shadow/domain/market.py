"""Typed, provider-neutral market-data values and their local invariants."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Self

from shadow.domain.errors import MarketDataValidationError

DATASET_SCHEMA_VERSION = "shadow.market-data.v1"


class AvailabilitySemantics(StrEnum):
    """How ``availability_time`` was established for an observation."""

    PROVIDER_PUBLISHED = "provider_published"
    SYSTEM_RECEIVED = "system_received"
    MODELED = "modeled"


class ValidationStatus(StrEnum):
    """The declared validation state of a dataset delivery."""

    UNVALIDATED = "unvalidated"
    VALIDATED = "validated"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A provider-neutral instrument identifier within a declared dataset scope."""

    identifier: str

    def __post_init__(self) -> None:
        if not self.identifier or self.identifier != self.identifier.strip():
            raise MarketDataValidationError(
                "instrument_identifier", "identifier must be a non-empty trimmed string"
            )


@dataclass(frozen=True, slots=True)
class BarInterval:
    """The explicit elapsed duration represented by a bar."""

    duration: timedelta

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0):
            raise MarketDataValidationError("bar_interval", "duration must be positive")

    @property
    def microseconds(self) -> int:
        """Return the duration as an exact integer, avoiding float serialization."""
        return (
            self.duration.days * 86_400_000_000
            + self.duration.seconds * 1_000_000
            + self.duration.microseconds
        )


@dataclass(frozen=True, slots=True)
class Provenance:
    """Source context retained alongside one market observation."""

    source: str
    source_timezone: str | None = None
    session: str | None = None

    def __post_init__(self) -> None:
        if not self.source or self.source != self.source.strip():
            raise MarketDataValidationError(
                "provenance_source", "source must be a non-empty trimmed string"
            )


def _canonical_utc(timestamp: datetime, field_name: str) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise MarketDataValidationError(
            "timestamp_awareness", f"{field_name} must be timezone-aware"
        )
    return timestamp.astimezone(UTC)


def _finite_decimal(value: Decimal, field_name: str, *, non_negative: bool = False) -> None:
    if not isinstance(value, Decimal):
        raise MarketDataValidationError(field_name, "must be Decimal at the domain boundary")
    if not value.is_finite():
        raise MarketDataValidationError(field_name, "must be finite")
    if non_negative and value < Decimal(0):
        raise MarketDataValidationError(field_name, "must be non-negative")


@dataclass(frozen=True, slots=True)
class Bar:
    """OHLCV over an interval ending at ``observation_time``.

    ``observation_time`` is the canonical interval end, not a provider timestamp or
    interval start. ``availability_time`` is the earliest time a modeled strategy may
    consume this completed observation and is explicit about how it was obtained.
    """

    instrument: Instrument
    interval: BarInterval
    observation_time: datetime
    availability_time: datetime
    availability_semantics: AvailabilitySemantics
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    provenance: Provenance

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise MarketDataValidationError("instrument", "must be an Instrument")
        if not isinstance(self.interval, BarInterval):
            raise MarketDataValidationError("bar_interval", "must be a BarInterval")
        if not isinstance(self.availability_semantics, AvailabilitySemantics):
            raise MarketDataValidationError(
                "availability_semantics", "must be an AvailabilitySemantics value"
            )
        if not isinstance(self.provenance, Provenance):
            raise MarketDataValidationError("provenance", "must be a Provenance value")
        observation_time = _canonical_utc(self.observation_time, "observation_time")
        availability_time = _canonical_utc(self.availability_time, "availability_time")
        object.__setattr__(self, "observation_time", observation_time)
        object.__setattr__(self, "availability_time", availability_time)
        if availability_time < observation_time:
            raise MarketDataValidationError(
                "availability_time", "must not precede observation_time"
            )
        for name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            _finite_decimal(value, name)
        if self.volume is not None:
            _finite_decimal(self.volume, "volume", non_negative=True)
        if self.high < self.open or self.high < self.close:
            raise MarketDataValidationError("ohlc_high", "high must be at least open and close")
        if self.low > self.open or self.low > self.close:
            raise MarketDataValidationError("ohlc_low", "low must be at most open and close")
        if self.high < self.low:
            raise MarketDataValidationError("ohlc_range", "high must be at least low")


@dataclass(frozen=True, slots=True)
class Quote:
    """A bid/ask observation; locked and crossed states are retained and classified."""

    instrument: Instrument
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None
    observation_time: datetime
    availability_time: datetime
    availability_semantics: AvailabilitySemantics
    provenance: Provenance

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise MarketDataValidationError("instrument", "must be an Instrument")
        if not isinstance(self.availability_semantics, AvailabilitySemantics):
            raise MarketDataValidationError(
                "availability_semantics", "must be an AvailabilitySemantics value"
            )
        if not isinstance(self.provenance, Provenance):
            raise MarketDataValidationError("provenance", "must be a Provenance value")
        observation_time = _canonical_utc(self.observation_time, "observation_time")
        availability_time = _canonical_utc(self.availability_time, "availability_time")
        object.__setattr__(self, "observation_time", observation_time)
        object.__setattr__(self, "availability_time", availability_time)
        if availability_time < observation_time:
            raise MarketDataValidationError(
                "availability_time", "must not precede observation_time"
            )
        _finite_decimal(self.bid_price, "bid_price")
        _finite_decimal(self.ask_price, "ask_price")
        if self.bid_size is not None:
            _finite_decimal(self.bid_size, "bid_size", non_negative=True)
        if self.ask_size is not None:
            _finite_decimal(self.ask_size, "ask_size", non_negative=True)

    @property
    def market_state(self) -> QuoteMarketState:
        if self.bid_price < self.ask_price:
            return QuoteMarketState.NORMAL
        if self.bid_price == self.ask_price:
            return QuoteMarketState.LOCKED
        return QuoteMarketState.CROSSED


class QuoteMarketState(StrEnum):
    """The observed bid/ask relationship, without silently repairing it."""

    NORMAL = "normal"
    LOCKED = "locked"
    CROSSED = "crossed"


@dataclass(frozen=True, slots=True)
class DatasetMetadata:
    """Material provenance and declared scope for one research dataset."""

    source: str
    dataset_id: str
    instruments: tuple[Instrument, ...]
    coverage_start: datetime
    coverage_end: datetime
    interval: BarInterval | None = None
    retrieval_method: str | None = None
    source_timezone: str | None = None
    session: str | None = None
    adjustment_policy: str | None = None
    validation_status: ValidationStatus = ValidationStatus.UNVALIDATED
    schema_version: str = DATASET_SCHEMA_VERSION
    quality_notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name, value in (("source", self.source), ("dataset_id", self.dataset_id)):
            if not value or value != value.strip():
                raise MarketDataValidationError(field_name, "must be a non-empty trimmed string")
        if not self.instruments:
            raise MarketDataValidationError(
                "dataset_instruments", "must contain at least one instrument"
            )
        if not all(isinstance(instrument, Instrument) for instrument in self.instruments):
            raise MarketDataValidationError("dataset_instruments", "must contain Instrument values")
        if len(set(self.instruments)) != len(self.instruments):
            raise MarketDataValidationError("dataset_instruments", "must not contain duplicates")
        ordered_instruments = tuple(sorted(self.instruments, key=lambda item: item.identifier))
        object.__setattr__(self, "instruments", ordered_instruments)
        coverage_start = _canonical_utc(self.coverage_start, "coverage_start")
        coverage_end = _canonical_utc(self.coverage_end, "coverage_end")
        object.__setattr__(self, "coverage_start", coverage_start)
        object.__setattr__(self, "coverage_end", coverage_end)
        if coverage_end < coverage_start:
            raise MarketDataValidationError(
                "coverage", "coverage_end must not precede coverage_start"
            )
        if not self.schema_version or self.schema_version != self.schema_version.strip():
            raise MarketDataValidationError("schema_version", "must be a non-empty trimmed string")

    def with_validation_status(self, status: ValidationStatus) -> Self:
        """Return metadata with a changed declared delivery status, without mutation."""
        return self.__class__(
            source=self.source,
            dataset_id=self.dataset_id,
            instruments=self.instruments,
            coverage_start=self.coverage_start,
            coverage_end=self.coverage_end,
            interval=self.interval,
            retrieval_method=self.retrieval_method,
            source_timezone=self.source_timezone,
            session=self.session,
            adjustment_policy=self.adjustment_policy,
            validation_status=status,
            schema_version=self.schema_version,
            quality_notes=self.quality_notes,
        )
