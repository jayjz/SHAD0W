"""Immutable crypto observations; no provider parsing or operational authority."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import AvailabilitySemantics, Instrument, Provenance, QuoteMarketState

CRYPTO_MARKET_SCHEMA = "shadow.crypto-market.v1"
C1_INSTRUMENTS = (Instrument("BTC/USD"), Instrument("ETH/USD"))


@dataclass(frozen=True, slots=True, order=True)
class UtcNanoseconds:
    """Exact Unix-epoch nanoseconds in UTC, within calendar years 1 through 9999.

    This is a value boundary, not a timestamp parser. Adapters must convert offsets
    without passing fractional seconds through float or datetime microseconds.
    """

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or not (
            -62_135_596_800_000_000_000 <= self.value <= 253_402_300_799_999_999_999
        ):
            raise MarketDataValidationError("utc_nanoseconds", "integer UTC instant required")


class TakerSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class BookAction(StrEnum):
    RESET = "reset"
    UPDATE = "update"


class BookQuality(StrEnum):
    """Research reconstruction quality, never permission to execute."""

    UNKNOWN = "unknown"
    RESET_SNAPSHOT = "reset_snapshot"
    RECONSTRUCTED_UNVERIFIED = "reconstructed_unverified"
    UNTRUSTED = "untrusted"


def _number(value: Decimal, name: str, *, zero_allowed: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MarketDataValidationError(name, "finite Decimal required")
    if value < 0 or (not zero_allowed and value == 0):
        raise MarketDataValidationError(name, "must be nonnegative" if zero_allowed else "positive")


def _context(
    instrument: Instrument,
    observation_time: UtcNanoseconds,
    availability_time: UtcNanoseconds,
    availability_semantics: AvailabilitySemantics,
    provenance: Provenance,
) -> None:
    if not isinstance(instrument, Instrument) or instrument not in C1_INSTRUMENTS:
        raise MarketDataValidationError("instrument", "C1 requires BTC/USD or ETH/USD")
    if not isinstance(observation_time, UtcNanoseconds) or not isinstance(
        availability_time, UtcNanoseconds
    ):
        raise MarketDataValidationError("timestamp", "UtcNanoseconds required")
    if availability_time < observation_time:
        raise MarketDataValidationError(
            "availability_time",
            "must not precede observation_time; local wall-clock authority may be insufficient",
        )
    if availability_semantics is not AvailabilitySemantics.SYSTEM_RECEIVED:
        raise MarketDataValidationError("availability_semantics", "SYSTEM_RECEIVED required")
    if not isinstance(provenance, Provenance):
        raise MarketDataValidationError("provenance", "Provenance required")


@dataclass(frozen=True, slots=True)
class CryptoTrade:
    instrument: Instrument
    price: Decimal
    size: Decimal
    trade_id: str
    taker_side: TakerSide
    observation_time: UtcNanoseconds
    availability_time: UtcNanoseconds
    availability_semantics: AvailabilitySemantics
    provenance: Provenance

    def __post_init__(self) -> None:
        _context(
            self.instrument,
            self.observation_time,
            self.availability_time,
            self.availability_semantics,
            self.provenance,
        )
        _number(self.price, "price")
        _number(self.size, "size")
        if (
            not isinstance(self.trade_id, str)
            or not self.trade_id
            or self.trade_id != self.trade_id.strip()
        ):
            raise MarketDataValidationError("trade_id", "nonempty trimmed opaque string required")
        if not isinstance(self.taker_side, TakerSide):
            raise MarketDataValidationError("taker_side", "TakerSide required")


@dataclass(frozen=True, slots=True)
class CryptoQuote:
    instrument: Instrument
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal
    observation_time: UtcNanoseconds
    availability_time: UtcNanoseconds
    availability_semantics: AvailabilitySemantics
    provenance: Provenance

    def __post_init__(self) -> None:
        _context(
            self.instrument,
            self.observation_time,
            self.availability_time,
            self.availability_semantics,
            self.provenance,
        )
        _number(self.bid_price, "bid_price")
        _number(self.ask_price, "ask_price")
        _number(self.bid_size, "bid_size", zero_allowed=True)
        _number(self.ask_size, "ask_size", zero_allowed=True)

    @property
    def market_state(self) -> QuoteMarketState:
        if self.bid_price < self.ask_price:
            return QuoteMarketState.NORMAL
        if self.bid_price == self.ask_price:
            return QuoteMarketState.LOCKED
        return QuoteMarketState.CROSSED


@dataclass(frozen=True, slots=True)
class BookLevel:
    """An absolute size at an exact price; zero denotes absence/deletion."""

    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        _number(self.price, "price")
        _number(self.size, "size", zero_allowed=True)


@dataclass(frozen=True, slots=True)
class CryptoBookEvent:
    """Both sides are required, but either may be empty. No book is reduced here."""

    instrument: Instrument
    action: BookAction
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    observation_time: UtcNanoseconds
    availability_time: UtcNanoseconds
    availability_semantics: AvailabilitySemantics
    provenance: Provenance

    def __post_init__(self) -> None:
        _context(
            self.instrument,
            self.observation_time,
            self.availability_time,
            self.availability_semantics,
            self.provenance,
        )
        if not isinstance(self.action, BookAction):
            raise MarketDataValidationError("book_action", "BookAction required")
        for name, levels in (("bids", self.bids), ("asks", self.asks)):
            if not isinstance(levels, tuple) or not all(
                isinstance(level, BookLevel) for level in levels
            ):
                raise MarketDataValidationError(name, "tuple of BookLevel required")
            if len({level.price for level in levels}) != len(levels):
                raise MarketDataValidationError(name, "duplicate price level")
