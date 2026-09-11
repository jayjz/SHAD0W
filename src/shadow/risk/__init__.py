"""Minimal deterministic paper risk authority."""

from .evaluator import evaluate_risk
from .gate import DispatchAuthorizationError, RiskAdmission, RiskGate
from .models import (
    RISK_MODEL_VERSION,
    AuthorizedOrder,
    OpenLongPosition,
    OperationalQuantityConfig,
    OperatorControls,
    OrderIntent,
    OrderSide,
    OrderTarget,
    OrderType,
    OutstandingOrder,
    RiskContractError,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
    RiskRejectionReason,
    RiskState,
    TimeInForce,
)

__all__ = [
    "RISK_MODEL_VERSION",
    "AuthorizedOrder",
    "DispatchAuthorizationError",
    "OpenLongPosition",
    "OperationalQuantityConfig",
    "OperatorControls",
    "OrderIntent",
    "OrderSide",
    "OrderTarget",
    "OrderType",
    "OutstandingOrder",
    "RiskAdmission",
    "RiskContractError",
    "RiskDecision",
    "RiskDecisionStatus",
    "RiskGate",
    "RiskPolicy",
    "RiskRejectionReason",
    "RiskState",
    "TimeInForce",
    "evaluate_risk",
]
