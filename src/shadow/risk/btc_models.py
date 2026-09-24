"""Immutable BTC PAPER policy/evaluation evidence; not a send capability."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from decimal import Decimal

from shadow.execution.crypto import BtcSubmitRequest
from shadow.strategies.btc_trend import BtcProposal, BtcTrendConfig


@dataclass(frozen=True, slots=True)
class BtcRiskPolicy:
    account_id: str
    operational_scope: str
    maximum_quantity: Decimal
    maximum_entry_notional: Decimal
    cash_buffer: Decimal
    maximum_market_age_ns: int
    maximum_broker_age_ns: int
    maximum_control_age_ns: int

    def __post_init__(self) -> None:
        for name in (self.account_id, self.operational_scope):
            if not isinstance(name, str) or not name or name != name.strip():
                raise ValueError("account and scope required")
        for amount in (self.maximum_quantity, self.maximum_entry_notional, self.cash_buffer):
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
                raise ValueError("positive Decimal risk limits required")
        for age in (
            self.maximum_market_age_ns,
            self.maximum_broker_age_ns,
            self.maximum_control_age_ns,
        ):
            if type(age) is not int or age <= 0:
                raise ValueError("positive integer risk ages required")

    @property
    def policy_id(self) -> str:
        payload = {field.name: str(getattr(self, field.name)) for field in fields(self)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class BtcRiskEvaluation:
    """A pure evaluation that must be durably admitted/revalidated before send."""

    request: BtcSubmitRequest
    config: BtcTrendConfig
    policy: BtcRiskPolicy
    proposal: BtcProposal
    evaluated_ns: int
    reconciliation_state: str
    reasons: tuple[str, ...]

    @property
    def authorized(self) -> bool:
        return not self.reasons
