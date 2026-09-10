"""Small immutable event vocabulary for deterministic simulation time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from shadow.domain import Bar, Instrument
from shadow.features import FeatureSnapshot
from shadow.strategies import Signal


class TimelineContractError(ValueError):
    """An invalid chronological simulation contract."""


class EventKind(StrEnum):
    """The explicit causal stages represented by the P0.4A timeline."""

    MARKET_OBSERVATION_AVAILABLE = "market_observation_available"
    FEATURE_AVAILABLE = "feature_available"
    SIGNAL_AVAILABLE = "signal_available"
    EXECUTION_OPPORTUNITY = "execution_opportunity"


# This mapping, not enum declaration order, is the equal-time causal precedence.
EVENT_PRECEDENCE: dict[EventKind, int] = {
    EventKind.MARKET_OBSERVATION_AVAILABLE: 10,
    EventKind.FEATURE_AVAILABLE: 20,
    EventKind.SIGNAL_AVAILABLE: 30,
    EventKind.EXECUTION_OPPORTUNITY: 40,
}


def _canonical_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimelineContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _nonempty_trimmed(value: str, field_name: str) -> None:
    if not value or value != value.strip():
        raise TimelineContractError(f"{field_name} must be a non-empty trimmed string")


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One immutable, externally identified event on simulated time.

    ``effective_time`` is when this event can affect simulation state.  For market
    and feature events it is their upstream availability time; for a signal it is
    its decision/availability time; for an execution opportunity it is the modelled
    opportunity time.  ``source_reference`` and ``event_id`` are stable evidence
    identifiers supplied by the caller, not object identities or input positions.
    """

    event_id: str
    kind: EventKind
    effective_time: datetime
    instrument: Instrument
    source_reference: str
    signal: Signal | None = None

    def __post_init__(self) -> None:
        _nonempty_trimmed(self.event_id, "event_id")
        _nonempty_trimmed(self.source_reference, "source_reference")
        if not isinstance(self.kind, EventKind):
            raise TimelineContractError("kind must be an EventKind")
        if not isinstance(self.instrument, Instrument):
            raise TimelineContractError("instrument must be an Instrument")
        effective_time = _canonical_utc(self.effective_time, "effective_time")
        object.__setattr__(self, "effective_time", effective_time)
        if self.kind is EventKind.SIGNAL_AVAILABLE:
            if not isinstance(self.signal, Signal):
                raise TimelineContractError("signal events require a Signal")
            if self.signal.instrument != self.instrument:
                raise TimelineContractError("signal event instrument must match signal instrument")
            if self.signal.availability_time != effective_time:
                raise TimelineContractError(
                    "signal event effective_time must equal signal availability_time"
                )
        elif self.signal is not None:
            raise TimelineContractError("only signal events may contain a Signal")

    @property
    def sort_key(self) -> tuple[datetime, int, str, str]:
        """Canonical chronological key, independent of input collection order."""
        return (
            self.effective_time,
            EVENT_PRECEDENCE[self.kind],
            self.instrument.identifier,
            self.event_id,
        )


@dataclass(frozen=True, slots=True)
class ExecutionOpportunity:
    """A provider-neutral future event that may satisfy a pending action.

    It deliberately has no price, quantity, order type, fill, or broker semantics.
    ``event_time`` is an explicit modelled execution-event instant, not the
    observation time of a completed bar.
    """

    event_id: str
    instrument: Instrument
    event_time: datetime
    source_reference: str

    def __post_init__(self) -> None:
        _nonempty_trimmed(self.event_id, "event_id")
        _nonempty_trimmed(self.source_reference, "source_reference")
        if not isinstance(self.instrument, Instrument):
            raise TimelineContractError("instrument must be an Instrument")
        event_time = _canonical_utc(self.event_time, "event_time")
        object.__setattr__(self, "event_time", event_time)

    def as_event(self) -> TimelineEvent:
        """Return the timeline event carrying this opportunity's evidence."""
        return TimelineEvent(
            event_id=self.event_id,
            kind=EventKind.EXECUTION_OPPORTUNITY,
            effective_time=self.event_time,
            instrument=self.instrument,
            source_reference=self.source_reference,
        )


def market_observation_available_event(bar: Bar, *, event_id: str) -> TimelineEvent:
    """Represent the instant a completed market observation became consumable."""
    if not isinstance(bar, Bar):
        raise TimelineContractError("bar must be a Bar")
    return TimelineEvent(
        event_id=event_id,
        kind=EventKind.MARKET_OBSERVATION_AVAILABLE,
        effective_time=bar.availability_time,
        instrument=bar.instrument,
        source_reference=(
            f"bar:{bar.instrument.identifier}:{bar.interval.microseconds}:{bar.observation_time.isoformat()}"
        ),
    )


def feature_available_event(snapshot: FeatureSnapshot, *, event_id: str) -> TimelineEvent:
    """Represent availability propagated by the upstream feature kernel."""
    if not isinstance(snapshot, FeatureSnapshot):
        raise TimelineContractError("snapshot must be a FeatureSnapshot")
    return TimelineEvent(
        event_id=event_id,
        kind=EventKind.FEATURE_AVAILABLE,
        effective_time=snapshot.availability_time,
        instrument=snapshot.instrument,
        source_reference=(
            f"feature:{snapshot.feature_name}:{snapshot.window}:"
            f"{snapshot.observation_time.isoformat()}:{snapshot.source_dataset_id}"
        ),
    )


def signal_available_event(signal: Signal, *, event_id: str) -> TimelineEvent:
    """Represent the strategy proposal at its already-validated decision instant."""
    if not isinstance(signal, Signal):
        raise TimelineContractError("signal must be a Signal")
    return TimelineEvent(
        event_id=event_id,
        kind=EventKind.SIGNAL_AVAILABLE,
        effective_time=signal.availability_time,
        instrument=signal.instrument,
        source_reference=(
            f"signal:{signal.strategy_id}:{signal.strategy_version}:"
            f"{signal.configuration_id}:{signal.decision_time.isoformat()}"
        ),
        signal=signal,
    )
