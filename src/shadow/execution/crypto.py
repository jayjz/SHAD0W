"""Typed BTC PAPER envelope. Values are evidence, never dispatch capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum

from shadow.domain.market import Instrument
from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerContractError,
    Eligibility,
    SubmitRequest,
)
from shadow.risk.models import OrderSide, OrderTarget, OrderType, TimeInForce


class ExecutionAsset(StrEnum):
    US_EQUITY = "us_equity"
    BTC_SPOT = "btc_spot"


@dataclass(frozen=True, slots=True)
class BtcSubmitRequest(SubmitRequest):
    """Exact BTC/USD PAPER MARKET/GTC payload; size is never rounded."""

    def __post_init__(self) -> None:
        for value in (self.account_id, self.operational_scope, self.client_id):
            if not isinstance(value, str) or not value or value != value.strip():
                raise BrokerContractError("nonempty request identity required")
        if (
            self.instrument != Instrument("BTC/USD")
            or not isinstance(self.side, OrderSide)
            or not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= 0
            or self.target is not OrderTarget.PAPER
            or self.order_type is not OrderType.MARKET
            or self.time_in_force is not TimeInForce.GTC
            or self.extended_hours is not False
        ):
            raise BrokerContractError("BTC requires positive quantity PAPER MARKET/GTC")


@dataclass(frozen=True, slots=True)
class BtcBrokerAsset(BrokerAsset):
    minimum_order_size: Decimal
    minimum_trade_increment: Decimal
    price_increment: Decimal
    fractionable: bool

    # Alpaca's current crypto contract describes USD-pair minimum quantity as
    # $10 divided by the USD asset price. Keep this explicit at the typed
    # boundary so the live asset metadata can be compared conservatively.
    USD_PAIR_MINIMUM_NOTIONAL = Decimal("10")

    def __post_init__(self) -> None:
        super(BtcBrokerAsset, self).__post_init__()
        if self.instrument != Instrument("BTC/USD") or self.us_equity is not Eligibility.INELIGIBLE:
            raise BrokerContractError("BTC asset evidence required")
        if type(self.fractionable) is not bool:
            raise BrokerContractError("explicit fractionable evidence required")
        for amount in (self.minimum_order_size, self.minimum_trade_increment, self.price_increment):
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
                raise BrokerContractError("positive current provider constraints required")

    def accepts_quantity(self, quantity: Decimal) -> bool:
        """Conservative intersection of size, increment, and nine-place precision.

        Provider describes increment as added to minimum size. Require both the
        zero-based grid and that offset grid; disagreement rejects, never rounds.
        Integer ratios avoid ambient Decimal rounding, even for very large values.
        """
        if (
            self.tradable is not Eligibility.ELIGIBLE
            or not self.fractionable
            or not isinstance(quantity, Decimal)
            or not quantity.is_finite()
            or quantity < self.minimum_order_size
        ):
            return False
        qn, qd = quantity.as_integer_ratio()
        mn, md = self.minimum_order_size.as_integer_ratio()
        stepn, stepd = self.minimum_trade_increment.as_integer_ratio()
        return (
            (qn * 1_000_000_000) % qd == 0
            and (qn * stepd) % (qd * stepn) == 0
            and ((qn * md - mn * qd) * stepd) % (qd * md * stepn) == 0
        )

    def minimum_quantity_at_price(self, price: Decimal) -> Decimal:
        """Smallest legal quantity meeting both asset and documented USD floors.

        This does not change or round a submitted order. Callers can use the
        returned boundary to validate an explicitly selected quantity. A
        malformed or mutually inconsistent provider grid fails closed.
        """
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise BrokerContractError("positive BTC/USD reference price required")
        exponent = self.minimum_trade_increment.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -9:
            raise BrokerContractError("BTC increment exceeds provider precision")
        if not self.accepts_quantity(self.minimum_order_size):
            raise BrokerContractError("provider minimum and increment disagree")
        with localcontext() as context:
            context.prec = 60
            floor = max(self.minimum_order_size, self.USD_PAIR_MINIMUM_NOTIONAL / price)
            units = (floor / self.minimum_trade_increment).to_integral_value(rounding=ROUND_CEILING)
            result = units * self.minimum_trade_increment
        if not self.accepts_quantity(result):
            raise BrokerContractError("provider minimum cannot be represented on asset grid")
        if result * price < self.USD_PAIR_MINIMUM_NOTIONAL:
            raise BrokerContractError("provider minimum rounding failed")
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class BtcCashAccount(BrokerAccount):
    """Cash-limited crypto account evidence, separate from equity buying power."""

    available_cash: Decimal
    crypto_trading: Eligibility

    def __post_init__(self) -> None:
        super(BtcCashAccount, self).__post_init__()
        if (
            not isinstance(self.available_cash, Decimal)
            or not self.available_cash.is_finite()
            or self.available_cash < 0
            or not isinstance(self.crypto_trading, Eligibility)
        ):
            raise BrokerContractError("nonnegative cash and explicit crypto eligibility required")


def execution_asset(request: SubmitRequest) -> ExecutionAsset:
    return (
        ExecutionAsset.BTC_SPOT
        if isinstance(request, BtcSubmitRequest)
        else ExecutionAsset.US_EQUITY
    )
