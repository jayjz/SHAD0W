"""Documented crypto/us historical *trades*, never provider bars."""

import json
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from shadow.adapters.alpaca.crypto_normalize import normalize
from shadow.adapters.alpaca.crypto_stream import CryptoDataCredentials
from shadow.adapters.alpaca.paper_broker import HttpTransport
from shadow.domain.crypto_market import CryptoTrade, UtcNanoseconds
from shadow.domain.market import Provenance


def timestamp_ns(value: int) -> str:
    seconds, fraction = divmod(value, 1_000_000_000)
    stamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:09d}Z"


def historical_trades(
    *,
    transport: HttpTransport,
    credentials: CryptoDataCredentials,
    start_ns: int,
    end_ns: int,
    now_ns: Callable[[], int],
    remaining_seconds: Callable[[], float],
    max_pages: int = 1000,
) -> Iterator[tuple[CryptoTrade, ...]]:
    """Bounded ascending pagination; exhaustion is a query fact, not fee authority.

    Each page retains its actual retrieval time. No historical original receipt
    time is asserted. Inclusive API endpoints are mapped to [start,end).
    """
    if start_ns >= end_ns or end_ns > now_ns() or max_pages <= 0:
        raise ValueError("invalid historical range/budget")
    token: str | None = None
    seen: set[str] = set()
    last: CryptoTrade | None = None
    for _ in range(max_pages):
        remaining = remaining_seconds()
        if remaining <= 0:
            raise TimeoutError("historical runtime bound")
        query = {
            "symbols": "BTC/USD",
            "start": timestamp_ns(start_ns),
            "end": timestamp_ns(end_ns - 1),
            "sort": "asc",
            "limit": "10000",
        }
        if token is not None:
            query["page_token"] = token
        response = transport.request(
            method="GET",
            url="https://data.alpaca.markets/v1beta3/crypto/us/trades?" + urlencode(query),
            headers={"APCA-API-KEY-ID": credentials.key, "APCA-API-SECRET-KEY": credentials.secret},
            body=None,
            timeout=min(10.0, remaining),
        )
        received = UtcNanoseconds(now_ns())
        if response.status != 200:
            raise ValueError(f"historical trades HTTP {response.status}")
        payload = json.loads(response.body, parse_float=Decimal)
        rows = payload.get("trades")
        if not isinstance(rows, dict) or set(rows) - {"BTC/USD"}:
            raise ValueError("historical trade mapping required")
        values = rows.get("BTC/USD", [])
        if not isinstance(values, list):
            raise ValueError("historical trade array required")
        trades = []
        for row in values:
            if not isinstance(row, dict):
                raise ValueError("historical trade object required")
            event = normalize({**row, "T": "t", "S": "BTC/USD"}, received_at=received).event
            assert isinstance(event, CryptoTrade)
            event = replace(
                event, provenance=Provenance("alpaca:crypto:us:historical-fetch", "UTC")
            )
            if not start_ns <= event.observation_time.value < end_ns:
                raise ValueError("historical trade outside requested cut")
            if last is not None and (
                event.observation_time < last.observation_time or event.trade_id == last.trade_id
            ):
                raise ValueError("historical duplicate or unordered trade")
            last = event
            trades.append(event)
        yield tuple(trades)
        token = payload.get("next_page_token")
        if token is None:
            return
        if not isinstance(token, str) or not token or token in seen:
            raise ValueError("historical pagination cycle/invalid token")
        seen.add(token)
    raise ValueError("historical page budget exhausted")
