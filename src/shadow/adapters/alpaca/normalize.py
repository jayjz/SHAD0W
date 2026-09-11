"""Strict translation of Alpaca JSON minute bars and quotes, without SDK models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shadow.domain.market import AvailabilitySemantics, Bar, BarInterval, Instrument, Provenance, Quote

MINUTE = BarInterval(timedelta(minutes=1))


def symbols_checked(symbols: tuple[str, ...]) -> tuple[str, ...]:
    if not symbols or len(set(symbols)) != len(symbols) or any(
        not re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", symbol) for symbol in symbols
    ):
        raise ValueError("explicit unique uppercase equity symbols required; wildcards unsupported")
    return tuple(sorted(symbols))


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware receive time required")
    return value.astimezone(UTC)


def event_time(value: object) -> datetime:
    # Domain precision is microseconds. Ceiling prevents sub-microsecond lookahead.
    # The transport retains the original timestamp and complete payload separately.
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
    # JSON decoding uses Decimal, never binary floating point or numeric strings.
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"invalid numeric field: {key}")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"nonfinite numeric field: {key}")
    return result


def required_number(payload: Mapping[str, object], key: str) -> Decimal:
    value = number(payload, key)
    assert value is not None
    return value


def translate(
    payload: Mapping[str, object], *, received_at: datetime, symbols: tuple[str, ...], feed: str
) -> Bar | Quote:
    symbols_checked(symbols)
    if feed not in ("iex", "sip"):
        raise ValueError("only real-time iex or sip feeds supported")
    symbol = payload.get("S")
    if not isinstance(symbol, str) or symbol not in symbols:
        raise ValueError("observation symbol outside configured scope")
    observed = event_time(payload.get("t"))
    received = utc(received_at)
    instrument = Instrument(symbol)
    semantics = AvailabilitySemantics.SYSTEM_RECEIVED
    if payload.get("T") == "b":
        if observed.second or observed.microsecond:
            raise ValueError("minute bar start must be minute-aligned")
        return Bar(
            instrument, MINUTE, observed + MINUTE.duration, received, semantics,
            required_number(payload, "o"), required_number(payload, "h"),
            required_number(payload, "l"), required_number(payload, "c"),
            number(payload, "v", optional=True), Provenance(f"alpaca:{feed}", "UTC"),
        )
    if payload.get("T") == "q":
        return Quote(
            instrument, required_number(payload, "bp"), required_number(payload, "ap"),
            number(payload, "bs", optional=True), number(payload, "as", optional=True),
            observed, received, semantics,
            Provenance(f"alpaca:{feed}", "UTC", "quote_sizes_in_provider_round_lots"),
        )
    raise ValueError("only completed minute bars and quotes supported")
