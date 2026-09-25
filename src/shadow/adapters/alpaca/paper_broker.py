# ruff: noqa: E501
"""Alpaca Trading API adapter hard-bound to the PAPER origin.

The transport is deliberately tiny and injectable.  It follows no redirects and
does not retry writes; provider JSON is translated at this boundary.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from shadow.adapters.alpaca.crypto_activities import collect_activity_rows, translate_activities
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
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoActivityEvidence
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


_PROVIDER_MESSAGE_LIMIT = 180
_PROVIDER_BODY_LIMIT = 4096
_STATIC_PAPER_ROUTES = frozenset(
    {
        "/v2/account",
        "/v2/account/activities",
        "/v2/clock",
        "/v2/orders",
        "/v2/orders:by_client_order_id",
        "/v2/positions",
    }
)
_UUID_TEXT = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
# Mixed identifiers long enough to be account numbers, without eating numeric error codes.
_MIXED_IDENTIFIER = re.compile(r"\b(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{12,}\b")


def _endpoint_class(method: str, path: str) -> str:
    """Method plus a stable route. Query strings and raw ids are not evidence."""
    verb = method if method in {"GET", "POST", "PATCH", "DELETE"} else "HTTP"
    route = path.split("?", 1)[0]
    if route not in _STATIC_PAPER_ROUTES:
        if route.startswith("/v2/assets/"):
            route = "/v2/assets/{symbol}"
        elif route.startswith("/v2/positions/"):
            route = "/v2/positions/{symbol}"
        elif route.startswith("/v2/orders/"):
            route = "/v2/orders/{order_id}"
        else:
            route = "/v2/unclassified"
    return f"{verb} {route}"


def _provider_code(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if text.isascii() and text.isdigit() and 1 <= len(text) <= 12:
        return text
    return None


def _provider_message(value: object, *, forbidden: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > _PROVIDER_MESSAGE_LIMIT:
        return None
    if any(ord(char) < 32 or ord(char) > 126 for char in text):
        return None
    lowered = text.casefold()
    if "apca-api-" in lowered or "authorization" in lowered or "bearer " in lowered:
        return None
    if any(secret and secret in text for secret in forbidden):
        return None
    redacted = _UUID_TEXT.sub("[redacted]", text)
    redacted = _LONG_HEX.sub("[redacted]", redacted)
    redacted = _MIXED_IDENTIFIER.sub("[redacted]", redacted)
    redacted = " ".join(redacted.split())
    return redacted or None


def _provider_rejection_detail(
    *,
    status: int,
    method: str,
    path: str,
    body: bytes,
    forbidden: tuple[str, ...],
) -> str:
    """HTTP status, route class, and optional Alpaca `code`/`message` only.

    Extra JSON fields, headers, and non-JSON bodies are discarded. Parsing
    failure leaves the status and route, which is still a complete diagnostic.
    """
    code: str | None = None
    message: str | None = None
    if len(body) <= _PROVIDER_BODY_LIMIT:
        try:
            payload: object = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            code = _provider_code(payload.get("code"))
            message = _provider_message(payload.get("message"), forbidden=forbidden)
    parts = [f"HTTP {status}", _endpoint_class(method, path)]
    if code is not None:
        parts.append(f"code={code}")
    if message is not None:
        parts.append(f"message={message}")
    return " ".join(parts)


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

    def _sensitive_fragments(self) -> tuple[str, ...]:
        return (self._credentials.key_id, self._credentials.secret_key, self._account_id)

    def _error(
        self,
        response: HttpResponse,
        received: datetime,
        operation: str,
        method: str,
        path: str,
    ) -> BrokerError:
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
            self._evidence(reference, received, received),
            category,
            _provider_rejection_detail(
                status=response.status,
                method=method,
                path=path,
                body=response.body,
                forbidden=self._sensitive_fragments(),
            ),
        )

    @staticmethod
    def _instrument(payload: dict[str, object]) -> Instrument:
        symbol = payload["symbol"]
        if payload.get("asset_class") == "crypto" and symbol in ("BTCUSD", "BTC/USD"):
            return Instrument("BTC/USD")
        return Instrument(str(symbol))

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
            instrument = self._instrument(payload)
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
        return self._read_account(btc=False)

    def read_btc_account(self) -> BtcCashAccount | BrokerError:
        result = self._read_account(btc=True)
        assert isinstance(result, (BtcCashAccount, BrokerError))
        return result

    def _read_account(self, *, btc: bool) -> BrokerAccount | BrokerError:
        response, received = self._call("GET", "/v2/account")
        if response.status != 200:
            return self._error(response, received, "account", "GET", "/v2/account")
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
            if btc:
                cash = min(
                    _decimal(payload["cash"]), _decimal(payload["non_marginable_buying_power"])
                )
                crypto_eligible = (
                    payload.get("crypto_status") == "ACTIVE"
                    and payload.get("trading_blocked") is False
                    and payload.get("account_blocked") is False
                )
                return BtcCashAccount(
                    self._evidence(
                        _request_id(response.headers) or "alpaca:btc-account", received, received
                    ),
                    OrderTarget.PAPER,
                    eligible,
                    cash,
                    str(payload.get("currency", "")),
                    provider_account_id,
                    available_cash=cash,
                    crypto_trading=Eligibility.ELIGIBLE
                    if crypto_eligible
                    else Eligibility.INELIGIBLE,
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
        except (KeyError, ValueError, AlpacaPaperError) as exc:
            return BrokerError(
                self._evidence("alpaca:account:malformed", received, received),
                ErrorCategory.MALFORMED,
                "malformed PAPER account response" if isinstance(exc, KeyError) else str(exc),
            )

    def read_clock(self) -> BrokerClock | BrokerError:
        response, received = self._call("GET", "/v2/clock")
        if response.status != 200:
            return self._error(response, received, "clock", "GET", "/v2/clock")
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
            return self._error(
                response,
                received,
                "asset",
                "GET",
                "/v2/assets/" + urllib.parse.quote(instrument.identifier, safe=""),
            )
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

    def read_btc_asset(self) -> BtcBrokerAsset | BrokerError:
        response, received = self._call("GET", "/v2/assets/BTC%2FUSD")
        if response.status != 200:
            return self._error(response, received, "btc-asset", "GET", "/v2/assets/BTC%2FUSD")
        try:
            payload = _object(response.body)
            if payload.get("symbol") != "BTC/USD" or payload.get("class") != "crypto":
                raise AlpacaPaperError("BTC crypto asset required")
            if type(payload.get("fractionable")) is not bool:
                raise AlpacaPaperError("fractionable evidence required")
            return BtcBrokerAsset(
                self._evidence(
                    _request_id(response.headers) or "alpaca:btc-asset", received, received
                ),
                Instrument("BTC/USD"),
                Eligibility.INELIGIBLE,
                Eligibility.ELIGIBLE
                if payload.get("tradable") is True and payload.get("status") == "active"
                else Eligibility.INELIGIBLE,
                _decimal(payload["min_order_size"], positive=True),
                _decimal(payload["min_trade_increment"], positive=True),
                _decimal(payload["price_increment"], positive=True),
                payload["fractionable"] is True,
            )
        except (KeyError, ValueError, AlpacaPaperError):
            return BrokerError(
                self._evidence("alpaca:btc-asset:malformed", received, received),
                ErrorCategory.MALFORMED,
                "malformed current BTC asset constraints",
            )

    def read_btc_available(self) -> BrokerPosition | BrokerError:
        """Explicit sellable BTC, never substituted from gross filled quantity."""
        response, received = self._call("GET", "/v2/positions/BTCUSD")
        if response.status != 200:
            return self._error(response, received, "btc_available", "GET", "/v2/positions/BTCUSD")
        evidence = self._evidence("alpaca:btc_available", received, received)
        try:
            row = _object(response.body)
            if self._instrument(row) != Instrument("BTC/USD"):
                raise AlpacaPaperError("unexpected available-position instrument")
            available = _decimal(row["qty_available"], positive=True)
            if available > _decimal(row["qty"], positive=True):
                raise AlpacaPaperError("available BTC exceeds position")
            return BrokerPosition(evidence, Instrument("BTC/USD"), available)
        except (KeyError, ValueError, TypeError, AlpacaPaperError):
            return BrokerError(evidence, ErrorCategory.MALFORMED, "BTC availability unavailable")

    def read_snapshot(self) -> BrokerSnapshot | BrokerError:
        positions_response, received = self._call("GET", "/v2/positions")
        if positions_response.status != 200:
            return self._error(positions_response, received, "positions", "GET", "/v2/positions")
        orders_response, orders_received = self._call(
            "GET", "/v2/orders?status=all&limit=500&nested=false"
        )
        if orders_response.status != 200:
            return self._error(
                orders_response,
                orders_received,
                "orders",
                "GET",
                "/v2/orders?status=all&limit=500&nested=false",
            )
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
                            self._instrument(row),
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
            # A returned row's updated_at is not the start of a complete
            # history query.  This unbounded read establishes only current
            # inventory; prior journal coverage requires the explicit collector.
            history_start = orders_received
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

    def read_reconciliation_snapshot(
        self,
        *,
        earliest_attempt: datetime,
        max_pages: int = 10,
        history_end: datetime | None = None,
    ) -> BrokerSnapshot | BrokerError:
        """Collect a bounded, explicit submission-history window for P5A.3.

        The first page uses Alpaca's exclusive time bounds. Later pages use its
        exclusive order-ID cursor, which cannot be combined with time bounds.
        Every page is checked against the fixed cut before coverage is asserted.
        This read is evidence only; it does not create an atomic broker cut.
        """
        if earliest_attempt.tzinfo is None or earliest_attempt.utcoffset() is None:
            raise AlpacaPaperError("aware earliest attempt required")
        if isinstance(max_pages, bool) or not 1 <= max_pages <= 100:
            raise AlpacaPaperError("bounded positive page limit required")
        start = earliest_attempt.astimezone(UTC)
        positions_response, cut = self._call("GET", "/v2/positions")
        if history_end is not None:
            if history_end.tzinfo is None or not start <= history_end <= cut:
                raise AlpacaPaperError("invalid requested reconciliation cut")
            cut = history_end
        if positions_response.status != 200:
            return self._error(positions_response, cut, "positions", "GET", "/v2/positions")
        if start > cut:
            return BrokerError(
                self._evidence("alpaca:history:future", cut, cut),
                ErrorCategory.MALFORMED,
                "earliest attempt is after broker cut",
            )
        try:
            positions_payload = json.loads(positions_response.body.decode("utf-8"))
            if not isinstance(positions_payload, list):
                raise AlpacaPaperError("malformed PAPER positions response")
            positions: list[BrokerPosition] = []
            position_ref = _request_id(positions_response.headers) or "unknown"
            for row in positions_payload:
                if not isinstance(row, dict):
                    raise AlpacaPaperError("malformed PAPER position row")
                positions.append(
                    BrokerPosition(
                        self._evidence(position_ref, cut, cut),
                        self._instrument(row),
                        _decimal(row["qty"], positive=True),
                    )
                )

            # Open orders predating the journal window remain operationally
            # relevant.  A full open page cannot prove their complete inventory.
            open_response, open_received = self._call(
                "GET", "/v2/orders?status=open&limit=500&nested=false"
            )
            if open_response.status != 200:
                return self._error(
                    open_response,
                    open_received,
                    "open-orders",
                    "GET",
                    "/v2/orders?status=open&limit=500&nested=false",
                )
            open_payload = json.loads(open_response.body.decode("utf-8"))
            if not isinstance(open_payload, list) or len(open_payload) >= 500:
                raise AlpacaPaperError("incomplete PAPER open-order inventory")
            rows: dict[str, dict[str, object]] = {}
            for row in open_payload:
                if not isinstance(row, dict):
                    raise AlpacaPaperError("malformed PAPER order row")
                order_id = str(row["id"])
                if order_id in rows:
                    raise AlpacaPaperError("duplicate PAPER open-order row")
                rows[order_id] = row

            cursor: str | None = None
            last_submitted: datetime | None = None
            history_ids: set[str] = set()
            exhausted = False
            received = open_received
            for _ in range(max_pages):
                if cursor is None:
                    # Alpaca's `after` is exclusive; subtract one microsecond so
                    # an order submitted exactly at commit is in the window.
                    after = (start - timedelta(microseconds=1)).isoformat()
                    until = (cut + timedelta(microseconds=1)).isoformat()
                    query = urllib.parse.urlencode(
                        {
                            "status": "all",
                            "limit": 500,
                            "direction": "asc",
                            "after": after,
                            "until": until,
                            "nested": "false",
                        }
                    )
                else:
                    query = urllib.parse.urlencode(
                        {
                            "status": "all",
                            "limit": 500,
                            "direction": "asc",
                            "after_order_id": cursor,
                            "nested": "false",
                        }
                    )
                page_response, received = self._call("GET", "/v2/orders?" + query)
                if page_response.status != 200:
                    return self._error(
                        page_response, received, "history", "GET", "/v2/orders?" + query
                    )
                page = json.loads(page_response.body.decode("utf-8"))
                if not isinstance(page, list) or len(page) > 500:
                    raise AlpacaPaperError("malformed PAPER order history page")
                crossed_cut = False
                for row in page:
                    if not isinstance(row, dict):
                        raise AlpacaPaperError("malformed PAPER order row")
                    submitted = _timestamp(row.get("submitted_at"))
                    if last_submitted is not None and submitted < last_submitted:
                        raise AlpacaPaperError("nonmonotonic PAPER order pagination")
                    last_submitted = submitted
                    if submitted > cut:
                        crossed_cut = True
                        break
                    if submitted < start:
                        raise AlpacaPaperError("PAPER history escaped requested window")
                    order_id = str(row["id"])
                    if order_id in history_ids:
                        raise AlpacaPaperError("duplicate PAPER history page row")
                    history_ids.add(order_id)
                    prior = rows.get(order_id)
                    if prior is not None and prior != row:
                        raise AlpacaPaperError("contradictory PAPER order pages")
                    rows[order_id] = row
                if crossed_cut or len(page) < 500:
                    exhausted = True
                    break
                next_cursor = str(page[-1]["id"])
                if next_cursor == cursor:
                    raise AlpacaPaperError("stalled PAPER order pagination")
                cursor = next_cursor
            if not exhausted:
                raise AlpacaPaperError("incomplete PAPER order history/result window")
            reference = f"positions:{position_ref}|history:{_request_id(page_response.headers) or 'unknown'}"
            evidence = self._evidence(reference, received, received)
            normalized_positions = tuple(
                BrokerPosition(evidence, row.instrument, row.quantity) for row in positions
            )
            orders = tuple(self._order(row, received, reference) for row in rows.values())
            return BrokerSnapshot(evidence, normalized_positions, orders, True, True, start, cut)
        except (
            KeyError,
            ValueError,
            AlpacaPaperError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            return BrokerError(
                self._evidence("alpaca:history:malformed", datetime.now(UTC), datetime.now(UTC)),
                ErrorCategory.MALFORMED,
                str(exc),
            )

    def read_btc_activities(
        self,
        *,
        history_start: datetime,
        history_end: datetime,
        max_pages: int = 20,
    ) -> CryptoActivityEvidence | BrokerError:
        """Sequential account-bound GETs; exhaustion is not historical finality."""
        account = self.read_btc_account()
        if isinstance(account, BrokerError):
            return account

        def read(path: str) -> tuple[bytes, datetime]:
            response, received = self._call("GET", path)
            if response.status != 200:
                raise AlpacaPaperError(
                    _provider_rejection_detail(
                        status=response.status,
                        method="GET",
                        path=path,
                        body=response.body,
                        forbidden=self._sensitive_fragments(),
                    )
                )
            return response.body, received

        try:
            rows, received = collect_activity_rows(
                read,
                history_start=history_start,
                history_end=history_end,
                max_pages=max_pages,
            )
            return translate_activities(
                rows,
                evidence=self._evidence("alpaca:activities", received, received),
                history_start=history_start,
                history_end=history_end,
            )
        except (ValueError, TypeError, KeyError, OSError, AlpacaPaperError) as exc:
            now = datetime.now(UTC)
            return BrokerError(
                self._evidence("alpaca:activities:unusable", now, now),
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
            return self._error(response, received, "lookup", "GET", path)
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

    def submit(
        self, request: SubmitRequest, *, before_post: Callable[[], None] | None = None
    ) -> SubmissionResult:
        common = (
            request.account_id == self._account_id
            and request.operational_scope == self._scope
            and request.target is OrderTarget.PAPER
            and request.order_type is OrderType.MARKET
            and request.extended_hours is False
        )
        if isinstance(request, BtcSubmitRequest):
            request.__post_init__()
            if not common:
                raise AlpacaPaperError("BTC request binding mismatch")
            supported = (
                request.instrument == Instrument("BTC/USD")
                and request.time_in_force is TimeInForce.GTC
            )
            asset = self.read_btc_asset()
            if not isinstance(asset, BtcBrokerAsset) or not asset.accepts_quantity(
                request.quantity
            ):
                raise AlpacaPaperError("BTC quantity lacks valid current asset evidence")
        else:
            supported = (
                request.instrument == Instrument("SPY")
                and request.side is OrderSide.BUY
                and request.quantity == Decimal(1)
                and request.time_in_force is TimeInForce.DAY
            )
        if not common or not supported:
            raise AlpacaPaperError("request is outside the supported SPY PAPER BUY canary")
        payload = {
            "symbol": request.instrument.identifier,
            "qty": str(request.quantity) if isinstance(request, BtcSubmitRequest) else "1",
            "side": request.side.value,
            "type": "market",
            "time_in_force": request.time_in_force.value,
            "extended_hours": False,
            "client_order_id": request.client_id,
        }
        if before_post is not None:
            before_post()
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
            except (AlpacaPaperError, ValueError, TypeError, KeyError):
                return SubmissionResult(
                    evidence,
                    request,
                    SubmissionStatus.UNCERTAIN,
                    None,
                    BrokerError(evidence, ErrorCategory.MALFORMED, "malformed POST response"),
                )
        detail = _provider_rejection_detail(
            status=response.status,
            method="POST",
            path="/v2/orders",
            body=response.body,
            forbidden=self._sensitive_fragments(),
        )
        if 400 <= response.status < 500:
            return SubmissionResult(
                evidence,
                request,
                SubmissionStatus.REJECTED,
                None,
                BrokerError(evidence, ErrorCategory.DEFINITIVE_REJECTION, detail),
            )
        return SubmissionResult(
            evidence,
            request,
            SubmissionStatus.UNCERTAIN,
            None,
            BrokerError(evidence, ErrorCategory.UNAVAILABLE, detail),
        )
