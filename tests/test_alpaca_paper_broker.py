"""Offline conformance checks for the fixed-origin Alpaca PAPER adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

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
from shadow.execution.broker import SubmissionStatus, SubmitRequest
from shadow.risk.models import OrderSide, OrderTarget, OrderType, TimeInForce


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


def test_read_only_evidence_contains_sanitized_broker_state() -> None:
    transport = RecordingTransport(
        [
            response(
                200,
                {
                    "id": "paper-account",
                    "status": "ACTIVE",
                    "trading_blocked": False,
                    "account_blocked": False,
                    "buying_power": "1000",
                    "currency": "USD",
                },
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
        account_id="paper-account",
        operational_scope="scope",
        transport=transport,
    )
    evidence = _read_only(broker, Instrument("SPY"))
    assert evidence["target"] == "paper"
    assert evidence["account"]["eligibility"] == "eligible"  # type: ignore[index]
    assert evidence["clock"]["session_close"] == "2026-09-18T20:00:00+00:00"  # type: ignore[index]
    assert evidence["asset"]["tradable"] == "eligible"  # type: ignore[index]
    assert evidence["snapshot"]["positions"] == []  # type: ignore[index]
    assert evidence["snapshot"]["orders"] == []  # type: ignore[index]
    serialized = json.dumps(evidence)
    assert "secret" not in serialized
    assert "positions-request" in serialized
    assert "orders-request" in serialized
