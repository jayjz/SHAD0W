"""Counterexamples for P0.4A chronological eligibility semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shadow.domain import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    Instrument,
    Provenance,
)
from shadow.features import (
    FEATURE_IMPLEMENTATION_VERSION,
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
)
from shadow.simulation import (
    EligibilityDecision,
    EligibilityReason,
    EventKind,
    ExecutionOpportunity,
    TimelineContractError,
    TimelineEvent,
    TimelineRecord,
    TimelineResult,
    feature_available_event,
    market_observation_available_event,
    process_timeline,
    signal_available_event,
)
from shadow.strategies import (
    MeanReversionConfig,
    PositionState,
    Signal,
    evaluate_mean_reversion,
)

TIME = datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
SPY = Instrument("SPY")
QQQ = Instrument("QQQ")


def _signal(*, availability_time: datetime = TIME, instrument: Instrument = SPY) -> Signal:
    snapshot = FeatureSnapshot(
        instrument=instrument,
        feature_name=FeatureName.Z_SCORE,
        input_value=FeatureInput.CLOSE,
        implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        value=Decimal("-2"),
        observation_time=TIME,
        availability_time=availability_time,
        window=20,
        state=FeatureState.READY,
        unavailable_reason=None,
        source_dataset_id="timeline-fixture",
    )
    config = MeanReversionConfig(
        instrument=instrument,
        configuration_id="timeline-mean-reversion-v1",
        rolling_window=20,
        entry_threshold=Decimal("-2"),
        exit_threshold=Decimal("0"),
        maximum_feature_age=timedelta(minutes=1),
    )
    signal = evaluate_mean_reversion(
        snapshot, config, PositionState.FLAT, decision_time=availability_time
    )
    assert signal is not None
    return signal


def _opportunity(
    event_id: str, event_time: datetime, *, instrument: Instrument = SPY
) -> TimelineEvent:
    return ExecutionOpportunity(
        event_id=event_id,
        instrument=instrument,
        event_time=event_time,
        source_reference=f"synthetic-opportunity:{event_id}",
    ).as_event()


def _bar(*, observation_time: datetime, availability_time: datetime) -> Bar:
    return Bar(
        instrument=SPY,
        interval=BarInterval(timedelta(minutes=1)),
        observation_time=observation_time,
        availability_time=availability_time,
        availability_semantics=AvailabilitySemantics.PROVIDER_PUBLISHED,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
        provenance=Provenance(source="synthetic"),
    )


def _decisions(result: TimelineResult) -> list[TimelineRecord]:
    return [
        record
        for record in result.records
        if record.eligibility_decision
        in {EligibilityDecision.ELIGIBLE, EligibilityDecision.INELIGIBLE}
    ]


def test_completed_bar_signal_cannot_use_the_same_bar_close_opportunity() -> None:
    signal = _signal()
    result = process_timeline(
        [
            signal_available_event(signal, event_id="signal-bar-n"),
            _opportunity("bar-n-close", TIME),
        ]
    )

    decisions = _decisions(result)
    assert len(decisions) == 1
    record = decisions[0]
    assert record.eligibility_decision is EligibilityDecision.INELIGIBLE
    assert record.reason is EligibilityReason.OPPORTUNITY_NOT_STRICTLY_AFTER_SIGNAL_AVAILABILITY
    assert record.signal_reference == "signal-bar-n"
    assert record.execution_event_reference == "bar-n-close"
    assert result.pending_actions[0].signal_reference == "signal-bar-n"


def test_first_strictly_later_opportunity_is_the_earliest_legal_one() -> None:
    signal = _signal()
    result = process_timeline(
        [
            _opportunity("bar-n-close", TIME),
            signal_available_event(signal, event_id="signal-bar-n"),
            _opportunity("bar-n-plus-one", TIME + timedelta(microseconds=1)),
            _opportunity("bar-n-plus-two", TIME + timedelta(minutes=1)),
        ]
    )

    eligible = [
        record
        for record in _decisions(result)
        if record.eligibility_decision is EligibilityDecision.ELIGIBLE
    ]
    assert len(eligible) == 1
    assert eligible[0].execution_event_reference == "bar-n-plus-one"
    assert eligible[0].execution_event_time == TIME + timedelta(microseconds=1)
    assert eligible[0].reason is EligibilityReason.EARLIEST_LEGAL_OPPORTUNITY
    assert result.pending_actions == ()


def test_delayed_market_availability_cannot_be_replaced_by_observation_time() -> None:
    observation_time = TIME
    availability_time = TIME + timedelta(milliseconds=450)
    bar = _bar(observation_time=observation_time, availability_time=availability_time)
    signal = _signal(availability_time=availability_time)
    result = process_timeline(
        [
            market_observation_available_event(bar, event_id="bar-n-available"),
            signal_available_event(signal, event_id="signal-after-delivery"),
            _opportunity("observation-time", observation_time),
            _opportunity("delivery-instant", availability_time),
            _opportunity("after-delivery", availability_time + timedelta(microseconds=1)),
        ]
    )

    market_event = result.ordered_events[1]
    assert market_event.event_id == "bar-n-available"
    assert market_event.effective_time == availability_time
    decisions = _decisions(result)
    assert [record.execution_event_reference for record in decisions] == [
        "delivery-instant",
        "after-delivery",
    ]
    assert decisions[0].eligibility_decision is EligibilityDecision.INELIGIBLE
    assert decisions[1].eligibility_decision is EligibilityDecision.ELIGIBLE


def test_tied_events_have_explicit_causal_precedence_and_stable_secondary_keys() -> None:
    signal = _signal()
    qqq_signal = _signal(instrument=QQQ)
    snapshot = FeatureSnapshot(
        instrument=SPY,
        feature_name=FeatureName.Z_SCORE,
        input_value=FeatureInput.CLOSE,
        implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        value=Decimal("-2"),
        observation_time=TIME,
        availability_time=TIME,
        window=20,
        state=FeatureState.READY,
        unavailable_reason=None,
        source_dataset_id="timeline-fixture",
    )
    same_time_events = [
        _opportunity("opportunity-z", TIME),
        signal_available_event(signal, event_id="signal-z"),
        signal_available_event(qqq_signal, event_id="signal-a"),
        TimelineEvent(
            event_id="market-z",
            kind=EventKind.MARKET_OBSERVATION_AVAILABLE,
            effective_time=TIME,
            instrument=SPY,
            source_reference="synthetic-market",
        ),
        feature_available_event(snapshot, event_id="feature-z"),
        TimelineEvent(
            event_id="market-a",
            kind=EventKind.MARKET_OBSERVATION_AVAILABLE,
            effective_time=TIME,
            instrument=QQQ,
            source_reference="synthetic-market",
        ),
    ]

    expected = ["market-a", "market-z", "feature-z", "signal-a", "signal-z", "opportunity-z"]
    result = process_timeline(same_time_events)
    reversed_result = process_timeline(reversed(same_time_events))
    assert [event.event_id for event in result.ordered_events] == expected
    assert [event.event_id for event in reversed_result.ordered_events] == expected
    assert reversed_result == result


def test_appending_future_events_does_not_rewrite_prefix_trace_or_eligibility() -> None:
    signal = _signal()
    prefix = [
        signal_available_event(signal, event_id="signal-bar-n"),
        _opportunity("same-time", TIME),
    ]
    prefix_result = process_timeline(prefix)
    expanded_result = process_timeline(
        prefix + [_opportunity("later", TIME + timedelta(seconds=1))]
    )

    assert (
        expanded_result.ordered_events[: len(prefix_result.ordered_events)]
        == prefix_result.ordered_events
    )
    assert expanded_result.records[: len(prefix_result.records)] == prefix_result.records
    assert _decisions(prefix_result)[0].eligibility_decision is EligibilityDecision.INELIGIBLE
    assert _decisions(expanded_result)[0].eligibility_decision is EligibilityDecision.INELIGIBLE
    assert _decisions(expanded_result)[1].eligibility_decision is EligibilityDecision.ELIGIBLE


def test_signal_event_contract_rejects_inconsistent_time_and_duplicate_ids() -> None:
    signal = _signal()
    with pytest.raises(TimelineContractError, match="effective_time"):
        TimelineEvent(
            event_id="wrong-time",
            kind=EventKind.SIGNAL_AVAILABLE,
            effective_time=TIME + timedelta(microseconds=1),
            instrument=SPY,
            source_reference="synthetic-signal",
            signal=signal,
        )
    with pytest.raises(TimelineContractError, match="unique"):
        process_timeline(
            [
                signal_available_event(signal, event_id="same-id"),
                _opportunity("same-id", TIME + timedelta(seconds=1)),
            ]
        )
