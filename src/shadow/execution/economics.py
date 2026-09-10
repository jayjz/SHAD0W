"""Deterministic fixed-quantity economics for resolved execution outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    Underflow,
    localcontext,
)

from shadow.domain import Instrument
from shadow.execution.models import ExecutionOutcome, ExecutionSide, ExecutionStatus

ECONOMICS_MODEL_ID = "shadow.execution.fixed_quantity_fee_bps.v1"


class ExecutionEconomicsError(ValueError):
    """An invalid economics contract or failed deterministic calculation."""


def _canonical_decimal(value: Decimal) -> Decimal:
    """Canonicalize one finite Decimal without caller-context arithmetic."""
    if value == 0:
        return Decimal(0)
    sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    while len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    return Decimal((sign, digits, exponent))


def _validate_decimal(
    value: Decimal,
    field_name: str,
    *,
    strictly_positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ExecutionEconomicsError(f"{field_name} must be a finite Decimal")
    if strictly_positive and value <= 0:
        raise ExecutionEconomicsError(f"{field_name} must be strictly positive")
    if nonnegative and value < 0:
        raise ExecutionEconomicsError(f"{field_name} must be nonnegative")
    return _canonical_decimal(value)


@dataclass(frozen=True, slots=True)
class ExecutionEconomicsConfig:
    """Fixed research quantity and synthetic proportional-fee assumptions.

    Quantity is a caller-declared number of abstract instrument units. Fractional
    quantity support is a research-domain abstraction and does not imply broker or
    venue support. The fixed unit/contract multiplier is one.

    ``quote_currency`` is the caller-declared denomination used to interpret the
    evidence. The current ``Instrument`` contract does not independently verify it.
    """

    instrument: Instrument
    quantity: Decimal
    quote_currency: str
    fee_bps: Decimal
    economics_model_id: str = ECONOMICS_MODEL_ID

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise ExecutionEconomicsError("instrument must be an Instrument")
        object.__setattr__(
            self,
            "quantity",
            _validate_decimal(self.quantity, "quantity", strictly_positive=True),
        )
        if (
            not isinstance(self.quote_currency, str)
            or not self.quote_currency
            or self.quote_currency != self.quote_currency.strip()
        ):
            raise ExecutionEconomicsError("quote_currency must be a non-empty trimmed string")
        object.__setattr__(
            self,
            "fee_bps",
            _validate_decimal(self.fee_bps, "fee_bps", nonnegative=True),
        )
        if self.economics_model_id != ECONOMICS_MODEL_ID:
            raise ExecutionEconomicsError(
                f"economics_model_id must be {ECONOMICS_MODEL_ID!r} for ExecutionEconomicsConfig"
            )


def _calculate_values(
    outcome: ExecutionOutcome,
    config: ExecutionEconomicsConfig,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if outcome.status is not ExecutionStatus.FILLED or outcome.execution_price is None:
        raise ExecutionEconomicsError("economic execution requires a FILLED outcome")
    if outcome.attempt.instrument != config.instrument:
        raise ExecutionEconomicsError(
            "economics config instrument must match execution outcome instrument"
        )

    try:
        with localcontext(
            Context(
                prec=34,
                rounding=ROUND_HALF_EVEN,
                Emin=-999999,
                Emax=999999,
                capitals=1,
                clamp=0,
                traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
            )
        ):
            signed_notional = outcome.execution_price * config.quantity
            gross_notional = signed_notional.copy_abs()
            fee_rate = config.fee_bps / Decimal(10000)
            fee = gross_notional * fee_rate
            execution_cash_flow = (
                -signed_notional - fee
                if outcome.attempt.execution_side is ExecutionSide.BUY
                else signed_notional - fee
            )
    except DecimalException as exc:
        raise ExecutionEconomicsError(
            "execution economics calculation exceeds the Decimal numeric domain"
        ) from exc

    return (
        _canonical_decimal(signed_notional),
        _canonical_decimal(gross_notional),
        _canonical_decimal(fee),
        _canonical_decimal(execution_cash_flow),
    )


@dataclass(frozen=True, slots=True)
class EconomicExecution:
    """Immutable fixed-quantity economics attached to exactly one filled outcome."""

    outcome: ExecutionOutcome
    config: ExecutionEconomicsConfig
    gross_notional: Decimal
    fee: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ExecutionOutcome):
            raise ExecutionEconomicsError("outcome must be an ExecutionOutcome")
        if not isinstance(self.config, ExecutionEconomicsConfig):
            raise ExecutionEconomicsError("config must be an ExecutionEconomicsConfig")
        supplied_gross = _validate_decimal(self.gross_notional, "gross_notional", nonnegative=True)
        supplied_fee = _validate_decimal(self.fee, "fee", nonnegative=True)
        _, expected_gross, expected_fee, _ = _calculate_values(self.outcome, self.config)
        if supplied_gross != expected_gross or supplied_fee != expected_fee:
            raise ExecutionEconomicsError(
                "gross_notional and fee must match the configured execution economics"
            )
        object.__setattr__(self, "gross_notional", expected_gross)
        object.__setattr__(self, "fee", expected_fee)

    @property
    def instrument(self) -> Instrument:
        return self.outcome.attempt.instrument

    @property
    def side(self) -> ExecutionSide:
        return self.outcome.attempt.execution_side

    @property
    def execution_price(self) -> Decimal:
        price = self.outcome.execution_price
        assert price is not None
        return price

    @property
    def quantity(self) -> Decimal:
        return self.config.quantity

    @property
    def quote_currency(self) -> str:
        return self.config.quote_currency

    @property
    def attempt_reference(self) -> str:
        return self.outcome.attempt.attempt_id

    @property
    def signed_notional(self) -> Decimal:
        signed_notional, _, _, _ = _calculate_values(self.outcome, self.config)
        return signed_notional

    @property
    def execution_cash_flow(self) -> Decimal:
        """Cash-flow value for this execution, not a balance, ledger, or P&L."""
        _, _, _, cash_flow = _calculate_values(self.outcome, self.config)
        return cash_flow


def attach_execution_economics(
    outcome: ExecutionOutcome,
    config: ExecutionEconomicsConfig,
) -> EconomicExecution | None:
    """Attach economics to a filled outcome; return no evidence for other statuses."""
    if not isinstance(outcome, ExecutionOutcome):
        raise ExecutionEconomicsError("outcome must be an ExecutionOutcome")
    if not isinstance(config, ExecutionEconomicsConfig):
        raise ExecutionEconomicsError("config must be an ExecutionEconomicsConfig")
    if outcome.status is not ExecutionStatus.FILLED:
        return None
    _, gross_notional, fee, _ = _calculate_values(outcome, config)
    return EconomicExecution(
        outcome=outcome,
        config=config,
        gross_notional=gross_notional,
        fee=fee,
    )
