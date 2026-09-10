"""Authoritative, price-free position lifecycle transitions for simulation.

This module owns simulated per-instrument position state.  It composes the
P0.4A timeline's canonical ordering and strict eligibility evidence, but does
not model authorization, fills, prices, quantities, costs, or economics.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from shadow.domain import Instrument
from shadow.simulation.events import EventKind, TimelineEvent
from shadow.simulation.timeline import EligibilityDecision, TimelineResult, process_timeline
from shadow.strategies import SignalType


class LifecycleContractError(ValueError):
    """An invalid lifecycle contract or an impossible internal transition."""


class LifecycleState(StrEnum):
    """The complete simulated state for one initial-hypothesis instrument."""

    FLAT = "flat"
    PENDING_ENTRY = "pending_entry"
    HOLDING = "holding"
    PENDING_EXIT = "pending_exit"


class LifecycleActionType(StrEnum):
    """The intentionally small action vocabulary derived from a signal."""

    ENTRY = "entry"
    EXIT = "exit"


class LifecycleDecision(StrEnum):
    """The result of applying one timeline event to lifecycle state."""

    NOT_APPLICABLE = "not_applicable"
    ACTION_CREATED = "action_created"
    ACTION_EXECUTED = "action_executed"
    REJECTED = "rejected"
    IGNORED_REDUNDANT = "ignored_redundant"
    INELIGIBLE = "ineligible"


class LifecycleReason(StrEnum):
    """Machine-readable explanation for a lifecycle record."""

    EVENT_NOT_LIFECYCLE_RELEVANT = "event_not_lifecycle_relevant"
    ENTRY_ACTION_CREATED = "entry_action_created"
    EXIT_ACTION_CREATED = "exit_action_created"
    ENTRY_ACTION_EXECUTED = "entry_action_executed"
    EXIT_ACTION_EXECUTED = "exit_action_executed"
    ENTRY_ALREADY_PENDING = "entry_already_pending"
    ENTRY_ALREADY_HOLDING = "entry_already_holding"
    ENTRY_WHILE_EXIT_PENDING = "entry_while_exit_pending"
    EXIT_WHILE_FLAT = "exit_while_flat"
    EXIT_ALREADY_PENDING = "exit_already_pending"
    EXIT_WHILE_ENTRY_PENDING = "exit_while_entry_pending"
    SIMULTANEOUS_ENTRY_AND_EXIT = "simultaneous_entry_and_exit"
    OPPORTUNITY_NOT_STRICTLY_AFTER_ELIGIBILITY_BOUNDARY = (
        "opportunity_not_strictly_after_eligibility_boundary"
    )
    OPPORTUNITY_WITHOUT_PENDING_ACTION = "opportunity_without_pending_action"


def _canonical_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LifecycleContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _nonempty_trimmed(value: str, field_name: str) -> None:
    if not value or value != value.strip():
        raise LifecycleContractError(f"{field_name} must be a non-empty trimmed string")


@dataclass(frozen=True, slots=True)
class LifecycleAction:
    """A pending lifecycle action, deliberately without order or price fields."""

    action_id: str
    signal_reference: str
    instrument: Instrument
    action_type: LifecycleActionType
    created_time: datetime
    eligibility_after_time: datetime
    origin_state: LifecycleState

    def __post_init__(self) -> None:
        _nonempty_trimmed(self.action_id, "action_id")
        _nonempty_trimmed(self.signal_reference, "signal_reference")
        if not isinstance(self.instrument, Instrument):
            raise LifecycleContractError("instrument must be an Instrument")
        if not isinstance(self.action_type, LifecycleActionType):
            raise LifecycleContractError("action_type must be a LifecycleActionType")
        if not isinstance(self.origin_state, LifecycleState):
            raise LifecycleContractError("origin_state must be a LifecycleState")
        created_time = _canonical_utc(self.created_time, "created_time")
        eligibility_after_time = _canonical_utc(
            self.eligibility_after_time, "eligibility_after_time"
        )
        object.__setattr__(self, "created_time", created_time)
        object.__setattr__(self, "eligibility_after_time", eligibility_after_time)
        if created_time != eligibility_after_time:
            raise LifecycleContractError(
                "created_time must equal eligibility_after_time for a signal action"
            )
        expected_origin = (
            LifecycleState.FLAT
            if self.action_type is LifecycleActionType.ENTRY
            else LifecycleState.HOLDING
        )
        if self.origin_state is not expected_origin:
            raise LifecycleContractError("action_type is incompatible with origin_state")


@dataclass(frozen=True, slots=True)
class SimulatedPosition:
    """One open, quantity-free position created by an executed entry action."""

    instrument: Instrument
    opening_action_id: str
    opened_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise LifecycleContractError("instrument must be an Instrument")
        _nonempty_trimmed(self.opening_action_id, "opening_action_id")
        object.__setattr__(self, "opened_at", _canonical_utc(self.opened_at, "opened_at"))


@dataclass(frozen=True, slots=True)
class InstrumentLifecycleState:
    """Immutable authoritative snapshot for exactly one instrument."""

    instrument: Instrument
    state: LifecycleState
    pending_action: LifecycleAction | None
    open_position: SimulatedPosition | None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise LifecycleContractError("instrument must be an Instrument")
        if not isinstance(self.state, LifecycleState):
            raise LifecycleContractError("state must be a LifecycleState")
        if self.pending_action is not None:
            if not isinstance(self.pending_action, LifecycleAction):
                raise LifecycleContractError("pending_action must be a LifecycleAction or None")
            if self.pending_action.instrument != self.instrument:
                raise LifecycleContractError(
                    "pending_action instrument must match state instrument"
                )
        if self.open_position is not None:
            if not isinstance(self.open_position, SimulatedPosition):
                raise LifecycleContractError("open_position must be a SimulatedPosition or None")
            if self.open_position.instrument != self.instrument:
                raise LifecycleContractError("open_position instrument must match state instrument")

        expected = {
            LifecycleState.FLAT: (None, None),
            LifecycleState.PENDING_ENTRY: (LifecycleActionType.ENTRY, None),
            LifecycleState.HOLDING: (None, "position"),
            LifecycleState.PENDING_EXIT: (LifecycleActionType.EXIT, "position"),
        }
        expected_action, expected_position = expected[self.state]
        actual_action = None if self.pending_action is None else self.pending_action.action_type
        actual_position = None if self.open_position is None else "position"
        if (actual_action, actual_position) != (expected_action, expected_position):
            raise LifecycleContractError("state is incompatible with pending_action/open_position")


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    """Immutable evidence sufficient to reconstruct one lifecycle decision."""

    event_reference: str
    event_kind: EventKind
    event_time: datetime
    source_reference: str
    instrument: Instrument
    prior_state: LifecycleState
    decision: LifecycleDecision
    reason: LifecycleReason
    resulting_state: LifecycleState
    action: LifecycleAction | None
    position: SimulatedPosition | None


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Canonical events, immutable trace, and visible end-of-simulation state."""

    ordered_events: tuple[TimelineEvent, ...]
    records: tuple[LifecycleRecord, ...]
    final_states: tuple[InstrumentLifecycleState, ...]
    unresolved_actions: tuple[LifecycleAction, ...]
    open_positions: tuple[SimulatedPosition, ...]

    def state_for(self, instrument: Instrument) -> InstrumentLifecycleState:
        """Return an explicit final state; unseen instruments are flat."""
        if not isinstance(instrument, Instrument):
            raise LifecycleContractError("instrument must be an Instrument")
        for state in self.final_states:
            if state.instrument == instrument:
                return state
        return _flat_state(instrument)


def _flat_state(instrument: Instrument) -> InstrumentLifecycleState:
    return InstrumentLifecycleState(
        instrument=instrument,
        state=LifecycleState.FLAT,
        pending_action=None,
        open_position=None,
    )


def _action_for(event: TimelineEvent, prior_state: LifecycleState) -> LifecycleAction:
    assert event.signal is not None  # guaranteed by TimelineEvent's contract
    action_type = (
        LifecycleActionType.ENTRY
        if event.signal.signal_type is SignalType.LONG_ENTRY
        else LifecycleActionType.EXIT
    )
    return LifecycleAction(
        action_id=f"lifecycle-action:{event.event_id}",
        signal_reference=event.event_id,
        instrument=event.instrument,
        action_type=action_type,
        created_time=event.effective_time,
        eligibility_after_time=event.signal.availability_time,
        origin_state=prior_state,
    )


def _conflicted_signal_references(events: tuple[TimelineEvent, ...]) -> frozenset[str]:
    """Reject same-instant opposing proposals independently of input order."""
    signals_by_instant: defaultdict[tuple[datetime, Instrument], list[TimelineEvent]] = defaultdict(
        list
    )
    for event in events:
        if event.kind is EventKind.SIGNAL_AVAILABLE:
            signals_by_instant[(event.effective_time, event.instrument)].append(event)

    conflicted: set[str] = set()
    for candidates in signals_by_instant.values():
        assert all(candidate.signal is not None for candidate in candidates)
        signal_types = {
            candidate.signal.signal_type for candidate in candidates if candidate.signal
        }
        if SignalType.LONG_ENTRY in signal_types and SignalType.EXIT in signal_types:
            conflicted.update(candidate.event_id for candidate in candidates)
    return frozenset(conflicted)


def _eligible_pairs(timeline: TimelineResult) -> frozenset[tuple[str, str]]:
    return frozenset(
        (record.signal_reference, record.execution_event_reference)
        for record in timeline.records
        if record.eligibility_decision is EligibilityDecision.ELIGIBLE
        and record.signal_reference is not None
        and record.execution_event_reference is not None
    )


def _record(
    event: TimelineEvent,
    prior_state: LifecycleState,
    decision: LifecycleDecision,
    reason: LifecycleReason,
    resulting_state: InstrumentLifecycleState,
    *,
    action: LifecycleAction | None = None,
    position: SimulatedPosition | None = None,
) -> LifecycleRecord:
    return LifecycleRecord(
        event_reference=event.event_id,
        event_kind=event.kind,
        event_time=event.effective_time,
        source_reference=event.source_reference,
        instrument=event.instrument,
        prior_state=prior_state,
        decision=decision,
        reason=reason,
        resulting_state=resulting_state.state,
        action=action,
        position=position,
    )


def process_lifecycle(events: Iterable[TimelineEvent]) -> LifecycleResult:
    """Apply price-free lifecycle transitions to a fixed P0.4A event stream.

    P0.4A remains the source of temporal legality: this function first obtains its
    canonical event ordering and strict, same-instrument, strictly-later eligibility
    trace.  The lifecycle then accepts at most one active action and one open
    position per instrument.  An eligible opportunity is a neutral lifecycle event,
    not a broker fill or an assertion about a fill price.

    Same-time entry and exit signals for one instrument reject *both* signals.  For
    repeated same-direction signals, the first canonical event is retained and later
    ones are recorded as redundant; the canonical key is documented by P0.4A and is
    independent of caller input order.  Pending actions have no expiry: they remain
    visible in ``unresolved_actions`` at the end of this fixed simulation input.
    """
    timeline = process_timeline(events)
    eligible_pairs = _eligible_pairs(timeline)
    conflicts = _conflicted_signal_references(timeline.ordered_events)
    states: dict[Instrument, InstrumentLifecycleState] = {}
    records: list[LifecycleRecord] = []

    for event in timeline.ordered_events:
        current = states.setdefault(event.instrument, _flat_state(event.instrument))
        prior_state = current.state

        if event.kind is EventKind.SIGNAL_AVAILABLE:
            assert event.signal is not None  # guaranteed by TimelineEvent's contract
            if event.event_id in conflicts:
                records.append(
                    _record(
                        event,
                        prior_state,
                        LifecycleDecision.REJECTED,
                        LifecycleReason.SIMULTANEOUS_ENTRY_AND_EXIT,
                        current,
                    )
                )
                continue

            if event.signal.signal_type is SignalType.LONG_ENTRY:
                if current.state is LifecycleState.FLAT:
                    action = _action_for(event, current.state)
                    next_state = InstrumentLifecycleState(
                        instrument=event.instrument,
                        state=LifecycleState.PENDING_ENTRY,
                        pending_action=action,
                        open_position=None,
                    )
                    states[event.instrument] = next_state
                    records.append(
                        _record(
                            event,
                            prior_state,
                            LifecycleDecision.ACTION_CREATED,
                            LifecycleReason.ENTRY_ACTION_CREATED,
                            next_state,
                            action=action,
                        )
                    )
                elif current.state is LifecycleState.PENDING_ENTRY:
                    records.append(
                        _record(
                            event,
                            prior_state,
                            LifecycleDecision.IGNORED_REDUNDANT,
                            LifecycleReason.ENTRY_ALREADY_PENDING,
                            current,
                            action=current.pending_action,
                        )
                    )
                elif current.state is LifecycleState.HOLDING:
                    records.append(
                        _record(
                            event,
                            prior_state,
                            LifecycleDecision.REJECTED,
                            LifecycleReason.ENTRY_ALREADY_HOLDING,
                            current,
                            position=current.open_position,
                        )
                    )
                else:
                    records.append(
                        _record(
                            event,
                            prior_state,
                            LifecycleDecision.REJECTED,
                            LifecycleReason.ENTRY_WHILE_EXIT_PENDING,
                            current,
                            action=current.pending_action,
                            position=current.open_position,
                        )
                    )
                continue

            if current.state is LifecycleState.HOLDING:
                action = _action_for(event, current.state)
                next_state = InstrumentLifecycleState(
                    instrument=event.instrument,
                    state=LifecycleState.PENDING_EXIT,
                    pending_action=action,
                    open_position=current.open_position,
                )
                states[event.instrument] = next_state
                records.append(
                    _record(
                        event,
                        prior_state,
                        LifecycleDecision.ACTION_CREATED,
                        LifecycleReason.EXIT_ACTION_CREATED,
                        next_state,
                        action=action,
                        position=current.open_position,
                    )
                )
            elif current.state is LifecycleState.PENDING_EXIT:
                records.append(
                    _record(
                        event,
                        prior_state,
                        LifecycleDecision.IGNORED_REDUNDANT,
                        LifecycleReason.EXIT_ALREADY_PENDING,
                        current,
                        action=current.pending_action,
                        position=current.open_position,
                    )
                )
            elif current.state is LifecycleState.FLAT:
                records.append(
                    _record(
                        event,
                        prior_state,
                        LifecycleDecision.REJECTED,
                        LifecycleReason.EXIT_WHILE_FLAT,
                        current,
                    )
                )
            else:
                records.append(
                    _record(
                        event,
                        prior_state,
                        LifecycleDecision.REJECTED,
                        LifecycleReason.EXIT_WHILE_ENTRY_PENDING,
                        current,
                        action=current.pending_action,
                    )
                )
            continue

        if event.kind is EventKind.EXECUTION_OPPORTUNITY:
            pending_action = current.pending_action
            if pending_action is None:
                records.append(
                    _record(
                        event,
                        prior_state,
                        LifecycleDecision.NOT_APPLICABLE,
                        LifecycleReason.OPPORTUNITY_WITHOUT_PENDING_ACTION,
                        current,
                        position=current.open_position,
                    )
                )
                continue
            if (pending_action.signal_reference, event.event_id) not in eligible_pairs:
                records.append(
                    _record(
                        event,
                        prior_state,
                        LifecycleDecision.INELIGIBLE,
                        LifecycleReason.OPPORTUNITY_NOT_STRICTLY_AFTER_ELIGIBILITY_BOUNDARY,
                        current,
                        action=pending_action,
                        position=current.open_position,
                    )
                )
                continue

            if current.state is LifecycleState.PENDING_ENTRY:
                position = SimulatedPosition(
                    instrument=event.instrument,
                    opening_action_id=pending_action.action_id,
                    opened_at=event.effective_time,
                )
                next_state = InstrumentLifecycleState(
                    instrument=event.instrument,
                    state=LifecycleState.HOLDING,
                    pending_action=None,
                    open_position=position,
                )
                decision = LifecycleDecision.ACTION_EXECUTED
                reason = LifecycleReason.ENTRY_ACTION_EXECUTED
            elif current.state is LifecycleState.PENDING_EXIT:
                closed_position = current.open_position
                if closed_position is None:
                    raise LifecycleContractError("pending exit requires an open position")
                position = closed_position
                next_state = _flat_state(event.instrument)
                decision = LifecycleDecision.ACTION_EXECUTED
                reason = LifecycleReason.EXIT_ACTION_EXECUTED
            else:
                raise LifecycleContractError("pending action is incompatible with lifecycle state")
            states[event.instrument] = next_state
            records.append(
                _record(
                    event,
                    prior_state,
                    decision,
                    reason,
                    next_state,
                    action=pending_action,
                    position=position,
                )
            )
            continue

        records.append(
            _record(
                event,
                prior_state,
                LifecycleDecision.NOT_APPLICABLE,
                LifecycleReason.EVENT_NOT_LIFECYCLE_RELEVANT,
                current,
                position=current.open_position,
            )
        )

    final_states = tuple(sorted(states.values(), key=lambda state: state.instrument.identifier))
    unresolved_actions = tuple(
        state.pending_action for state in final_states if state.pending_action is not None
    )
    open_positions = tuple(
        state.open_position for state in final_states if state.open_position is not None
    )
    return LifecycleResult(
        ordered_events=timeline.ordered_events,
        records=tuple(records),
        final_states=final_states,
        unresolved_actions=unresolved_actions,
        open_positions=open_positions,
    )
