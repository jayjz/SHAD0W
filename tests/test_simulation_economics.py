"""P0.5C runner integration and upstream non-authority regressions."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from shadow.domain import Instrument
from shadow.execution import ExecutionEconomicsConfig, ExecutionStatus
from shadow.simulation import SimulationContractError, SimulationInput, run_simulation
from tests.test_simulation_runner import (
    QQQ,
    SPY,
    _bar,
    _config,
    _input,
    _opportunity,
    _quote,
)


def _economics(
    instrument: Instrument = SPY,
    *,
    quantity: str = "2",
    fee_bps: str = "10",
    currency: str = "USD",
) -> ExecutionEconomicsConfig:
    return ExecutionEconomicsConfig(
        instrument=instrument,
        quantity=Decimal(quantity),
        quote_currency=currency,
        fee_bps=Decimal(fee_bps),
    )


def _filled_input() -> SimulationInput:
    bars = (_bar(SPY, "10", 0), _bar(SPY, "9", 1))
    return _input(
        bars,
        opportunities=(
            _opportunity(
                "entry-opportunity",
                SPY,
                bars[-1].availability_time + timedelta(microseconds=1),
            ),
        ),
    )


def test_economics_is_explicitly_disabled_by_none() -> None:
    result = run_simulation(_filled_input())

    assert result.economics_configs is None
    assert result.economic_executions == ()
    assert len(result.execution_outcomes) == 1
    assert result.execution_outcomes[0].status is ExecutionStatus.FILLED


def test_existing_positional_simulation_input_remains_price_only_compatible() -> None:
    source = _filled_input()
    legacy = SimulationInput(
        source.dataset_metadata,
        source.bars,
        source.strategy_configs,
        source.execution_config,
        source.quotes,
        source.execution_opportunities,
        source.simulation_id,
    )

    assert legacy.economics_configs is None
    assert run_simulation(legacy).execution_outcomes == run_simulation(source).execution_outcomes


def test_changing_economics_cannot_change_upstream_or_lifecycle_evidence() -> None:
    simulation_input = _filled_input()
    first = run_simulation(replace(simulation_input, economics_configs=(_economics(),)))
    second = run_simulation(
        replace(
            simulation_input,
            economics_configs=(_economics(quantity="0.25", fee_bps="250", currency="EUR"),),
        )
    )

    assert first.features == second.features
    assert first.evaluations == second.evaluations
    assert first.signals == second.signals
    assert first.timeline == second.timeline
    assert first.lifecycle == second.lifecycle
    assert first.execution_attempts == second.execution_attempts
    assert first.execution_outcomes == second.execution_outcomes
    assert first.economic_executions != second.economic_executions
    assert first.economic_executions[0].outcome is first.execution_outcomes[0]
    assert second.economic_executions[0].outcome is second.execution_outcomes[0]


def test_retry_attaches_economics_only_to_later_fill() -> None:
    bars = (_bar(SPY, "10", 0), _bar(SPY, "9", 1))
    first_time = bars[-1].availability_time + timedelta(microseconds=1)
    retry_time = bars[-1].availability_time + timedelta(seconds=1)
    result = run_simulation(
        replace(
            _input(
                bars,
                quotes=(
                    _quote(
                        observation_time=retry_time,
                        availability_time=retry_time,
                    ),
                ),
                opportunities=(
                    _opportunity("first", SPY, first_time),
                    _opportunity("retry", SPY, retry_time),
                ),
            ),
            economics_configs=(_economics(),),
        )
    )

    assert [outcome.status for outcome in result.execution_outcomes] == [
        ExecutionStatus.UNFILLED,
        ExecutionStatus.FILLED,
    ]
    assert len(result.economic_executions) == 1
    assert result.economic_executions[0].outcome == result.execution_outcomes[1]


def test_multi_instrument_configs_are_canonical_and_isolated() -> None:
    spy_zero, qqq_zero = _bar(SPY, "10", 0), _bar(QQQ, "20", 0)
    spy_one, qqq_one = _bar(SPY, "9", 1), _bar(QQQ, "19", 1)
    opportunity_time = spy_one.availability_time + timedelta(microseconds=1)
    input_value = _input(
        (spy_zero, qqq_zero, spy_one, qqq_one),
        configs=(_config(SPY), _config(QQQ)),
        quotes=(_quote(SPY), _quote(QQQ, bid="198", ask="202")),
        opportunities=(
            _opportunity("spy-fill", SPY, opportunity_time),
            _opportunity("qqq-fill", QQQ, opportunity_time),
        ),
    )
    first = run_simulation(
        replace(
            input_value,
            economics_configs=(
                _economics(SPY, quantity="2", fee_bps="10"),
                _economics(QQQ, quantity="0.5", fee_bps="20", currency="CAD"),
            ),
        )
    )
    second = run_simulation(
        replace(
            input_value,
            economics_configs=tuple(reversed(first.economics_configs or ())),
        )
    )

    assert first == second
    assert [config.instrument for config in first.economics_configs or ()] == [QQQ, SPY]
    by_instrument = {evidence.instrument: evidence for evidence in first.economic_executions}
    assert by_instrument[SPY].quantity == Decimal(2)
    assert by_instrument[SPY].execution_price == Decimal(101)
    assert by_instrument[QQQ].quantity == Decimal("0.5")
    assert by_instrument[QQQ].quote_currency == "CAD"
    assert by_instrument[QQQ].execution_price == Decimal(202)


def test_enabled_economics_requires_exact_instrument_coverage_and_no_duplicates() -> None:
    bars = (_bar(SPY, "10", 0), _bar(QQQ, "20", 0))
    input_value = _input(
        bars,
        configs=(_config(SPY), _config(QQQ)),
        quotes=(),
    )

    with pytest.raises(SimulationContractError, match="missing=.*QQQ"):
        replace(input_value, economics_configs=(_economics(SPY),))
    with pytest.raises(SimulationContractError, match="duplicates"):
        replace(
            input_value,
            economics_configs=(
                _economics(SPY),
                _economics(SPY, quantity="3"),
                _economics(QQQ),
            ),
        )
    with pytest.raises(SimulationContractError, match="extra=.*QQQ"):
        replace(
            _input((_bar(SPY, "10", 0),), quotes=()),
            economics_configs=(_economics(SPY), _economics(QQQ)),
        )
    with pytest.raises(SimulationContractError, match="missing"):
        replace(input_value, economics_configs=())


def test_future_inputs_preserve_prior_economic_execution_prefix() -> None:
    prefix_input = _filled_input()
    economics_configs = (_economics(),)
    prefix = run_simulation(replace(prefix_input, economics_configs=economics_configs))
    future_bar = _bar(SPY, "10", 2)
    future_time = future_bar.availability_time + timedelta(microseconds=1)
    expanded = run_simulation(
        replace(
            _input(
                (*prefix_input.bars, future_bar),
                quotes=(
                    *prefix_input.quotes,
                    _quote(
                        bid="102",
                        ask="103",
                        observation_time=future_time,
                        availability_time=future_time,
                    ),
                ),
                opportunities=(
                    *prefix_input.execution_opportunities,
                    _opportunity("exit-opportunity", SPY, future_time),
                ),
            ),
            economics_configs=economics_configs,
        )
    )

    assert expanded.execution_outcomes[: len(prefix.execution_outcomes)] == (
        prefix.execution_outcomes
    )
    assert expanded.economic_executions[: len(prefix.economic_executions)] == (
        prefix.economic_executions
    )
    assert len(expanded.economic_executions) == 2


def test_same_simulation_input_produces_identical_economic_evidence() -> None:
    simulation_input = replace(_filled_input(), economics_configs=(_economics(),))
    assert run_simulation(simulation_input) == run_simulation(simulation_input)
