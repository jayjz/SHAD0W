"""Deterministic delivered-depth reconstruction, without provider or order authority.

One reducer binds one source (including location), instrument and connection.
New connections require new reducers. Only accepted resets advance reset_epoch.
No-op updates advance accepted time but retain quality; immediate repetitions
ignore receipt time and leave the entire snapshot unchanged. Equality compares
ordered typed provider payloads, not hashes or invented provider sequence IDs.
After invalidation, a recovery reset must be strictly newer than accepted time;
a reset at the disputed timestamp cannot establish an ordering winner.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from shadow.domain.crypto_market import (
    C1_INSTRUMENTS,
    BookAction,
    BookLevel,
    BookQuality,
    CryptoBookEvent,
    UtcNanoseconds,
)
from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import Instrument, Provenance, QuoteMarketState


class BookDisposition(StrEnum):
    RESET_APPLIED = "reset_applied"
    UPDATE_APPLIED = "update_applied"
    NO_CHANGE = "no_change"
    IMMEDIATE_REPEAT = "immediate_repeat"
    AWAITING_RESET = "awaiting_reset"
    UNTRUSTED_OLDER_EVENT = "untrusted_older_event"
    UNTRUSTED_SAME_TIME_CONFLICT = "untrusted_same_time_conflict"
    UNTRUSTED_MALFORMED_EVENT = "untrusted_malformed_event"


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    source: str
    instrument: Instrument
    quality: BookQuality = BookQuality.UNKNOWN
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    accepted_time: UtcNanoseconds | None = None
    reset_epoch: int = 0

    @property
    def market_state(self) -> QuoteMarketState | None:
        """Delivered top relationship only; None if either side is empty."""
        if not self.bids or not self.asks:
            return None
        bid, ask = self.bids[0].price, self.asks[0].price
        if bid < ask:
            return QuoteMarketState.NORMAL
        return QuoteMarketState.LOCKED if bid == ask else QuoteMarketState.CROSSED


@dataclass(frozen=True, slots=True)
class BookResult:
    disposition: BookDisposition
    snapshot: BookSnapshot
    absent_deletions: int = 0
    repeated_assignments: int = 0
    error: str | None = None


def _same_payload(left: CryptoBookEvent, right: CryptoBookEvent) -> bool:
    return (left.action, left.bids, left.asks, left.observation_time) == (
        right.action,
        right.bids,
        right.asks,
        right.observation_time,
    )


class CryptoBookReducer:
    """Atomic reducer scoped to a provider/location source and one C1 instrument.

    Wrong routing raises without changing this book. Malformed evidence attributed
    to this book invalidates quality while retaining all accepted depth, time and
    reset lineage. The result carries the rejection reason for future evidence.
    A normalizer rejection can be explicitly attributed via invalidate().
    """

    def __init__(self, *, source: str, instrument: Instrument) -> None:
        Provenance(source)
        if not isinstance(instrument, Instrument) or instrument not in C1_INSTRUMENTS:
            raise MarketDataValidationError("instrument", "C1 instrument required")
        self._snapshot = BookSnapshot(source, instrument)
        self._last_delivery: CryptoBookEvent | None = None
        self._last_accepted: CryptoBookEvent | None = None

    @property
    def snapshot(self) -> BookSnapshot:
        return self._snapshot

    def invalidate(self, reason: str) -> BookResult:
        """Record malformed evidence already attributed to this reducer's scope."""
        if not isinstance(reason, str) or not reason.strip():
            raise MarketDataValidationError("reason", "explicit invalidation reason required")
        self._snapshot = replace(self._snapshot, quality=BookQuality.UNTRUSTED)
        self._last_delivery = None
        return BookResult(BookDisposition.UNTRUSTED_MALFORMED_EVENT, self._snapshot, error=reason)

    def apply(self, event: CryptoBookEvent) -> BookResult:
        previous = self._snapshot
        if not isinstance(event, CryptoBookEvent):
            raise MarketDataValidationError("event", "CryptoBookEvent required")
        if event.instrument != previous.instrument or (
            isinstance(event.provenance, Provenance) and event.provenance.source != previous.source
        ):
            raise MarketDataValidationError("scope", "event belongs to a different book")
        try:
            # Revalidate even objects whose frozen construction was bypassed.
            for levels in (event.bids, event.asks):
                if isinstance(levels, tuple):
                    for level in levels:
                        if isinstance(level, BookLevel):
                            replace(level)
            for instant in (event.observation_time, event.availability_time):
                if isinstance(instant, UtcNanoseconds):
                    replace(instant)
            if isinstance(event.provenance, Provenance):
                replace(event.provenance)
            event = replace(event)
        except MarketDataValidationError as exc:
            return self.invalidate(str(exc))

        immediate_repeat = self._last_delivery is not None and _same_payload(
            event, self._last_delivery
        )
        self._last_delivery = event
        watermark = previous.accepted_time
        disposition: BookDisposition | None = None
        if watermark is not None and event.observation_time < watermark:
            disposition = BookDisposition.UNTRUSTED_OLDER_EVENT
        elif (
            watermark == event.observation_time
            and self._last_accepted is not None
            and (
                not _same_payload(event, self._last_accepted)
                or previous.quality is BookQuality.UNTRUSTED
            )
        ):
            disposition = BookDisposition.UNTRUSTED_SAME_TIME_CONFLICT
        if disposition is not None:
            self._snapshot = replace(previous, quality=BookQuality.UNTRUSTED)
            return BookResult(disposition, self._snapshot)
        if event.action is BookAction.UPDATE and previous.quality in (
            BookQuality.UNKNOWN,
            BookQuality.UNTRUSTED,
        ):
            return BookResult(BookDisposition.AWAITING_RESET, previous)
        if immediate_repeat and previous.quality not in (
            BookQuality.UNKNOWN,
            BookQuality.UNTRUSTED,
        ):
            return BookResult(BookDisposition.IMMEDIATE_REPEAT, previous)

        reset = event.action is BookAction.RESET
        bids = {} if reset else {level.price: level.size for level in previous.bids}
        asks = {} if reset else {level.price: level.size for level in previous.asks}
        absent, repeated = 0, 0
        for levels, target in ((event.bids, bids), (event.asks, asks)):
            for level in levels:
                if level.size == 0:
                    absent += int(level.price not in target)
                    target.pop(level.price, None)
                else:
                    repeated += int(target.get(level.price) == level.size)
                    target[level.price] = level.size

        def project(values: dict[Decimal, Decimal], *, reverse: bool) -> tuple[BookLevel, ...]:
            return tuple(
                BookLevel(price, values[price]) for price in sorted(values, reverse=reverse)
            )

        projected_bids, projected_asks = project(bids, reverse=True), project(asks, reverse=False)
        changed = (projected_bids, projected_asks) != (previous.bids, previous.asks)
        quality = previous.quality
        if reset:
            quality, disposition = BookQuality.RESET_SNAPSHOT, BookDisposition.RESET_APPLIED
        elif changed:
            quality = BookQuality.RECONSTRUCTED_UNVERIFIED
            disposition = BookDisposition.UPDATE_APPLIED
        else:
            disposition = BookDisposition.NO_CHANGE
        prospective = BookSnapshot(
            previous.source,
            previous.instrument,
            quality,
            projected_bids,
            projected_asks,
            event.observation_time,
            previous.reset_epoch + int(reset),
        )
        self._snapshot = prospective
        self._last_accepted = event
        return BookResult(disposition, prospective, absent, repeated)
