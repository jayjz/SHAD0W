"""Pure Alpaca ``crypto/us`` translation; callers inject exact receipt evidence.

Wire reference: https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data
Decode JSON fractional numbers as Decimal before calling (binary floats reject).
Exact integers, Decimals and JSON-number-shaped strings are accepted as numbers.
Only trades, quotes and orderbooks are supported. No I/O or execution authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from shadow.domain.crypto_market import (
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

_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


@dataclass(frozen=True, slots=True)
class NormalizedCryptoMessage:
    """Typed observation plus verbatim timestamp for the future evidence layer."""

    event: CryptoTrade | CryptoQuote | CryptoBookEvent
    source_timestamp: str


def parse_timestamp(value: object) -> UtcNanoseconds:
    """Parse RFC3339 calendar instants using only integer epoch arithmetic.

    Accept Z/z or known numeric offsets through 23:59. Reject naive timestamps,
    unknown-offset -00:00, leap seconds (not representable by this Unix contract),
    invalid dates and fractions longer than nine digits. Fractions never enter
    datetime microsecond precision. UTC range validation is delegated to C0.
    """
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise MarketDataValidationError("timestamp", "explicit RFC3339 instant required")
    year, month, day, hour, minute, second = (int(match[i]) for i in range(1, 7))
    try:
        days = date(year, month, day).toordinal() - 719163
    except ValueError as exc:
        raise MarketDataValidationError("timestamp", "invalid calendar date") from exc
    zone = match[8]
    if hour > 23 or minute > 59 or second > 59 or zone == "-00:00":
        raise MarketDataValidationError("timestamp", "invalid time or unknown UTC offset")
    offset = 0
    if zone not in ("Z", "z"):
        offset_hour, offset_minute = int(zone[1:3]), int(zone[4:6])
        if offset_hour > 23 or offset_minute > 59:
            raise MarketDataValidationError("timestamp", "invalid UTC offset")
        offset = (offset_hour * 3600 + offset_minute * 60) * (1 if zone[0] == "+" else -1)
    seconds = days * 86400 + hour * 3600 + minute * 60 + second - offset
    fraction = int((match[7] or "").ljust(9, "0"))
    return UtcNanoseconds(seconds * 1_000_000_000 + fraction)


def _number(value: object, field: str) -> Decimal:
    if type(value) is int or isinstance(value, Decimal):
        result = Decimal(value)
    elif isinstance(value, str) and _NUMBER.fullmatch(value):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise MarketDataValidationError(field, "invalid Decimal") from exc
    else:
        raise MarketDataValidationError(field, "exact numeric value required; floats reject")
    if not result.is_finite():
        raise MarketDataValidationError(field, "finite Decimal required")
    return result


def _levels(value: object, side: str) -> tuple[BookLevel, ...]:
    if not isinstance(value, list):
        raise MarketDataValidationError(side, "explicit side array required")
    levels = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MarketDataValidationError(side, "price/size object required")
        levels.append(BookLevel(_number(item.get("p"), "price"), _number(item.get("s"), "size")))
    # C0 rejects duplicate numerical prices, preserving original provider order.
    return tuple(levels)


def normalize(
    payload: object, *, received_at: UtcNanoseconds, location: str = "us"
) -> NormalizedCryptoMessage:
    """Translate one decoded object, preserving source time and receipt separately.

    The documented integer trade ID is rendered losslessly as opaque decimal text;
    no uniqueness or sequence meaning is assigned to it. Extra provider fields are
    ignored; all required fields and known discriminators must be valid.
    """
    if location != "us":
        raise MarketDataValidationError("location", "only Alpaca crypto us supported")
    if not isinstance(payload, Mapping):
        raise MarketDataValidationError("payload", "provider object required")
    symbol = payload.get("S")
    if not isinstance(symbol, str) or symbol not in ("BTC/USD", "ETH/USD"):
        raise MarketDataValidationError("instrument", "BTC/USD or ETH/USD required")
    timestamp = payload.get("t")
    observed = parse_timestamp(timestamp)
    assert isinstance(timestamp, str)
    instrument = Instrument(symbol)
    # Preserve the source's actual timezone representation, including offsets.
    zone = "UTC" if timestamp[-1] in "Zz" else timestamp[-6:]
    provenance = Provenance("alpaca:crypto:us", zone)
    semantics = AvailabilitySemantics.SYSTEM_RECEIVED
    event: CryptoTrade | CryptoQuote | CryptoBookEvent
    kind = payload.get("T")
    if kind == "t":
        trade_id = payload.get("i")
        side = payload.get("tks")
        if type(trade_id) is not int:
            raise MarketDataValidationError("trade_id", "documented integer ID required")
        if not isinstance(side, str) or side not in ("B", "S"):
            raise MarketDataValidationError("taker_side", "B or S required")
        event = CryptoTrade(
            instrument,
            _number(payload.get("p"), "price"),
            _number(payload.get("s"), "size"),
            str(trade_id),
            TakerSide.BUY if side == "B" else TakerSide.SELL,
            observed,
            received_at,
            semantics,
            provenance,
        )
    elif kind == "q":
        event = CryptoQuote(
            instrument,
            _number(payload.get("bp"), "bid_price"),
            _number(payload.get("ap"), "ask_price"),
            _number(payload.get("bs"), "bid_size"),
            _number(payload.get("as"), "ask_size"),
            observed,
            received_at,
            semantics,
            provenance,
        )
    elif kind == "o":
        reset = payload.get("r", False)
        if type(reset) is not bool:
            raise MarketDataValidationError("reset", "boolean r required when present")
        event = CryptoBookEvent(
            instrument,
            BookAction.RESET if reset else BookAction.UPDATE,
            _levels(payload.get("b"), "bids"),
            _levels(payload.get("a"), "asks"),
            observed,
            received_at,
            semantics,
            provenance,
        )
    else:
        raise MarketDataValidationError("event_type", "only t, q and o supported")
    return NormalizedCryptoMessage(event, timestamp)
