"""Offline adversarial P5A.1 contract and fake-port checks."""

import ast
import inspect
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from shadow.domain import Instrument
from shadow.execution import broker as module
from shadow.execution.broker import (
    Broker,
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerContractError,
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    CanaryConfig,
    Eligibility,
    ErrorCategory,
    Evidence,
    OrderStatus,
    SessionState,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
    TradeUpdate,
    UpdateKind,
)
from shadow.execution.fake_broker import BrokerCall, BrokerStep, FakeBroker
from shadow.risk.models import OrderSide, OrderTarget, OrderType, TimeInForce

NOW = datetime(2026, 9, 16, 15, tzinfo=UTC)
SPY = Instrument("SPY")
D = Decimal


def evidence(**changes: Any) -> Evidence:
    return replace(Evidence("e1", "synthetic-account", "scope", NOW, NOW), **changes)


def order(**changes: Any) -> BrokerOrder:
    return replace(
        BrokerOrder(
            evidence(),
            "order1",
            "client1",
            SPY,
            OrderSide.BUY,
            D(1),
            D(0),
            OrderStatus.NEW,
            OrderType.MARKET,
            TimeInForce.DAY,
            False,
            None,
            None,
        ),
        **changes,
    )


def request() -> SubmitRequest:
    return SubmitRequest(
        "synthetic-account",
        "scope",
        "client1",
        SPY,
        OrderSide.BUY,
        D(1),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )


def snapshot(**changes: Any) -> BrokerSnapshot:
    return replace(BrokerSnapshot(evidence(), (), (), True, True, NOW, NOW), **changes)


def config(**changes: Any) -> CanaryConfig:
    age = timedelta(seconds=10)
    return replace(
        CanaryConfig(
            configuration_id="canary-v1",
            account_id="synthetic-account",
            operational_scope="scope",
            enabled=False,
            provider="alpaca",
            target=OrderTarget.PAPER,
            instrument=SPY,
            quantity=D(1),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            regular_hours_only=True,
            maximum_concurrent_positions=1,
            maximum_outstanding_orders=1,
            daily_submission_limit=2,
            maximum_entry_notional=D(1000),
            buying_power_buffer=D(50),
            currency="USD",
            maximum_signal_age=age,
            maximum_feature_age=age,
            maximum_quote_age=age,
            maximum_broker_evidence_age=age,
            maximum_control_age=age,
            maximum_clock_skew=timedelta(seconds=1),
            dispatch_deadline=timedelta(seconds=2),
            close_guard=timedelta(seconds=5),
            run_duration=timedelta(minutes=10),
        ),
        **changes,
    )


def error(category: ErrorCategory = ErrorCategory.UNAVAILABLE) -> BrokerError:
    return BrokerError(evidence(), category, "synthetic failure")


def update() -> TradeUpdate:
    fill = BrokerFill(evidence(), "execution1", "order1", SPY, OrderSide.BUY, D(".25"), D(100))
    return TradeUpdate(
        evidence(),
        "event1",
        UpdateKind.FILL,
        order(status=OrderStatus.PARTIALLY_FILLED, filled_quantity=D(".25")),
        fill,
        None,
    )


def test_canonical_utc_and_immutable_evidence() -> None:
    offset = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert evidence(observation_time=offset, availability_time=offset) == evidence()
    assert evidence(observation_time=offset).observation_time.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        evidence().reference = "changed"  # type: ignore[misc]
    assert order(quantity=D("1.00")) == order()


@pytest.mark.parametrize("value", [None, "2026-09-16", NOW.replace(tzinfo=None), 1])
@pytest.mark.parametrize("field", ["observation_time", "availability_time"])
def test_invalid_timestamps(field: str, value: Any) -> None:
    with pytest.raises(BrokerContractError):
        evidence(**{field: value})


@pytest.mark.parametrize(
    "value", [D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"), 1, 1.0, "1", None, D(-1)]
)
def test_invalid_decimals(value: Any) -> None:
    fill = update().fill
    assert fill is not None
    for build in (
        lambda: order(quantity=value),
        lambda: BrokerPosition(evidence(), SPY, value),
        lambda: BrokerAccount(evidence(), OrderTarget.PAPER, Eligibility.ELIGIBLE, value, "USD"),
        lambda: config(maximum_entry_notional=value),
        lambda: replace(fill, price=value),
    ):
        with pytest.raises(BrokerContractError):
            build()


def test_unknown_account_asset_and_missing_buying_power_are_not_eligible() -> None:
    account = BrokerAccount(evidence(), OrderTarget.PAPER, Eligibility.UNKNOWN, None, "USD")
    assert account.eligibility is Eligibility.UNKNOWN
    assert (
        BrokerAsset(evidence(), SPY, Eligibility.UNKNOWN, Eligibility.INELIGIBLE).tradable
        is Eligibility.INELIGIBLE
    )
    with pytest.raises(BrokerContractError):
        replace(account, eligibility=Eligibility.ELIGIBLE)
    with pytest.raises(BrokerContractError):
        replace(account, target="live")  # type: ignore[arg-type]
    with pytest.raises(BrokerContractError):
        replace(account, eligibility="active")  # type: ignore[arg-type]


def test_clock_early_close_holiday_and_contradictions() -> None:
    clock = BrokerClock(
        evidence(),
        SessionState.REGULAR,
        NOW.date(),
        NOW - timedelta(hours=1),
        NOW + timedelta(hours=2),
    )
    assert clock.session_close == NOW + timedelta(hours=2)
    assert BrokerClock(evidence(), SessionState.CLOSED, NOW.date(), None, None)
    assert BrokerClock(evidence(), SessionState.UNKNOWN, NOW.date(), None, None)
    for changes in (
        {"state": SessionState.CLOSED},
        {"session_close": NOW},
        {"session_open": None},
        {"trading_date": NOW},
        {"session_close": NOW.replace(tzinfo=None)},
    ):
        with pytest.raises(BrokerContractError):
            replace(clock, **changes)


@pytest.mark.parametrize("status", list(OrderStatus))
def test_all_order_states(status: OrderStatus) -> None:
    quantity = (
        D(1)
        if status is OrderStatus.FILLED
        else (D(".3") if status is OrderStatus.PARTIALLY_FILLED else D(0))
    )
    assert order(status=status, filled_quantity=quantity).status is status


def test_terminal_and_outstanding_order_views_are_explicit_and_exhaustive() -> None:
    terminal = {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
    }
    assert {status for status in OrderStatus if status.is_terminal} == terminal
    orders = tuple(
        order(
            order_id=f"order-{status.value}",
            client_id=f"client-{status.value}",
            status=status,
            filled_quantity=(
                D(1)
                if status is OrderStatus.FILLED
                else (D(".3") if status is OrderStatus.PARTIALLY_FILLED else D(0))
            ),
        )
        for status in OrderStatus
    )
    result = snapshot(orders=orders)
    assert {item.status for item in result.terminal_orders} == terminal
    assert {item.status for item in result.outstanding_orders} == set(OrderStatus) - terminal


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "new"},
        {"filled_quantity": D(2)},
        {"quantity": D(0)},
        {"status": OrderStatus.FILLED},
        {"status": OrderStatus.PARTIALLY_FILLED},
        {"status": OrderStatus.NEW, "filled_quantity": D(".2")},
        {"replaces": "order1"},
        {"replaced_by": "order1"},
        {"replaces": "other", "replaced_by": "other"},
        {"side": "buy"},
    ],
)
def test_contradictory_order_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(BrokerContractError):
        order(**changes)


def test_partial_terminal_exposure_and_replacement_links_remain_evidence() -> None:
    predecessor = order(status=OrderStatus.REPLACED, filled_quantity=D(".25"), replaced_by="next")
    successor = order(order_id="next", client_id="client2", replaces="order1")
    assert len(snapshot(orders=(successor, predecessor)).orders) == 2
    assert order(status=OrderStatus.REPLACED).replaced_by is None  # unresolved, never flat
    assert order(status=OrderStatus.CANCELED, filled_quantity=D(".25")).filled_quantity == D(".25")
    assert BrokerPosition(evidence(), SPY, D(".25")).quantity == D(".25")


@pytest.mark.parametrize(
    "positions_complete,orders_complete",
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_completeness_is_not_empty(positions_complete: bool, orders_complete: bool) -> None:
    result = snapshot(positions_complete=positions_complete, orders_complete=orders_complete)
    assert result.positions == ()
    assert result.orders == ()
    assert result.complete is (positions_complete and orders_complete)


@pytest.mark.parametrize(
    "changes",
    [
        {"orders": (order(), order())},
        {"orders": (order(), order(order_id="other"))},
        {"orders": (order(), order(filled_quantity=D(".2"), status=OrderStatus.PARTIALLY_FILLED))},
        {"positions": (BrokerPosition(evidence(), SPY, D(1)),) * 2},
        {"orders": (order(evidence=evidence(account_id="other")),)},
        {"orders": ({"id": "provider-json"},)},
        {"positions": []},
        {"positions_complete": None},
        {"orders_complete": 1},
        {"history_end": NOW + timedelta(seconds=1)},
        {"history_start": NOW + timedelta(seconds=1)},
    ],
)
def test_snapshot_duplicates_bindings_and_malformed_rows(changes: dict[str, Any]) -> None:
    with pytest.raises(BrokerContractError):
        snapshot(**changes)


def test_updates_keep_execution_identity_and_corrections() -> None:
    event = update()
    assert event.fill is not None and event.fill.quantity == D(".25")
    for kind in (UpdateKind.TRADE_CORRECTION, UpdateKind.TRADE_BUST):
        assert replace(event, kind=kind, affected_execution_id="execution0")
        with pytest.raises(BrokerContractError):
            replace(event, kind=kind)
    for kind in (UpdateKind.CANCEL_REJECTED, UpdateKind.REPLACE_REJECTED, UpdateKind.UNKNOWN):
        assert replace(event, kind=kind, fill=None)
    with pytest.raises(BrokerContractError):
        replace(event, fill=None)
    with pytest.raises(BrokerContractError):
        replace(event, fill=replace(event.fill, order_id="other"))
    with pytest.raises(BrokerContractError):
        replace(event, fill=replace(event.fill, quantity=D(".5")))


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_submission_error_classification(category: ErrorCategory) -> None:
    status = (
        SubmissionStatus.REJECTED
        if category is ErrorCategory.DEFINITIVE_REJECTION
        else SubmissionStatus.UNCERTAIN
    )
    result = SubmissionResult(evidence(), request(), status, None, error(category))
    assert result.status is status
    wrong = (
        SubmissionStatus.UNCERTAIN
        if status is SubmissionStatus.REJECTED
        else SubmissionStatus.REJECTED
    )
    with pytest.raises(BrokerContractError):
        replace(result, status=wrong)


def test_acceptance_is_matching_order_not_position_or_authority() -> None:
    result = SubmissionResult(evidence(), request(), SubmissionStatus.ACCEPTED, order(), None)
    assert result.order is not None and result.order.filled_quantity == 0
    for changes in (
        {"order": None},
        {"order": order(client_id="other")},
        {"order": order(status=OrderStatus.UNKNOWN)},
        {"order": order(status=OrderStatus.REJECTED)},
        {"error": error()},
        {"evidence": evidence(account_id="other")},
    ):
        with pytest.raises(BrokerContractError):
            replace(result, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "other"},
        {"target": "live"},
        {"target": "paper"},
        {"instrument": (SPY, Instrument("QQQ"))},
        {"quantity": D(".5")},
        {"quantity": D(2)},
        {"quantity": True},
        {"order_type": "limit"},
        {"time_in_force": "gtc"},
        {"time_in_force": TimeInForce.GTC},
        {"regular_hours_only": False},
        {"regular_hours_only": 1},
        {"enabled": 1},
        {"maximum_concurrent_positions": 2},
        {"maximum_outstanding_orders": True},
        {"daily_submission_limit": 0},
        {"daily_submission_limit": -1},
        {"daily_submission_limit": True},
        {"daily_submission_limit": float("inf")},
        {"buying_power_buffer": D(0)},
        {"maximum_entry_notional": D(0)},
        {"maximum_clock_skew": timedelta(seconds=-1)},
        {"close_guard": timedelta(seconds=3)},
        {"dispatch_deadline": timedelta(seconds=11)},
        {"run_duration": timedelta(seconds=1)},
        {"account_id": " "},
        {"operational_scope": ""},
    ],
)
def test_canary_restrictions(changes: dict[str, Any]) -> None:
    with pytest.raises(BrokerContractError):
        config(**changes)


@pytest.mark.parametrize(
    "name",
    [
        "maximum_signal_age",
        "maximum_feature_age",
        "maximum_quote_age",
        "maximum_broker_evidence_age",
        "maximum_control_age",
        "dispatch_deadline",
        "close_guard",
        "run_duration",
    ],
)
@pytest.mark.parametrize("value", [timedelta(0), timedelta(seconds=-1), 1, None])
def test_canary_invalid_durations(name: str, value: Any) -> None:
    with pytest.raises(BrokerContractError):
        config(**{name: value})


def test_configuration_requires_explicit_choices_and_supports_disabled() -> None:
    from dataclasses import MISSING

    assert all(
        field.default is MISSING and field.default_factory is MISSING
        for field in fields(CanaryConfig)
    )
    assert not config().enabled
    assert config(enabled=True).enabled  # still no dispatch authority
    assert config(daily_submission_limit=1).daily_submission_limit == 1  # entry later needs two


def test_fake_deterministic_reads_lifecycle_and_all_submission_results() -> None:
    account = BrokerAccount(evidence(), OrderTarget.PAPER, Eligibility.ELIGIBLE, D(2000), "USD")
    clock = BrokerClock(evidence(), SessionState.CLOSED, NOW.date(), None, None)
    asset = BrokerAsset(evidence(), SPY, Eligibility.ELIGIBLE, Eligibility.ELIGIBLE)
    outcomes = (
        SubmissionResult(evidence(), request(), SubmissionStatus.ACCEPTED, order(), None),
        SubmissionResult(
            evidence(),
            request(),
            SubmissionStatus.REJECTED,
            None,
            error(ErrorCategory.DEFINITIVE_REJECTION),
        ),
        SubmissionResult(evidence(), request(), SubmissionStatus.UNCERTAIN, None, error()),
    )
    steps = (
        BrokerStep(BrokerCall("read_account"), account),
        BrokerStep(BrokerCall("read_account"), account),
        BrokerStep(BrokerCall("read_clock"), clock),
        BrokerStep(BrokerCall("read_asset", instrument=SPY), asset),
        BrokerStep(BrokerCall("read_snapshot"), snapshot(positions_complete=False)),
        BrokerStep(BrokerCall("lookup_order", client_id="client1"), error(ErrorCategory.NOT_FOUND)),
        BrokerStep(BrokerCall("lookup_order", order_id="order1"), order()),
        BrokerStep(BrokerCall("read_updates"), (update(), update())),
        *(BrokerStep(BrokerCall("submit", request=request()), result) for result in outcomes),
    )
    for _ in range(2):
        fake = FakeBroker(steps)
        port: Broker = fake
        assert port.read_account() == port.read_account() == account
        assert port.read_clock() == clock
        assert port.read_asset(SPY) == asset
        assert port.read_snapshot() == snapshot(positions_complete=False)
        assert port.lookup_order(order_id=None, client_id="client1") == error(
            ErrorCategory.NOT_FOUND
        )
        assert port.lookup_order(order_id="order1", client_id=None) == order()
        assert port.read_updates() == (update(), update())
        assert tuple(port.submit(request()) for _ in outcomes) == outcomes
        assert fake.calls == tuple(step.call for step in steps)
        with pytest.raises(BrokerContractError):
            port.read_account()


@pytest.mark.parametrize("category", [ErrorCategory.MALFORMED, ErrorCategory.UNAVAILABLE])
def test_fake_scripted_read_failure(category: ErrorCategory) -> None:
    fake = FakeBroker((BrokerStep(BrokerCall("read_snapshot"), error(category)),))
    assert fake.read_snapshot() == error(category)
    assert fake.calls == (BrokerCall("read_snapshot"),)


def test_fake_wrong_call_and_wrong_reply_fail_closed() -> None:
    fake = FakeBroker((BrokerStep(BrokerCall("read_account"), order()),))
    with pytest.raises(BrokerContractError):
        fake.read_account()
    assert len(fake.calls) == 1
    with pytest.raises(BrokerContractError):
        FakeBroker((BrokerStep(BrokerCall("read_clock"), error()),)).read_account()
    with pytest.raises(BrokerContractError):
        FakeBroker(()).lookup_order(order_id=None, client_id=None)
    with pytest.raises(BrokerContractError):
        FakeBroker((BrokerStep(BrokerCall("submit", request=request()), error()),)).submit(
            request()
        )


def test_core_boundary_has_no_provider_or_ambient_io_imports() -> None:
    from shadow.execution import fake_broker

    for source in (module, fake_broker):
        tree = ast.parse(inspect.getsource(source))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert all(
            name is not None
            and name.split(".")[0]
            in {"__future__", "dataclasses", "datetime", "decimal", "enum", "typing", "shadow"}
            for name in imports
        )
        assert not any(isinstance(node, ast.Import) for node in ast.walk(tree))
        assert not any(name and ("adapters" in name or "simulation" in name) for name in imports)
    with pytest.raises(BrokerContractError):
        order(evidence={"provider": "json"})
    with pytest.raises(BrokerContractError):
        BrokerPosition(evidence(), SPY, object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": D(".5")},
        {"quantity": D(0)},
        {"quantity": D("NaN")},
        {"quantity": 1},
        {"target": "live"},
        {"order_type": "limit"},
        {"time_in_force": "gtc"},
        {"time_in_force": TimeInForce.GTC},
        {"extended_hours": True},
        {"extended_hours": 0},
        {"side": "buy"},
        {"instrument": {"symbol": "SPY"}},
        {"client_id": " "},
    ],
)
def test_submission_payload_is_strict_but_not_authority(changes: dict[str, Any]) -> None:
    with pytest.raises(BrokerContractError):
        replace(request(), **changes)


def test_causal_evidence_and_canonical_snapshot_order() -> None:
    with pytest.raises(BrokerContractError):
        evidence(availability_time=NOW - timedelta(microseconds=1))
    with pytest.raises(BrokerContractError):
        snapshot(
            orders=(
                order(
                    evidence=evidence(
                        observation_time=NOW + timedelta(seconds=1),
                        availability_time=NOW + timedelta(seconds=1),
                    )
                ),
            )
        )
    first = BrokerPosition(evidence(), Instrument("AAA"), D(".1"))
    second = BrokerPosition(evidence(), SPY, D(1))
    assert snapshot(positions=(second, first)) == snapshot(positions=(first, second))
    assert order(order_type=None, time_in_force=None, extended_hours=None).order_type is None


def test_fake_preserves_partial_position_and_out_of_order_updates() -> None:
    current = update()
    older = replace(current, evidence=evidence(observation_time=NOW - timedelta(seconds=1)))
    partial = snapshot(
        positions=(BrokerPosition(evidence(), SPY, D(".25")),), orders=(current.order,)
    )
    fake = FakeBroker(
        (
            BrokerStep(BrokerCall("read_snapshot"), partial),
            BrokerStep(BrokerCall("read_updates"), (current, older, current)),
        )
    )
    assert fake.read_snapshot() == partial
    assert fake.read_updates() == (current, older, current)


def test_fake_never_coerces_raw_provider_responses() -> None:
    raw: Any = {"account": "provider-object"}
    fake = FakeBroker((BrokerStep(BrokerCall("read_account"), raw),))
    with pytest.raises(BrokerContractError):
        fake.read_account()
