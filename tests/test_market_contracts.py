"""Behavioral tests for provider-neutral individual market observations."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from shadow.domain import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    Instrument,
    MarketDataValidationError,
    Provenance,
    Quote,
    QuoteMarketState,
)


def _bar(
    *, observation_time: datetime | None = None, availability_time: datetime | None = None
) -> Bar:
    observed = observation_time or datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
    available = availability_time or datetime(2024, 1, 2, 14, 31, 1, tzinfo=UTC)
    return Bar(
        instrument=Instrument("SPY"),
        interval=BarInterval(timedelta(minutes=1)),
        observation_time=observed,
        availability_time=available,
        availability_semantics=AvailabilitySemantics.PROVIDER_PUBLISHED,
        open=Decimal("470.00"),
        high=Decimal("470.30"),
        low=Decimal("469.90"),
        close=Decimal("470.20"),
        volume=Decimal("0"),
        provenance=Provenance("synthetic", "America/New_York", "regular"),
    )


def test_timezone_equivalent_aware_timestamps_normalize_to_utc() -> None:
    bar = _bar(
        observation_time=datetime(2024, 1, 2, 9, 31, tzinfo=timezone(timedelta(hours=-5))),
        availability_time=datetime(2024, 1, 2, 9, 31, 1, tzinfo=timezone(timedelta(hours=-5))),
    )

    assert bar.observation_time == datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
    assert bar.availability_time == datetime(2024, 1, 2, 14, 31, 1, tzinfo=UTC)


def test_naive_timestamp_fails_explicitly() -> None:
    with pytest.raises(MarketDataValidationError, match="timestamp_awareness"):
        _bar(observation_time=datetime(2024, 1, 2, 14, 31))


def test_ohlcv_and_nonfinite_values_fail_explicitly() -> None:
    with pytest.raises(MarketDataValidationError, match="ohlc_high"):
        replace(_bar(), high=Decimal("469.99"))
    with pytest.raises(MarketDataValidationError, match="open"):
        replace(_bar(), open=Decimal("NaN"))
    with pytest.raises(MarketDataValidationError, match="open"):
        replace(_bar(), open=Decimal("Infinity"))


def test_availability_cannot_precede_observation() -> None:
    with pytest.raises(MarketDataValidationError, match="availability_time"):
        _bar(availability_time=datetime(2024, 1, 2, 14, 30, 59, tzinfo=UTC))


def test_quote_retains_and_classifies_crossed_market() -> None:
    quote = Quote(
        instrument=Instrument("SPY"),
        bid_price=Decimal("470.01"),
        ask_price=Decimal("470.00"),
        bid_size=Decimal("10"),
        ask_size=Decimal("0"),
        observation_time=datetime(2024, 1, 2, 14, 31, tzinfo=UTC),
        availability_time=datetime(2024, 1, 2, 14, 31, 1, tzinfo=UTC),
        availability_semantics=AvailabilitySemantics.SYSTEM_RECEIVED,
        provenance=Provenance("synthetic"),
    )

    assert quote.market_state is QuoteMarketState.CROSSED
