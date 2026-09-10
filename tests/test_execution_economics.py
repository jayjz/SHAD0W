"""Adversarial P0.5C tests for fixed-quantity execution economics."""

from dataclasses import FrozenInstanceError, replace
from decimal import ROUND_UP, Decimal, Inexact, localcontext

import pytest

from shadow.domain import Instrument
from shadow.execution import (
    ECONOMICS_MODEL_ID,
    EconomicExecution,
    ExecutionActionType,
    ExecutionEconomicsConfig,
    ExecutionEconomicsError,
    ExecutionOutcome,
    ExecutionSide,
    ExecutionStatus,
    QuoteExecutionConfig,
    attach_execution_economics,
    resolve_execution_attempt,
)
from tests.test_execution import CONFIG, QQQ, SPY, _attempt, _quote


def _economics_config(
    *,
    instrument: Instrument = SPY,
    quantity: Decimal = Decimal(2),
    quote_currency: str = "USD",
    fee_bps: Decimal = Decimal(10),
) -> ExecutionEconomicsConfig:
    return ExecutionEconomicsConfig(
        instrument=instrument,
        quantity=quantity,
        quote_currency=quote_currency,
        fee_bps=fee_bps,
    )


def _filled(
    *,
    action_type: ExecutionActionType = ExecutionActionType.ENTRY,
    bid: str = "99",
    ask: str = "101.10",
    execution_config: QuoteExecutionConfig = CONFIG,
) -> ExecutionOutcome:
    outcome = resolve_execution_attempt(
        _attempt(action_type=action_type),
        (_quote(bid=bid, ask=ask),),
        execution_config,
    )
    assert outcome.status is ExecutionStatus.FILLED
    return outcome


@pytest.mark.parametrize(
    "action,bid,ask,expected_cash_flow",
    [
        (ExecutionActionType.ENTRY, "99", "101.10", Decimal("-202.4022")),
        (ExecutionActionType.EXIT, "101.10", "102", Decimal("201.9978")),
    ],
)
def test_manual_buy_and_sell_economics(
    action: ExecutionActionType,
    bid: str,
    ask: str,
    expected_cash_flow: Decimal,
) -> None:
    evidence = attach_execution_economics(
        _filled(action_type=action, bid=bid, ask=ask),
        _economics_config(),
    )

    assert evidence is not None
    assert evidence.execution_price == Decimal("101.10")
    assert evidence.quantity == Decimal(2)
    assert evidence.signed_notional == Decimal("202.20")
    assert evidence.gross_notional == Decimal("202.20")
    assert evidence.fee == Decimal("0.20220")
    assert evidence.execution_cash_flow == expected_cash_flow
    assert evidence.side is (
        ExecutionSide.BUY if action is ExecutionActionType.ENTRY else ExecutionSide.SELL
    )


def test_fractional_quantity_and_explicit_zero_fee_are_exact() -> None:
    evidence = attach_execution_economics(
        _filled(),
        _economics_config(quantity=Decimal("0.125"), fee_bps=Decimal("-0.00")),
    )

    assert evidence is not None
    assert evidence.signed_notional == Decimal("12.6375")
    assert evidence.gross_notional == Decimal("12.6375")
    assert evidence.fee == Decimal(0)
    assert evidence.execution_cash_flow == Decimal("-12.6375")
    assert evidence.config.fee_bps == Decimal(0)


@pytest.mark.parametrize(
    "quantity",
    [
        Decimal(0),
        Decimal(-1),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        1.0,
    ],
)
def test_invalid_quantity_is_rejected(quantity: object) -> None:
    with pytest.raises(ExecutionEconomicsError):
        _economics_config(quantity=quantity)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fee_bps",
    [
        Decimal(-1),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        1.0,
    ],
)
def test_invalid_fee_is_rejected(fee_bps: object) -> None:
    with pytest.raises(ExecutionEconomicsError):
        _economics_config(fee_bps=fee_bps)  # type: ignore[arg-type]


@pytest.mark.parametrize("quote_currency", ["", " USD", "USD ", 1])
def test_invalid_quote_currency_is_rejected(quote_currency: object) -> None:
    with pytest.raises(ExecutionEconomicsError, match="quote_currency"):
        _economics_config(quote_currency=quote_currency)  # type: ignore[arg-type]


def test_config_is_immutable_versioned_and_canonical() -> None:
    first = _economics_config(quantity=Decimal("2.00"), fee_bps=Decimal("-0.000"))
    second = _economics_config(quantity=Decimal(2), fee_bps=Decimal(0))

    assert first == second
    assert repr(first) == repr(second)
    assert first.quantity == Decimal(2)
    assert first.fee_bps == Decimal(0)
    assert first.economics_model_id == ECONOMICS_MODEL_ID
    with pytest.raises(FrozenInstanceError):
        first.quantity = Decimal(3)  # type: ignore[misc]
    with pytest.raises(ExecutionEconomicsError, match="economics_model_id"):
        replace(first, economics_model_id="shadow.execution.fixed_quantity_fee_bps.v2")

    assert first != replace(first, instrument=QQQ)
    assert first != replace(first, quantity=Decimal(3))
    assert first != replace(first, fee_bps=Decimal(1))
    assert first != replace(first, quote_currency="EUR")


def test_only_filled_outcomes_produce_economic_evidence() -> None:
    unfilled = resolve_execution_attempt(_attempt(), (), CONFIG)
    rejected = resolve_execution_attempt(_attempt(), (_quote(bid="102", ask="100"),), CONFIG)
    config = _economics_config()

    assert unfilled.status is ExecutionStatus.UNFILLED
    assert rejected.status is ExecutionStatus.REJECTED
    assert attach_execution_economics(unfilled, config) is None
    assert attach_execution_economics(rejected, config) is None
    with pytest.raises(ExecutionEconomicsError, match="FILLED"):
        EconomicExecution(
            outcome=unfilled,
            config=config,
            gross_notional=Decimal(0),
            fee=Decimal(0),
        )


def test_fee_uses_final_slipped_price_without_double_charging_spread_or_slippage() -> None:
    execution_config = replace(CONFIG, slippage_bps=Decimal(10))
    outcome = _filled(bid="99", ask="101", execution_config=execution_config)
    evidence = attach_execution_economics(outcome, _economics_config())

    assert evidence is not None
    assert outcome.baseline_execution_price == Decimal("101")
    assert evidence.execution_price == Decimal("101.101")
    assert evidence.signed_notional == Decimal("202.202")
    assert evidence.fee == Decimal("0.202202")
    assert evidence.execution_cash_flow == Decimal("-202.404202")


def test_full_outcome_distinguishes_sensitivity_evidence_for_one_attempt() -> None:
    zero_outcome = _filled()
    slipped_outcome = _filled(execution_config=replace(CONFIG, slippage_bps=Decimal(10)))
    config = _economics_config()
    zero = attach_execution_economics(zero_outcome, config)
    slipped = attach_execution_economics(slipped_outcome, config)

    assert zero is not None and slipped is not None
    assert zero.attempt_reference == slipped.attempt_reference
    assert zero.outcome != slipped.outcome
    assert zero != slipped


def test_hostile_decimal_context_cannot_change_economics() -> None:
    outcome = _filled(
        bid="100.00000000000000000000000000000001",
        ask="100.00000000000000000000000000000009",
    )
    config = _economics_config(
        quantity=Decimal("1.2345678901234567890123456789012345"),
        fee_bps=Decimal("0.12345678901234567890123456789012345"),
    )
    expected = attach_execution_economics(outcome, config)

    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.Emin = -2
        context.Emax = 2
        context.traps[Inexact] = True
        actual = attach_execution_economics(outcome, config)

    assert actual == expected
    assert actual is not None and expected is not None
    assert actual.execution_cash_flow == expected.execution_cash_flow


@pytest.mark.parametrize(
    "price,quantity",
    [
        ("1e999999", Decimal("1e100")),
        ("1e-999999", Decimal("1e-100")),
    ],
)
def test_overflow_and_underflow_are_explicit_contract_failures(
    price: str, quantity: Decimal
) -> None:
    outcome = _filled(bid="0", ask=price)
    with pytest.raises(ExecutionEconomicsError, match="numeric domain"):
        attach_execution_economics(outcome, _economics_config(quantity=quantity))
    assert outcome.status is ExecutionStatus.FILLED
    assert outcome.execution_price == Decimal(price)


@pytest.mark.parametrize(
    "action,bid,ask,expected_signed,expected_fee,expected_cash_flow",
    [
        (
            ExecutionActionType.ENTRY,
            "-101",
            "-99",
            Decimal("-198"),
            Decimal("0.198"),
            Decimal("197.802"),
        ),
        (
            ExecutionActionType.EXIT,
            "-101",
            "-99",
            Decimal("-202"),
            Decimal("0.202"),
            Decimal("-202.202"),
        ),
        (
            ExecutionActionType.ENTRY,
            "-1",
            "-0",
            Decimal(0),
            Decimal(0),
            Decimal(0),
        ),
    ],
)
def test_zero_and_negative_stress_prices_preserve_signs_and_nonnegative_fees(
    action: ExecutionActionType,
    bid: str,
    ask: str,
    expected_signed: Decimal,
    expected_fee: Decimal,
    expected_cash_flow: Decimal,
) -> None:
    evidence = attach_execution_economics(
        _filled(action_type=action, bid=bid, ask=ask), _economics_config()
    )

    assert evidence is not None
    assert evidence.signed_notional == expected_signed
    assert evidence.gross_notional == abs(expected_signed)
    assert evidence.fee == expected_fee
    assert evidence.fee >= 0
    assert evidence.execution_cash_flow == expected_cash_flow
    if expected_signed == 0:
        assert not evidence.signed_notional.is_signed()
        assert not evidence.fee.is_signed()
        assert not evidence.execution_cash_flow.is_signed()


@pytest.mark.parametrize(
    "slippage_bps,expected_price,expected_fee,expected_cash_flow",
    [
        (Decimal("10000"), Decimal(0), Decimal(0), Decimal(0)),
        (Decimal("20000"), Decimal("-99"), Decimal("0.198"), Decimal("-198.198")),
    ],
)
def test_sell_slippage_stress_outputs_flow_into_economics_unchanged(
    slippage_bps: Decimal,
    expected_price: Decimal,
    expected_fee: Decimal,
    expected_cash_flow: Decimal,
) -> None:
    outcome = _filled(
        action_type=ExecutionActionType.EXIT,
        bid="99",
        ask="101",
        execution_config=replace(CONFIG, slippage_bps=slippage_bps),
    )
    evidence = attach_execution_economics(outcome, _economics_config())

    assert evidence is not None
    assert evidence.execution_price == expected_price
    assert evidence.outcome is outcome
    assert evidence.fee == expected_fee
    assert evidence.fee >= 0
    assert evidence.execution_cash_flow == expected_cash_flow


def test_instrument_mismatch_and_forged_values_fail_explicitly() -> None:
    outcome = _filled()
    with pytest.raises(ExecutionEconomicsError, match="instrument"):
        attach_execution_economics(outcome, _economics_config(instrument=QQQ))
    with pytest.raises(ExecutionEconomicsError, match="must match"):
        EconomicExecution(
            outcome=outcome,
            config=_economics_config(),
            gross_notional=Decimal(1),
            fee=Decimal(0),
        )


def test_repeated_attachment_produces_identical_evidence() -> None:
    outcome = _filled()
    config = _economics_config()
    assert attach_execution_economics(outcome, config) == attach_execution_economics(
        outcome, config
    )


def test_economics_contract_contains_no_portfolio_or_trade_pairing_state() -> None:
    forbidden = {
        "cash_balance",
        "equity",
        "position_size",
        "realized_pnl",
        "trade_record",
        "unrealized_pnl",
    }
    assert forbidden.isdisjoint(ExecutionEconomicsConfig.__dataclass_fields__)
    assert forbidden.isdisjoint(EconomicExecution.__dataclass_fields__)
