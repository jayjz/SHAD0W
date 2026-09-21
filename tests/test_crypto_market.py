"""Independent adversarial checks for C0 values, without provider or reducer code."""

import ast
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from shadow.domain.crypto_market import (
    BookAction,
    BookLevel,
    BookQuality,
    CryptoBookEvent,
    CryptoQuote,
    CryptoTrade,
    TakerSide,
    UtcNanoseconds,
)
from shadow.domain.market import AvailabilitySemantics, Instrument, Provenance, QuoteMarketState


def trade() -> CryptoTrade:
    return CryptoTrade(
        Instrument("BTC/USD"),
        Decimal("60000.000000001"),
        Decimal("0.00001"),
        "opaque-123",
        TakerSide.BUY,
        UtcNanoseconds(1_700_000_000_123_456_789),
        UtcNanoseconds(1_700_000_000_123_456_790),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("synthetic", "UTC"),
    )


def quote() -> CryptoQuote:
    return CryptoQuote(
        Instrument("ETH/USD"),
        Decimal("2000"),
        Decimal("2001"),
        Decimal("0"),
        Decimal("2"),
        UtcNanoseconds(10),
        UtcNanoseconds(11),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("synthetic", "UTC"),
    )


def book() -> CryptoBookEvent:
    return CryptoBookEvent(
        Instrument("BTC/USD"),
        BookAction.RESET,
        (BookLevel(Decimal("10"), Decimal("2")),),
        (),
        UtcNanoseconds(10),
        UtcNanoseconds(11),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("synthetic", "UTC"),
    )


@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD"])
def test_supported_identity_preserves_all_event_types(symbol: str) -> None:
    for event in (trade(), quote(), book()):
        assert replace(event, instrument=Instrument(symbol)).instrument.identifier == symbol


@pytest.mark.parametrize("symbol", ["BTCUSD", "btc/usd", "SPY", "SOL/USD", "ETH/USDT"])
def test_other_instruments_are_outside_c1(symbol: str) -> None:
    for event in (trade(), quote(), book()):
        with pytest.raises(ValueError, match="instrument"):
            replace(event, instrument=Instrument(symbol))


def test_nanoseconds_are_exact_and_independent_of_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 2
        event = trade()
    assert event.observation_time.value == 1_700_000_000_123_456_789
    assert event.availability_time.value - event.observation_time.value == 1
    assert event.price == Decimal("60000.000000001")
    assert UtcNanoseconds(-1) < UtcNanoseconds(0) < UtcNanoseconds(1)


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1.0,
        "1",
        Decimal(1),
        datetime(1970, 1, 1, tzinfo=UTC),
        -62_135_596_800_000_000_001,
        253_402_300_800_000_000_000,
    ],
)
def test_time_requires_exact_integer_utc_not_implicit_conversion(value: object) -> None:
    with pytest.raises(ValueError, match="utc_nanoseconds"):
        UtcNanoseconds(value)  # type: ignore[arg-type]


def test_time_calendar_boundaries() -> None:
    assert UtcNanoseconds(-62_135_596_800_000_000_000).value == -62_135_596_800_000_000_000
    assert UtcNanoseconds(253_402_300_799_999_999_999).value == 253_402_300_799_999_999_999


def test_availability_may_equal_but_never_precede_observation() -> None:
    for event in (trade(), quote(), book()):
        assert replace(event, availability_time=event.observation_time)
        with pytest.raises(ValueError, match="availability_time"):
            replace(event, availability_time=UtcNanoseconds(event.observation_time.value - 1))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instrument", "BTC/USD"),
        ("provenance", "synthetic"),
        ("observation_time", 10),
        ("availability_time", 11),
        ("availability_semantics", "system_received"),
        ("availability_semantics", AvailabilitySemantics.MODELED),
    ],
)
def test_common_boundary_rejects_untyped_or_untruthful_context(field: str, value: object) -> None:
    for event in (trade(), quote(), book()):
        with pytest.raises(ValueError):
            replace(event, **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        1,
        1.0,
        True,
        "1",
    ],
)
def test_invalid_numbers_reject_in_every_numeric_field(value: object) -> None:
    for event, fields in (
        (trade(), ("price", "size")),
        (quote(), ("bid_price", "ask_price", "bid_size", "ask_size")),
        (BookLevel(Decimal(1), Decimal(1)), ("price", "size")),
    ):
        for field in fields:
            with pytest.raises(ValueError):
                replace(event, **{field: value})  # type: ignore[arg-type]


def test_zero_policy_distinguishes_prices_trades_and_displayed_sizes() -> None:
    for zero in (Decimal("0"), Decimal("-0")):
        for event, field in (
            (trade(), "price"),
            (trade(), "size"),
            (quote(), "bid_price"),
            (quote(), "ask_price"),
            (BookLevel(Decimal(1), Decimal(1)), "price"),
        ):
            with pytest.raises(ValueError):
                replace(event, **{field: zero})  # type: ignore[arg-type]
        assert replace(quote(), bid_size=zero, ask_size=zero).bid_size == 0
        assert BookLevel(Decimal(1), zero).size == 0


@pytest.mark.parametrize(
    ("ask", "state"),
    [
        ("2001", QuoteMarketState.NORMAL),
        ("2000", QuoteMarketState.LOCKED),
        ("1999", QuoteMarketState.CROSSED),
    ],
)
def test_quote_preserves_market_relationship(ask: str, state: QuoteMarketState) -> None:
    event = replace(quote(), ask_price=Decimal(ask))
    assert event.ask_price == Decimal(ask)
    assert event.market_state is state


@pytest.mark.parametrize(
    ("field", "value"),
    [("trade_id", ""), ("trade_id", " x "), ("trade_id", 123), ("taker_side", "buy")],
)
def test_trade_identity_and_side_are_explicit(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(trade(), **{field: value})  # type: ignore[arg-type]


def test_book_action_and_quality_are_distinct_closed_types() -> None:
    reset = book()
    update = replace(reset, action=BookAction.UPDATE)
    assert reset != update
    assert reset.action is BookAction.RESET
    assert update.action is BookAction.UPDATE
    for invalid in ("reset", True, BookQuality.RESET_SNAPSHOT):
        with pytest.raises(ValueError, match="book_action"):
            replace(reset, action=invalid)  # type: ignore[arg-type]
    assert {quality.value for quality in BookQuality} == {
        "unknown",
        "reset_snapshot",
        "reconstructed_unverified",
        "untrusted",
    }
    for invalid_quality in ("healthy", "trusted", "reset", 1, None):
        with pytest.raises(ValueError):
            BookQuality(invalid_quality)  # type: ignore[arg-type]


def test_book_levels_preserve_order_and_require_immutable_unambiguous_sides() -> None:
    levels = (BookLevel(Decimal(10), Decimal(0)), BookLevel(Decimal(11), Decimal(3)))
    for action in BookAction:
        assert replace(book(), action=action, bids=levels).bids == levels
        assert replace(book(), action=action, bids=(), asks=()).bids == ()
    for side in ("bids", "asks"):
        for invalid in (
            None,
            list(levels),
            ("level",),
            (levels[0], BookLevel(Decimal("10.0"), Decimal(7))),
        ):
            with pytest.raises(ValueError, match=side):
                replace(book(), **{side: invalid})  # type: ignore[arg-type]


def test_values_and_nested_levels_are_immutable() -> None:
    for value, field in (
        (trade(), "size"),
        (quote(), "bid_size"),
        (book(), "bids"),
        (book().bids[0], "size"),
        (UtcNanoseconds(1), "value"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, None)


def test_domain_import_boundary_has_no_execution_or_risk_dependency() -> None:
    source = Path("src/shadow/domain/crypto_market.py").read_text(encoding="utf-8")
    allowed = {
        "__future__",
        "dataclasses",
        "decimal",
        "enum",
        "shadow.domain.errors",
        "shadow.domain.market",
    }
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module in allowed
        elif isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
    # Fresh interpreter checks transitive package initialization as well.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import shadow.domain.crypto_market; "
            "assert not any(n.startswith(('shadow.execution', 'shadow.risk', 'shadow.strategies', "
            "'shadow.adapters', 'shadow.application')) for n in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
