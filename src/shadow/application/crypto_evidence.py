"""Canonical append-only C1 evidence. It deliberately contains no provider transport."""
# ruff: noqa: E501, E701, E702

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

from shadow.application.crypto_session import CryptoSessionResult
from shadow.data.crypto_book import BookSnapshot
from shadow.domain.crypto_market import (
    CRYPTO_MARKET_SCHEMA,
    BookAction,
    BookLevel,
    CryptoBookEvent,
    CryptoQuote,
    CryptoTrade,
    TakerSide,
    UtcNanoseconds,
)
from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import AvailabilitySemantics, Instrument, Provenance


class CryptoCaptureStatus(StrEnum):
    COMPLETE_STOPPED = "complete_stopped"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class CryptoEvidenceError(ValueError):
    pass


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def capture_sha256(path: Path) -> str:
    """Return the identity of the exact persisted UTF-8 capture bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CryptoEvidenceError("finite Decimal required")
    return str(value)


def encode_event(event: CryptoTrade | CryptoQuote | CryptoBookEvent) -> dict[str, object]:
    common: dict[str, object] = {
        "instrument": event.instrument.identifier,
        "observation_time": event.observation_time.value,
        "availability_time": event.availability_time.value,
        "availability_semantics": event.availability_semantics.value,
        "provenance": {
            "source": event.provenance.source,
            "source_timezone": event.provenance.source_timezone,
            "session": event.provenance.session,
        },
    }
    if isinstance(event, CryptoTrade):
        return {
            "type": "trade",
            **common,
            "price": _decimal(event.price),
            "size": _decimal(event.size),
            "trade_id": event.trade_id,
            "taker_side": event.taker_side.value,
        }
    if isinstance(event, CryptoQuote):
        return {
            "type": "quote",
            **common,
            "bid_price": _decimal(event.bid_price),
            "ask_price": _decimal(event.ask_price),
            "bid_size": _decimal(event.bid_size),
            "ask_size": _decimal(event.ask_size),
        }
    return {
        "type": "book",
        **common,
        "action": event.action.value,
        "bids": [
            {"price": _decimal(level.price), "size": _decimal(level.size)} for level in event.bids
        ],
        "asks": [
            {"price": _decimal(level.price), "size": _decimal(level.size)} for level in event.asks
        ],
    }


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CryptoEvidenceError(f"{name} object required")
    return value


def _decimal_decode(value: object) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CryptoEvidenceError("Decimal string required")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise CryptoEvidenceError("invalid Decimal") from exc
    if not result.is_finite():
        raise CryptoEvidenceError("finite Decimal required")
    return result


def decode_event(value: object) -> CryptoTrade | CryptoQuote | CryptoBookEvent:
    item = _mapping(value, "event")
    required = {
        "type",
        "instrument",
        "observation_time",
        "availability_time",
        "availability_semantics",
        "provenance",
    }
    if not required <= item.keys():
        raise CryptoEvidenceError("event fields required")
    try:
        instrument = Instrument(item["instrument"])
        provenance_value = _mapping(item["provenance"], "provenance")
        provenance = Provenance(
            provenance_value["source"],
            provenance_value.get("source_timezone"),
            provenance_value.get("session"),
        )
        observation_time = UtcNanoseconds(item["observation_time"])
        availability_time = UtcNanoseconds(item["availability_time"])
        availability_semantics = AvailabilitySemantics(item["availability_semantics"])
        event_type = item["type"]
        if event_type == "trade":
            return CryptoTrade(
                instrument,
                _decimal_decode(item["price"]),
                _decimal_decode(item["size"]),
                item["trade_id"],
                TakerSide(item["taker_side"]),
                observation_time,
                availability_time,
                availability_semantics,
                provenance,
            )
        if event_type == "quote":
            return CryptoQuote(
                instrument,
                _decimal_decode(item["bid_price"]),
                _decimal_decode(item["ask_price"]),
                _decimal_decode(item["bid_size"]),
                _decimal_decode(item["ask_size"]),
                observation_time,
                availability_time,
                availability_semantics,
                provenance,
            )
        if event_type == "book":

            def levels(side: str) -> tuple[BookLevel, ...]:
                if not isinstance(item[side], list):
                    raise CryptoEvidenceError("book side list required")
                return tuple(
                    BookLevel(
                        _decimal_decode(_mapping(level, "level")["price"]),
                        _decimal_decode(_mapping(level, "level")["size"]),
                    )
                    for level in item[side]
                )

            return CryptoBookEvent(
                instrument,
                BookAction(item["action"]),
                levels("bids"),
                levels("asks"),
                observation_time,
                availability_time,
                availability_semantics,
                provenance,
            )
    except (KeyError, TypeError, ValueError, MarketDataValidationError) as exc:
        raise CryptoEvidenceError("invalid normalized event") from exc
    raise CryptoEvidenceError("unknown event type")


def _snapshot(snapshot: BookSnapshot | None) -> object:
    if snapshot is None:
        return None
    return {
        "instrument": snapshot.instrument.identifier,
        "quality": snapshot.quality.value,
        "bids": [[_decimal(level.price), _decimal(level.size)] for level in snapshot.bids],
        "asks": [[_decimal(level.price), _decimal(level.size)] for level in snapshot.asks],
        "accepted_time": snapshot.accepted_time.value if snapshot.accepted_time else None,
        "reset_epoch": snapshot.reset_epoch,
    }


@dataclass(frozen=True, slots=True)
class CryptoEvidenceHeader:
    session_id: str
    code_revision: str
    implementation_versions: dict[str, str]
    numeric_time_policy: dict[str, str]
    capture_limits: dict[str, object] | None = None

    def encode(self) -> dict[str, object]:
        return {
            "schema": CRYPTO_MARKET_SCHEMA,
            "session_id": self.session_id,
            "code_revision": self.code_revision,
            "provider": "alpaca",
            "crypto_location": "us",
            "instruments": ["BTC/USD", "ETH/USD"],
            "requested_channels": ["trades", "quotes", "orderbooks"],
            "implementation_versions": self.implementation_versions,
            "numeric_time_policy": self.numeric_time_policy,
            "capture_limits": self.capture_limits,
        }


class CryptoEvidenceWriter:
    def __init__(self, path: Path, header: CryptoEvidenceHeader) -> None:
        self._file: TextIO = path.open("x", encoding="utf-8", newline="\n")
        self._sequence = 0
        self._previous = ""
        self._terminal = False
        self._write(header.encode())

    def _write(self, value: object) -> None:
        self._file.write(canonical_json(value) + "\n")
        self._file.flush()

    def append(
        self,
        result: CryptoSessionResult,
        *,
        frame_sequence: int,
        element_index: int,
        source_timestamp: str | None = None,
        receipt_monotonic_ns: int | None = None,
        raw_frame_sha256: str | None = None,
    ) -> dict[str, object]:
        if self._terminal:
            raise CryptoEvidenceError("terminal record already written")
        if frame_sequence < 0 or element_index < 0:
            raise CryptoEvidenceError("nonnegative frame identity required")
        payload: dict[str, object] = {
            "record_type": "event",
            "sequence": self._sequence,
            "connection_epoch": result.connection_epoch,
            "frame_sequence": frame_sequence,
            "element_index": element_index,
            "kind": "control" if result.event is None else encode_event(result.event)["type"],
            "event": encode_event(result.event) if result.event else None,
            "disposition": result.disposition.value,
            "book_quality": result.snapshot.quality.value if result.snapshot else None,
            "snapshot": _snapshot(result.snapshot),
            "previous_record_hash": self._previous,
            "source_timestamp": source_timestamp,
            "receipt_monotonic_ns": receipt_monotonic_ns,
            "raw_frame_sha256": raw_frame_sha256,
        }
        payload["state_lineage"] = hashlib.sha256(
            (self._previous + canonical_json(payload["event"])).encode("utf-8")
        ).hexdigest()
        payload["record_hash"] = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        self._write(payload)
        self._previous = str(payload["record_hash"])
        self._sequence += 1
        return payload

    def close(self, status: CryptoCaptureStatus = CryptoCaptureStatus.COMPLETE_STOPPED) -> None:
        if not self._terminal:
            self._write(
                {
                    "record_type": "terminal",
                    "status": status.value,
                    "previous_record_hash": self._previous,
                }
            )
            self._terminal = True
        self._file.close()
