"""End-to-end adversarial tests for P0.4C chronological composition."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shadow.domain import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    DatasetMetadata,
    Instrument,
    MarketDataValidationError,
    Provenance,
    Quote,
    ValidationStatus,
)
from shadow.execution import QuoteExecutionConfig
from shadow.features import FeatureState
from shadow.simulation import (
    ExecutionOpportunity,
    LifecycleDecision,
    LifecycleRecord,
    LifecycleState,
    SimulationContractError,
    SimulationInput,
    SimulationResult,
    StrategyEvaluationDisposition,
    run_simulation,
)
from shadow.strategies import MeanReversionConfig, SignalType

START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
INTERVAL = BarInterval(timedelta(minutes=1))
SPY = Instrument("SPY")
QQQ = Instrument("QQQ")
EXECUTION_CONFIG = QuoteExecutionConfig(maximum_quote_age=timedelta(days=1))


def _bar(
    instrument: Instrument,
    close: str,
    minute: int,
    *,
    availability_time: datetime | None = None,
) -> Bar:
    observation_time = START + timedelta(minutes=minute)
    return Bar(
        instrument=instrument,
        interval=INTERVAL,
        observation_time=observation_time,
        availability_time=availability_time or observation_time,
        availability_semantics=AvailabilitySemantics.MODELED,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1"),
        provenance=Provenance("synthetic"),
    )


def _metadata(bars: tuple[Bar, ...]) -> DatasetMetadata:
    return DatasetMetadata(
        source="synthetic",
        dataset_id="p04c-fixture",
        instruments=tuple({bar.instrument for bar in bars}),
        interval=INTERVAL,
        coverage_start=min(bar.observation_time for bar in bars),
        coverage_end=max(bar.observation_time for bar in bars),
        validation_status=ValidationStatus.VALIDATED,
    )


def _config(instrument: Instrument) -> MeanReversionConfig:
    return MeanReversionConfig(
        instrument=instrument,
        configuration_id=f"p04c-{instrument.identifier.lower()}-zscore-v1",
        rolling_window=2,
        entry_threshold=Decimal("-1"),
        exit_threshold=Decimal("0"),
        maximum_feature_age=timedelta(days=1),
    )


def _opportunity(
    event_id: str, instrument: Instrument, event_time: datetime
) -> ExecutionOpportunity:
    return ExecutionOpportunity(
        event_id=event_id,
        instrument=instrument,
        event_time=event_time,
        source_reference=f"synthetic-opportunity:{event_id}",
    )


def _quote(
    instrument: Instrument = SPY,
    *,
    bid: str = "99",
    ask: str = "101",
    observation_time: datetime = START,
    availability_time: datetime = START,
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


def _input(
    bars: tuple[Bar, ...],
    *,
    configs: tuple[MeanReversionConfig, ...] = (_config(SPY),),
    quotes: tuple[Quote, ...] = (_quote(),),
    opportunities: tuple[ExecutionOpportunity, ...] = (),
) -> SimulationInput:
    return SimulationInput(
        dataset_metadata=_metadata(bars),
        bars=bars,
        strategy_configs=configs,
        execution_config=EXECUTION_CONFIG,
        quotes=quotes,
        execution_opportunities=opportunities,
        simulation_id="p04c-integration-fixture",
    )


def _lifecycle_record(result: SimulationResult, event_id: str) -> LifecycleRecord:
    return next(record for record in result.lifecycle.records if record.event_reference == event_id)


def test_complete_entry_path_preserves_same_bar_ineligibility_end_to_end() -> None:
    bars = (_bar(SPY, "10", 0), _bar(SPY, "9", 1))
    entry_time = bars[-1].availability_time
    result = run_simulation(
        _input(
            bars,
            opportunities=(
                _opportunity("same-bar", SPY, entry_time),
                _opportunity("later", SPY, entry_time + timedelta(microseconds=1)),
            ),
        )
    )

    assert [signal.signal_type for signal in result.signals] == [SignalType.LONG_ENTRY]
    assert len(result.evaluations) == len(result.features) == 2
    assert result.evaluations[-1].disposition is StrategyEvaluationDisposition.SIGNAL_PROPOSED
    assert _lifecycle_record(result, "same-bar").decision is LifecycleDecision.INELIGIBLE
    later = _lifecycle_record(result, "later")
    assert later.decision is LifecycleDecision.ACTION_FILLED
    assert result.lifecycle.state_for(SPY).state is LifecycleState.HOLDING
    assert result.lifecycle.state_for(SPY).open_position is not None


def test_legal_exit_returns_holding_lifecycle_to_flat() -> None:
    bars = (_bar(SPY, "10", 0), _bar(SPY, "9", 1), _bar(SPY, "10", 2))
    entry_time = bars[1].availability_time
    exit_time = bars[2].availability_time
    result = run_simulation(
        _input(
            bars,
            opportunities=(
                _opportunity("entry-opportunity", SPY, entry_time + timedelta(microseconds=1)),
                _opportunity("exit-opportunity", SPY, exit_time + timedelta(microseconds=1)),
            ),
        )
    )

    assert [signal.signal_type for signal in result.signals] == [
        SignalType.LONG_ENTRY,
        SignalType.EXIT,
    ]
    assert _lifecycle_record(result, "exit-opportunity").decision is LifecycleDecision.ACTION_FILLED
    assert result.lifecycle.state_for(SPY).state is LifecycleState.FLAT
    assert result.lifecycle.open_positions == ()


def test_pending_states_suppress_repeated_strategy_actions() -> None:
    bars = (
        _bar(SPY, "10", 0),
        _bar(SPY, "9", 1),
        _bar(SPY, "8", 2),
        _bar(SPY, "10", 3),
        _bar(SPY, "11", 4),
    )
    result = run_simulation(
        _input(
            bars,
            opportunities=(
                _opportunity(
                    "entry-opportunity", SPY, bars[2].availability_time + timedelta(seconds=1)
                ),
            ),
        )
    )

    assert [signal.signal_type for signal in result.signals] == [
        SignalType.LONG_ENTRY,
        SignalType.EXIT,
    ]
    assert result.evaluations[2].disposition is StrategyEvaluationDisposition.PENDING_LIFECYCLE
    assert result.evaluations[4].disposition is StrategyEvaluationDisposition.PENDING_LIFECYCLE
    assert result.lifecycle.state_for(SPY).state is LifecycleState.PENDING_EXIT
    assert len(result.lifecycle.unresolved_actions) == 1


def test_delayed_availability_prevents_retroactive_feature_signal_or_action() -> None:
    delayed_time = START + timedelta(minutes=3)
    bars = (
        _bar(SPY, "10", 0),
        _bar(SPY, "9", 1, availability_time=delayed_time),
        _bar(SPY, "8", 2),
    )
    result = run_simulation(
        _input(
            bars,
            opportunities=(
                _opportunity("before-delivery", SPY, START + timedelta(minutes=2)),
                _opportunity("delivery-instant", SPY, delayed_time),
                _opportunity("after-delivery", SPY, delayed_time + timedelta(microseconds=1)),
            ),
        )
    )

    ready_features = [feature for feature in result.features if feature.state is FeatureState.READY]
    assert [feature.availability_time for feature in ready_features] == [delayed_time, delayed_time]
    assert len(result.evaluations) == 2
    assert result.evaluations[-1].snapshot.observation_time == bars[-1].observation_time
    assert result.evaluations[-1].disposition is StrategyEvaluationDisposition.SIGNAL_PROPOSED
    assert len(result.signals) == 1
    assert all(signal.decision_time >= delayed_time for signal in result.signals)
    assert _lifecycle_record(result, "delivery-instant").decision is LifecycleDecision.INELIGIBLE
    assert _lifecycle_record(result, "after-delivery").decision is LifecycleDecision.ACTION_FILLED
    assert result.lifecycle.state_for(SPY).state is LifecycleState.HOLDING


def test_ready_no_signal_and_unavailable_feature_remain_distinct_evidence() -> None:
    no_signal = run_simulation(_input((_bar(SPY, "10", 0), _bar(SPY, "11", 1))))
    unavailable = run_simulation(_input((_bar(SPY, "10", 0), _bar(SPY, "10", 1))))

    assert no_signal.evaluations[-1].disposition is StrategyEvaluationDisposition.NO_SIGNAL
    assert no_signal.signals == ()
    assert unavailable.features[-1].state is FeatureState.UNAVAILABLE
    assert unavailable.evaluations[-1].disposition is (
        StrategyEvaluationDisposition.FEATURE_NOT_ACTIONABLE
    )
    assert unavailable.signals == ()


def test_interleaved_instruments_are_isolated_and_equal_time_order_is_canonical() -> None:
    spy_zero, qqq_zero = _bar(SPY, "10", 0), _bar(QQQ, "20", 0)
    spy_one, qqq_one = _bar(SPY, "9", 1), _bar(QQQ, "19", 1)
    opportunities = (
        _opportunity("qqq-before-signal", QQQ, qqq_zero.availability_time),
        _opportunity(
            "spy-after-signal", SPY, spy_one.availability_time + timedelta(microseconds=1)
        ),
    )
    first = run_simulation(
        _input(
            (spy_zero, qqq_zero, spy_one, qqq_one),
            configs=(_config(SPY), _config(QQQ)),
            opportunities=opportunities,
        )
    )
    second = run_simulation(
        _input(
            (qqq_zero, spy_zero, qqq_one, spy_one),
            configs=(_config(QQQ), _config(SPY)),
            opportunities=tuple(reversed(opportunities)),
        )
    )

    assert first == second
    assert first.lifecycle.state_for(SPY).state is LifecycleState.HOLDING
    assert first.lifecycle.state_for(QQQ).state is LifecycleState.PENDING_ENTRY
    assert len(first.lifecycle.unresolved_actions) == 1


def test_future_extension_preserves_historical_simulation_evidence() -> None:
    prefix_bars = (_bar(SPY, "10", 0), _bar(SPY, "9", 1))
    same_time = _opportunity("same-time", SPY, prefix_bars[-1].availability_time)
    prefix = run_simulation(_input(prefix_bars, opportunities=(same_time,)))

    future_bar = _bar(SPY, "8", 2)
    expanded = run_simulation(
        _input(
            (*prefix_bars, future_bar),
            opportunities=(
                same_time,
                _opportunity(
                    "future-opportunity",
                    SPY,
                    future_bar.availability_time + timedelta(microseconds=1),
                ),
            ),
        )
    )

    assert expanded.features[: len(prefix.features)] == prefix.features
    assert expanded.evaluations[: len(prefix.evaluations)] == prefix.evaluations
    assert expanded.signals[: len(prefix.signals)] == prefix.signals
    assert expanded.timeline.ordered_events[: len(prefix.timeline.ordered_events)] == (
        prefix.timeline.ordered_events
    )
    assert expanded.timeline.records[: len(prefix.timeline.records)] == prefix.timeline.records
    assert expanded.lifecycle.records[: len(prefix.lifecycle.records)] == prefix.lifecycle.records
    assert prefix.lifecycle.state_for(SPY).state is LifecycleState.PENDING_ENTRY
    assert expanded.lifecycle.state_for(SPY).state is LifecycleState.HOLDING


def test_future_quote_cannot_rewrite_completed_execution_outcome_end_to_end() -> None:
    prefix_bars = (_bar(SPY, "10", 0), _bar(SPY, "9", 1))
    first_opportunity = _opportunity(
        "entry-opportunity", SPY, prefix_bars[-1].availability_time + timedelta(microseconds=1)
    )
    prefix = run_simulation(
        _input(prefix_bars, opportunities=(first_opportunity,), quotes=(_quote(),))
    )

    future_bar = _bar(SPY, "8", 2)
    future_quote_time = future_bar.availability_time
    expanded = run_simulation(
        _input(
            (*prefix_bars, future_bar),
            opportunities=(
                first_opportunity,
                _opportunity("future-opportunity", SPY, future_quote_time),
            ),
            quotes=(
                _quote(),
                _quote(
                    bid="100",
                    ask="100.5",
                    observation_time=future_quote_time,
                    availability_time=future_quote_time,
                ),
            ),
        )
    )

    assert prefix.execution_outcomes[0].execution_price == Decimal("101")
    assert (
        expanded.execution_attempts[: len(prefix.execution_attempts)] == prefix.execution_attempts
    )
    assert (
        expanded.execution_outcomes[: len(prefix.execution_outcomes)] == prefix.execution_outcomes
    )


def test_end_of_stream_retains_pending_entry_holding_and_pending_exit() -> None:
    entry_bars = (_bar(SPY, "10", 0), _bar(SPY, "9", 1))
    pending_entry = run_simulation(_input(entry_bars))
    holding = run_simulation(
        _input(
            entry_bars,
            opportunities=(
                _opportunity(
                    "entry-opportunity",
                    SPY,
                    entry_bars[-1].availability_time + timedelta(microseconds=1),
                ),
            ),
        )
    )
    exit_bars = (*entry_bars, _bar(SPY, "10", 2))
    pending_exit = run_simulation(
        _input(
            exit_bars,
            opportunities=(
                _opportunity(
                    "entry-opportunity",
                    SPY,
                    entry_bars[-1].availability_time + timedelta(microseconds=1),
                ),
            ),
        )
    )

    assert pending_entry.lifecycle.state_for(SPY).state is LifecycleState.PENDING_ENTRY
    assert holding.lifecycle.state_for(SPY).state is LifecycleState.HOLDING
    assert pending_exit.lifecycle.state_for(SPY).state is LifecycleState.PENDING_EXIT
    assert len(pending_exit.lifecycle.open_positions) == 1
    assert len(pending_exit.lifecycle.unresolved_actions) == 1


def test_runner_requires_declared_validated_data_and_preserves_p01_order_failure() -> None:
    bars = (_bar(SPY, "10", 0), _bar(SPY, "9", 1))
    unvalidated = SimulationInput(
        dataset_metadata=replace(_metadata(bars), validation_status=ValidationStatus.UNVALIDATED),
        bars=bars,
        strategy_configs=(_config(SPY),),
        execution_config=EXECUTION_CONFIG,
    )
    with pytest.raises(SimulationContractError, match="validated status"):
        run_simulation(unvalidated)

    with pytest.raises(MarketDataValidationError, match="chronological_order"):
        run_simulation(_input((bars[1], bars[0])))


def test_result_retains_structured_evidence_without_economic_artifacts() -> None:
    result = run_simulation(_input((_bar(SPY, "10", 0), _bar(SPY, "9", 1))))

    assert result.dataset_fingerprint
    assert result.features
    assert result.evaluations
    assert result.signals
    assert result.timeline.records
    assert result.lifecycle.records
    assert {
        "fill_price",
        "commission",
        "cash",
        "pnl",
        "return",
        "equity_curve",
        "sharpe",
        "drawdown",
    }.isdisjoint(SimulationResult.__dataclass_fields__)
