"""Deterministic execution and fixed-quantity economics evidence."""

from shadow.execution.economics import (
    ECONOMICS_MODEL_ID,
    EconomicExecution,
    ExecutionEconomicsConfig,
    ExecutionEconomicsError,
    attach_execution_economics,
)
from shadow.execution.models import (
    EXECUTION_MODEL_ID,
    ExecutionActionType,
    ExecutionAttempt,
    ExecutionContractError,
    ExecutionOutcome,
    ExecutionReason,
    ExecutionSide,
    ExecutionStatus,
    QuoteExecutionConfig,
    execution_sensitivity,
    quote_reference,
    resolve_execution_attempt,
    resolve_execution_attempts,
)

__all__ = [
    "ECONOMICS_MODEL_ID",
    "EXECUTION_MODEL_ID",
    "EconomicExecution",
    "ExecutionActionType",
    "ExecutionAttempt",
    "ExecutionContractError",
    "ExecutionEconomicsConfig",
    "ExecutionEconomicsError",
    "ExecutionOutcome",
    "ExecutionReason",
    "ExecutionSide",
    "ExecutionStatus",
    "QuoteExecutionConfig",
    "attach_execution_economics",
    "execution_sensitivity",
    "quote_reference",
    "resolve_execution_attempt",
    "resolve_execution_attempts",
]
