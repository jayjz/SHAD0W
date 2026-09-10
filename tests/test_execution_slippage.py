"""Price-level P0.5B counterexamples; fixture bps are not market estimates."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import ROUND_UP, Decimal, Inexact, localcontext

import pytest

from shadow.domain import Quote
from shadow.execution import (
    ExecutionActionType,
    ExecutionContractError,
    ExecutionStatus,
    execution_sensitivity,
    resolve_execution_attempt,
    resolve_execution_attempts,
)
from shadow.simulation import LifecycleState, process_lifecycle
from tests.test_execution import (
    CONFIG,
    QQQ,
    SPY,
    TIME,
    _attempt,
    _opportunity,
    _quote,
    _signal_event,
)


@pytest.mark.parametrize(
    "action,baseline,expected",
    [
        (ExecutionActionType.ENTRY, "101", "101.101"),
        (ExecutionActionType.EXIT, "99", "98.901"),
    ],
)
def test_side_spread_manual_calculation_and_exact_zero(
    action: ExecutionActionType, baseline: str, expected: str
) -> None:
    attempt = _attempt(action_type=action)
    quote = _quote()
    zero, slipped = execution_sensitivity(attempt, (quote,), CONFIG, (Decimal(0), Decimal(10)))
    assert zero.execution_price == Decimal(baseline)
    assert zero.execution_price is zero.baseline_execution_price
    assert slipped.baseline_execution_price == Decimal(baseline)
    assert slipped.execution_price == Decimal(expected)
    assert slipped.market_evidence is quote
    assert slipped.market_evidence_reference == zero.market_evidence_reference
    locked = resolve_execution_attempt(
        attempt, (_quote(bid="100", ask="100"),), replace(CONFIG, slippage_bps=Decimal(10))
    )
    assert locked.execution_price == (
        Decimal("100.10") if action is ExecutionActionType.ENTRY else Decimal("99.90")
    )


@pytest.mark.parametrize("action", list(ExecutionActionType))
def test_sensitivity_monotonic_canonical_repeatable_and_immutable(
    action: ExecutionActionType,
) -> None:
    scenarios = tuple(map(Decimal, (0, 1, 5, 10, 10000, 20000)))
    quotes = (_quote(), _quote(instrument=QQQ, bid="1", ask="2"))
    outcomes = execution_sensitivity(_attempt(action_type=action), quotes, CONFIG, scenarios)
    assert outcomes == execution_sensitivity(
        _attempt(action_type=action), reversed(quotes), CONFIG, reversed(scenarios)
    )
    prices = [outcome.execution_price for outcome in outcomes]
    assert all(price is not None for price in prices)
    for left, right in zip(prices, prices[1:], strict=False):
        assert left is not None and right is not None
        assert left <= right if action is ExecutionActionType.ENTRY else left >= right
    with pytest.raises(FrozenInstanceError):
        outcomes[0].execution_config.slippage_bps = Decimal(99)  # type: ignore[misc]
    assert outcomes[0].execution_config.slippage_bps == 0
    assert execution_sensitivity(_attempt(), (), CONFIG, ()) == ()


@pytest.mark.parametrize(
    "bps",
    [Decimal(-1), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), 1.0],
)
def test_invalid_slippage_is_rejected(bps: Decimal) -> None:
    with pytest.raises(ExecutionContractError, match="finite nonnegative Decimal"):
        replace(CONFIG, slippage_bps=bps)
    with pytest.raises(ExecutionContractError):
        execution_sensitivity(_attempt(), (), CONFIG, (bps,))


@pytest.mark.parametrize(
    "quotes",
    [
        (),
        (_quote(bid="102", ask="100"),),
        (_quote(observation_time=TIME - timedelta(seconds=10)),),
        (_quote(availability_time=TIME + timedelta(seconds=10)),),
        (_quote(instrument=QQQ),),
    ],
)
def test_slippage_cannot_rescue_failed_evidence(quotes: tuple[Quote, ...]) -> None:
    baseline = resolve_execution_attempt(_attempt(), quotes, CONFIG)
    slipped = resolve_execution_attempt(
        _attempt(), quotes, replace(CONFIG, slippage_bps=Decimal(10))
    )
    assert slipped.status is baseline.status
    assert slipped.status is not ExecutionStatus.FILLED
    assert slipped.reason is baseline.reason
    assert slipped.execution_price is slipped.baseline_execution_price is None
    assert slipped.market_evidence == baseline.market_evidence
    duplicate = resolve_execution_attempts(
        (_attempt(), _attempt()), quotes, slipped.execution_config
    )
    assert all(item.status is ExecutionStatus.REJECTED for item in duplicate)


@pytest.mark.parametrize("action", list(ExecutionActionType))
def test_hostile_decimal_context_and_rounding_cannot_improve_price(
    action: ExecutionActionType,
) -> None:
    quote = _quote(
        bid="100.00000000000000000000000000000001", ask="100.00000000000000000000000000000009"
    )
    scenarios = (Decimal(0), Decimal("1e-80"), Decimal(1), Decimal(10))
    attempt = _attempt(action_type=action)
    expected = execution_sensitivity(attempt, (quote,), CONFIG, scenarios)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.Emin = -2
        context.Emax = 2
        context.traps[Inexact] = True
        actual = execution_sensitivity(attempt, (quote,), CONFIG, scenarios)
    assert actual == expected
    baseline = expected[0].execution_price
    assert baseline is not None
    for outcome in actual:
        assert outcome.execution_price is not None
        assert (
            outcome.execution_price >= baseline
            if action is ExecutionActionType.ENTRY
            else outcome.execution_price <= baseline
        )


@pytest.mark.parametrize("action", list(ExecutionActionType))
def test_negative_baseline_cannot_reverse_adversity(action: ExecutionActionType) -> None:
    quote = _quote(bid="-101", ask="-99")
    attempt = _attempt(action_type=action)
    assert resolve_execution_attempt(attempt, (quote,), CONFIG).status is ExecutionStatus.FILLED
    with pytest.raises(ExecutionContractError, match="nonnegative executable price"):
        resolve_execution_attempt(attempt, (quote,), replace(CONFIG, slippage_bps=Decimal(1)))


def test_overflow_is_contract_failure_and_large_finite_assumptions_have_no_policy_cap() -> None:
    config = replace(CONFIG, slippage_bps=Decimal("1e1000010"))
    with pytest.raises(ExecutionContractError, match="numeric domain"):
        resolve_execution_attempt(_attempt(), (_quote(),), config)
    assert resolve_execution_attempt(_attempt(), (), config).status is ExecutionStatus.UNFILLED


def test_retry_and_future_quotes_preserve_each_attempts_slippage_evidence() -> None:
    config = replace(CONFIG, slippage_bps=Decimal(10))
    events = (_signal_event(), _opportunity("first", TIME + timedelta(microseconds=1)))
    crossed = _quote(bid="102", ask="100")
    prefix = process_lifecycle(events, execution_config=config, quotes=(crossed,))
    future_time = TIME + timedelta(seconds=1)
    future = _quote(
        bid="98", ask="100", observation_time=future_time, availability_time=future_time
    )
    expanded = process_lifecycle(
        (*events, _opportunity("retry", future_time)),
        execution_config=config,
        quotes=(crossed, future),
    )
    assert expanded.execution_outcomes[:1] == prefix.execution_outcomes
    assert prefix.state_for(SPY).state is LifecycleState.PENDING_ENTRY
    assert expanded.state_for(SPY).state is LifecycleState.HOLDING
    assert expanded.execution_outcomes[-1].execution_price == Decimal("100.10")
    before = expanded.execution_outcomes
    later_config = replace(config, slippage_bps=Decimal(100))
    resolve_execution_attempt(before[-1].attempt, (future,), later_config)
    assert expanded.execution_outcomes == before


def test_quote_reference_does_not_inherit_decimal_capitals() -> None:
    quote = _quote(bid="1e10", ask="2e10")
    config = replace(CONFIG, slippage_bps=Decimal(10))
    outcome = resolve_execution_attempt(_attempt(), (quote,), config)
    expected = outcome.market_evidence_reference
    with localcontext() as context:
        context.capitals = 0
        assert outcome.market_evidence_reference == expected


def test_equivalent_scenario_representations_have_canonical_evidence() -> None:
    assumptions = (Decimal("1.00"), Decimal("1"), Decimal("-0.0"), Decimal("0"))
    first = execution_sensitivity(_attempt(), (_quote(),), CONFIG, assumptions)
    second = execution_sensitivity(_attempt(), (_quote(),), CONFIG, reversed(assumptions))
    assert repr(first) == repr(second)


def test_future_quote_cannot_rewrite_missing_quote_outcome() -> None:
    config = replace(CONFIG, slippage_bps=Decimal(10))
    prefix = resolve_execution_attempt(_attempt(), (), config)
    future_time = TIME + timedelta(seconds=1)
    future = _quote(observation_time=future_time, availability_time=future_time)
    assert resolve_execution_attempt(_attempt(), (future,), config) == prefix
