"""Net accounting and legacy activity limitations; synthetic provider evidence only."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from shadow.adapters.alpaca.paper_broker import AlpacaPaperBroker, HttpResponse, PaperCredentials
from shadow.execution.broker import (
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Evidence,
    OrderStatus,
)
from shadow.execution.crypto_accounting import (
    CryptoActivityEvidence,
    CryptoFeeActivity,
    inventory_effects,
)
from shadow.execution.journal_codec import canonical_bytes, decode_canonical
from shadow.execution.reconciliation import OperationalState, reconcile
from shadow.risk.models import OrderSide
from tests.test_alpaca_paper_broker import RecordingTransport, account, response
from tests.test_btc_execution import btc_request
from tests.test_paper_dispatch import _dispatch_with_historical_status
from tests.test_paper_reconciliation import NOW

EV = Evidence("activity", "paper-account", "paper-scope", NOW, NOW)


def batch() -> CryptoActivityEvidence:
    fill = BrokerFill(
        EV, "fill", "buy", btc_request().instrument, OrderSide.BUY, Decimal("0.0012"), Decimal(100)
    )
    fee = CryptoFeeActivity(EV, "fee", "crypto-fee", NOW.date(), Decimal("0.000003"), "BTC", "fill")
    return CryptoActivityEvidence(
        EV,
        NOW - timedelta(minutes=1),
        NOW,
        (fill,),
        (fee,),
        (),
        True,
        True,
        ("fill",),
        "synthetic-provider-finality",
    )


def test_exact_net_fee_and_explicit_zero_fee() -> None:
    evidence = batch()
    with localcontext() as context:
        context.prec = 2
        effect = inventory_effects(evidence)[0]
        assert effect.net_btc == Decimal("0.001197")
        assert effect.usd_cash == Decimal("-0.12")
    assert inventory_effects(replace(evidence, fees=()))[0].net_btc == Decimal("0.0012")
    duplicate = replace(evidence, executions=evidence.executions * 2, fees=evidence.fees * 2)
    assert inventory_effects(duplicate) == inventory_effects(evidence)
    assert decode_canonical(canonical_bytes(evidence)) == evidence


@pytest.mark.parametrize(
    "variant",
    [
        "unknown_asset",
        "unknown_link",
        "delayed",
        "truncated",
        "correction",
        "conflicting_duplicate",
    ],
)
def test_unusable_accounting(variant: str) -> None:
    evidence = batch()
    fee = evidence.fees[0]
    error: type[Exception] = ValueError
    if variant == "unknown_asset":
        evidence = replace(evidence, fees=(replace(fee, asset="ETH"),))
    elif variant == "unknown_link":
        evidence = replace(evidence, fees=(replace(fee, execution_id=None),))
        error = LookupError
    elif variant == "delayed":
        evidence = replace(evidence, fees=(), fee_complete_ids=())
        error = LookupError
    elif variant == "truncated":
        evidence = replace(evidence, history_verified=False)
        error = LookupError
    elif variant == "correction":
        evidence = replace(evidence, fees=(replace(fee, correction_of="earlier"),))
    else:
        evidence = replace(evidence, fees=(fee, replace(fee, amount=Decimal(1))))
    with pytest.raises(error):
        inventory_effects(evidence)


def test_net_entry_and_exit_reconciliation(tmp_path: Path) -> None:
    outcome, _ = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    assert outcome.attempt is not None
    request = replace(
        btc_request(), operational_scope="paper-scope", client_id=outcome.attempt.client_order_id
    )
    buy = replace(outcome.attempt, request=request)
    order = BrokerOrder(
        EV,
        "buy",
        request.client_id,
        request.instrument,
        request.side,
        request.quantity,
        request.quantity,
        OrderStatus.FILLED,
        request.order_type,
        request.time_in_force,
        False,
        None,
        None,
    )
    snapshot = BrokerSnapshot(
        EV,
        (BrokerPosition(EV, request.instrument, Decimal("0.001197")),),
        (order,),
        True,
        True,
        NOW - timedelta(minutes=1),
        NOW,
    )
    evidence = batch()
    result = reconcile(attempts=(buy,), snapshot=snapshot, crypto_evidence=evidence)
    assert result.state is OperationalState.HOLDING
    assert (
        reconcile(attempts=(buy,), snapshot=snapshot, require_crypto_evidence=True).state
        is OperationalState.UNRESOLVED
    )
    assert (
        reconcile(
            attempts=(buy,),
            snapshot=snapshot,
            crypto_evidence=replace(evidence, fees=(), fee_complete_ids=()),
        ).state
        is OperationalState.UNRESOLVED
    )
    sell_request = replace(request, side=OrderSide.SELL, quantity=result.exposure, client_id="exit")
    sell = replace(buy, request=sell_request, client_order_id="exit", intent_identity="exit")
    sell_order = replace(
        order,
        order_id="sell",
        client_id="exit",
        side=OrderSide.SELL,
        quantity=result.exposure,
        filled_quantity=result.exposure,
    )
    sell_fill = replace(
        evidence.executions[0],
        execution_id="sell-fill",
        order_id="sell",
        side=OrderSide.SELL,
        quantity=result.exposure,
    )
    sell_fee = replace(
        evidence.fees[0],
        activity_id="sell-fee",
        asset="USD",
        amount=Decimal("0.01"),
        execution_id="sell-fill",
    )
    evidence = replace(
        evidence,
        executions=(*evidence.executions, sell_fill),
        fees=(*evidence.fees, sell_fee),
        fee_complete_ids=("fill", "sell-fill"),
    )
    assert inventory_effects(evidence)[1].usd_cash == Decimal("0.1097")
    assert (
        reconcile(
            attempts=(buy, sell),
            snapshot=replace(snapshot, positions=(), orders=(order, sell_order)),
            crypto_evidence=evidence,
        ).state
        is OperationalState.FLAT
    )


def fill_row(identity: str = "fill") -> dict[str, object]:
    return dict(
        id=identity,
        activity_type="FILL",
        type="fill",
        symbol="BTC/USD",
        side="buy",
        qty="0.0012",
        cum_qty="0.0012",
        leaves_qty="0",
        price="100",
        transaction_time=NOW.isoformat(),
        order_id="buy",
    )


def collect(
    pages: list[list[dict[str, object]]], max_pages: int = 20
) -> tuple[CryptoActivityEvidence | BrokerError, RecordingTransport]:
    transport = RecordingTransport(
        [
            response(
                200,
                dict(
                    account(),
                    account_number="paper-account",
                    crypto_status="ACTIVE",
                    cash="100",
                    non_marginable_buying_power="100",
                ),
            ),
            *(HttpResponse(200, {}, json.dumps(page).encode()) for page in pages),
        ]
    )
    broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="paper-scope",
        transport=transport,
    )
    result = broker.read_btc_activities(
        history_start=NOW - timedelta(minutes=1), history_end=NOW, max_pages=max_pages
    )
    assert all(call[0] == "GET" for call in transport.calls)
    return result, transport


def test_activity_pagination_and_unproven_finality() -> None:
    page = [fill_row(str(i)) for i in range(100)]
    result, transport = collect([page, [page[-1]]])
    assert isinstance(result, CryptoActivityEvidence)
    assert len(result.executions) == 100
    assert "page_token=99" in transport.calls[-1][1]
    assert result.query_exhausted and not result.history_verified
    with pytest.raises(LookupError):
        inventory_effects(result)
    result, _ = collect([page], max_pages=1)
    assert isinstance(result, BrokerError)
    result, _ = collect([page, page])
    assert isinstance(result, BrokerError)


@pytest.mark.parametrize(
    "row",
    [
        dict(fill_row(), qty="NaN"),
        dict(fill_row(), order_id=None),
        dict(fill_row(), transaction_time="bad"),
        dict(fill_row(), side="unknown"),
    ],
)
def test_malformed_activity_fails_closed(row: dict[str, object]) -> None:
    assert isinstance(collect([[row]])[0], BrokerError)


def test_legacy_fee_has_no_invented_link_or_asset_and_unknown_is_explicit() -> None:
    fee: dict[str, object] = dict(
        id="fee",
        activity_type="CFEE",
        date=NOW.date().isoformat(),
        net_amount="0",
        qty="-0.000003",
        symbol="BTCUSD",
        status="executed",
    )
    result, _ = collect([[fill_row(), fee, dict(id="correction", activity_type="BUST")]])
    assert isinstance(result, CryptoActivityEvidence)
    assert result.fees[0].asset is None and result.fees[0].execution_id is None
    assert result.unsupported == ("correction:BUST",)
