"""Independent fail-closed BTC PAPER evaluation, with no dispatch or I/O."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import localcontext
from fractions import Fraction

from shadow.domain.crypto_market import CryptoQuote, UtcNanoseconds
from shadow.execution.broker import BrokerFill, BrokerSnapshot, Eligibility, SubmissionStatus
from shadow.execution.btc_authority import BtcAttempt
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoActivityEvidence, inventory_effects
from shadow.execution.journal import CommittedAttempt
from shadow.execution.reconciliation import OperationalState, Reconciliation, reconcile
from shadow.features.btc_trend import BTC, BTC_CONTEXT, CompletedBtcInterval
from shadow.risk.btc_models import BtcLifecycleAuthority, BtcRiskEvaluation, BtcRiskPolicy
from shadow.risk.models import OperatorControls, OrderSide
from shadow.strategies.btc_trend import (
    BtcAction,
    BtcProposal,
    BtcTrendConfig,
    high_water_since_entry,
)


def utc_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware UTC-convertible timestamp required")
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def linked_entry_ns(
    *,
    fills: tuple[BrokerFill, ...],
    snapshot: BrokerSnapshot,
    reconciliation: Reconciliation,
    crypto_evidence: CryptoActivityEvidence | None = None,
) -> int | None:
    """Derive the current long's first execution time from complete linked fills.

    Complete per-order sums must equal broker cumulative fills. Replacements or
    timestamp ties with opposite sides require richer chronology and fail closed.
    Fill observation time is the normalized execution time, never commit time.
    """
    if reconciliation.state is not OperationalState.HOLDING or not fills:
        return None
    orders = {order.order_id: order for order in snapshot.orders}
    if set(orders) != set(reconciliation.linked_order_ids):
        return None
    if any(
        order.replaces is not None or order.replaced_by is not None for order in orders.values()
    ):
        return None
    if len({fill.execution_id for fill in fills}) != len(fills):
        return None
    totals = dict.fromkeys(orders, Fraction(0))
    for fill in fills:
        order = orders.get(fill.order_id)
        if (
            order is None
            or fill.instrument != BTC
            or fill.side is not order.side
            or (fill.evidence.account_id, fill.evidence.operational_scope)
            != (snapshot.evidence.account_id, snapshot.evidence.operational_scope)
            or not snapshot.history_start <= fill.evidence.observation_time <= snapshot.history_end
            or fill.evidence.availability_time > snapshot.evidence.availability_time
        ):
            return None
        totals[fill.order_id] += Fraction(fill.quantity)
    if any(totals[key] != order.filled_quantity for key, order in orders.items()):
        return None
    ordered = sorted(fills, key=lambda fill: (fill.evidence.observation_time, fill.execution_id))
    if any(
        a.evidence.observation_time == b.evidence.observation_time and a.side is not b.side
        for a, b in zip(ordered, ordered[1:], strict=False)
    ):
        return None
    net_effects = None
    if crypto_evidence is not None:
        try:
            effects = inventory_effects(crypto_evidence)
            net_effects = {
                effect.execution.execution_id: Fraction(effect.net_btc) for effect in effects
            }
            originals = {effect.execution.execution_id: effect.execution for effect in effects}
            if any(originals.get(fill.execution_id) != fill for fill in fills):
                return None
        except (ValueError, LookupError):
            return None
        if set(net_effects) != {fill.execution_id for fill in fills}:
            return None
    exposure = Fraction(0)
    entry_ns = None
    for fill in ordered:
        if exposure == 0 and fill.side is OrderSide.BUY:
            entry_ns = utc_ns(fill.evidence.observation_time)
        exposure += (
            net_effects[fill.execution_id]
            if net_effects is not None
            else Fraction(fill.quantity) * (1 if fill.side is OrderSide.BUY else -1)
        )
        if exposure < 0:
            return None
        if exposure == 0:
            entry_ns = None
    return entry_ns if exposure == reconciliation.exposure else None


def evaluate_btc_risk(
    *,
    policy: BtcRiskPolicy,
    config: BtcTrendConfig,
    proposal: BtcProposal,
    request: BtcSubmitRequest,
    intervals: tuple[CompletedBtcInterval, ...],
    quote: CryptoQuote,
    account: BtcCashAccount,
    asset: BtcBrokerAsset,
    snapshot: BrokerSnapshot,
    attempts: tuple[CommittedAttempt | BtcAttempt, ...],
    controls: OperatorControls,
    now_ns: int,
    fills: tuple[BrokerFill, ...] = (),
    independent_risk_halt: bool = False,
    crypto_evidence: CryptoActivityEvidence | None = None,
    require_crypto_evidence: bool = False,
    lifecycle_authority: BtcLifecycleAuthority = BtcLifecycleAuthority.PROOF,
    plumbing_probe: bool = False,
) -> BtcRiskEvaluation:
    """Recompute lifecycle/features; proposals cannot declare their own authority."""
    UtcNanoseconds(now_ns)
    if not isinstance(request, BtcSubmitRequest):
        raise TypeError("typed BTC request required")
    request.__post_init__()
    reasons: list[str] = []
    if (proposal.reason == "paper_plumbing_probe") != plumbing_probe:
        reasons.append("probe_authority_mismatch")
    if plumbing_probe and policy.maximum_entry_notional > 100:
        reasons.append("probe_notional_limit")
    state = reconcile(
        attempts=attempts,
        snapshot=snapshot,
        crypto_evidence=crypto_evidence,
        require_crypto_evidence=require_crypto_evidence,
    )
    experiment_lifecycle = (
        lifecycle_authority is BtcLifecycleAuthority.INITIAL_EXPERIMENT
        and not attempts
        and crypto_evidence is not None
        and state.state is OperationalState.UNRESOLVED
        and state.reason == "activity history unproven"
        and crypto_evidence.query_exhausted
        and not crypto_evidence.history_verified
        and not crypto_evidence.executions
        and not crypto_evidence.fees
        and not crypto_evidence.unsupported
        and snapshot.complete
        and snapshot.positions_complete
        and snapshot.orders_complete
        and not snapshot.positions
        and not snapshot.orders
    )
    if lifecycle_authority is BtcLifecycleAuthority.INITIAL_EXPERIMENT and not experiment_lifecycle:
        reasons.append("invalid_initial_experiment_lifecycle")
    if any(
        attempt.submission is None or attempt.submission.status is SubmissionStatus.UNCERTAIN
        for attempt in attempts
    ) and not (
        require_crypto_evidence
        and crypto_evidence is not None
        and state.state in (OperationalState.FLAT, OperationalState.HOLDING)
    ):
        reasons.append("uncertain_submission")
    binding = (policy.account_id, policy.operational_scope)
    if (request.account_id, request.operational_scope) != binding or any(
        (item.evidence.account_id, item.evidence.operational_scope) != binding
        for item in (account, asset, snapshot)
    ):
        reasons.append("binding_conflict")
    if any(not isinstance(attempt.request, BtcSubmitRequest) for attempt in attempts):
        reasons.append("non_btc_attempt")
    if request.instrument != BTC or quote.instrument != BTC or asset.instrument != BTC:
        reasons.append("instrument_mismatch")
    provider_minimum_valid = True
    if proposal.action is BtcAction.ENTER:
        try:
            provider_minimum = asset.minimum_quantity_at_price(quote.ask_price)
        except (ArithmeticError, ValueError):
            provider_minimum_valid = False
        else:
            provider_minimum_valid = request.quantity >= provider_minimum
    if (
        not asset.accepts_quantity(request.quantity)
        or not provider_minimum_valid
        or request.quantity > policy.maximum_quantity
    ):
        reasons.append("invalid_quantity")
    if (
        account.eligibility is not Eligibility.ELIGIBLE
        or account.crypto_trading is not Eligibility.ELIGIBLE
        or account.currency != "USD"
    ):
        reasons.append("ineligible_cash_account")
    if not controls.trading_enabled or controls.kill_switch_active:
        reasons.append("operator_disabled")
    if (
        not utc_ns(controls.observation_time) <= utc_ns(controls.availability_time) <= now_ns
        or now_ns - utc_ns(controls.observation_time) > policy.maximum_control_age_ns
    ):
        reasons.append("stale_controls")
    if (
        any(
            not utc_ns(item.evidence.observation_time)
            <= utc_ns(item.evidence.availability_time)
            <= now_ns
            or now_ns - utc_ns(item.evidence.observation_time) > policy.maximum_broker_age_ns
            for item in (account, asset, snapshot)
        )
        or now_ns - utc_ns(snapshot.history_end) > policy.maximum_broker_age_ns
    ):
        reasons.append("stale_broker_evidence")
    if (
        not quote.observation_time.value <= quote.availability_time.value <= now_ns
        or now_ns - quote.observation_time.value > policy.maximum_market_age_ns
        or quote.bid_price >= quote.ask_price
        or quote.bid_size <= 0
        or quote.ask_size <= 0
    ):
        reasons.append("invalid_market_evidence")
    features = config.features(intervals, now_ns)
    if not plumbing_probe and (
        features is None
        or features != proposal.features
        or now_ns - features.end_ns > config.maximum_evidence_age_ns
        or proposal.configuration_id != config.configuration_id
    ):
        reasons.append("invalid_strategy_evidence")
    if (
        proposal.round_trip_cost != config.round_trip_cost
        or proposal.safety_margin != config.cost_safety_margin
    ):
        reasons.append("cost_assumption_mismatch")
    with localcontext(BTC_CONTEXT):
        if proposal.action is BtcAction.ENTER:
            if (
                state.state is not OperationalState.FLAT and not experiment_lifecycle
            ) or request.side is not OrderSide.BUY:
                reasons.append("entry_requires_flat")
            if independent_risk_halt:
                reasons.append("independent_risk_halt")
            if (
                not plumbing_probe
                and features is not None
                and not (
                    features.trend_distance > config.round_trip_cost + config.cost_safety_margin
                    and features.fast_return > 0
                    and features.volatility <= config.maximum_volatility
                )
            ):
                reasons.append("entry_filters_failed")
            notional = Fraction(request.quantity) * Fraction(quote.ask_price)
            estimate = notional * (
                1 + Fraction(config.estimated_taker_fee) + Fraction(config.slippage_allowance)
            ) + Fraction(policy.cash_buffer)
            if notional > policy.maximum_entry_notional or estimate > account.available_cash:
                reasons.append("cash_or_notional_limit")
        elif proposal.action is BtcAction.EXIT:
            if state.state is not OperationalState.HOLDING or request.side is not OrderSide.SELL:
                reasons.append("exit_requires_holding")
            if request.quantity > state.exposure:
                reasons.append("exit_exceeds_exposure")
            entry = linked_entry_ns(
                fills=fills,
                snapshot=snapshot,
                reconciliation=state,
                crypto_evidence=crypto_evidence,
            )
            high = (
                None
                if entry is None
                else high_water_since_entry(
                    intervals,
                    entry_ns=entry,
                    as_of_ns=now_ns,
                    interval_ns=config.interval_ns,
                )
            )
            if plumbing_probe:
                pass  # Explicit broker plumbing intent has no strategy exit claim.
            elif high is None or high != proposal.high_water_mark:
                reasons.append("incomplete_position_history")
            elif features is not None and not (
                independent_risk_halt
                or features.trend_distance <= 0
                or features.close
                < high * (1 - config.trailing_volatility_multiple * features.volatility)
            ):
                reasons.append("exit_condition_absent")
        else:
            reasons.append("unsupported_action")
    return BtcRiskEvaluation(
        request, config, policy, proposal, now_ns, state.state.value, tuple(reasons)
    )
