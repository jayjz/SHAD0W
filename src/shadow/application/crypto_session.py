"""Offline C1 session ownership; this module has no transport or execution authority."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from shadow.data.crypto_book import BookDisposition, BookResult, BookSnapshot, CryptoBookReducer
from shadow.domain.crypto_market import (
    C1_INSTRUMENTS,
    CryptoBookEvent,
    CryptoQuote,
    CryptoTrade,
)
from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import Instrument


class SessionDisposition(StrEnum):
    TRADE_OBSERVED = "trade_observed"
    QUOTE_OBSERVED = "quote_observed"
    CONNECTION_ESTABLISHED = "connection_established"
    CONNECTION_DISCONNECTED = "connection_disconnected"


@dataclass(frozen=True, slots=True)
class CryptoSessionResult:
    event: CryptoTrade | CryptoQuote | CryptoBookEvent | None
    disposition: SessionDisposition | BookDisposition
    connection_epoch: int
    snapshot: BookSnapshot | None = None
    book_result: BookResult | None = None


class CryptoSession:
    """Own reducers for exactly the two C1 pairs during one offline capture."""

    def __init__(self, *, source: str = "alpaca:crypto:us") -> None:
        self._source = source
        self._epoch = 0
        self._connected = False
        self._books: dict[Instrument, CryptoBookReducer] = {}

    @property
    def connection_epoch(self) -> int:
        return self._epoch

    @property
    def connected(self) -> bool:
        return self._connected

    def establish_connection(self) -> CryptoSessionResult:
        self._epoch += 1
        self._connected = True
        self._books = {
            instrument: CryptoBookReducer(source=self._source, instrument=instrument)
            for instrument in C1_INSTRUMENTS
        }
        return CryptoSessionResult(None, SessionDisposition.CONNECTION_ESTABLISHED, self._epoch)

    def disconnect(self) -> CryptoSessionResult:
        self._connected = False
        self._books = {}
        return CryptoSessionResult(None, SessionDisposition.CONNECTION_DISCONNECTED, self._epoch)

    def snapshot(self, instrument: Instrument) -> BookSnapshot | None:
        reducer = self._books.get(instrument)
        return reducer.snapshot if reducer is not None else None

    def process(self, event: CryptoTrade | CryptoQuote | CryptoBookEvent) -> CryptoSessionResult:
        if not self._connected:
            raise MarketDataValidationError("connection", "connection must be established")
        if event.instrument not in C1_INSTRUMENTS:
            raise MarketDataValidationError("instrument", "C1 instrument required")
        if isinstance(event, CryptoTrade):
            return CryptoSessionResult(event, SessionDisposition.TRADE_OBSERVED, self._epoch)
        if isinstance(event, CryptoQuote):
            return CryptoSessionResult(event, SessionDisposition.QUOTE_OBSERVED, self._epoch)
        if not isinstance(event, CryptoBookEvent):
            raise MarketDataValidationError("event", "normalized crypto event required")
        result = self._books[event.instrument].apply(event)
        return CryptoSessionResult(event, result.disposition, self._epoch, result.snapshot, result)

    def state_digest(self, lineage: str = "") -> str:
        """Digest includes caller-owned evidence lineage, never only visible depth."""
        payload = {
            "epoch": self._epoch,
            "connected": self._connected,
            "lineage": lineage,
            "books": [
                {
                    "instrument": instrument.identifier,
                    "quality": snapshot.quality.value,
                    "bids": [[str(level.price), str(level.size)] for level in snapshot.bids],
                    "asks": [[str(level.price), str(level.size)] for level in snapshot.asks],
                    "time": snapshot.accepted_time.value if snapshot.accepted_time else None,
                    "reset_epoch": snapshot.reset_epoch,
                }
                for instrument in C1_INSTRUMENTS
                if (snapshot := self.snapshot(instrument)) is not None
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
