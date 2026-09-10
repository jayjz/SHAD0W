"""Adversarial P0.5A tests for deterministic quote-side execution semantics."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shadow.domain import AvailabilitySemantics, Instrument, Provenance, Quote
from shadow.execution import (
    ExecutionActionType,
    ExecutionAttempt,
    ExecutionReason,
    ExecutionStatus,
    QuoteExecutionConfig,
    resolve_execution_attempt,
    resolve_execution_attempts,
)
from shadow.features import (
    FEATURE_IMPLEMENTATION_VERSION,
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
)
from shadow.simulation import (
    ExecutionOpportunity,
    LifecycleDecision,
    LifecycleState,
    TimelineEvent,
    process_lifecycle,
    signal_available_event,
)
from shadow.strategies import MeanReversionConfig, PositionState, evaluate_mean_reversion

TIME = datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
SPY = Instrument("SPY")
QQQ = Instrument("QQQ")
CONFIG = QuoteExecutionConfig(maximum_quote_age=timedelta(seconds=1))


def _quote(
    *,
    instrument: Instrument = SPY,
    bid: str = "99",
    ask: str = "101",
    observation_time: datetime = TIME,
    availability_time: datetime = TIME,
) -> Quote:
    return Quote(
        instrument=instrument,
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        bid_size=None,
        ask_size=None,
        observation_time=observation_time,
        availability_time=availability_time,
        availability_semantics=AvailabilitySemantics.MODELED,
        provenance=Provenance("synthetic"),
    )


def _attempt(
    *,
    action_type: ExecutionActionType = ExecutionActionType.ENTRY,
    instrument: Instrument = SPY,
    opportunity_instrument: Instrument | None = None,
    opportunity_time: datetime = TIME + timedelta(microseconds=1),
    attempt_time: datetime | None = None,
    attempt_id: str = "attempt-1",
) -> ExecutionAttempt:
    return ExecutionAttempt(
        attempt_id=attempt_id,
        action_id="action-1",
        signal_reference="signal-1",
        instrument=instrument,
        action_type=action_type,
        eligibility_after_time=TIME,
        opportunity_reference="opportunity-1",
        opportunity_instrument=opportunity_instrument or instrument,
        opportunity_time=opportunity_time,
        attempt_time=attempt_time or opportunity_time,
        execution_model_id=CONFIG.execution_model_id,
    )


def _signal_event(event_id: str = "entry") -> TimelineEvent:
    snapshot = FeatureSnapshot(
        instrument=SPY,
        feature_name=FeatureName.Z_SCORE,
        input_value=FeatureInput.CLOSE,
        implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        value=Decimal("-2"),
        observation_time=TIME,
        availability_time=TIME,
        window=2,
        state=FeatureState.READY,
        unavailable_reason=None,
        source_dataset_id="execution-fixture",
    )
    config = MeanReversionConfig(
        instrument=SPY,
        configuration_id="execution-fixture",
        rolling_window=2,
        entry_threshold=Decimal("-1"),
        exit_threshold=Decimal("0"),
        maximum_feature_age=timedelta(days=1),
    )
    signal = evaluate_mean_reversion(snapshot, config, PositionState.FLAT, decision_time=TIME)
    assert signal is not None
    return signal_available_event(signal, event_id=event_id)


def _opportunity(
    event_id: str, event_time: datetime, *, instrument: Instrument = SPY
) -> TimelineEvent:
    return ExecutionOpportunity(
        event_id=event_id,
        instrument=instrument,
        event_time=event_time,
        source_reference=f"synthetic-opportunity:{event_id}",
    ).as_event()


def test_buy_uses_ask_and_exit_uses_bid_without_extra_spread_cost() -> None:
    quote = _quote(bid="99", ask="101")

    entry = resolve_execution_attempt(_attempt(), (quote,), CONFIG)
    exit_outcome = resolve_execution_attempt(
        _attempt(action_type=ExecutionActionType.EXIT, attempt_id="exit-attempt"),
        (quote,),
        CONFIG,
    )

    assert entry.status is ExecutionStatus.FILLED
    assert entry.execution_price == Decimal("101")
    assert exit_outcome.status is ExecutionStatus.FILLED
    assert exit_outcome.execution_price == Decimal("99")
    assert entry.execution_price != exit_outcome.execution_price


def test_future_better_quote_cannot_improve_an_earlier_execution() -> None:
    attempt = _attempt(opportunity_time=TIME + timedelta(seconds=1))
    legal_quote = _quote(bid="99", ask="101")
    future_quote = _quote(
        bid="100",
        ask="100.5",
        observation_time=TIME + timedelta(seconds=2),
        availability_time=TIME + timedelta(seconds=2),
    )

    outcome = resolve_execution_attempt(attempt, (legal_quote, future_quote), CONFIG)

    assert outcome.status is ExecutionStatus.FILLED
    assert outcome.market_evidence == legal_quote
    assert outcome.execution_price == Decimal("101")


def test_delayed_quote_cannot_travel_back_in_time() -> None:
    attempt = _attempt(opportunity_time=TIME + timedelta(seconds=1))
    delayed = _quote(
        observation_time=TIME,
        availability_time=TIME + timedelta(seconds=2),
    )

    outcome = resolve_execution_attempt(attempt, (delayed,), CONFIG)

    assert outcome.status is ExecutionStatus.UNFILLED
    assert outcome.reason is ExecutionReason.QUOTE_NOT_YET_AVAILABLE
    assert outcome.market_evidence is None


def test_stale_and_missing_quotes_fail_closed_without_a_fill() -> None:
    stale_attempt = _attempt(opportunity_time=TIME + timedelta(seconds=2))
    stale = resolve_execution_attempt(stale_attempt, (_quote(),), CONFIG)
    missing = resolve_execution_attempt(_attempt(), (), CONFIG)

    assert (stale.status, stale.reason, stale.execution_price) == (
        ExecutionStatus.UNFILLED,
        ExecutionReason.STALE_QUOTE,
        None,
    )
    assert (missing.status, missing.reason, missing.execution_price) == (
        ExecutionStatus.UNFILLED,
        ExecutionReason.NO_LEGAL_QUOTE,
        None,
    )


def test_crossed_quote_is_rejected_without_repairing_the_market() -> None:
    crossed = _quote(bid="101", ask="99")

    outcome = resolve_execution_attempt(_attempt(), (crossed,), CONFIG)

    assert outcome.status is ExecutionStatus.REJECTED
    assert outcome.reason is ExecutionReason.CROSSED_QUOTE
    assert outcome.market_evidence == crossed
    assert outcome.execution_price is None


def test_locked_quote_remains_executable_under_the_p01_quote_contract() -> None:
    outcome = resolve_execution_attempt(_attempt(), (_quote(bid="100", ask="100"),), CONFIG)

    assert outcome.status is ExecutionStatus.FILLED
    assert outcome.execution_price == Decimal("100")


def test_invalid_attempts_and_duplicate_ids_are_explicitly_rejected() -> None:
    mismatched = _attempt(opportunity_instrument=QQQ)
    before_eligibility = _attempt(
        attempt_id="too-early",
        opportunity_time=TIME,
        attempt_time=TIME,
    )
    duplicate = replace(_attempt(), action_id="action-2")

    assert resolve_execution_attempt(mismatched, (), CONFIG).reason is (
        ExecutionReason.ACTION_OPPORTUNITY_INSTRUMENT_MISMATCH
    )
    assert resolve_execution_attempt(before_eligibility, (), CONFIG).reason is (
        ExecutionReason.OPPORTUNITY_NOT_STRICTLY_AFTER_ELIGIBILITY_BOUNDARY
    )
    outcomes = resolve_execution_attempts((_attempt(), duplicate), (), CONFIG)
    assert {outcome.status for outcome in outcomes} == {ExecutionStatus.REJECTED}
    assert {outcome.reason for outcome in outcomes} == {ExecutionReason.DUPLICATE_ATTEMPT_ID}
    assert outcomes == resolve_execution_attempts((duplicate, _attempt()), (), CONFIG)


def test_quote_selection_is_independent_of_caller_order_at_equal_times() -> None:
    first = _quote(bid="99", ask="101")
    second = _quote(bid="98", ask="102")
    attempt = _attempt()

    forward = resolve_execution_attempt(attempt, (first, second), CONFIG)
    reverse = resolve_execution_attempt(attempt, (second, first), CONFIG)

    assert forward == reverse


def test_equal_time_opportunity_remains_illegal_and_never_creates_an_attempt() -> None:
    result = process_lifecycle(
        [_signal_event(), _opportunity("same-time", TIME)],
        execution_config=CONFIG,
        quotes=(_quote(),),
    )

    assert result.state_for(SPY).state is LifecycleState.PENDING_ENTRY
    assert result.execution_attempts == ()
    assert result.execution_outcomes == ()


def test_unfilled_attempt_keeps_lifecycle_action_pending() -> None:
    result = process_lifecycle(
        [_signal_event(), _opportunity("later", TIME + timedelta(microseconds=1))],
        execution_config=CONFIG,
        quotes=(),
    )

    record = next(record for record in result.records if record.event_reference == "later")
    assert result.state_for(SPY).state is LifecycleState.PENDING_ENTRY
    assert record.decision is LifecycleDecision.ATTEMPT_UNFILLED
    assert record.execution_outcome is not None
    assert record.execution_outcome.status is ExecutionStatus.UNFILLED


def test_pending_action_can_fill_at_a_later_legal_opportunity() -> None:
    first_time = TIME + timedelta(microseconds=1)
    second_time = TIME + timedelta(seconds=1)
    quote = _quote(observation_time=second_time, availability_time=second_time)
    result = process_lifecycle(
        [
            _signal_event(),
            _opportunity("first", first_time),
            _opportunity("second", second_time),
        ],
        execution_config=CONFIG,
        quotes=(quote,),
    )

    assert [outcome.status for outcome in result.execution_outcomes] == [
        ExecutionStatus.UNFILLED,
        ExecutionStatus.FILLED,
    ]
    assert result.execution_outcomes[0].reason is ExecutionReason.QUOTE_NOT_YET_AVAILABLE
    assert result.state_for(SPY).state is LifecycleState.HOLDING


def test_only_filled_outcome_transitions_lifecycle_state() -> None:
    result = process_lifecycle(
        [_signal_event(), _opportunity("later", TIME + timedelta(microseconds=1))],
        execution_config=CONFIG,
        quotes=(_quote(),),
    )

    record = next(record for record in result.records if record.event_reference == "later")
    assert result.state_for(SPY).state is LifecycleState.HOLDING
    assert record.decision is LifecycleDecision.ACTION_FILLED
    assert record.execution_outcome is not None
    assert record.execution_outcome.status is ExecutionStatus.FILLED


def test_wrong_instrument_quote_cannot_fill_the_action() -> None:
    result = process_lifecycle(
        [_signal_event(), _opportunity("later", TIME + timedelta(microseconds=1))],
        execution_config=CONFIG,
        quotes=(_quote(instrument=QQQ),),
    )

    assert result.state_for(SPY).state is LifecycleState.PENDING_ENTRY
    assert result.execution_outcomes[0].reason is ExecutionReason.NO_LEGAL_QUOTE


def test_future_market_evidence_preserves_completed_execution_prefix() -> None:
    first_opportunity = _opportunity("first", TIME + timedelta(microseconds=1))
    prefix = process_lifecycle(
        [_signal_event(), first_opportunity], execution_config=CONFIG, quotes=(_quote(),)
    )
    future_time = TIME + timedelta(seconds=2)
    future_quote = _quote(
        bid="100", ask="100.5", observation_time=future_time, availability_time=future_time
    )
    expanded = process_lifecycle(
        [_signal_event(), first_opportunity, _opportunity("future", future_time)],
        execution_config=CONFIG,
        quotes=(_quote(), future_quote),
    )

    assert expanded.records[: len(prefix.records)] == prefix.records
    assert (
        expanded.execution_attempts[: len(prefix.execution_attempts)] == prefix.execution_attempts
    )
    assert (
        expanded.execution_outcomes[: len(prefix.execution_outcomes)] == prefix.execution_outcomes
    )
