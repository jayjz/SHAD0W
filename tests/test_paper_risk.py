from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Context, Decimal, localcontext

import pytest

from shadow.domain import AvailabilitySemantics, Instrument, Provenance, Quote
from shadow.features import (
    FEATURE_IMPLEMENTATION_VERSION,
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
)
from shadow.risk import (
    AuthorizedOrder,
    DispatchAuthorizationError,
    OpenLongPosition,
    OperationalQuantityConfig,
    OperatorControls,
    OrderIntent,
    OrderSide,
    OutstandingOrder,
    RiskAdmission,
    RiskContractError,
    RiskDecision,
    RiskDecisionStatus,
    RiskGate,
    RiskPolicy,
    RiskRejectionReason,
    RiskState,
    evaluate_risk,
)
from shadow.strategies import (
    MEAN_REVERSION_STRATEGY_ID,
    MEAN_REVERSION_STRATEGY_VERSION,
    Signal,
    SignalReason,
    SignalType,
)

SPY = Instrument("SPY")
QQQ = Instrument("QQQ")
NOW = datetime(2026, 9, 11, 14, 30, tzinfo=UTC)


def feature(
    *,
    instrument: Instrument = SPY,
    observation_time: datetime = NOW - timedelta(seconds=2),
    availability_time: datetime = NOW - timedelta(seconds=1),
    value: Decimal = Decimal("-2"),
    dataset_id: str = "dataset-1",
) -> FeatureSnapshot:
    return FeatureSnapshot(
        instrument=instrument,
        feature_name=FeatureName.Z_SCORE,
        input_value=FeatureInput.CLOSE,
        implementation_version=FEATURE_IMPLEMENTATION_VERSION,
        value=value,
        observation_time=observation_time,
        availability_time=availability_time,
        window=20,
        state=FeatureState.READY,
        unavailable_reason=None,
        source_dataset_id=dataset_id,
    )


def signal(
    snapshot: FeatureSnapshot,
    *,
    signal_type: SignalType = SignalType.LONG_ENTRY,
    decision_time: datetime = NOW,
) -> Signal:
    return Signal(
        instrument=snapshot.instrument,
        strategy_id=MEAN_REVERSION_STRATEGY_ID,
        strategy_version=MEAN_REVERSION_STRATEGY_VERSION,
        signal_type=signal_type,
        decision_time=decision_time,
        availability_time=decision_time,
        feature_name=snapshot.feature_name,
        feature_input=snapshot.input_value,
        feature_implementation_version=snapshot.implementation_version,
        feature_window=snapshot.window,
        feature_observation_time=snapshot.observation_time,
        feature_availability_time=snapshot.availability_time,
        observed_feature_value=snapshot.value or Decimal(0),
        configuration_id="mean-reversion-v1",
        entry_threshold=Decimal("-1.5"),
        exit_threshold=Decimal("0"),
        maximum_feature_age=timedelta(minutes=1),
        reason=(
            SignalReason.ENTRY_THRESHOLD
            if signal_type is SignalType.LONG_ENTRY
            else SignalReason.EXIT_THRESHOLD
        ),
        source_dataset_id=snapshot.source_dataset_id,
    )


def intent(
    snapshot: FeatureSnapshot | None = None,
    *,
    signal_type: SignalType = SignalType.LONG_ENTRY,
    quantity: Decimal = Decimal("5"),
    quantity_configuration_id: str = "paper-quantity-v1",
    decision_time: datetime = NOW,
) -> OrderIntent:
    snapshot = feature() if snapshot is None else snapshot
    source_signal = signal(snapshot, signal_type=signal_type, decision_time=decision_time)
    return OrderIntent.from_signal(
        operational_scope="paper-primary",
        signal=source_signal,
        quantity_config=OperationalQuantityConfig(
            instrument=snapshot.instrument,
            configuration_id=quantity_configuration_id,
            quantity=quantity,
        ),
    )


def quote(
    *,
    instrument: Instrument = SPY,
    bid: Decimal = Decimal("100"),
    ask: Decimal = Decimal("100.01"),
    observation_time: datetime = NOW - timedelta(seconds=1),
    availability_time: datetime = NOW,
) -> Quote:
    return Quote(
        instrument=instrument,
        bid_price=bid,
        ask_price=ask,
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        observation_time=observation_time,
        availability_time=availability_time,
        availability_semantics=AvailabilitySemantics.SYSTEM_RECEIVED,
        provenance=Provenance(source="test"),
    )


def controls(
    *,
    trading_enabled: bool = True,
    kill_switch_active: bool = False,
    observation_time: datetime = NOW,
    availability_time: datetime = NOW,
) -> OperatorControls:
    return OperatorControls(
        trading_enabled=trading_enabled,
        kill_switch_active=kill_switch_active,
        observation_time=observation_time,
        availability_time=availability_time,
    )


def state(
    *,
    positions: tuple[OpenLongPosition, ...] = (),
    outstanding: tuple[OutstandingOrder, ...] = (),
    inventory_complete: bool = True,
    operator_controls: OperatorControls | None = None,
    observation_time: datetime = NOW,
    availability_time: datetime = NOW,
    state_id: str = "state-1",
) -> RiskState:
    return RiskState(
        operational_scope="paper-primary",
        state_id=state_id,
        revision=1,
        inventory_complete=inventory_complete,
        open_positions=positions,
        outstanding_orders=outstanding,
        observation_time=observation_time,
        availability_time=availability_time,
        controls=controls() if operator_controls is None else operator_controls,
    )


def policy(
    *,
    enabled: bool = True,
    allowed: tuple[Instrument, ...] = (SPY, QQQ),
    maximum_quantity: Decimal = Decimal("10"),
    maximum_positions: int = 2,
) -> RiskPolicy:
    return RiskPolicy(
        policy_id="paper-policy-v1",
        enabled=enabled,
        allowed_instruments=allowed,
        maximum_quantity_per_order=maximum_quantity,
        maximum_concurrent_positions=maximum_positions,
        maximum_signal_age=timedelta(minutes=1),
        maximum_feature_age=timedelta(minutes=1),
        maximum_quote_age=timedelta(seconds=10),
        maximum_operational_state_age=timedelta(seconds=5),
    )


def decide(
    order_intent: OrderIntent,
    *,
    risk_policy: RiskPolicy | None = None,
    risk_state: RiskState | None = None,
    supporting_feature: FeatureSnapshot | None = None,
    market_quote: Quote | None = None,
    decision_time: datetime = NOW,
) -> RiskDecision:
    return evaluate_risk(
        intent=order_intent,
        policy=policy() if risk_policy is None else risk_policy,
        state=state() if risk_state is None else risk_state,
        feature=feature() if supporting_feature is None else supporting_feature,
        quote=quote(instrument=order_intent.instrument) if market_quote is None else market_quote,
        decision_time=decision_time,
    )


def reasons(decision: RiskDecision) -> set[RiskRejectionReason]:
    return set(decision.reasons)


def test_valid_flat_entry_authorizes_once_and_reserves_before_grant_exposure() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    order_intent = intent()
    admission = gate.admit(
        intent=order_intent,
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW,
    )

    assert admission.decision.status is RiskDecisionStatus.AUTHORIZED
    assert admission.authorization is not None
    assert gate.reservations == (
        OutstandingOrder(
            instrument=SPY,
            intent_identity=order_intent.intent_identity,
            side=OrderSide.BUY,
            quantity=Decimal("5"),
            reference=admission.authorization.grant_id,
        ),
    )

    duplicate = gate.admit(
        intent=order_intent,
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW + timedelta(seconds=1),
    )
    assert duplicate.authorization is None
    assert duplicate.decision.reasons == (RiskRejectionReason.DUPLICATE_INTENT,)
    assert duplicate.decision.prior_decision_reference == admission.decision.decision_id
    assert len(gate.recorded_decisions) == 1
    assert len(gate.reservations) == 1


@pytest.mark.parametrize(
    "changed",
    [
        lambda original: replace(original, quantity=Decimal("6")),
        lambda original: replace(original, quantity_configuration_id="paper-quantity-v2"),
        lambda original: replace(
            original,
            source_signal=replace(
                original.source_signal,
                entry_threshold=Decimal("-1.75"),
            ),
        ),
    ],
)
def test_material_change_under_same_business_identity_is_conflict(
    changed: Callable[[OrderIntent], OrderIntent],
) -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    original = intent()
    first = gate.admit(
        intent=original,
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW,
    )
    altered = changed(original)
    assert altered.intent_identity == original.intent_identity
    assert altered.payload_fingerprint != original.payload_fingerprint

    retry = gate.admit(
        intent=altered,
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW + timedelta(seconds=1),
    )
    assert retry.decision.reasons == (RiskRejectionReason.INTENT_IDENTITY_CONFLICT,)
    assert retry.decision.prior_decision_reference == first.decision.decision_id
    assert retry.authorization is None


def test_consumer_decision_time_does_not_change_business_identity() -> None:
    order_intent = intent()
    first = decide(order_intent, decision_time=NOW)
    second = decide(order_intent, decision_time=NOW + timedelta(seconds=1))
    assert first.intent.intent_identity == second.intent.intent_identity
    assert first.intent.payload_fingerprint == second.intent.payload_fingerprint

    reevaluated_intent = intent(feature(), decision_time=NOW + timedelta(seconds=1))
    assert reevaluated_intent.intent_identity == order_intent.intent_identity
    assert reevaluated_intent.payload_fingerprint != order_intent.payload_fingerprint


def test_numerically_equivalent_quantity_retry_is_duplicate_not_conflict() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    original = intent(quantity=Decimal("5"))
    first = gate.admit(
        intent=original,
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW,
    )
    equivalent = replace(original, quantity=Decimal("5.000"))
    assert equivalent.payload_fingerprint == original.payload_fingerprint
    retry = gate.admit(
        intent=equivalent,
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW + timedelta(seconds=1),
    )
    assert retry.decision.reasons == (RiskRejectionReason.DUPLICATE_INTENT,)
    assert retry.decision.prior_decision_reference == first.decision.decision_id


def test_two_serialized_entries_competing_for_final_capacity_yield_one_grant() -> None:
    gate = RiskGate(
        operational_scope="paper-primary",
        policy=policy(maximum_positions=1),
    )
    spy_feature = feature()
    qqq_feature = feature(instrument=QQQ)
    first = gate.admit(
        intent=intent(spy_feature),
        state=state(),
        feature=spy_feature,
        quote=quote(),
        decision_time=NOW,
    )
    second = gate.admit(
        intent=intent(qqq_feature),
        state=state(),
        feature=qqq_feature,
        quote=quote(instrument=QQQ),
        decision_time=NOW,
    )
    assert first.authorization is not None
    assert second.authorization is None
    assert RiskRejectionReason.MAX_CONCURRENT_POSITIONS in reasons(second.decision)
    assert len(gate.reservations) == 1


def test_atomic_gate_serializes_concurrent_final_capacity_admissions() -> None:
    gate = RiskGate(
        operational_scope="paper-primary",
        policy=policy(maximum_positions=1),
    )
    snapshots = (feature(), feature(instrument=QQQ))

    def admit(snapshot: FeatureSnapshot) -> RiskAdmission:
        return gate.admit(
            intent=intent(snapshot),
            state=state(),
            feature=snapshot,
            quote=quote(instrument=snapshot.instrument),
            decision_time=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        admissions = tuple(executor.map(admit, snapshots))

    assert sum(admission.authorization is not None for admission in admissions) == 1
    assert (
        sum(
            RiskRejectionReason.MAX_CONCURRENT_POSITIONS in reasons(admission.decision)
            for admission in admissions
        )
        == 1
    )
    assert len(gate.reservations) == 1


def test_existing_position_and_pending_order_each_block_entry() -> None:
    order_intent = intent()
    position = OpenLongPosition(SPY, Decimal("5"), "position-1")
    pending = OutstandingOrder(SPY, "another-intent", OrderSide.BUY, Decimal("5"), "pending-1")
    assert RiskRejectionReason.POSITION_ALREADY_OPEN in reasons(
        decide(order_intent, risk_state=state(positions=(position,)))
    )
    assert RiskRejectionReason.OUTSTANDING_ORDER_EXISTS in reasons(
        decide(order_intent, risk_state=state(outstanding=(pending,)))
    )


def test_full_exit_authorizes_but_missing_partial_and_oversell_reject() -> None:
    snapshot = feature(value=Decimal("1"))
    position = OpenLongPosition(SPY, Decimal("5"), "position-1")
    full = intent(snapshot, signal_type=SignalType.EXIT, quantity=Decimal("5"))
    assert decide(
        full, risk_state=state(positions=(position,)), supporting_feature=snapshot
    ).status is (RiskDecisionStatus.AUTHORIZED)
    assert RiskRejectionReason.POSITION_NOT_OPEN in reasons(
        decide(full, supporting_feature=snapshot)
    )
    partial = intent(snapshot, signal_type=SignalType.EXIT, quantity=Decimal("4"))
    assert RiskRejectionReason.PARTIAL_EXIT in reasons(
        decide(partial, risk_state=state(positions=(position,)), supporting_feature=snapshot)
    )
    oversell = intent(snapshot, signal_type=SignalType.EXIT, quantity=Decimal("6"))
    assert RiskRejectionReason.OVERSELL in reasons(
        decide(oversell, risk_state=state(positions=(position,)), supporting_feature=snapshot)
    )


def test_exit_reservation_does_not_free_position_capacity() -> None:
    exit_feature = feature(value=Decimal("1"))
    position = OpenLongPosition(SPY, Decimal("5"), "position-1")
    gate = RiskGate(operational_scope="paper-primary", policy=policy(maximum_positions=1))
    exit_admission = gate.admit(
        intent=intent(exit_feature, signal_type=SignalType.EXIT),
        state=state(positions=(position,)),
        feature=exit_feature,
        quote=quote(),
        decision_time=NOW,
    )
    assert exit_admission.authorization is not None

    qqq_feature = feature(instrument=QQQ)
    entry = gate.admit(
        intent=intent(qqq_feature),
        state=state(positions=(position,)),
        feature=qqq_feature,
        quote=quote(instrument=QQQ),
        decision_time=NOW,
    )
    assert RiskRejectionReason.MAX_CONCURRENT_POSITIONS in reasons(entry.decision)


def test_max_concurrent_positions_rejects_entry() -> None:
    qqq_position = OpenLongPosition(QQQ, Decimal("5"), "position-qqq")
    decision = decide(
        intent(),
        risk_policy=policy(maximum_positions=1),
        risk_state=state(positions=(qqq_position,)),
    )
    assert RiskRejectionReason.MAX_CONCURRENT_POSITIONS in reasons(decision)


@pytest.mark.parametrize(
    ("risk_policy", "operator_controls", "expected"),
    [
        (policy(enabled=False), controls(), RiskRejectionReason.POLICY_DISABLED),
        (policy(), controls(trading_enabled=False), RiskRejectionReason.TRADING_DISABLED),
        (policy(), controls(kill_switch_active=True), RiskRejectionReason.KILL_SWITCH_ACTIVE),
    ],
)
def test_policy_and_operator_freezes_reject_entries_and_exits(
    risk_policy: RiskPolicy,
    operator_controls: OperatorControls,
    expected: RiskRejectionReason,
) -> None:
    for signal_type in (SignalType.LONG_ENTRY, SignalType.EXIT):
        snapshot = feature(value=Decimal("1") if signal_type is SignalType.EXIT else Decimal("-2"))
        positions = (
            (OpenLongPosition(SPY, Decimal("5"), "position-1"),)
            if signal_type is SignalType.EXIT
            else ()
        )
        decision = decide(
            intent(snapshot, signal_type=signal_type),
            risk_policy=risk_policy,
            risk_state=state(positions=positions, operator_controls=operator_controls),
            supporting_feature=snapshot,
        )
        assert expected in reasons(decision)


def test_missing_inventory_is_not_flat_and_rejected_admission_does_not_reserve() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    admission = gate.admit(
        intent=intent(),
        state=state(inventory_complete=False),
        feature=feature(),
        quote=quote(),
        decision_time=NOW,
    )
    assert RiskRejectionReason.INCOMPLETE_INVENTORY in reasons(admission.decision)
    assert admission.authorization is None
    assert gate.reservations == ()


def test_stale_operational_state_rejects_even_if_recently_available() -> None:
    old = NOW - timedelta(seconds=6)
    decision = decide(
        intent(),
        risk_state=state(observation_time=old, availability_time=NOW),
    )
    assert RiskRejectionReason.STALE_OPERATIONAL_STATE in reasons(decision)


def test_stale_controls_reject() -> None:
    old = NOW - timedelta(seconds=6)
    decision = decide(
        intent(),
        risk_state=state(operator_controls=controls(observation_time=old, availability_time=NOW)),
    )
    assert RiskRejectionReason.STALE_CONTROLS in reasons(decision)


def test_stale_signal_rejects() -> None:
    old_feature = feature(
        observation_time=NOW - timedelta(minutes=2, seconds=2),
        availability_time=NOW - timedelta(minutes=2, seconds=1),
    )
    order_intent = intent(old_feature, decision_time=NOW - timedelta(minutes=2))
    decision = decide(order_intent, supporting_feature=old_feature)
    assert RiskRejectionReason.STALE_SIGNAL in reasons(decision)


def test_stale_feature_rejects() -> None:
    old_feature = feature(
        observation_time=NOW - timedelta(minutes=2),
        availability_time=NOW - timedelta(seconds=1),
    )
    decision = decide(intent(old_feature), supporting_feature=old_feature)
    assert RiskRejectionReason.STALE_FEATURE in reasons(decision)


def test_stale_quote_rejects() -> None:
    old = NOW - timedelta(seconds=11)
    decision = decide(intent(), market_quote=quote(observation_time=old, availability_time=NOW))
    assert RiskRejectionReason.STALE_QUOTE in reasons(decision)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("signal", RiskRejectionReason.FUTURE_SIGNAL),
        ("feature", RiskRejectionReason.FUTURE_FEATURE),
        ("quote", RiskRejectionReason.FUTURE_QUOTE),
        ("state", RiskRejectionReason.FUTURE_OPERATIONAL_STATE),
        ("controls", RiskRejectionReason.FUTURE_CONTROLS),
    ],
)
def test_future_evidence_rejects(kind: str, expected: RiskRejectionReason) -> None:
    future = NOW + timedelta(seconds=1)
    snapshot = feature()
    order_intent = intent(snapshot)
    risk_state = state()
    market_quote = quote()
    if kind == "signal":
        snapshot = feature(observation_time=NOW, availability_time=future)
        order_intent = intent(snapshot, decision_time=future)
    elif kind == "feature":
        snapshot = feature(observation_time=future, availability_time=future)
        order_intent = intent(snapshot, decision_time=future)
    elif kind == "quote":
        market_quote = quote(observation_time=future, availability_time=future)
    elif kind == "state":
        risk_state = state(observation_time=future, availability_time=future)
    else:
        risk_state = state(
            operator_controls=controls(observation_time=future, availability_time=future)
        )
    decision = decide(
        order_intent,
        risk_state=risk_state,
        supporting_feature=snapshot,
        market_quote=market_quote,
    )
    assert expected in reasons(decision)


@pytest.mark.parametrize(
    ("market_quote", "expected"),
    [
        (quote(bid=Decimal("101"), ask=Decimal("100")), RiskRejectionReason.CROSSED_QUOTE),
        (quote(bid=Decimal("0"), ask=Decimal("1")), RiskRejectionReason.NONPOSITIVE_QUOTE),
        (quote(bid=Decimal("-1"), ask=Decimal("1")), RiskRejectionReason.NONPOSITIVE_QUOTE),
    ],
)
def test_invalid_operational_quotes_reject(
    market_quote: Quote, expected: RiskRejectionReason
) -> None:
    assert expected in reasons(decide(intent(), market_quote=market_quote))


def test_positive_locked_quote_is_accepted() -> None:
    assert decide(intent(), market_quote=quote(bid=Decimal("100"), ask=Decimal("100"))).status is (
        RiskDecisionStatus.AUTHORIZED
    )


def test_quote_instrument_and_feature_lineage_mismatches_reject() -> None:
    order_intent = intent()
    wrong_lineage = feature(dataset_id="dataset-2")
    assert RiskRejectionReason.LINEAGE_MISMATCH in reasons(
        decide(order_intent, supporting_feature=wrong_lineage)
    )
    assert RiskRejectionReason.QUOTE_INSTRUMENT_MISMATCH in reasons(
        decide(order_intent, market_quote=quote(instrument=QQQ))
    )


def test_unsupported_instrument_rejects() -> None:
    qqq_feature = feature(instrument=QQQ)
    decision = decide(
        intent(qqq_feature),
        risk_policy=policy(allowed=(SPY,)),
        supporting_feature=qqq_feature,
        market_quote=quote(instrument=QQQ),
    )
    assert RiskRejectionReason.UNSUPPORTED_INSTRUMENT in reasons(decision)


def test_explicit_empty_allowlist_is_fail_closed() -> None:
    assert RiskRejectionReason.UNSUPPORTED_INSTRUMENT in reasons(
        decide(intent(), risk_policy=policy(allowed=()))
    )


@pytest.mark.parametrize("quantity", [Decimal("0"), Decimal("-1"), Decimal("1.5")])
def test_invalid_operational_quantity_rejects(quantity: Decimal) -> None:
    assert RiskRejectionReason.INVALID_QUANTITY in reasons(decide(intent(quantity=quantity)))


def test_quantity_above_policy_rejects() -> None:
    assert RiskRejectionReason.MAX_QUANTITY_EXCEEDED in reasons(
        decide(intent(quantity=Decimal("6")), risk_policy=policy(maximum_quantity=Decimal("5")))
    )


def test_future_input_cannot_rewrite_recorded_rejection() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    order_intent = intent()
    first = gate.admit(
        intent=order_intent,
        state=state(operator_controls=controls(trading_enabled=False)),
        feature=feature(),
        quote=quote(),
        decision_time=NOW,
    )
    retry = gate.admit(
        intent=order_intent,
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW + timedelta(seconds=1),
    )
    assert RiskRejectionReason.TRADING_DISABLED in reasons(first.decision)
    assert retry.decision.reasons == (RiskRejectionReason.DUPLICATE_INTENT,)
    assert retry.decision.prior_decision_reference == first.decision.decision_id
    assert gate.recorded_decisions == (first.decision,)
    assert gate.reservations == ()


def test_reordered_equivalent_collections_produce_equal_decisions() -> None:
    spy_position = OpenLongPosition(SPY, Decimal("5"), "spy-position")
    qqq_position = OpenLongPosition(QQQ, Decimal("5"), "qqq-position")
    exit_feature = feature(value=Decimal("1"))
    order_intent = intent(exit_feature, signal_type=SignalType.EXIT)
    left = decide(
        order_intent,
        risk_policy=policy(allowed=(SPY, QQQ)),
        risk_state=state(positions=(spy_position, qqq_position)),
        supporting_feature=exit_feature,
    )
    right = decide(
        order_intent,
        risk_policy=policy(allowed=(QQQ, SPY)),
        risk_state=state(positions=(qqq_position, spy_position)),
        supporting_feature=exit_feature,
    )
    assert left == right


def test_hostile_decimal_context_does_not_change_identity_policy_or_decision() -> None:
    order_intent = intent(quantity=Decimal("5.000"))
    risk_policy = policy(maximum_quantity=Decimal("10.000"))
    baseline = decide(order_intent, risk_policy=risk_policy)
    baseline_identity = order_intent.intent_identity
    baseline_payload = order_intent.payload_fingerprint
    baseline_policy = risk_policy.fingerprint
    hostile = Context(prec=2, rounding=ROUND_CEILING, Emin=-2, Emax=2)
    with localcontext(hostile):
        observed = decide(order_intent, risk_policy=risk_policy)
        assert order_intent.intent_identity == baseline_identity
        assert order_intent.payload_fingerprint == baseline_payload
        assert risk_policy.fingerprint == baseline_policy
    assert observed == baseline


def test_dispatch_accepts_only_one_genuine_gate_grant() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    order_intent = intent()
    admission = gate.admit(
        intent=order_intent,
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW,
    )
    authorization = admission.authorization
    assert authorization is not None

    for raw in (order_intent.source_signal, order_intent, admission.decision):
        with pytest.raises(DispatchAuthorizationError):
            gate.claim_for_dispatch(raw)

    assert gate.claim_for_dispatch(authorization) == order_intent
    with pytest.raises(DispatchAuthorizationError, match="already been consumed"):
        gate.claim_for_dispatch(authorization)


def test_historical_evidence_cannot_be_forged_into_an_operational_grant() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    admission = gate.admit(
        intent=intent(),
        state=state(),
        feature=feature(),
        quote=quote(),
        decision_time=NOW,
    )
    authorization = admission.authorization
    assert authorization is not None
    with pytest.raises(RiskContractError, match="only be issued by RiskGate"):
        AuthorizedOrder(
            grant_id=authorization.grant_id,
            intent_identity=authorization.intent_identity,
            payload_fingerprint=authorization.payload_fingerprint,
            decision_id=authorization.decision_id,
            operational_scope=authorization.operational_scope,
            _gate_token=object(),
        )

    other_gate = RiskGate(operational_scope="paper-primary", policy=policy())
    with pytest.raises(DispatchAuthorizationError, match="not issued by this gate"):
        other_gate.claim_for_dispatch(authorization)


def test_intent_signal_inconsistency_rejects() -> None:
    order_intent = replace(intent(), side=OrderSide.SELL)
    assert RiskRejectionReason.INTENT_SIGNAL_MISMATCH in reasons(decide(order_intent))


def test_inconsistent_duplicate_inventory_rejects() -> None:
    positions = (
        OpenLongPosition(SPY, Decimal("5"), "position-1"),
        OpenLongPosition(SPY, Decimal("5"), "position-2"),
    )
    assert RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE in reasons(
        decide(intent(), risk_state=state(positions=positions))
    )


def test_inconsistent_state_elsewhere_in_scope_fails_closed() -> None:
    position = OpenLongPosition(SPY, Decimal("5"), "position-1")
    conflicting_entry = OutstandingOrder(
        SPY,
        "pending-entry",
        OrderSide.BUY,
        Decimal("5"),
        "pending-1",
    )
    qqq_feature = feature(instrument=QQQ)
    decision = decide(
        intent(qqq_feature),
        risk_state=state(positions=(position,), outstanding=(conflicting_entry,)),
        supporting_feature=qqq_feature,
        market_quote=quote(instrument=QQQ),
    )
    assert RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE in reasons(decision)
