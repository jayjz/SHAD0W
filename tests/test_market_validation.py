"""Fixture-backed tests for fail-closed market-data collection validation."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from shadow.data import validate_bars, validate_quotes
from shadow.domain import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    DatasetMetadata,
    Instrument,
    MarketDataValidationError,
    Provenance,
    Quote,
    ValidationStatus,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> list[dict[str, str | int]]:
    return cast(
        list[dict[str, str | int]], json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    )


def _bars(name: str) -> list[Bar]:
    return [
        Bar(
            instrument=Instrument(str(item["instrument"])),
            interval=BarInterval(timedelta(seconds=int(item["interval_seconds"]))),
            observation_time=datetime.fromisoformat(str(item["observation_time"])),
            availability_time=datetime.fromisoformat(str(item["availability_time"])),
            availability_semantics=AvailabilitySemantics.PROVIDER_PUBLISHED,
            open=Decimal(str(item["open"])),
            high=Decimal(str(item["high"])),
            low=Decimal(str(item["low"])),
            close=Decimal(str(item["close"])),
            volume=Decimal(str(item["volume"])),
            provenance=Provenance("synthetic", "America/New_York", "regular"),
        )
        for item in _read_fixture(name)
    ]


def _metadata() -> DatasetMetadata:
    return DatasetMetadata(
        source="synthetic",
        dataset_id="p01-bars",
        instruments=(Instrument("SPY"),),
        interval=BarInterval(timedelta(minutes=1)),
        coverage_start=datetime(2024, 1, 2, 14, 31, tzinfo=UTC),
        coverage_end=datetime(2024, 1, 2, 14, 33, tzinfo=UTC),
        retrieval_method="repository_fixture",
        source_timezone="America/New_York",
        session="regular",
        adjustment_policy="unadjusted",
        validation_status=ValidationStatus.VALIDATED,
    )


def _quotes() -> list[Quote]:
    return [
        Quote(
            instrument=Instrument(str(item["instrument"])),
            bid_price=Decimal(str(item["bid_price"])),
            ask_price=Decimal(str(item["ask_price"])),
            bid_size=Decimal(str(item["bid_size"])),
            ask_size=Decimal(str(item["ask_size"])),
            observation_time=datetime.fromisoformat(str(item["observation_time"])),
            availability_time=datetime.fromisoformat(str(item["availability_time"])),
            availability_semantics=AvailabilitySemantics.PROVIDER_PUBLISHED,
            provenance=Provenance("synthetic", "America/New_York", "regular"),
        )
        for item in _read_fixture("valid_quotes.json")
    ]


def _quote_metadata() -> DatasetMetadata:
    return DatasetMetadata(
        source="synthetic",
        dataset_id="p01-quotes",
        instruments=(Instrument("SPY"),),
        coverage_start=datetime(2024, 1, 2, 14, 31, tzinfo=UTC),
        coverage_end=datetime(2024, 1, 2, 14, 32, tzinfo=UTC),
        retrieval_method="repository_fixture",
        validation_status=ValidationStatus.VALIDATED,
    )


def test_valid_fixture_passes_without_repair() -> None:
    report = validate_bars(_bars("valid_bars.json"), _metadata())

    assert report.record_count == 3
    assert report.observation_kind == "bar"


def test_quote_fixture_including_crossed_quote_passes_without_repair() -> None:
    report = validate_quotes(_quotes(), _quote_metadata())

    assert report.record_count == 2
    assert report.observation_kind == "quote"


@pytest.mark.parametrize(
    ("fixture", "invariant"),
    [
        ("out_of_order_bars.json", "chronological_order"),
        ("duplicate_bars.json", "duplicate_observation"),
    ],
)
def test_invalid_sequence_fixtures_fail_for_the_documented_reason(
    fixture: str, invariant: str
) -> None:
    with pytest.raises(MarketDataValidationError, match=invariant):
        validate_bars(_bars(fixture), _metadata())


@pytest.mark.parametrize(
    ("fixture", "invariant"),
    [
        ("invalid_ohlc_bar.json", "ohlc_high"),
        ("naive_timestamp_bar.json", "timestamp_awareness"),
    ],
)
def test_invalid_observation_fixtures_fail_at_the_domain_boundary(
    fixture: str, invariant: str
) -> None:
    with pytest.raises(MarketDataValidationError, match=invariant):
        _bars(fixture)
