"""Strict translation of Alpaca stock-stream JSON into provider-neutral values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shadow.domain.market import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    Instrument,
    Provenance,
    Quote,
)

MINUTE = BarInterval(timedelta(minutes=1))
SUPPORTED_FEEDS = ("iex", "sip")


def symbols_checked(symbols: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not symbols
        or len(set(symbols)) != len(symbols)
        or any(not re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", symbol) for symbol in symbols)
    ):
        raise ValueError("explicit unique uppercase equity symbols required; wildcards unsupported")
    return tuple(sorted(symbols))


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware receive time required")
    return value.astimezone(UTC)


def event_time(value: object) -> datetime:
    """Parse the RFC-3339 wire instant, ceiling nanoseconds to domain precision."""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)", value
    ):
        raise ValueError("aware RFC3339 provider timestamp required")
    result = utc(datetime.fromisoformat(value))
    fraction = re.search(r"\.(\d+)", value)
    if fraction and any(char != "0" for char in fraction[1][6:]):
        result += timedelta(microseconds=1)
    return result


def number(payload: Mapping[str, object], key: str, *, optional: bool = False) -> Decimal | None:
    value = payload.get(key)
    if optional and value is None:
        return None
    # JSON decoding uses Decimal, never binary float or a numeric string.
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"invalid numeric field: {key}")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"nonfinite numeric field: {key}")
    return result


def required_number(payload: Mapping[str, object], key: str) -> Decimal:
    result = number(payload, key)
    assert result is not None
    return result


def translate(
    payload: Mapping[str, object], *, received_at: datetime, symbols: tuple[str, ...], feed: str
) -> Bar | Quote:
    """Translate one complete minute bar or quote received at an injected instant.

    Alpaca minute-bar ``t`` is the left edge. SHAD0W bars use their represented
    interval end, and neither a quote nor a completed bar may be accepted before
    its source instant is actually received.
    """
    checked_symbols = symbols_checked(symbols)
    if feed not in SUPPORTED_FEEDS:
        raise ValueError("only real-time iex or sip feeds supported")
    symbol = payload.get("S")
    if not isinstance(symbol, str) or symbol not in checked_symbols:
        raise ValueError("observation symbol outside configured scope")
    observed = event_time(payload.get("t"))
    received = utc(received_at)
    kind = payload.get("T")
    instrument = Instrument(symbol)
    provenance = Provenance(f"alpaca:{feed}", "UTC")
    if kind == "b":
        if observed.second or observed.microsecond:
            raise ValueError("minute bar start must be minute-aligned")
        interval_end = observed + MINUTE.duration
        if interval_end > received:
            raise ValueError("completed minute bar interval ends after receipt")
        return Bar(
            instrument,
            MINUTE,
            interval_end,
            received,
            AvailabilitySemantics.SYSTEM_RECEIVED,
            required_number(payload, "o"),
            required_number(payload, "h"),
            required_number(payload, "l"),
            required_number(payload, "c"),
            number(payload, "v", optional=True),
            provenance,
        )
    if kind == "q":
        if observed > received:
            raise ValueError("quote source timestamp is after receipt")
        return Quote(
            instrument,
            required_number(payload, "bp"),
            required_number(payload, "ap"),
            number(payload, "bs", optional=True),
            number(payload, "as", optional=True),
            observed,
            received,
            AvailabilitySemantics.SYSTEM_RECEIVED,
            Provenance(f"alpaca:{feed}", "UTC", "quote_sizes_in_provider_round_lots"),
        )
    raise ValueError("only completed minute bars and quotes supported")
