"""Pure deterministic P2A paper risk evaluation."""

from __future__ import annotations

from datetime import UTC, datetime

from shadow.domain import Quote, QuoteMarketState
from shadow.features import FeatureSnapshot, FeatureState
from shadow.strategies import SignalType

from .models import (
    OrderIntent,
    OrderSide,
    RiskContractError,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
    RiskRejectionReason,
    RiskState,
    feature_reference,
    quote_reference,
)


def _decision_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RiskContractError("decision_time must be timezone-aware")
    return value.astimezone(UTC)


def _lineage_matches(intent: OrderIntent, feature: FeatureSnapshot) -> bool:
    signal = intent.source_signal
    return (
        feature.instrument == signal.instrument
        and feature.feature_name is signal.feature_name
        and feature.input_value is signal.feature_input
        and feature.implementation_version == signal.feature_implementation_version
        and feature.window == signal.feature_window
        and feature.observation_time == signal.feature_observation_time
        and feature.availability_time == signal.feature_availability_time
        and feature.value == signal.observed_feature_value
        and feature.source_dataset_id == signal.source_dataset_id
        and feature.state is FeatureState.READY
        and feature.value is not None
    )


def evaluate_risk(
    *,
    intent: OrderIntent,
    policy: RiskPolicy,
    state: RiskState,
    feature: FeatureSnapshot,
    quote: Quote,
    decision_time: datetime,
) -> RiskDecision:
    """Return a deterministic authorization or structured fail-closed rejection."""
    if not isinstance(intent, OrderIntent):
        raise RiskContractError("intent must be an OrderIntent")
    if not isinstance(policy, RiskPolicy):
        raise RiskContractError("policy must be a RiskPolicy")
    if not isinstance(state, RiskState):
        raise RiskContractError("state must be a RiskState")
    if not isinstance(feature, FeatureSnapshot):
        raise RiskContractError("feature must be a FeatureSnapshot")
    if not isinstance(quote, Quote):
        raise RiskContractError("quote must be a Quote")

    now = _decision_time(decision_time)
    reasons: set[RiskRejectionReason] = set()
    signal = intent.source_signal

    if not policy.enabled:
        reasons.add(RiskRejectionReason.POLICY_DISABLED)
    if not state.controls.trading_enabled:
        reasons.add(RiskRejectionReason.TRADING_DISABLED)
    if state.controls.kill_switch_active:
        reasons.add(RiskRejectionReason.KILL_SWITCH_ACTIVE)
    if intent.instrument not in policy.allowed_instruments:
        reasons.add(RiskRejectionReason.UNSUPPORTED_INSTRUMENT)

    if intent.quantity <= 0 or intent.quantity != intent.quantity.to_integral_value():
        reasons.add(RiskRejectionReason.INVALID_QUANTITY)
    elif intent.quantity > policy.maximum_quantity_per_order:
        reasons.add(RiskRejectionReason.MAX_QUANTITY_EXCEEDED)

    expected_side = OrderSide.BUY if signal.signal_type is SignalType.LONG_ENTRY else OrderSide.SELL
    if (
        intent.instrument != signal.instrument
        or intent.side is not expected_side
        or intent.intent_time != signal.decision_time
        or intent.operational_scope != state.operational_scope
    ):
        reasons.add(RiskRejectionReason.INTENT_SIGNAL_MISMATCH)

    if not _lineage_matches(intent, feature):
        reasons.add(RiskRejectionReason.LINEAGE_MISMATCH)

    if signal.decision_time > now or signal.availability_time > now:
        reasons.add(RiskRejectionReason.FUTURE_SIGNAL)
    elif now - signal.decision_time > policy.maximum_signal_age:
        reasons.add(RiskRejectionReason.STALE_SIGNAL)

    if feature.observation_time > now or feature.availability_time > now:
        reasons.add(RiskRejectionReason.FUTURE_FEATURE)
    elif now - feature.observation_time > policy.maximum_feature_age:
        reasons.add(RiskRejectionReason.STALE_FEATURE)

    if quote.instrument != intent.instrument:
        reasons.add(RiskRejectionReason.QUOTE_INSTRUMENT_MISMATCH)
    if quote.observation_time > now or quote.availability_time > now:
        reasons.add(RiskRejectionReason.FUTURE_QUOTE)
    elif now - quote.observation_time > policy.maximum_quote_age:
        reasons.add(RiskRejectionReason.STALE_QUOTE)
    if quote.market_state is QuoteMarketState.CROSSED:
        reasons.add(RiskRejectionReason.CROSSED_QUOTE)
    if quote.bid_price <= 0 or quote.ask_price <= 0:
        reasons.add(RiskRejectionReason.NONPOSITIVE_QUOTE)

    if not state.inventory_complete:
        reasons.add(RiskRejectionReason.INCOMPLETE_INVENTORY)
    if state.observation_time > now or state.availability_time > now:
        reasons.add(RiskRejectionReason.FUTURE_OPERATIONAL_STATE)
    elif now - state.observation_time > policy.maximum_operational_state_age:
        reasons.add(RiskRejectionReason.STALE_OPERATIONAL_STATE)
    controls = state.controls
    if controls.observation_time > now or controls.availability_time > now:
        reasons.add(RiskRejectionReason.FUTURE_CONTROLS)
    elif now - controls.observation_time > policy.maximum_operational_state_age:
        reasons.add(RiskRejectionReason.STALE_CONTROLS)

    position_instruments = [position.instrument for position in state.open_positions]
    outstanding_instruments = [order.instrument for order in state.outstanding_orders]
    position_ids = [position.position_id for position in state.open_positions]
    outstanding_ids = [order.intent_identity for order in state.outstanding_orders]
    outstanding_references = [order.reference for order in state.outstanding_orders]
    positions_by_instrument = {position.instrument: position for position in state.open_positions}
    inconsistent_order_state = any(
        (order.side is OrderSide.BUY and order.instrument in positions_by_instrument)
        or (
            order.side is OrderSide.SELL
            and (
                order.instrument not in positions_by_instrument
                or order.quantity != positions_by_instrument[order.instrument].quantity
            )
        )
        for order in state.outstanding_orders
    )
    if (
        len(set(position_instruments)) != len(position_instruments)
        or len(set(outstanding_instruments)) != len(outstanding_instruments)
        or len(set(position_ids)) != len(position_ids)
        or len(set(outstanding_ids)) != len(outstanding_ids)
        or len(set(outstanding_references)) != len(outstanding_references)
        or inconsistent_order_state
    ):
        reasons.add(RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE)

    positions = [
        position for position in state.open_positions if position.instrument == intent.instrument
    ]
    outstanding = [
        order for order in state.outstanding_orders if order.instrument == intent.instrument
    ]
    if outstanding:
        reasons.add(RiskRejectionReason.OUTSTANDING_ORDER_EXISTS)

    if intent.side is OrderSide.BUY:
        if positions:
            reasons.add(RiskRejectionReason.POSITION_ALREADY_OPEN)
        entry_reservations = sum(
            1 for order in state.outstanding_orders if order.side is OrderSide.BUY
        )
        if len(state.open_positions) + entry_reservations >= policy.maximum_concurrent_positions:
            reasons.add(RiskRejectionReason.MAX_CONCURRENT_POSITIONS)
    else:
        if not positions:
            reasons.add(RiskRejectionReason.POSITION_NOT_OPEN)
        elif len(positions) == 1:
            position_quantity = positions[0].quantity
            if intent.quantity < position_quantity:
                reasons.add(RiskRejectionReason.PARTIAL_EXIT)
            elif intent.quantity > position_quantity:
                reasons.add(RiskRejectionReason.OVERSELL)

    status = RiskDecisionStatus.REJECTED if reasons else RiskDecisionStatus.AUTHORIZED
    return RiskDecision(
        intent=intent,
        status=status,
        reasons=tuple(reasons),
        policy_id=policy.policy_id,
        policy_fingerprint=policy.fingerprint,
        risk_state_id=state.state_id,
        risk_state_revision=state.revision,
        risk_state_fingerprint=state.fingerprint,
        feature_reference=feature_reference(feature),
        quote_reference=quote_reference(quote),
        decision_time=now,
    )
