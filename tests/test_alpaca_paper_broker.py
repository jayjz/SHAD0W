"""Offline conformance checks for the fixed-origin Alpaca PAPER adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from shadow.adapters.alpaca.paper_broker import (
    PAPER_TRADING_ORIGIN,
    AlpacaPaperBroker,
    AlpacaPaperError,
    HttpResponse,
    PaperCredentials,
)
from shadow.application.paper_canary import _read_only
from shadow.domain import Instrument
from shadow.execution.broker import (
    BrokerContractError,
    BrokerError,
    OrderStatus,
    SubmissionStatus,
    SubmitRequest,
)
from shadow.execution.reconciliation import OperationalState, reconcile
from shadow.risk.models import OrderSide, OrderTarget, OrderType, TimeInForce
from tests.test_paper_dispatch import _dispatch_with_historical_status


@dataclass
class RecordingTransport:
    replies: list[HttpResponse]

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        del headers, timeout
        self.calls.append((method, url, body))
        return self.replies.pop(0)


def response(status: int, payload: dict[str, object]) -> HttpResponse:
    return HttpResponse(status, {"X-Request-ID": "request-1"}, json.dumps(payload).encode())


def request() -> SubmitRequest:
    return SubmitRequest(
        "paper-account",
        "scope",
        "shp1_" + "a" * 40,
        Instrument("SPY"),
        OrderSide.BUY,
        Decimal(1),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )


def order() -> dict[str, object]:
    return {
        "id": "broker-order",
        "client_order_id": request().client_id,
        "symbol": "SPY",
        "side": "buy",
        "qty": "1",
        "filled_qty": "0",
        "status": "new",
        "type": "market",
        "time_in_force": "day",
        "extended_hours": False,
        "created_at": "2026-09-18T15:00:00Z",
    }


ACCOUNT_UUID = "a67ee0f2-11d4-4bb5-a02f-ef1241497e48"
ACCOUNT_NUMBER = "PA34U6RNDIPQ"


def account(
    *, account_number: object = ACCOUNT_NUMBER, account_id: object = ACCOUNT_UUID
) -> dict[str, object]:
    return {
        "id": account_id,
        "account_number": account_number,
        "status": "ACTIVE",
        "trading_blocked": False,
        "account_blocked": False,
        "buying_power": "1000",
        "currency": "USD",
    }


def broker(replies: list[HttpResponse]) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id=ACCOUNT_NUMBER,
        operational_scope="scope",
        transport=RecordingTransport(replies),
    )


def test_origin_is_hard_bound_and_post_payload_is_one_whole_paper_share() -> None:
    transport = RecordingTransport([response(201, order())])
    broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="scope",
        transport=transport,
    )
    result = broker.submit(request())
    assert broker.origin == PAPER_TRADING_ORIGIN
    assert result.status is SubmissionStatus.ACCEPTED
    assert len(transport.calls) == 1
    method, url, payload = transport.calls[0]
    assert method == "POST"
    assert url == PAPER_TRADING_ORIGIN + "/v2/orders"
    assert json.loads(payload or b"{}") == {
        "client_order_id": request().client_id,
        "extended_hours": False,
        "qty": "1",
        "side": "buy",
        "symbol": "SPY",
        "time_in_force": "day",
        "type": "market",
    }


def test_missing_or_malformed_paper_credentials_fail_closed() -> None:
    with pytest.raises(AlpacaPaperError):
        PaperCredentials.from_environment({})
    with pytest.raises(AlpacaPaperError):
        PaperCredentials(" key", "secret")


@pytest.mark.parametrize("field", ["instrument", "side"])
def test_unsupported_request_is_rejected_before_transport(field: str) -> None:
    transport = RecordingTransport([])
    broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="scope",
        transport=transport,
    )
    unsupported = request()
    if field == "instrument":
        unsupported = SubmitRequest(
            unsupported.account_id,
            unsupported.operational_scope,
            unsupported.client_id,
            Instrument("AAPL"),
            unsupported.side,
            unsupported.quantity,
            unsupported.target,
            unsupported.order_type,
            unsupported.time_in_force,
            unsupported.extended_hours,
        )
    else:
        unsupported = SubmitRequest(
            unsupported.account_id,
            unsupported.operational_scope,
            unsupported.client_id,
            unsupported.instrument,
            OrderSide.SELL,
            unsupported.quantity,
            unsupported.target,
            unsupported.order_type,
            unsupported.time_in_force,
            unsupported.extended_hours,
        )
    with pytest.raises(AlpacaPaperError, match="supported SPY PAPER BUY canary"):
        broker.submit(unsupported)
    assert transport.calls == []


def test_gtc_observation_does_not_broaden_market_day_submission() -> None:
    with pytest.raises(BrokerContractError, match="market/DAY"):
        replace(request(), time_in_force=TimeInForce.GTC)

    transport = RecordingTransport([])
    paper_broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="scope",
        transport=transport,
    )
    invalid_request = request()
    # Exercise the adapter boundary even if an invalid request bypasses its frozen
    # domain constructor.  It must remain a transport-free rejection.
    object.__setattr__(invalid_request, "time_in_force", TimeInForce.GTC)
    with pytest.raises(AlpacaPaperError, match="supported SPY PAPER BUY canary"):
        paper_broker.submit(invalid_request)
    assert transport.calls == []


def test_crypto_history_does_not_grant_submission_authority() -> None:
    transport = RecordingTransport([])
    paper_broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="scope",
        transport=transport,
    )
    with pytest.raises(AlpacaPaperError, match="supported SPY PAPER BUY canary"):
        paper_broker.submit(replace(request(), instrument=Instrument("BTC/USD")))
    assert transport.calls == []


def test_read_only_evidence_contains_sanitized_broker_state() -> None:
    transport = RecordingTransport(
        [
            response(
                200,
                account(),
            ),
            response(
                200,
                {
                    "timestamp": "2026-09-18T15:00:00Z",
                    "is_open": True,
                    "next_close": "2026-09-18T20:00:00Z",
                },
            ),
            response(
                200,
                {"symbol": "SPY", "class": "us_equity", "tradable": True},
            ),
            HttpResponse(200, {"X-Request-ID": "positions-request"}, b"[]"),
            HttpResponse(200, {"X-Request-ID": "orders-request"}, b"[]"),
        ]
    )
    broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id=ACCOUNT_NUMBER,
        operational_scope="scope",
        transport=transport,
    )
    evidence = _read_only(broker, Instrument("SPY"))
    assert evidence["target"] == "paper"
    assert evidence["account"]["eligibility"] == "eligible"  # type: ignore[index]
    assert evidence["account"]["provider_account_id"] == ACCOUNT_UUID  # type: ignore[index]
    assert evidence["clock"]["session_close"] == "2026-09-18T20:00:00+00:00"  # type: ignore[index]
    assert evidence["asset"]["tradable"] == "eligible"  # type: ignore[index]
    assert evidence["snapshot"]["positions"] == []  # type: ignore[index]
    assert evidence["snapshot"]["orders"] == []  # type: ignore[index]
    serialized = json.dumps(evidence)
    assert "secret" not in serialized
    assert "key" not in serialized
    assert "positions-request" in serialized
    assert "orders-request" in serialized


def test_account_number_binding_is_distinct_from_provider_uuid() -> None:
    result = broker([response(200, account())]).read_account()
    assert not isinstance(result, BrokerError)
    assert result.evidence.account_id == ACCOUNT_NUMBER
    assert result.provider_account_id == ACCOUNT_UUID


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (account(account_number="PA34U6RNDIPX"), "PAPER account binding mismatch"),
        (account(account_number=None), "PAPER account identity fields are malformed"),
        (account(account_number=""), "PAPER account number is malformed"),
        (account(account_number=" PA34U6RNDIPQ"), "PAPER account number is malformed"),
        (account(account_id="not-a-uuid"), "PAPER account UUID is malformed"),
        (account(account_id=None), "PAPER account identity fields are malformed"),
    ],
)
def test_invalid_or_mismatched_account_identity_fails_closed(
    payload: dict[str, object], reason: str
) -> None:
    result = broker([response(200, payload)]).read_account()
    assert isinstance(result, BrokerError)
    assert result.category.value == "malformed"
    assert result.reason == reason


DOCUMENTED_ALPACA_ORDER_STATUSES = (
    "accepted",
    "new",
    "pending_new",
    "accepted_for_bidding",
    "partially_filled",
    "filled",
    "done_for_day",
    "canceled",
    "expired",
    "replaced",
    "pending_cancel",
    "pending_replace",
    "rejected",
    "suspended",
    "stopped",
    "calculated",
    "held",
)


@pytest.mark.parametrize("status", DOCUMENTED_ALPACA_ORDER_STATUSES)
def test_documented_alpaca_order_statuses_translate_deterministically(status: str) -> None:
    payload = order()
    payload["status"] = status
    if status == "partially_filled":
        payload["filled_qty"] = "0.5"
    elif status == "filled":
        payload["filled_qty"] = "1"
    result = broker(
        [HttpResponse(200, {}, b"[]"), HttpResponse(200, {}, json.dumps([payload]).encode())]
    ).read_snapshot()
    assert not isinstance(result, BrokerError)
    assert result.orders[0].status is OrderStatus(status)


def test_historical_market_gtc_order_parses_as_observation() -> None:
    payload = order()
    payload.update({"status": "canceled", "time_in_force": "gtc"})
    result = broker(
        [HttpResponse(200, {}, b"[]"), HttpResponse(200, {}, json.dumps([payload]).encode())]
    ).read_snapshot()
    assert not isinstance(result, BrokerError)
    assert result.orders[0].time_in_force is TimeInForce.GTC
    assert result.orders[0].is_terminal


def test_mixed_equity_and_crypto_terminal_history_is_valid_without_inventory() -> None:
    spy_day = order()
    spy_day.update(
        {
            "id": "spy-day",
            "client_order_id": "client-spy-day",
            "status": "filled",
            "filled_qty": "1",
        }
    )
    spy_gtc = order()
    spy_gtc.update(
        {
            "id": "spy-gtc",
            "client_order_id": "client-spy-gtc",
            "status": "canceled",
            "time_in_force": "gtc",
        }
    )
    btc_gtc = order()
    btc_gtc.update(
        {
            "id": "btc-gtc",
            "client_order_id": "client-btc-gtc",
            "symbol": "BTC/USD",
            "status": "expired",
            "time_in_force": "gtc",
        }
    )
    result = broker(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps([spy_day, spy_gtc, btc_gtc]).encode()),
        ]
    ).read_snapshot()
    assert not isinstance(result, BrokerError)
    assert result.positions == ()
    assert tuple(order.instrument.identifier for order in result.terminal_orders) == (
        "BTC/USD",
        "SPY",
        "SPY",
    )
    assert result.outstanding_orders == ()


def test_held_historical_order_does_not_poison_valid_snapshot() -> None:
    payload = order()
    payload["status"] = "held"
    result = broker(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps([payload]).encode()),
        ]
    ).read_snapshot()
    assert not isinstance(result, BrokerError)
    assert result.orders[0].status is OrderStatus.HELD


def test_unknown_order_status_fails_closed_with_specific_diagnostic() -> None:
    payload = order()
    payload["status"] = "future_status"
    result = broker(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps([payload]).encode()),
        ]
    ).read_snapshot()
    assert isinstance(result, BrokerError)
    assert result.reason == "unsupported PAPER order status"


def test_snapshot_accepts_empty_inventory_one_position_and_historical_order() -> None:
    empty = broker([HttpResponse(200, {}, b"[]"), HttpResponse(200, {}, b"[]")]).read_snapshot()
    assert not isinstance(empty, BrokerError)
    position = broker(
        [
            HttpResponse(200, {}, b'[{"symbol":"SPY","qty":"1"}]'),
            HttpResponse(200, {}, b"[]"),
        ]
    ).read_snapshot()
    assert not isinstance(position, BrokerError)
    historical = order()
    historical["status"] = "calculated"
    orders = broker(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps([historical]).encode()),
        ]
    ).read_snapshot()
    assert not isinstance(orders, BrokerError)


@pytest.mark.parametrize(
    ("positions", "orders", "reason"),
    [
        (b"{}", b"[]", "malformed PAPER positions response"),
        (b'[{"symbol":"SPY","qty":"0"}]', b"[]", "malformed PAPER position row"),
        (b"[]", b"{}", "malformed PAPER orders response"),
        (b"[]", b'[{"status":"new"}]', "malformed PAPER order row"),
    ],
)
def test_snapshot_failure_diagnostics_are_specific(
    positions: bytes, orders: bytes, reason: str
) -> None:
    result = broker(
        [HttpResponse(200, {}, positions), HttpResponse(200, {}, orders)]
    ).read_snapshot()
    assert isinstance(result, BrokerError)
    assert result.reason == reason


def test_full_documented_order_page_fails_closed_as_incomplete() -> None:
    payload = order()
    result = broker(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps([payload] * 500).encode()),
        ]
    ).read_snapshot()
    assert isinstance(result, BrokerError)
    assert result.reason == "incomplete PAPER order history/result window"


def test_real_order_update_cannot_prove_precommit_history_but_window_collector_can(
    tmp_path: Path,
) -> None:
    outcome, _ = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    assert outcome.attempt is not None
    attempt = outcome.attempt
    committed = attempt.committed_at
    payload = order()
    payload["client_order_id"] = attempt.client_order_id
    payload["submitted_at"] = (committed + timedelta(seconds=1)).isoformat()
    payload["updated_at"] = (committed + timedelta(seconds=2)).isoformat()
    snapshot = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="paper-scope",
        transport=RecordingTransport(
            [HttpResponse(200, {}, b"[]"), HttpResponse(200, {}, json.dumps([payload]).encode())]
        ),
    ).read_snapshot()
    assert not isinstance(snapshot, BrokerError)
    assert snapshot.history_start > committed
    assert reconcile(attempts=(attempt,), snapshot=snapshot).state is OperationalState.UNRESOLVED

    transport = RecordingTransport(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps([payload]).encode()),
        ]
    )
    result = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="paper-scope",
        transport=transport,
    ).read_reconciliation_snapshot(earliest_attempt=committed)
    assert not isinstance(result, BrokerError)
    assert result.complete and result.history_start == committed
    assert len(result.orders) == 1
    assert reconcile(attempts=(attempt,), snapshot=result).state is OperationalState.ENTRY_PENDING
    assert "after=" in transport.calls[2][1] and "until=" in transport.calls[2][1]


def test_reconciliation_history_full_page_requires_cursor_and_exhaustion() -> None:
    committed = datetime.now(UTC) - timedelta(minutes=2)
    page = []
    for index in range(500):
        row = order()
        row["id"] = f"order-{index}"
        row["client_order_id"] = f"client-{index}"
        row["submitted_at"] = (committed + timedelta(microseconds=index)).isoformat()
        page.append(row)
    transport = RecordingTransport(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps(page).encode()),
            HttpResponse(200, {}, b"[]"),
        ]
    )
    adapter = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id=ACCOUNT_NUMBER,
        operational_scope="scope",
        transport=transport,
    )
    result = adapter.read_reconciliation_snapshot(earliest_attempt=committed)
    assert not isinstance(result, BrokerError)
    assert len(result.orders) == 500
    assert "after_order_id=order-499" in transport.calls[3][1]
    assert "after=" not in transport.calls[3][1]

    capped = broker(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps(page).encode()),
        ]
    ).read_reconciliation_snapshot(earliest_attempt=committed, max_pages=1)
    assert isinstance(capped, BrokerError)
    assert capped.reason == "incomplete PAPER order history/result window"

    duplicate = broker(
        [
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, b"[]"),
            HttpResponse(200, {}, json.dumps([page[0], page[0]]).encode()),
        ]
    ).read_reconciliation_snapshot(earliest_attempt=committed)
    assert isinstance(duplicate, BrokerError)
    assert duplicate.reason == "duplicate PAPER history page row"
