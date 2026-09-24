"""BTC envelope and broker-authoritative lifecycle: fake transport only."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from shadow.adapters.alpaca.paper_broker import (
    AlpacaPaperBroker,
    AlpacaPaperError,
    PaperCredentials,
)
from shadow.execution.broker import (
    BrokerContractError,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Eligibility,
    Evidence,
    OrderStatus,
    SubmissionStatus,
)
from shadow.execution.crypto import BtcBrokerAsset, BtcSubmitRequest
from shadow.execution.journal_codec import canonical_bytes, decode_canonical
from shadow.execution.reconciliation import OperationalState, reconcile
from shadow.features.btc_trend import BTC
from shadow.risk.models import OrderSide, OrderTarget, OrderType, TimeInForce
from tests.test_alpaca_paper_broker import RecordingTransport, order, response
from tests.test_paper_dispatch import _dispatch_with_historical_status
from tests.test_paper_reconciliation import NOW


def btc_request(side: OrderSide = OrderSide.BUY) -> BtcSubmitRequest:
    return BtcSubmitRequest(
        "paper-account",
        "scope",
        "shp1_" + "a" * 40,
        BTC,
        side,
        Decimal("0.0012"),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.GTC,
        False,
    )


def asset_payload() -> dict[str, object]:
    # Synthetic provider observations, not operational constants.
    return dict(
        symbol="BTC/USD",
        **{"class": "crypto"},
        status="active",
        tradable=True,
        fractionable=True,
        min_order_size="0.0001",
        min_trade_increment="0.0001",
        price_increment="0.01",
    )


def asset() -> BtcBrokerAsset:
    return BtcBrokerAsset(
        Evidence("asset", "paper-account", "scope", NOW, NOW),
        BTC,
        Eligibility.INELIGIBLE,
        Eligibility.ELIGIBLE,
        Decimal("0.0001"),
        Decimal("0.0001"),
        Decimal("0.01"),
        True,
    )


@pytest.mark.parametrize("side", list(OrderSide))
def test_btc_market_gtc_fractional_payload_and_codec(side: OrderSide) -> None:
    request = btc_request(side)
    payload = dict(
        order(),
        symbol="BTCUSD",
        asset_class="crypto",
        qty="0.0012",
        side=side.value,
        time_in_force="gtc",
    )
    transport = RecordingTransport([response(200, asset_payload()), response(201, payload)])
    broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="scope",
        transport=transport,
    )
    result = broker.submit(request)
    assert result.status is SubmissionStatus.ACCEPTED
    assert result.order is not None and result.order.instrument == BTC
    assert len([call for call in transport.calls if call[0] == "POST"]) == 1
    sent = json.loads(transport.calls[-1][2] or b"{}")
    assert sent["qty"] == "0.0012" and sent["time_in_force"] == "gtc"
    assert decode_canonical(canonical_bytes(request)) == request
    assert decode_canonical(canonical_bytes(asset())) == asset()
    with pytest.raises(BrokerContractError):
        replace(request, time_in_force=TimeInForce.DAY)
    with pytest.raises(BrokerContractError):
        replace(request, quantity=Decimal("-0.1"))


@pytest.mark.parametrize("quantity", ["0.00001", "0.00015", "0.0012000001", "NaN", "Infinity", "0"])
def test_invalid_quantity_never_rounds_or_posts(quantity: str) -> None:
    assert not asset().accepts_quantity(Decimal(quantity))
    if quantity not in ("NaN", "Infinity", "0"):
        transport = RecordingTransport([response(200, asset_payload())])
        broker = AlpacaPaperBroker(
            credentials=PaperCredentials("key", "secret"),
            account_id="paper-account",
            operational_scope="scope",
            transport=transport,
        )
        with pytest.raises(AlpacaPaperError, match="quantity"):
            broker.submit(replace(btc_request(), quantity=Decimal(quantity)))
        assert all(call[0] == "GET" for call in transport.calls)


def test_constraints_are_current_evidence_and_context_independent() -> None:
    assert asset().accepts_quantity(Decimal("0.0012"))
    with localcontext() as context:
        context.prec = 2
        assert not asset().accepts_quantity(Decimal("0.00121"))
    assert not replace(asset(), minimum_order_size=Decimal("0.002")).accepts_quantity(
        Decimal("0.0012")
    )
    for payload in (
        dict(asset_payload(), min_trade_increment=None),
        dict(asset_payload(), min_order_size="NaN"),
    ):
        transport = RecordingTransport([response(200, payload)])
        broker = AlpacaPaperBroker(
            credentials=PaperCredentials("key", "secret"),
            account_id="paper-account",
            operational_scope="scope",
            transport=transport,
        )
        assert isinstance(broker.read_btc_asset(), BrokerError)
    assert not replace(asset(), tradable=Eligibility.INELIGIBLE).accepts_quantity(Decimal("0.0012"))
    assert not replace(asset(), fractionable=False).accepts_quantity(Decimal("0.0012"))


def test_btc_fractional_lifecycle_and_uncertainty(tmp_path: Path) -> None:
    # Reuse a typed durable-attempt fixture; only the reducer is under test here.
    # This is not a claim that equity RiskDecision authorizes BTC dispatch.
    outcome, _ = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    assert outcome.attempt is not None
    request = replace(
        btc_request(), operational_scope="paper-scope", client_id=outcome.attempt.client_order_id
    )
    buy = replace(outcome.attempt, request=request, submission=None)
    ev = Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
    filled = BrokerOrder(
        ev,
        "buy",
        buy.client_order_id,
        BTC,
        OrderSide.BUY,
        request.quantity,
        request.quantity,
        OrderStatus.FILLED,
        OrderType.MARKET,
        TimeInForce.GTC,
        False,
        None,
        None,
    )
    position = BrokerPosition(ev, BTC, request.quantity)
    snapshot = BrokerSnapshot(
        ev, (position,), (filled,), True, True, NOW - timedelta(minutes=1), NOW
    )
    assert reconcile(attempts=(buy,), snapshot=snapshot).state is OperationalState.HOLDING
    assert (
        reconcile(attempts=(buy,), snapshot=replace(snapshot, orders=(), positions=())).state
        is OperationalState.UNRESOLVED
    )
    assert (
        reconcile(attempts=(buy,), snapshot=replace(snapshot, orders_complete=False)).state
        is OperationalState.UNRESOLVED
    )
    pending = replace(
        filled, status=OrderStatus.PARTIALLY_FILLED, filled_quantity=Decimal("0.0001")
    )
    assert (
        reconcile(
            attempts=(buy,),
            snapshot=replace(
                snapshot,
                orders=(pending,),
                positions=(replace(position, quantity=Decimal("0.0001")),),
            ),
        ).state
        is OperationalState.ENTRY_PENDING
    )
    sell = replace(
        buy,
        intent_identity="exit",
        client_order_id="exit",
        request=replace(request, client_id="exit", side=OrderSide.SELL),
    )
    exit_order = replace(
        filled,
        order_id="sell",
        client_id="exit",
        side=OrderSide.SELL,
        filled_quantity=Decimal(0),
        status=OrderStatus.NEW,
    )
    assert (
        reconcile(
            attempts=(buy, sell), snapshot=replace(snapshot, orders=(filled, exit_order))
        ).state
        is OperationalState.EXIT_PENDING
    )
    completed = replace(exit_order, filled_quantity=request.quantity, status=OrderStatus.FILLED)
    assert (
        reconcile(
            attempts=(buy, sell),
            snapshot=replace(snapshot, orders=(filled, completed), positions=()),
        ).state
        is OperationalState.FLAT
    )
    assert reconcile(attempts=(), snapshot=snapshot).state is OperationalState.HALTED
    assert (
        reconcile(
            attempts=(buy,),
            snapshot=replace(snapshot, positions=(replace(position, quantity=Decimal("0.0011")),)),
        ).state
        is OperationalState.HALTED
    )
