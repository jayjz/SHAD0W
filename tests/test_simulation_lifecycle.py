"""Adversarial P0.4B tests for authoritative price-free lifecycle state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from shadow.domain import Instrument
from shadow.features import (
    FEATURE_IMPLEMENTATION_VERSION,
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
)
from shadow.simulation import (
    ExecutionOpportunity,
    InstrumentLifecycleState,
    LifecycleContractError,
    LifecycleDecision,
    LifecycleReason,
    LifecycleRecord,
    LifecycleResult,
    LifecycleState,
    TimelineContractError,
    TimelineEvent,
    process_lifecycle,
    signal_available_event,
)
from shadow.strategies import MeanReversionConfig, PositionState, Signal, evaluate_mean_reversion

TIME = datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
SPY = Instrument("SPY")
QQQ = Instrument("QQQ")


def _signal(
    *,
    signal_type: Literal["entry", "exit"],
    availability_time: datetime = TIME,
    instrument: Instrument = SPY,
) -> Signal:
    value = Decimal("-2") if signal_type == "entry" else Decimal("0")
    snapshot = FeatureSnapshot(
        instrument=instrument,
        feature_name=FeatureName.Z_SCORE,
        input_value=FeatureInput.CLOSE,
        implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        value=value,
        observation_time=TIME,
        availability_time=availability_time,
        window=20,
        state=FeatureState.READY,
        unavailable_reason=None,
        source_dataset_id="lifecycle-fixture",
    )
    config = MeanReversionConfig(
        instrument=instrument,
        configuration_id="lifecycle-mean-reversion-v1",
        rolling_window=20,
        entry_threshold=Decimal("-2"),
        exit_threshold=Decimal("0"),
        maximum_feature_age=timedelta(days=1),
    )
    state = PositionState.FLAT if signal_type == "entry" else PositionState.HOLDING
    signal = evaluate_mean_reversion(snapshot, config, state, decision_time=availability_time)
    assert signal is not None
    return signal


def _entry(event_id: str, time: datetime = TIME, *, instrument: Instrument = SPY) -> TimelineEvent:
    return signal_available_event(
        _signal(signal_type="entry", availability_time=time, instrument=instrument),
        event_id=event_id,
    )


def _exit(event_id: str, time: datetime, *, instrument: Instrument = SPY) -> TimelineEvent:
    return signal_available_event(
        _signal(signal_type="exit", availability_time=time, instrument=instrument),
        event_id=event_id,
    )


def _opportunity(event_id: str, time: datetime, *, instrument: Instrument = SPY) -> TimelineEvent:
    return ExecutionOpportunity(
        event_id=event_id,
        instrument=instrument,
        event_time=time,
        source_reference=f"synthetic-opportunity:{event_id}",
    ).as_event()


def _record(result: LifecycleResult, event_id: str) -> LifecycleRecord:
    return next(record for record in result.records if record.event_reference == event_id)


def _holding_events(*, instrument: Instrument = SPY) -> list[TimelineEvent]:
    return [
        _entry("entry", TIME, instrument=instrument),
        _opportunity("entry-opportunity", TIME + timedelta(microseconds=1), instrument=instrument),
    ]


def test_flat_entry_creates_one_pending_entry_action() -> None:
    result = process_lifecycle([_entry("entry")])

    state = result.state_for(SPY)
    assert state.state is LifecycleState.PENDING_ENTRY
    assert state.pending_action is not None
    assert state.pending_action.eligibility_after_time == TIME
    assert result.unresolved_actions == (state.pending_action,)
    assert _record(result, "entry").decision is LifecycleDecision.ACTION_CREATED


def test_pending_entry_becomes_holding_only_at_strictly_later_opportunity() -> None:
    result = process_lifecycle(
        [
            _entry("entry"),
            _opportunity("same-time", TIME),
            _opportunity("later", TIME + timedelta(microseconds=1)),
        ]
    )

    assert _record(result, "same-time").decision is LifecycleDecision.INELIGIBLE
    assert (
        _record(result, "same-time").reason
        is LifecycleReason.OPPORTUNITY_NOT_STRICTLY_AFTER_ELIGIBILITY_BOUNDARY
    )
    state = result.state_for(SPY)
    assert state.state is LifecycleState.HOLDING
    assert state.open_position is not None
    assert state.open_position.opened_at == TIME + timedelta(microseconds=1)


def test_holding_exit_creates_pending_exit_and_later_opportunity_closes_it() -> None:
    exit_time = TIME + timedelta(minutes=1)
    close_time = exit_time + timedelta(microseconds=1)
    result = process_lifecycle(
        [*_holding_events(), _exit("exit", exit_time), _opportunity("close", close_time)]
    )

    assert _record(result, "exit").resulting_state is LifecycleState.PENDING_EXIT
    close_record = _record(result, "close")
    assert close_record.prior_state is LifecycleState.PENDING_EXIT
    assert close_record.resulting_state is LifecycleState.FLAT
    assert close_record.reason is LifecycleReason.EXIT_ACTION_EXECUTED
    assert result.state_for(SPY).state is LifecycleState.FLAT


def test_duplicate_entry_is_suppressed_and_cannot_create_multiple_actions() -> None:
    result = process_lifecycle([_entry("entry-a"), _entry("entry-b", TIME + timedelta(seconds=1))])

    assert result.state_for(SPY).state is LifecycleState.PENDING_ENTRY
    duplicate = _record(result, "entry-b")
    assert duplicate.decision is LifecycleDecision.IGNORED_REDUNDANT
    assert duplicate.reason is LifecycleReason.ENTRY_ALREADY_PENDING
    assert len(result.unresolved_actions) == 1


def test_entry_while_holding_is_rejected_without_pyramiding() -> None:
    result = process_lifecycle(
        [*_holding_events(), _entry("second-entry", TIME + timedelta(minutes=1))]
    )

    record = _record(result, "second-entry")
    assert record.decision is LifecycleDecision.REJECTED
    assert record.reason is LifecycleReason.ENTRY_ALREADY_HOLDING
    assert result.state_for(SPY).state is LifecycleState.HOLDING
    assert len(result.open_positions) == 1


def test_exit_while_flat_is_rejected() -> None:
    result = process_lifecycle([_exit("exit", TIME)])

    record = _record(result, "exit")
    assert record.decision is LifecycleDecision.REJECTED
    assert record.reason is LifecycleReason.EXIT_WHILE_FLAT
    assert result.state_for(SPY).state is LifecycleState.FLAT


def test_duplicate_exit_is_suppressed() -> None:
    exit_time = TIME + timedelta(minutes=1)
    result = process_lifecycle(
        [
            *_holding_events(),
            _exit("exit-a", exit_time),
            _exit("exit-b", exit_time + timedelta(seconds=1)),
        ]
    )

    duplicate = _record(result, "exit-b")
    assert duplicate.decision is LifecycleDecision.IGNORED_REDUNDANT
    assert duplicate.reason is LifecycleReason.EXIT_ALREADY_PENDING
    assert result.state_for(SPY).state is LifecycleState.PENDING_EXIT
    assert len(result.unresolved_actions) == 1


def test_simultaneous_opposing_signals_are_rejected_independent_of_event_order() -> None:
    entry = _entry("entry", TIME)
    exit = _exit("exit", TIME)
    first = process_lifecycle([entry, exit])
    second = process_lifecycle([exit, entry])

    assert first == second
    assert all(
        record.reason is LifecycleReason.SIMULTANEOUS_ENTRY_AND_EXIT for record in first.records
    )
    assert first.state_for(SPY).state is LifecycleState.FLAT


def test_different_instruments_have_isolated_lifecycle_state() -> None:
    result = process_lifecycle(
        [
            _entry("spy-entry", TIME, instrument=SPY),
            _entry("qqq-entry", TIME, instrument=QQQ),
            _opportunity("spy-opportunity", TIME + timedelta(microseconds=1), instrument=SPY),
        ]
    )

    assert result.state_for(SPY).state is LifecycleState.HOLDING
    assert result.state_for(QQQ).state is LifecycleState.PENDING_ENTRY
    assert result.state_for(QQQ).pending_action is not None


def test_reversed_input_order_has_the_same_lifecycle_trace() -> None:
    events = [
        _entry("entry", TIME),
        _opportunity("entry-opportunity", TIME + timedelta(microseconds=1)),
        _exit("exit", TIME + timedelta(minutes=1)),
        _opportunity("exit-opportunity", TIME + timedelta(minutes=1, microseconds=1)),
    ]

    assert process_lifecycle(events) == process_lifecycle(reversed(events))


def test_future_append_cannot_rewrite_prior_lifecycle_records() -> None:
    prefix_events = _holding_events()
    prefix = process_lifecycle(prefix_events)
    appended = process_lifecycle(
        [
            *prefix_events,
            _exit("exit", TIME + timedelta(minutes=1)),
            _opportunity("close", TIME + timedelta(minutes=1, microseconds=1)),
        ]
    )

    assert appended.records[: len(prefix.records)] == prefix.records
    assert appended.ordered_events[: len(prefix.ordered_events)] == prefix.ordered_events


def test_pending_action_and_open_position_remain_visible_at_end_of_simulation() -> None:
    pending = process_lifecycle([_entry("pending")])
    holding = process_lifecycle(_holding_events())

    assert pending.state_for(SPY).state is LifecycleState.PENDING_ENTRY
    assert len(pending.unresolved_actions) == 1
    assert holding.state_for(SPY).state is LifecycleState.HOLDING
    assert len(holding.open_positions) == 1
    assert holding.unresolved_actions == ()


def test_duplicate_event_ids_and_invalid_state_snapshots_fail_explicitly() -> None:
    with pytest.raises(TimelineContractError, match="event_id values must be unique"):
        process_lifecycle([_entry("duplicate"), _entry("duplicate", TIME + timedelta(minutes=1))])

    with pytest.raises(LifecycleContractError, match="state is incompatible"):
        InstrumentLifecycleState(
            instrument=SPY,
            state=LifecycleState.HOLDING,
            pending_action=None,
            open_position=None,
        )
