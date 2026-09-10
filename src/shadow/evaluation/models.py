"""Immutable long-trade evidence and bounded deterministic arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from enum import StrEnum

from shadow.domain import Instrument
from shadow.execution import EconomicExecution, ExecutionActionType, ExecutionStatus
from shadow.simulation.lifecycle import LifecycleDecision, LifecycleRecord, LifecycleState

EVALUATION_MODEL_ID = "shadow.evaluation.trade_reconstruction.v1"


class EvaluationError(ValueError):
    """Inconsistent reconstruction evidence or arithmetic outside the numeric domain."""


def numeric_context() -> Context:
    """A fresh context; neither caller traps nor flags affect research results."""
    return Context(
        prec=34,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
    )


class TradeEligibility(StrEnum):
    ORDINARY = "ordinary"
    STRESS_PRICE = "stress_price"


def validate_fill(record: LifecycleRecord, economic: EconomicExecution) -> None:
    """Check the exact action/attempt/position reference chain for one fill."""
    action, attempt, position = record.action, record.execution_attempt, record.position
    if (
        record.decision is not LifecycleDecision.ACTION_FILLED
        or action is None
        or attempt is None
        or position is None
        or record.execution_outcome != economic.outcome
        or economic.outcome.status is not ExecutionStatus.FILLED
        or economic.outcome.attempt != attempt
        or record.instrument != economic.instrument
        or action.instrument != economic.instrument
        or position.instrument != economic.instrument
        or attempt.opportunity_instrument != economic.instrument
        or action.action_id != attempt.action_id
        or action.action_type != attempt.action_type
        or action.signal_reference != attempt.signal_reference
        or action.eligibility_after_time != attempt.eligibility_after_time
        or record.event_reference != attempt.opportunity_reference
        or record.event_time != attempt.attempt_time
        or attempt.attempt_time != attempt.opportunity_time
        or attempt.attempt_time <= action.eligibility_after_time
    ):
        raise EvaluationError("fill must match authoritative lifecycle/action/attempt evidence")
    entry = action.action_type is ExecutionActionType.ENTRY
    if (record.prior_state, record.resulting_state) != (
        (LifecycleState.PENDING_ENTRY, LifecycleState.HOLDING)
        if entry
        else (LifecycleState.PENDING_EXIT, LifecycleState.FLAT)
    ):
        raise EvaluationError("fill has incompatible lifecycle transition")
    if entry and (
        position.opening_action_id != action.action_id or position.opened_at != record.event_time
    ):
        raise EvaluationError("entry position must reference its opening action and fill time")
    quote = economic.outcome.market_evidence
    if quote is None or quote.availability_time > attempt.attempt_time:
        raise EvaluationError("fill cannot use future quote evidence")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One completed long trade. Monetary/return values derive from immutable fills."""

    entry: EconomicExecution
    exit: EconomicExecution
    opening_record: LifecycleRecord
    closing_record: LifecycleRecord
    evaluation_model_id: str = EVALUATION_MODEL_ID

    def __post_init__(self) -> None:
        if self.evaluation_model_id != EVALUATION_MODEL_ID:
            raise EvaluationError("unsupported evaluation model")
        validate_fill(self.opening_record, self.entry)
        validate_fill(self.closing_record, self.exit)
        if (
            self.entry.outcome.attempt.action_type is not ExecutionActionType.ENTRY
            or self.exit.outcome.attempt.action_type is not ExecutionActionType.EXIT
            or self.entry.instrument != self.exit.instrument
            or self.opening_record.position != self.closing_record.position
            or self.entry_action_reference == self.exit_action_reference
            or self.entry.attempt_reference == self.exit.attempt_reference
        ):
            raise EvaluationError("entry and exit must share the authoritative opening position")
        if self.entry.quantity != self.exit.quantity:
            raise EvaluationError("entry and exit quantity must match; partial closes unsupported")
        if self.entry.quote_currency != self.exit.quote_currency:
            raise EvaluationError("entry and exit quote currency must match; FX unsupported")
        if self.exit_time <= self.entry_time or (
            self.exit.outcome.attempt.eligibility_after_time <= self.entry_time
        ):
            raise EvaluationError("exit must causally follow entry")
        self._values()  # Reject numeric failures at construction, not silently during reporting.

    @property
    def instrument(self) -> Instrument:
        return self.entry.instrument

    @property
    def entry_action_reference(self) -> str:
        return self.entry.outcome.attempt.action_id

    @property
    def exit_action_reference(self) -> str:
        return self.exit.outcome.attempt.action_id

    @property
    def entry_time(self) -> datetime:
        return self.entry.outcome.attempt.attempt_time

    @property
    def exit_time(self) -> datetime:
        return self.exit.outcome.attempt.attempt_time

    @property
    def quantity(self) -> Decimal:
        return self.entry.quantity

    @property
    def quote_currency(self) -> str:
        return self.entry.quote_currency

    @property
    def eligibility(self) -> TradeEligibility:
        return (
            TradeEligibility.ORDINARY
            if self.entry.execution_price > 0 and self.exit.execution_price > 0
            else TradeEligibility.STRESS_PRICE
        )

    def _values(self) -> tuple[Decimal, Decimal, Decimal, Decimal | None, Decimal | None]:
        try:
            with localcontext(numeric_context()):
                gross = (self.exit.execution_price - self.entry.execution_price) * self.quantity
                fees = self.entry.fee + self.exit.fee
                net = gross - fees
                ordinary = self.eligibility is TradeEligibility.ORDINARY
                denominator = self.entry.gross_notional
                if ordinary and denominator <= 0:
                    raise EvaluationError("ordinary return requires positive entry notional")
                gross_return = gross / denominator if ordinary else None
                net_return = net / denominator if ordinary else None
        except DecimalException as exc:
            raise EvaluationError("trade calculation exceeds the Decimal numeric domain") from exc
        return gross, fees, net, gross_return, net_return

    @property
    def gross_result(self) -> Decimal:
        return self._values()[0]

    @property
    def total_fees(self) -> Decimal:
        return self._values()[1]

    @property
    def net_result(self) -> Decimal:
        return self._values()[2]

    @property
    def return_denominator(self) -> Decimal | None:
        return self.entry.gross_notional if self.eligibility is TradeEligibility.ORDINARY else None

    @property
    def gross_return(self) -> Decimal | None:
        return self._values()[3]

    @property
    def net_return(self) -> Decimal | None:
        return self._values()[4]
