"""Offline regressions for the bounded P4A Alpaca live-data shadow path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from shadow.adapters.alpaca.normalize import translate
from shadow.adapters.alpaca.stream import DataCredentials
from shadow.application.evidence import replay
from shadow.application.shadow import (
    FeedHealth,
    RiskObservabilityStatus,
    ShadowConfig,
    ShadowRecord,
    ShadowSession,
)
from shadow.domain import Bar, Instrument, Quote
from shadow.risk import OperationalQuantityConfig, OperatorControls, RiskPolicy, RiskState
from shadow.strategies import MeanReversionConfig

UTC_TIME = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)


def at(minutes: int, seconds: int = 0) -> datetime:
    return UTC_TIME + timedelta(minutes=minutes, seconds=seconds)


def bar(
    minute: int, close: str, *, symbol: str = "AAPL", received_delay: int = 1
) -> dict[str, object]:
    return {
        "T": "b",
        "S": symbol,
        "o": Decimal("100"),
        "h": Decimal("101"),
        "l": Decimal("90"),
        "c": Decimal(close),
        "v": Decimal("12"),
        "t": at(minute).isoformat().replace("+00:00", "Z"),
        "received": at(minute + 1, received_delay),
    }


def quote(
    minute: int, *, symbol: str = "AAPL", bid: str = "99", ask: str = "100"
) -> dict[str, object]:
    return {
        "T": "q",
        "S": symbol,
        "bp": Decimal(bid),
        "ap": Decimal(ask),
        "bs": Decimal("4"),
        "as": Decimal("5"),
        "t": at(minute, 30).isoformat().replace("+00:00", "Z"),
        "received": at(minute, 31),
    }


def observation(payload: dict[str, object]) -> Bar | Quote:
    received = payload.pop("received")
    assert isinstance(received, datetime)
    return translate(payload, received_at=received, symbols=("AAPL", "MSFT"), feed="iex")


def config(*, symbols: tuple[str, ...] = ("AAPL",), state: RiskState | None = None) -> ShadowConfig:
    instruments = tuple(Instrument(symbol) for symbol in symbols)
    strategies = tuple(
        MeanReversionConfig(
            instrument, f"{symbol}-strategy", 3, Decimal("-1"), Decimal("0"), timedelta(minutes=5)
        )
        for symbol, instrument in zip(symbols, instruments, strict=True)
    )
    quantities = tuple(
        OperationalQuantityConfig(instrument, f"{symbol}-quantity", Decimal("1"))
        for symbol, instrument in zip(symbols, instruments, strict=True)
    )
    return ShadowConfig(
        "shadow-test",
        "test-revision",
        strategies,
        quantities,
        RiskPolicy(
            "test-policy",
            True,
            instruments,
            Decimal("1"),
            2,
            timedelta(minutes=5),
            timedelta(minutes=5),
            timedelta(minutes=1),
            timedelta(minutes=1),
        ),
        "alpaca:iex",
        timedelta(minutes=5),
        timedelta(minutes=1),
        state,
    )


def session(
    *, symbols: tuple[str, ...] = ("AAPL",), state: RiskState | None = None
) -> ShadowSession:
    result = ShadowSession(config(symbols=symbols, state=state))
    result.control("connected", at(0))
    return result


def test_bar_start_translation_and_receive_availability() -> None:
    translated = observation(bar(0, "99"))
    assert translated.observation_time == at(1)
    assert translated.availability_time == at(1, 1)
    assert translated.availability_time >= translated.observation_time


def test_quote_translation_preserves_sizes_and_locked_crossed_states() -> None:
    locked = observation(quote(0, bid="100", ask="100"))
    crossed = observation(quote(1, bid="101", ask="100"))
    assert isinstance(locked, Quote) and isinstance(crossed, Quote)
    assert locked.bid_size == Decimal("4") and locked.ask_size == Decimal("5")
    assert locked.market_state.value == "locked"
    assert crossed.market_state.value == "crossed"


@pytest.mark.parametrize(
    "payload",
    [
        {"T": "q", "S": "AAPL", "bp": "99", "ap": Decimal("100"), "t": "2025-01-02T14:30:00Z"},
        {
            "T": "q",
            "S": "AAPL",
            "bp": Decimal("99"),
            "ap": Decimal("100"),
            "t": "2025-01-02T14:30:00",
        },
    ],
)
def test_malformed_numeric_or_naive_timestamp_reject(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        translate(payload, received_at=at(1), symbols=("AAPL",), feed="iex")


def test_future_provider_time_and_unfinished_bar_reject() -> None:
    with pytest.raises(ValueError, match="after receipt"):
        translate(quote(2), received_at=at(1), symbols=("AAPL",), feed="iex")
    raw = bar(2, "99")
    with pytest.raises(ValueError, match="ends after receipt"):
        translate(raw, received_at=at(2, 30), symbols=("AAPL",), feed="iex")


def test_unsupported_symbol_rejects_explicitly() -> None:
    with pytest.raises(ValueError, match="outside configured scope"):
        translate(quote(0, symbol="MSFT"), received_at=at(1), symbols=("AAPL",), feed="iex")


def _three_bars(shadow: ShadowSession) -> list[ShadowRecord]:
    return [
        shadow.accept(observation(bar(index, close)), f"bar-{index}")
        for index, close in enumerate(("100", "101", "90"))
    ]


def _two_bars_then_quote_then_final_bar(shadow: ShadowSession) -> list[ShadowRecord]:
    records = [
        shadow.accept(observation(bar(index, close)), f"bar-{index}")
        for index, close in enumerate(("100", "101"))
    ]
    shadow.accept(observation(quote(2)), "quote-2")
    records.append(shadow.accept(observation(bar(2, "90")), "bar-2"))
    return records


def test_duplicate_bar_and_quote_are_idempotent() -> None:
    shadow = session()
    raw_quote = quote(0)
    first_quote = observation(raw_quote)
    shadow.accept(first_quote, "quote-0")
    _three_bars(shadow)
    duplicate = shadow.accept(observation(bar(2, "90")), "bar-2")
    duplicate_quote = shadow.accept(
        translate(raw_quote, received_at=at(3, 2), symbols=("AAPL", "MSFT"), feed="iex"), "quote-0"
    )
    assert duplicate.disposition == "duplicate" and duplicate.signal is None
    assert duplicate_quote.disposition == "duplicate"
    assert len([record for record in shadow.records if record.signal is not None]) == 1


def test_same_time_variant_and_delayed_observations_never_rewrite() -> None:
    shadow = session()
    shadow.accept(observation(bar(0, "100")), "bar-0")
    shadow.accept(observation(bar(1, "101")), "bar-1")
    current = observation(quote(2))
    shadow.accept(current, "quote-current")
    variant = observation(quote(2, bid="98"))
    same_time = shadow.accept(variant, "quote-variant")
    old_raw = quote(1)
    older = shadow.accept(
        translate(old_raw, received_at=at(2, 32), symbols=("AAPL", "MSFT"), feed="iex"), "quote-old"
    )
    assert same_time.disposition == "same_time_variant"
    assert older.disposition == "out_of_order"
    shadow.accept(observation(bar(2, "90")), "bar-2")
    assert shadow.records[-1].risk_observability is not None
    assert shadow.records[-1].risk_observability.quote == current


def test_delayed_bar_and_reconnect_redelivery_never_create_another_signal() -> None:
    shadow = session()
    shadow.accept(observation(quote(2)), "quote-2")
    # Deliver the bar sequence chronologically after the quote's receipt for this test.
    for index, close in enumerate(("100", "101", "90"), start=2):
        raw = bar(index, close)
        shadow.accept(
            translate(raw, received_at=at(index + 1, 1), symbols=("AAPL",), feed="iex"),
            f"bar-{index}",
        )
    shadow.control("disconnected", at(6), "test_reconnect")
    shadow.control("connected", at(6, 1))
    late_raw = bar(1, "100")
    delayed = shadow.accept(
        translate(late_raw, received_at=at(6, 2), symbols=("AAPL",), feed="iex"), "late-bar"
    )
    redelivery_raw = bar(4, "90")
    redelivery = shadow.accept(
        translate(redelivery_raw, received_at=at(6, 3), symbols=("AAPL",), feed="iex"), "bar-4"
    )
    assert delayed.disposition == "out_of_order"
    assert redelivery.disposition == "duplicate"
    assert len([record for record in shadow.records if record.signal is not None]) == 1


def test_bars_are_healthy_without_quotes_but_candidate_is_not_risk_ready() -> None:
    shadow = session()
    records = _three_bars(shadow)
    last = records[-1]
    assert shadow.health is FeedHealth.HEALTHY
    assert last.signal is not None
    assert last.risk_observability is not None
    assert last.risk_observability.status is RiskObservabilityStatus.QUOTE_NOT_READY


def test_stale_bar_and_disconnect_are_explicit() -> None:
    shadow = session()
    shadow.accept(observation(bar(0, "100")), "bar")
    tick = shadow.control("tick", at(7))
    disconnected = shadow.control("disconnected", at(7, 1), "test")
    assert tick.health is FeedHealth.STALE
    assert disconnected.health is FeedHealth.DISCONNECTED


def test_symbols_are_isolated() -> None:
    shadow = session(symbols=("AAPL", "MSFT"))
    for index, close in enumerate(("100", "101", "90")):
        raw = bar(index, close, symbol="AAPL")
        translated = observation(raw)
        shadow.accept(translated)
    aapl_signals = [record.signal for record in shadow.records if record.signal is not None]
    assert len(aapl_signals) == 1
    assert aapl_signals[0].instrument == Instrument("AAPL")


def test_invalid_record_does_not_poison_later_valid_input() -> None:
    shadow = session()
    invalid = shadow.invalid(at(0, 1), "malformed_numeric")
    accepted = shadow.accept(observation(bar(0, "100")), "good")
    assert invalid.disposition.startswith("invalid:")
    assert accepted.disposition == "accepted"


def test_replay_and_future_append_preserve_existing_records() -> None:
    shadow = session()
    shadow.accept(observation(quote(0)), "quote")
    _three_bars(shadow)
    captured = shadow.records
    assert replay(shadow.config, captured) == captured
    shadow.accept(observation(bar(3, "91")), "future")
    assert shadow.records[: len(captured)] == captured


def test_optional_pure_risk_evaluation_never_creates_admission_artifact() -> None:
    state = RiskState(
        "shadow-test",
        "supplied-offline-state",
        1,
        True,
        (),
        (),
        at(3),
        at(3),
        OperatorControls(True, False, at(3), at(3)),
    )
    shadow = session(state=state)
    records = _two_bars_then_quote_then_final_bar(shadow)
    observability = records[-1].risk_observability
    assert observability is not None
    assert observability.status is RiskObservabilityStatus.EVALUATED
    assert observability.decision is not None
    assert all(record.__class__.__name__ == "ShadowRecord" for record in shadow.records)


def test_missing_credentials_and_no_gate_or_trading_imports() -> None:
    with pytest.raises(ValueError, match="ALPACA_DATA_KEY"):
        DataCredentials.from_environment({})
    p4a = Path("src/shadow/adapters/alpaca/stream.py").read_text(encoding="utf-8")
    composition = Path("src/shadow/application/shadow.py").read_text(encoding="utf-8")
    assert "RiskGate" not in composition and "claim_for_dispatch" not in composition
    assert "TradingClient" not in p4a and "submit_order" not in p4a
