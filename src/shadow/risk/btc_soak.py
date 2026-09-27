"""Independent experimental risk gate. Does not call or override proof risk."""

from datetime import datetime
from fractions import Fraction

from shadow.domain.crypto_market import CryptoQuote
from shadow.execution.broker import Eligibility
from shadow.execution.btc_soak import SoakAttempt, SoakCut, SoakState, fresh, operational_state
from shadow.execution.crypto import BtcSubmitRequest
from shadow.features.btc_trend import BTC, CompletedBtcInterval
from shadow.risk.btc import utc_ns
from shadow.risk.btc_models import BtcRiskPolicy
from shadow.risk.models import OperatorControls, OrderSide
from shadow.strategies.btc_trend import (
    BtcAction,
    BtcProposal,
    high_water_since_entry,
    paper_soak_canary,
    propose,
)


def evaluate_soak_risk(
    *,
    policy: BtcRiskPolicy,
    proposal: BtcProposal,
    request: BtcSubmitRequest,
    intervals: tuple[CompletedBtcInterval, ...],
    quote: CryptoQuote,
    cut: SoakCut,
    state: SoakState,
    controls: OperatorControls,
    now_ns: int,
    attempts: tuple[SoakAttempt, ...],
    start: datetime,
) -> tuple[str, ...]:
    """Recompute signal, controls, freshness, cash and sizing independently."""
    request.__post_init__()  # PAPER/MARKET/GTC only, even for direct callers.
    actual = operational_state(
        cut,
        attempts,
        binding=(policy.account_id, policy.operational_scope),
        start=start,
        now_ns=now_ns,
        maximum_age_ns=policy.maximum_broker_age_ns,
    )
    if actual != state:
        return ("OPERATIONAL_STATE_CONFLICT",)
    config = paper_soak_canary()
    reasons: list[str] = []
    binding = policy.account_id, policy.operational_scope
    if (request.account_id, request.operational_scope) != binding:
        reasons.append("BINDING_CONFLICT")
    if any(
        not fresh(v.evidence, binding, now_ns, policy.maximum_broker_age_ns)
        for v in (cut.account, cut.asset, cut.snapshot, cut.activities)
    ):
        reasons.append("STALE_BROKER_EVIDENCE")
    if not controls.trading_enabled or controls.kill_switch_active:
        reasons.append("OPERATOR_DISABLED")
    if not (
        utc_ns(controls.observation_time) <= utc_ns(controls.availability_time) <= now_ns
        and 0 <= now_ns - utc_ns(controls.observation_time) <= policy.maximum_control_age_ns
    ):
        reasons.append("STALE_CONTROLS")
    if (
        quote.instrument != BTC
        or cut.asset.instrument != BTC
        or not quote.observation_time.value <= quote.availability_time.value <= now_ns
        or not 0 <= now_ns - quote.observation_time.value <= policy.maximum_market_age_ns
        or quote.bid_price >= quote.ask_price
        or quote.bid_size <= 0
        or quote.ask_size <= 0
    ):
        reasons.append("INVALID_MARKET_EVIDENCE")
    if (
        cut.account.eligibility is not Eligibility.ELIGIBLE
        or cut.account.crypto_trading is not Eligibility.ELIGIBLE
        or cut.account.currency != "USD"
    ):
        reasons.append("INELIGIBLE_CASH_ACCOUNT")
    if (
        not cut.asset.accepts_quantity(request.quantity)
        or request.quantity > policy.maximum_quantity
    ):
        reasons.append("INVALID_QUANTITY")
    high = (
        None
        if state.entry_ns is None
        else high_water_since_entry(
            intervals,
            entry_ns=state.entry_ns,
            as_of_ns=now_ns,
            interval_ns=config.interval_ns,
        )
    )
    expected = propose(
        config,
        config.features(intervals, now_ns),
        now_ns=now_ns,
        holding=state.state == "HOLDING",
        high_water_mark=high,
    )
    if expected is None or expected != proposal:
        reasons.append("INVALID_STRATEGY_EVIDENCE")
    if proposal.action is BtcAction.ENTER:
        if state.state != "FLAT" or request.side is not OrderSide.BUY:
            reasons.append("ENTRY_REQUIRES_FLAT")
        try:
            if request.quantity < cut.asset.minimum_quantity_at_price(quote.ask_price):
                reasons.append("PROVIDER_MINIMUM")
        except (ValueError, ArithmeticError):
            reasons.append("PROVIDER_MINIMUM")
        notional = Fraction(request.quantity) * Fraction(quote.ask_price)
        estimate = notional * (
            1 + Fraction(config.estimated_taker_fee) + Fraction(config.slippage_allowance)
        ) + Fraction(policy.cash_buffer)
        if notional > policy.maximum_entry_notional or estimate > cut.account.available_cash:
            reasons.append("CASH_OR_NOTIONAL_LIMIT")
    else:
        if (
            state.state != "HOLDING"
            or request.side is not OrderSide.SELL
            or request.quantity != state.quantity
            or cut.available is None
            or cut.available.quantity != request.quantity
            or not fresh(cut.available.evidence, binding, now_ns, policy.maximum_broker_age_ns)
        ):
            reasons.append("EXIT_REQUIRES_OPERATIONAL_HOLDING")
    return tuple(reasons)
