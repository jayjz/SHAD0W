# ruff: noqa: E501
"""Alpaca Trading API adapter hard-bound to the PAPER origin.

The transport is deliberately tiny and injectable.  It follows no redirects and
does not retry writes; provider JSON is translated at this boundary.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from shadow.domain import Instrument
from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Eligibility,
    ErrorCategory,
    Evidence,
    OrderStatus,
    SessionState,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
    TradeUpdate,
)
from shadow.risk.models import OrderSide, OrderTarget, OrderType, TimeInForce

PAPER_TRADING_ORIGIN = "https://paper-api.alpaca.markets"


class AlpacaPaperError(RuntimeError):
    """The PAPER adapter cannot establish typed, safe evidence."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class UrllibTransport:
    """Standard-library transport with redirects and implicit retries disabled."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                return HttpResponse(
                    response.status, dict(response.headers.items()), response.read()
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, dict(error.headers.items()), error.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AlpacaPaperError("PAPER transport outcome is uncertain") from exc


@dataclass(frozen=True, slots=True)
class PaperCredentials:
    key_id: str
    secret_key: str

    def __post_init__(self) -> None:
        for value in (self.key_id, self.secret_key):
            if not isinstance(value, str) or not value or value != value.strip():
                raise AlpacaPaperError("PAPER credentials must be nonempty trimmed values")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> PaperCredentials:
        source = os.environ if environment is None else environment
        # Trading credentials are intentionally distinct from ALPACA_DATA_* credentials.
        try:
            return cls(source["ALPACA_PAPER_API_KEY_ID"], source["ALPACA_PAPER_API_SECRET_KEY"])
        except KeyError as exc:
            raise AlpacaPaperError("missing Alpaca PAPER trading credentials") from exc


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AlpacaPaperError("provider timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlpacaPaperError("provider timestamp is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AlpacaPaperError("provider timestamp is naive")
    return parsed.astimezone(UTC)


def _decimal(value: object, *, positive: bool = False) -> Decimal:
    if not isinstance(value, (str, int, float)):
        raise AlpacaPaperError("provider decimal is missing")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AlpacaPaperError("provider decimal is malformed") from exc
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise AlpacaPaperError("provider decimal is invalid")
    return result


def _object(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlpacaPaperError("provider response is not JSON") from exc
    if not isinstance(value, dict):
        raise AlpacaPaperError("provider response must be an object")
    return value


def _request_id(headers: Mapping[str, str]) -> str | None:
    for key, value in headers.items():
        if key.lower() == "x-request-id" and isinstance(value, str) and value.strip():
            return value.strip()
    return None


class AlpacaPaperBroker:
    """The only production trading adapter; its origin is a non-configurable constant."""

    def __init__(
        self,
        *,
        credentials: PaperCredentials,
        account_id: str,
        operational_scope: str,
        transport: HttpTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(credentials, PaperCredentials):
            raise AlpacaPaperError("PAPER credentials are required")
        if not isinstance(account_id, str) or not account_id or account_id != account_id.strip():
            raise AlpacaPaperError("configured account binding is required")
        if (
            not isinstance(operational_scope, str)
            or not operational_scope
            or operational_scope != operational_scope.strip()
        ):
            raise AlpacaPaperError("operational scope is required")
        if timeout_seconds <= 0:
            raise AlpacaPaperError("positive transport timeout required")
        self._credentials = credentials
        # The stable SHAD0W binding is Alpaca's human-facing account_number,
        # never its separate UUID-shaped `id` field.
        self._account_id = account_id
        self._scope = operational_scope
        self._transport = UrllibTransport() if transport is None else transport
        self._timeout = timeout_seconds

    @property
    def origin(self) -> str:
        return PAPER_TRADING_ORIGIN

    def _evidence(self, reference: str, observed: datetime, received: datetime) -> Evidence:
        return Evidence(reference, self._account_id, self._scope, observed, received)

    def _call(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> tuple[HttpResponse, datetime]:
        if not path.startswith("/") or "//" in path:
            raise AlpacaPaperError("invalid fixed PAPER API path")
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "APCA-API-KEY-ID": self._credentials.key_id,
            "APCA-API-SECRET-KEY": self._credentials.secret_key,
            "Accept": "application/json",
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        response = self._transport.request(
            method=method,
            url=PAPER_TRADING_ORIGIN + path,
            headers=headers,
            body=encoded,
            timeout=self._timeout,
        )
        return response, datetime.now(UTC)

    def _error(self, response: HttpResponse, received: datetime, operation: str) -> BrokerError:
        reference = _request_id(response.headers) or f"alpaca:{operation}:{response.status}"
        category = (
            ErrorCategory.NOT_FOUND
            if response.status == 404
            else (
                ErrorCategory.ACCESS_DENIED
                if response.status in (401, 403)
                else (
                    ErrorCategory.RATE_LIMITED
                    if response.status == 429
                    else ErrorCategory.UNAVAILABLE
                )
            )
        )
        return BrokerError(
            self._evidence(reference, received, received), category, f"HTTP {response.status}"
        )

    def _order(self, payload: dict[str, object], received: datetime, reference: str) -> BrokerOrder:
        try:
            raw_status = payload["status"]
            if not isinstance(raw_status, str):
                raise AlpacaPaperError("unsupported PAPER order status")
            try:
                status = OrderStatus(raw_status)
            except ValueError as exc:
                raise AlpacaPaperError("unsupported PAPER order status") from exc
            side = OrderSide(str(payload["side"]))
            order_type = OrderType(str(payload.get("type", payload.get("order_type"))))
            tif = TimeInForce(str(payload["time_in_force"]))
            instrument = Instrument(str(payload["symbol"]))
            order_id = str(payload["id"])
            client = payload.get("client_order_id")
            client_id = None if client is None else str(client)
            extended = payload.get("extended_hours")
            if type(extended) is not bool:
                raise AlpacaPaperError("provider extended-hours flag is malformed")
            observed = _timestamp(
                payload.get("updated_at")
                or payload.get("submitted_at")
                or payload.get("created_at")
            )
            return BrokerOrder(
                self._evidence(reference, observed, received),
                order_id,
                client_id,
                instrument,
                side,
                _decimal(payload["qty"], positive=True),
                _decimal(payload.get("filled_qty", "0")),
                status,
                order_type,
                tif,
                extended,
                None if payload.get("replaces") is None else str(payload["replaces"]),
                None if payload.get("replaced_by") is None else str(payload["replaced_by"]),
            )
        except (KeyError, ValueError, AlpacaPaperError) as exc:
            if isinstance(exc, AlpacaPaperError) and str(exc) == "unsupported PAPER order status":
                raise
            raise AlpacaPaperError("malformed PAPER order row") from exc

    def read_account(self) -> BrokerAccount | BrokerError:
        response, received = self._call("GET", "/v2/account")
        if response.status != 200:
            return self._error(response, received, "account")
        try:
            payload = _object(response.body)
            provider_account_id = payload["id"]
            account_number = payload["account_number"]
            if not isinstance(provider_account_id, str) or not isinstance(account_number, str):
                raise AlpacaPaperError("PAPER account identity fields are malformed")
            if not provider_account_id or provider_account_id != provider_account_id.strip():
                raise AlpacaPaperError("PAPER account UUID is malformed")
            try:
                uuid.UUID(provider_account_id)
            except ValueError as exc:
                raise AlpacaPaperError("PAPER account UUID is malformed") from exc
            if not account_number or account_number != account_number.strip():
                raise AlpacaPaperError("PAPER account number is malformed")
            if account_number != self._account_id:
                raise AlpacaPaperError("PAPER account binding mismatch")
            blocked = (
                payload.get("trading_blocked") is True or payload.get("account_blocked") is True
            )
            status = str(payload.get("status", "")).lower()
            eligible = (
                Eligibility.ELIGIBLE
                if not blocked and status in {"active", "approved", "paper_only"}
                else Eligibility.INELIGIBLE
            )
            return BrokerAccount(
                self._evidence(
                    _request_id(response.headers) or "alpaca:account", received, received
                ),
                OrderTarget.PAPER,
                eligible,
                _decimal(payload["buying_power"]),
                str(payload.get("currency", "USD")),
                provider_account_id,
            )
        except (KeyError, AlpacaPaperError) as exc:
            return BrokerError(
                self._evidence("alpaca:account:malformed", received, received),
                ErrorCategory.MALFORMED,
                "malformed PAPER account response" if isinstance(exc, KeyError) else str(exc),
            )

    def read_clock(self) -> BrokerClock | BrokerError:
        response, received = self._call("GET", "/v2/clock")
        if response.status != 200:
            return self._error(response, received, "clock")
        try:
            payload = _object(response.body)
            observed = _timestamp(payload["timestamp"])
            is_open = payload.get("is_open")
            if type(is_open) is not bool:
                raise AlpacaPaperError("clock state is malformed")
            if not is_open:
                return BrokerClock(
                    self._evidence(
                        _request_id(response.headers) or "alpaca:clock", observed, received
                    ),
                    SessionState.CLOSED,
                    observed.date(),
                    None,
                    None,
                )
            close = _timestamp(payload["next_close"])
            # The API provides the current timestamp and next close, not today's open.
            # A conservative lower bound is sufficient for regular-session membership;
            # close guard is checked against the provider's actual next_close.
            return BrokerClock(
                self._evidence(_request_id(response.headers) or "alpaca:clock", observed, received),
                SessionState.REGULAR,
                observed.date(),
                observed,
                close,
            )
        except (KeyError, AlpacaPaperError):
            return BrokerError(
                self._evidence("alpaca:clock:malformed", received, received),
                ErrorCategory.MALFORMED,
                "malformed PAPER clock response",
            )

    def read_asset(self, instrument: Instrument) -> BrokerAsset | BrokerError:
        response, received = self._call(
            "GET", "/v2/assets/" + urllib.parse.quote(instrument.identifier, safe="")
        )
        if response.status != 200:
            return self._error(response, received, "asset")
        try:
            payload = _object(response.body)
            if Instrument(str(payload["symbol"])) != instrument:
                raise AlpacaPaperError("asset symbol mismatch")
            reference = _request_id(response.headers) or "alpaca:asset"
            evidence = self._evidence(reference, received, received)
            return BrokerAsset(
                evidence,
                instrument,
                Eligibility.ELIGIBLE
                if payload.get("class") == "us_equity"
                else Eligibility.INELIGIBLE,
                Eligibility.ELIGIBLE if payload.get("tradable") is True else Eligibility.INELIGIBLE,
            )
        except (KeyError, ValueError, AlpacaPaperError):
            return BrokerError(
                self._evidence("alpaca:asset:malformed", received, received),
                ErrorCategory.MALFORMED,
                "malformed PAPER asset response",
            )

    def read_snapshot(self) -> BrokerSnapshot | BrokerError:
        positions_response, received = self._call("GET", "/v2/positions")
        if positions_response.status != 200:
            return self._error(positions_response, received, "positions")
        orders_response, orders_received = self._call(
            "GET", "/v2/orders?status=all&limit=500&nested=false"
        )
        if orders_response.status != 200:
            return self._error(orders_response, orders_received, "orders")
        try:
            try:
                positions_payload = json.loads(positions_response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AlpacaPaperError("malformed PAPER positions response") from exc
            if not isinstance(positions_payload, list):
                raise AlpacaPaperError("malformed PAPER positions response")
            try:
                orders_payload = json.loads(orders_response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AlpacaPaperError("malformed PAPER orders response") from exc
            if not isinstance(orders_payload, list):
                raise AlpacaPaperError("malformed PAPER orders response")
            # Alpaca documents 500 as the response maximum.  This bounded canary
            # does not paginate, so a full page cannot establish complete history.
            if len(orders_payload) >= 500:
                raise AlpacaPaperError("incomplete PAPER order history/result window")
            position_request = _request_id(positions_response.headers) or "unknown"
            order_request = _request_id(orders_response.headers) or "unknown"
            reference = f"positions:{position_request}|orders:{order_request}"
            evidence = self._evidence(reference, orders_received, orders_received)
            positions: list[BrokerPosition] = []
            for row in positions_payload:
                if not isinstance(row, dict):
                    raise AlpacaPaperError("malformed PAPER position row")
                try:
                    positions.append(
                        BrokerPosition(
                            evidence,
                            Instrument(str(row["symbol"])),
                            _decimal(row["qty"], positive=True),
                        )
                    )
                except (KeyError, ValueError, AlpacaPaperError) as exc:
                    raise AlpacaPaperError("malformed PAPER position row") from exc
            orders: list[BrokerOrder] = []
            for row in orders_payload:
                if not isinstance(row, dict):
                    raise AlpacaPaperError("malformed PAPER order row")
                orders.append(self._order(row, orders_received, reference))
            history_start = min(
                (order.evidence.observation_time for order in orders), default=orders_received
            )
            return BrokerSnapshot(
                evidence,
                tuple(positions),
                tuple(orders),
                True,
                True,
                history_start,
                orders_received,
            )
        except AlpacaPaperError as exc:
            return BrokerError(
                self._evidence("alpaca:snapshot:malformed", orders_received, orders_received),
                ErrorCategory.MALFORMED,
                str(exc),
            )

    def lookup_order(
        self, *, order_id: str | None, client_id: str | None
    ) -> BrokerOrder | BrokerError:
        if (order_id is None) == (client_id is None):
            raise AlpacaPaperError("exactly one order identity is required")
        path = (
            "/v2/orders/" + urllib.parse.quote(order_id, safe="")
            if order_id is not None
            else "/v2/orders:by_client_order_id?client_order_id="
            + urllib.parse.quote(client_id or "", safe="")
        )
        response, received = self._call("GET", path)
        if response.status != 200:
            return self._error(response, received, "lookup")
        try:
            return self._order(
                _object(response.body), received, _request_id(response.headers) or "alpaca:lookup"
            )
        except AlpacaPaperError:
            return BrokerError(
                self._evidence("alpaca:lookup:malformed", received, received),
                ErrorCategory.MALFORMED,
                "malformed PAPER order response",
            )

    def read_updates(self) -> tuple[TradeUpdate, ...] | BrokerError:
        return BrokerError(
            self._evidence("alpaca:updates:not-connected", datetime.now(UTC), datetime.now(UTC)),
            ErrorCategory.UNAVAILABLE,
            "trade-update stream is not part of one-shot canary",
        )

    def submit(self, request: SubmitRequest) -> SubmissionResult:
        if (
            request.account_id != self._account_id
            or request.operational_scope != self._scope
            or request.target is not OrderTarget.PAPER
            or request.instrument != Instrument("SPY")
            or request.side is not OrderSide.BUY
            or request.quantity != Decimal(1)
            or request.order_type is not OrderType.MARKET
            or request.time_in_force is not TimeInForce.DAY
            or request.extended_hours is not False
        ):
            raise AlpacaPaperError("request is outside the supported SPY PAPER BUY canary")
        payload = {
            "symbol": request.instrument.identifier,
            "qty": "1",
            "side": request.side.value,
            "type": "market",
            "time_in_force": "day",
            "extended_hours": False,
            "client_order_id": request.client_id,
        }
        try:
            response, received = self._call("POST", "/v2/orders", payload)
        except AlpacaPaperError as exc:
            evidence = self._evidence(
                "alpaca:submit:uncertain", datetime.now(UTC), datetime.now(UTC)
            )
            return SubmissionResult(
                evidence,
                request,
                SubmissionStatus.UNCERTAIN,
                None,
                BrokerError(evidence, ErrorCategory.UNAVAILABLE, str(exc)),
            )
        reference = _request_id(response.headers) or f"alpaca:submit:{response.status}"
        evidence = self._evidence(reference, received, received)
        if response.status in (200, 201):
            try:
                return SubmissionResult(
                    evidence,
                    request,
                    SubmissionStatus.ACCEPTED,
                    self._order(_object(response.body), received, reference),
                    None,
                )
            except AlpacaPaperError:
                return SubmissionResult(
                    evidence,
                    request,
                    SubmissionStatus.UNCERTAIN,
                    None,
                    BrokerError(evidence, ErrorCategory.MALFORMED, "malformed POST response"),
                )
        if 400 <= response.status < 500:
            return SubmissionResult(
                evidence,
                request,
                SubmissionStatus.REJECTED,
                None,
                BrokerError(
                    evidence, ErrorCategory.DEFINITIVE_REJECTION, f"HTTP {response.status}"
                ),
            )
        return SubmissionResult(
            evidence,
            request,
            SubmissionStatus.UNCERTAIN,
            None,
            BrokerError(evidence, ErrorCategory.UNAVAILABLE, f"HTTP {response.status}"),
        )
