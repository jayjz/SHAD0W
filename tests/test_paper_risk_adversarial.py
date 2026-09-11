"""Operational safety attacks and explicit limits of the P2A application seam."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import timedelta
from decimal import ROUND_CEILING, Context, Decimal, localcontext
from threading import Barrier

import pytest

from shadow.execution import attach_execution_economics
from shadow.features import FeatureSnapshot
from shadow.risk import (
    AuthorizedOrder,
    DispatchAuthorizationError,
    OpenLongPosition,
    OperationalQuantityConfig,
    OrderIntent,
    OrderSide,
    OutstandingOrder,
    RiskAdmission,
    RiskContractError,
    RiskDecisionStatus,
    RiskGate,
    RiskRejectionReason,
    RiskState,
)
from shadow.risk.models import _AUTHORIZATION_ISSUER
from shadow.simulation import LifecycleAction, LifecycleActionType, LifecycleState
from shadow.strategies import (
    MeanReversionConfig,
    PositionState,
    Signal,
    SignalReason,
    SignalType,
    StrategyContractError,
    evaluate_mean_reversion,
)
from tests.test_execution_economics import _economics_config, _filled
from tests.test_paper_risk import (
    NOW,
    QQQ,
    SPY,
    controls,
    decide,
    feature,
    intent,
    policy,
    quote,
    reasons,
)
from tests.test_paper_risk import state as make_state


def admit(
    gate: RiskGate,
    order: OrderIntent | None = None,
    state: RiskState | None = None,
    snapshot: FeatureSnapshot | None = None,
) -> RiskAdmission:
    snapshot = feature() if snapshot is None else snapshot
    return gate.admit(
        intent=intent(snapshot) if order is None else order,
        state=make_state() if state is None else state,
        feature=snapshot,
        quote=quote(instrument=snapshot.instrument),
        decision_time=NOW,
    )


@pytest.mark.parametrize(
    "initial",
    [
        make_state(inventory_complete=False),
        make_state(observation_time=NOW - timedelta(seconds=6)),
        make_state(operator_controls=controls(trading_enabled=False)),
    ],
)
def test_recovered_state_does_not_revive_a_terminal_opportunity(initial: RiskState) -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    first = admit(gate, state=initial)
    assert first.decision.status is RiskDecisionStatus.REJECTED
    retry = admit(gate)
    assert retry.decision.reasons == (RiskRejectionReason.DUPLICATE_INTENT,)
    assert retry.decision.prior_decision_reference == first.decision.decision_id
    assert retry.authorization is None
    assert gate.reservations == ()
    assert gate.recorded_decisions == (first.decision,)


def test_policy_is_fixed_and_prior_rejection_survives_even_private_policy_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy(enabled=False))
    first = admit(gate)
    assert first.decision.reasons == (RiskRejectionReason.POLICY_DISABLED,)
    with pytest.raises(FrozenInstanceError):
        gate.policy.enabled = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        gate.policy = policy()  # type: ignore[misc]
    # Unsupported introspection deliberately exercises the history-first rule.
    monkeypatch.setattr(gate, "_policy", replace(policy(), policy_id="policy-v2"))
    assert decide(intent(), risk_policy=gate.policy).status is RiskDecisionStatus.AUTHORIZED
    retry = admit(gate)
    assert retry.authorization is None
    assert retry.decision.prior_decision_reference == first.decision.decision_id
    assert retry.decision.policy_fingerprint == first.decision.policy_fingerprint


@pytest.mark.parametrize("freeze", ["kill_switch", "trading_disabled", "policy"])
def test_claim_proves_admission_only_and_does_not_revalidate_controls_or_policy(
    freeze: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    first = admit(gate)
    assert first.authorization is not None
    current_state = make_state()
    if freeze == "policy":
        monkeypatch.setattr(gate, "_policy", policy(enabled=False))
    else:
        current_state = make_state(
            operator_controls=controls(
                trading_enabled=freeze != "trading_disabled",
                kill_switch_active=freeze == "kill_switch",
            )
        )
    later = admit(gate, state=current_state, snapshot=feature(instrument=QQQ))
    assert later.decision.status is RiskDecisionStatus.REJECTED
    assert gate.claim_for_dispatch(first.authorization) == first.decision.intent
    assert len(gate.reservations) == 1


def test_claim_has_no_expiry_and_is_not_evidence_of_current_freshness() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    first = admit(gate)
    assert first.authorization is not None
    stale = decide(first.decision.intent, decision_time=NOW + timedelta(days=1))
    assert stale.status is RiskDecisionStatus.REJECTED
    assert RiskRejectionReason.STALE_SIGNAL in reasons(stale)
    # The claim accepts no time or fresh evidence. This must never mean external
    # submission is safe: it proves only one consumption of the prior admission.
    assert gate.claim_for_dispatch(first.authorization) is first.decision.intent
    assert len(gate.reservations) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda s: replace(s, instrument=QQQ),
        lambda s: replace(s, strategy_id="other-strategy"),
        lambda s: replace(s, strategy_version="strategy-v2"),
        lambda s: replace(s, configuration_id="config-v2"),
        lambda s: replace(s, signal_type=SignalType.EXIT),
        lambda s: replace(s, feature_implementation_version="feature-v2"),
        lambda s: replace(s, feature_window=21),
        lambda s: replace(
            s, feature_observation_time=s.feature_observation_time - timedelta(microseconds=1)
        ),
        lambda s: replace(
            s, feature_availability_time=s.feature_availability_time + timedelta(microseconds=1)
        ),
        lambda s: replace(s, source_dataset_id="dataset-2"),
    ],
)
def test_distinct_declared_source_opportunities_have_distinct_identities(
    change: Callable[[Signal], Signal],
) -> None:
    original = intent()
    assert replace(original, source_signal=change(original.source_signal)).intent_identity != (
        original.intent_identity
    )
    assert (
        replace(original, operational_scope="other-scope").intent_identity
        != original.intent_identity
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda s: replace(s, observed_feature_value=Decimal("-2.1")),
        lambda s: replace(s, entry_threshold=Decimal("-1.75")),
        lambda s: replace(s, exit_threshold=Decimal("0.5")),
        lambda s: replace(s, maximum_feature_age=timedelta(seconds=50)),
        lambda s: replace(s, reason=SignalReason.EXIT_THRESHOLD),
        lambda s: replace(
            s,
            decision_time=NOW + timedelta(seconds=1),
            availability_time=NOW + timedelta(seconds=1),
        ),
    ],
)
def test_changed_content_of_same_source_opportunity_conflicts(
    change: Callable[[Signal], Signal],
) -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    original = intent()
    assert admit(gate, original).authorization is not None
    changed_signal = change(original.source_signal)
    changed = replace(
        original, source_signal=changed_signal, intent_time=changed_signal.decision_time
    )
    assert changed.intent_identity == original.intent_identity
    assert changed.payload_fingerprint != original.payload_fingerprint
    assert admit(gate, changed).decision.reasons == (RiskRejectionReason.INTENT_IDENTITY_CONFLICT,)


@pytest.mark.parametrize(
    "changed",
    [
        replace(feature(), value=Decimal("-2.1")),
        replace(feature(), source_dataset_id="dataset-2"),
        replace(feature(), implementation_version="feature-v2"),
        replace(feature(), availability_time=NOW),
        replace(feature(), window=21),
    ],
)
def test_feature_metadata_and_value_must_match_the_signal(changed: FeatureSnapshot) -> None:
    assert RiskRejectionReason.LINEAGE_MISMATCH in reasons(
        decide(intent(), supporting_feature=changed)
    )


def test_quantity_configuration_for_another_instrument_cannot_form_intent() -> None:
    with pytest.raises(RiskContractError, match="instrument must match"):
        OrderIntent.from_signal(
            operational_scope="paper-primary",
            signal=intent().source_signal,
            quantity_config=OperationalQuantityConfig(QQQ, "quantity-v1", Decimal(5)),
        )


@pytest.mark.parametrize("kind", ["signal", "feature", "quote", "state", "controls"])
@pytest.mark.parametrize("over", [0, 1])
def test_exact_freshness_boundary_and_one_microsecond_over(kind: str, over: int) -> None:
    limits = {"signal": 60, "feature": 60, "quote": 10, "state": 5, "controls": 5}
    observed = NOW - timedelta(seconds=limits[kind], microseconds=over)
    snapshot = feature()
    order = intent()
    state = make_state()
    market = quote()
    risk_policy = policy()
    if kind == "signal":
        snapshot = feature(observation_time=observed, availability_time=observed)
        order = intent(snapshot, decision_time=observed)
        risk_policy = replace(risk_policy, maximum_feature_age=timedelta(minutes=2))
    elif kind == "feature":
        snapshot = feature(observation_time=observed, availability_time=NOW)
        order = intent(snapshot)
    elif kind == "quote":
        market = quote(observation_time=observed, availability_time=NOW)
    elif kind == "state":
        state = make_state(observation_time=observed, availability_time=NOW)
    else:
        state = make_state(
            operator_controls=controls(observation_time=observed, availability_time=NOW)
        )
    result = decide(
        order,
        risk_policy=risk_policy,
        risk_state=state,
        supporting_feature=snapshot,
        market_quote=market,
    )
    expected = {
        "signal": RiskRejectionReason.STALE_SIGNAL,
        "feature": RiskRejectionReason.STALE_FEATURE,
        "quote": RiskRejectionReason.STALE_QUOTE,
        "state": RiskRejectionReason.STALE_OPERATIONAL_STATE,
        "controls": RiskRejectionReason.STALE_CONTROLS,
    }[kind]
    if over:
        assert result.status is RiskDecisionStatus.REJECTED
        assert expected in reasons(result)
    else:
        assert result.status is RiskDecisionStatus.AUTHORIZED


@pytest.mark.parametrize("kind", ["signal", "feature", "quote", "state", "controls"])
@pytest.mark.parametrize("future_observation", [False, True])
def test_future_observation_or_availability_fails_closed(
    kind: str, future_observation: bool
) -> None:
    future = NOW + timedelta(microseconds=1)
    observed = future if future_observation else NOW
    snapshot = feature()
    order = intent()
    state = make_state()
    market = quote()
    if kind == "signal":
        if not future_observation:
            with pytest.raises(StrategyContractError, match="must equal decision_time"):
                replace(order.source_signal, availability_time=future)
            return
        order = intent(decision_time=future)
    elif kind == "feature":
        snapshot = feature(observation_time=observed, availability_time=future)
        order = intent(snapshot, decision_time=future)
    elif kind == "quote":
        market = quote(observation_time=observed, availability_time=future)
    elif kind == "state":
        state = make_state(observation_time=observed, availability_time=future)
    else:
        state = make_state(
            operator_controls=controls(observation_time=observed, availability_time=future)
        )
    result = decide(order, risk_state=state, supporting_feature=snapshot, market_quote=market)
    expected = {
        "signal": RiskRejectionReason.FUTURE_SIGNAL,
        "feature": RiskRejectionReason.FUTURE_FEATURE,
        "quote": RiskRejectionReason.FUTURE_QUOTE,
        "state": RiskRejectionReason.FUTURE_OPERATIONAL_STATE,
        "controls": RiskRejectionReason.FUTURE_CONTROLS,
    }[kind]
    assert expected in reasons(result)
    assert result.status is RiskDecisionStatus.REJECTED


def test_exact_echo_counts_once_but_changed_reference_is_inconsistent() -> None:
    for changed_reference in (False, True):
        gate = RiskGate(operational_scope="paper-primary", policy=policy(maximum_positions=2))
        assert admit(gate).authorization is not None
        reservation = gate.reservations[0]
        echoed = (
            replace(reservation, reference="broker-renamed") if changed_reference else reservation
        )
        result = admit(
            gate, state=make_state(outstanding=(echoed,)), snapshot=feature(instrument=QQQ)
        )
        if changed_reference:
            assert result.authorization is None
            assert RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE in reasons(result.decision)
        else:
            assert result.authorization is not None
            assert len(gate.reservations) == 2


@pytest.mark.parametrize("claimed", [False, True])
def test_reservation_never_releases_or_reconciles_a_reported_fill(claimed: bool) -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    first = admit(gate)
    assert first.authorization is not None
    if claimed:
        gate.claim_for_dispatch(first.authorization)
    initial_reservations = gate.reservations
    next_entry = admit(gate, snapshot=feature(dataset_id="next-dataset"))
    assert next_entry.authorization is None
    assert RiskRejectionReason.OUTSTANDING_ORDER_EXISTS in reasons(next_entry.decision)
    exit_feature = feature(value=Decimal(1))
    exit_result = admit(
        gate,
        intent(exit_feature, signal_type=SignalType.EXIT),
        make_state(positions=(OpenLongPosition(SPY, Decimal(5), "filled-position"),)),
        exit_feature,
    )
    assert exit_result.authorization is None
    assert RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE in reasons(exit_result.decision)
    assert gate.reservations == initial_reservations


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (make_state(inventory_complete=False), RiskRejectionReason.INCOMPLETE_INVENTORY),
        (
            make_state(observation_time=NOW - timedelta(seconds=6)),
            RiskRejectionReason.STALE_OPERATIONAL_STATE,
        ),
        (
            make_state(positions=(OpenLongPosition(SPY, Decimal(5), "position"),)),
            RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE,
        ),
        (
            make_state(
                outstanding=(
                    OutstandingOrder(QQQ, "orphan-exit", OrderSide.SELL, Decimal(5), "orphan"),
                )
            ),
            RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE,
        ),
    ],
)
def test_reservation_merge_does_not_repair_invalid_state(
    state: RiskState, expected: RiskRejectionReason
) -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    assert admit(gate).authorization is not None
    later = admit(gate, state=state, snapshot=feature(instrument=QQQ))
    assert later.authorization is None
    assert expected in reasons(later.decision)


def test_state_labels_are_evidence_not_a_monotonic_reconciliation_protocol() -> None:
    # Caller-reused IDs/revisions cannot conceal differing payload fingerprints.
    first = make_state()
    changed = make_state(positions=(OpenLongPosition(QQQ, Decimal(5), "position"),))
    assert (first.state_id, first.revision) == (changed.state_id, changed.revision)
    assert first.fingerprint != changed.fingerprint
    assert (
        decide(intent(), risk_state=first).decision_id
        != decide(intent(), risk_state=changed).decision_id
    )


@pytest.mark.parametrize(
    "scenario", ["same_intent", "same_instrument", "last_slot", "exit_entry", "conflict"]
)
def test_concurrent_admission_and_claim_have_exactly_one_winner(scenario: str) -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy(maximum_positions=1))
    snapshots = (feature(), feature())
    orders: tuple[OrderIntent, ...] = (intent(), intent())
    state = make_state()
    if scenario == "same_instrument":
        snapshots = (feature(), feature(dataset_id="next-dataset"))
        orders = tuple(intent(s) for s in snapshots)
    elif scenario == "last_slot":
        snapshots = (feature(), feature(instrument=QQQ))
        orders = tuple(intent(s) for s in snapshots)
    elif scenario == "exit_entry":
        snapshots = (feature(value=Decimal(1)), feature(instrument=QQQ))
        orders = (intent(snapshots[0], signal_type=SignalType.EXIT), intent(snapshots[1]))
        state = make_state(positions=(OpenLongPosition(SPY, Decimal(5), "position"),))
    elif scenario == "conflict":
        orders = (intent(), intent(quantity=Decimal(6)))
    barrier = Barrier(2, timeout=5)

    def attempt(index: int) -> RiskAdmission:
        barrier.wait()
        return admit(gate, orders[index], state, snapshots[index])

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt, index) for index in range(2)]
        results = [future.result(timeout=10) for future in futures]
    winners = [r for r in results if r.authorization is not None]
    assert len(winners) == len(gate.reservations) == 1
    grant = winners[0].authorization
    assert grant is not None
    if scenario == "exit_entry":
        assert winners[0].decision.intent.side is OrderSide.SELL
    if scenario == "conflict":
        assert any(
            RiskRejectionReason.INTENT_IDENTITY_CONFLICT in reasons(r.decision) for r in results
        )

    def claim(_: int) -> bool:
        barrier.wait()
        try:
            gate.claim_for_dispatch(grant)
            return True
        except DispatchAuthorizationError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures_claim = [executor.submit(claim, index) for index in range(2)]
        assert sum(future.result(timeout=10) for future in futures_claim) == 1


def test_private_token_copy_shares_one_claim_and_does_not_create_another_grant() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    first = admit(gate)
    grant = first.authorization
    assert grant is not None
    # Deliberate private issuer/token introspection: P2A is not hostile-process security.
    clone = replace(grant, _issuer=_AUTHORIZATION_ISSUER)
    assert clone is not grant and clone._gate_token is grant._gate_token
    for invalid in (deepcopy(grant), asdict(grant), replace(first.decision.intent)):
        with pytest.raises(DispatchAuthorizationError):
            gate.claim_for_dispatch(invalid)
    with pytest.raises(DispatchAuthorizationError):
        gate.claim_for_dispatch(
            replace(clone, payload_fingerprint="altered", _issuer=_AUTHORIZATION_ISSUER)
        )
    other_gate = RiskGate(operational_scope="paper-primary", policy=policy())
    assert admit(other_gate).authorization is not None
    with pytest.raises(DispatchAuthorizationError):
        other_gate.claim_for_dispatch(clone)
    assert gate.claim_for_dispatch(clone) is first.decision.intent
    for replay in (grant, clone, copy(grant)):
        with pytest.raises(DispatchAuthorizationError, match="already been consumed"):
            gate.claim_for_dispatch(replay)


def test_even_private_intent_mutation_cannot_claim_a_changed_payload() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    first = admit(gate)
    assert first.authorization is not None
    object.__setattr__(first.decision.intent, "quantity", Decimal(6))
    with pytest.raises(DispatchAuthorizationError, match="not issued by this gate"):
        gate.claim_for_dispatch(first.authorization)


def test_research_lifecycle_and_economic_evidence_cannot_claim() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    action = LifecycleAction(
        "action", "signal", SPY, LifecycleActionType.ENTRY, NOW, NOW, LifecycleState.FLAT
    )
    economics = attach_execution_economics(_filled(), _economics_config())
    assert economics is not None
    for raw in (action, economics):
        with pytest.raises(DispatchAuthorizationError):
            gate.claim_for_dispatch(raw)


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_quantity_cannot_enter_operational_contracts(value: str) -> None:
    with pytest.raises(RiskContractError, match="finite Decimal"):
        intent(quantity=Decimal(value))
    with pytest.raises(RiskContractError, match="finite Decimal"):
        replace(intent(), quantity=Decimal(value))


@pytest.mark.parametrize("value", ["5", "5.0", "5.000", "0", "-1", "1.5", "1E1000"])
def test_quantity_decisions_survive_decimal_precision_exponents_rounding_and_traps(
    value: str,
) -> None:
    order = intent(quantity=Decimal(value))
    baseline = decide(order)
    fingerprint = order.payload_fingerprint
    hostile = Context(prec=1, rounding=ROUND_CEILING, Emin=-1, Emax=1)
    for trap in hostile.traps:
        hostile.traps[trap] = True
    with localcontext(hostile):
        assert decide(order) == baseline
        assert order.payload_fingerprint == fingerprint
    if Decimal(value) == 5:
        assert baseline.status is RiskDecisionStatus.AUTHORIZED
        assert fingerprint == intent(quantity=Decimal(5)).payload_fingerprint
    else:
        assert baseline.status is RiskDecisionStatus.REJECTED


def test_grant_reconstruction_with_only_public_fields_is_rejected() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    grant = admit(gate).authorization
    assert grant is not None
    with pytest.raises(RiskContractError, match="only be issued"):
        AuthorizedOrder(
            grant.grant_id,
            grant.intent_identity,
            grant.payload_fingerprint,
            grant.decision_id,
            grant.operational_scope,
            object(),
        )


@pytest.mark.parametrize("position_state", [PositionState.FLAT, PositionState.HOLDING])
def test_legitimate_strategy_configurations_produce_distinct_authorizable_opportunities(
    position_state: PositionState,
) -> None:
    snapshot = feature(value=Decimal(-2) if position_state is PositionState.FLAT else Decimal(1))
    original = MeanReversionConfig(
        SPY, "config-v1", 20, Decimal("-1.5"), Decimal(0), timedelta(minutes=1)
    )
    configurations = (
        original,
        replace(original, configuration_id="config-v2", entry_threshold=Decimal("-1.75")),
    )
    identities = set()
    for config in configurations:
        proposal = evaluate_mean_reversion(snapshot, config, position_state, decision_time=NOW)
        assert proposal is not None
        order = OrderIntent.from_signal(
            operational_scope="paper-primary",
            signal=proposal,
            quantity_config=OperationalQuantityConfig(SPY, "quantity-v1", Decimal(5)),
        )
        positions = (
            (OpenLongPosition(SPY, Decimal(5), "position"),)
            if position_state is PositionState.HOLDING
            else ()
        )
        assert (
            decide(
                order, supporting_feature=snapshot, risk_state=make_state(positions=positions)
            ).status
            is RiskDecisionStatus.AUTHORIZED
        )
        identities.add(order.intent_identity)
    assert len(identities) == 2


@pytest.mark.parametrize("duplicate", ["position_id", "order_identity", "order_reference"])
def test_duplicate_operational_identifiers_reject_across_instruments(duplicate: str) -> None:
    positions: tuple[OpenLongPosition, ...] = ()
    orders: tuple[OutstandingOrder, ...] = ()
    if duplicate == "position_id":
        positions = (
            OpenLongPosition(SPY, Decimal(5), "same"),
            OpenLongPosition(QQQ, Decimal(5), "same"),
        )
    else:
        orders = (
            OutstandingOrder(SPY, "intent-1", OrderSide.BUY, Decimal(5), "reference-1"),
            OutstandingOrder(
                QQQ,
                "intent-1" if duplicate == "order_identity" else "intent-2",
                OrderSide.BUY,
                Decimal(5),
                "reference-1" if duplicate == "order_reference" else "reference-2",
            ),
        )
    result = decide(intent(), risk_state=make_state(positions=positions, outstanding=orders))
    assert RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE in reasons(result)


def test_departed_position_does_not_reconcile_an_exit_reservation() -> None:
    gate = RiskGate(operational_scope="paper-primary", policy=policy())
    snapshot = feature(value=Decimal(1))
    first = admit(
        gate,
        intent(snapshot, signal_type=SignalType.EXIT),
        make_state(positions=(OpenLongPosition(SPY, Decimal(5), "position"),)),
        snapshot,
    )
    assert first.authorization is not None
    gate.claim_for_dispatch(first.authorization)
    # The state reports flat after a hypothetical fill, but P2A has no release transition.
    result = admit(gate, snapshot=feature(instrument=QQQ))
    assert result.authorization is None
    assert RiskRejectionReason.INCONSISTENT_OPERATIONAL_STATE in reasons(result.decision)
    assert len(gate.reservations) == 1
