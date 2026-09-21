"""Hand-authored provider examples and adversarial translation boundaries."""

import ast
import json
import subprocess
import sys
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from shadow.adapters.alpaca.crypto_normalize import normalize, parse_timestamp
from shadow.data.crypto_book import BookDisposition, CryptoBookReducer
from shadow.domain.crypto_market import (
    BookAction,
    BookLevel,
    CryptoBookEvent,
    CryptoQuote,
    CryptoTrade,
    TakerSide,
    UtcNanoseconds,
)
from shadow.domain.market import AvailabilitySemantics, Instrument, Provenance, QuoteMarketState

RECEIPT = UtcNanoseconds(1_700_000_000_123_456_790)
TIMESTAMP = "2023-11-14T22:13:20.123456789Z"


def trade(**changes: object) -> dict[str, object]:
    return {
        "T": "t",
        "S": "BTC/USD",
        "p": "60000.000000000000001",
        "s": "0.000000000123",
        "i": 3447222699101865076,
        "tks": "B",
        "t": TIMESTAMP,
        **changes,
    }


def quote(**changes: object) -> dict[str, object]:
    return {
        "T": "q",
        "S": "BTC/USD",
        "bp": "60000.01",
        "bs": "0.02",
        "ap": "60001.02",
        "as": "0.03",
        "t": TIMESTAMP,
        **changes,
    }


def book(**changes: object) -> dict[str, object]:
    return {
        "T": "o",
        "S": "BTC/USD",
        "b": [{"p": "10", "s": "2"}],
        "a": [{"p": "11", "s": "3"}],
        "t": TIMESTAMP,
        **changes,
    }


@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD"])
@pytest.mark.parametrize(("wire_side", "side"), [("B", TakerSide.BUY), ("S", TakerSide.SELL)])
def test_trade_exact_fields(symbol: str, wire_side: str, side: TakerSide) -> None:
    result = normalize(trade(S=symbol, tks=wire_side), received_at=RECEIPT)
    assert result.source_timestamp == TIMESTAMP
    assert result.event == CryptoTrade(
        Instrument(symbol),
        Decimal("60000.000000000000001"),
        Decimal("0.000000000123"),
        "3447222699101865076",
        side,
        UtcNanoseconds(1_700_000_000_123_456_789),
        RECEIPT,
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("alpaca:crypto:us", "UTC"),
    )


@pytest.mark.parametrize(
    "stamp",
    [
        TIMESTAMP,
        "2023-11-15T03:43:20.123456789+05:30",
        "2023-11-14T18:13:20.123456789-04:00",
        "2023-11-14t22:13:20.123456789z",
        "2023-11-14T22:13:20.123456789+00:00",
    ],
)
def test_timestamp_equivalence_and_original_text(stamp: str) -> None:
    result = normalize(trade(t=stamp), received_at=RECEIPT)
    assert result.event.observation_time.value == 1_700_000_000_123_456_789
    assert result.source_timestamp == stamp
    assert result.event.provenance.source_timezone == ("UTC" if stamp[-1] in "Zz" else stamp[-6:])


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("1970-01-01T00:00:00Z", 0),
        ("1970-01-01T00:00:00.1Z", 100_000_000),
        ("1969-12-31T23:59:59.999999999Z", -1),
        ("0001-01-01T00:00:00Z", -62_135_596_800_000_000_000),
        ("9999-12-31T23:59:59.999999999Z", 253_402_300_799_999_999_999),
        ("2000-02-29T00:00:00Z", 951_782_400_000_000_000),
    ],
)
def test_exact_calendar_boundaries(stamp: str, expected: int) -> None:
    assert parse_timestamp(stamp).value == expected


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        123,
        "",
        "2023-11-14T22:13:20",
        "2023-11-14",
        "2023-11-14T22:13:20.Z",
        "2023-11-14T22:13:20.1234567890Z",
        "2023-02-29T00:00:00Z",
        "1900-02-29T00:00:00Z",
        "2023-11-14T24:00:00Z",
        "2023-11-14T22:60:00Z",
        "2023-11-14T22:13:60Z",
        "2023-11-14T22:13:20+24:00",
        "2023-11-14T22:13:20+01:60",
        "2023-11-14T22:13:20-00:00",
        "2023-11-14T22:13:20Z\n",
        " 2023-11-14T22:13:20Z",
        "2023-11-14 22:13:20Z",
        "0001-01-01T00:00:00+00:01",
        "9999-12-31T23:59:59-00:01",
    ],
)
def test_invalid_timestamp_fails_closed(stamp: object) -> None:
    with pytest.raises(ValueError):
        normalize(trade(t=stamp), received_at=RECEIPT)


@pytest.mark.parametrize("payload", [trade(), quote(), book()])
def test_receipt_boundary_including_one_nanosecond(payload: dict[str, object]) -> None:
    equal = UtcNanoseconds(1_700_000_000_123_456_789)
    assert normalize(payload, received_at=equal).event.availability_time == equal
    with pytest.raises(ValueError, match="availability_time"):
        normalize(payload, received_at=UtcNanoseconds(1_700_000_000_123_456_788))
    with pytest.raises(ValueError, match="timestamp"):
        normalize(payload, received_at=123)  # type: ignore[arg-type]


def test_decimal_json_and_hostile_context_never_round() -> None:
    payload = json.loads(
        '{"T":"t","S":"BTC/USD","p":60000.000000000000001,"s":0.000000000123,'
        '"i":3447222699101865076,"tks":"B","t":"2023-11-14T22:13:20.123456789Z"}',
        parse_float=Decimal,
    )
    with localcontext() as context:
        context.prec = 1
        context.Emax = 1
        context.Emin = -1
        for signal in context.traps:
            context.traps[signal] = True
        result = normalize(payload, received_at=RECEIPT)
    assert isinstance(result.event, CryptoTrade)
    assert result.event.price == Decimal("60000.000000000000001")
    assert result.event.size == Decimal("0.000000000123")
    assert result.event.trade_id == "3447222699101865076"


@pytest.mark.parametrize("value", [60000, Decimal("60000.000"), "6.0000000e4"])
def test_exact_numeric_forms(value: object) -> None:
    event = normalize(trade(p=value), received_at=RECEIPT).event
    assert isinstance(event, CryptoTrade)
    assert event.price == Decimal("60000")


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1.25,
        float("nan"),
        float("inf"),
        None,
        [],
        {},
        "",
        "abc",
        " 1",
        "1 ",
        "1_000",
        "+1",
        ".1",
        "1.",
        "01",
        "NaN",
        "Infinity",
        "-Infinity",
        "1e",
        "-1",
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-1"),
    ],
)
def test_all_numeric_fields_reject_bad_values(value: object) -> None:
    for payload, fields in ((trade(), ("p", "s")), (quote(), ("bp", "bs", "ap", "as"))):
        for field in fields:
            with pytest.raises(ValueError):
                normalize({**payload, field: value}, received_at=RECEIPT)
    for side in ("a", "b"):
        for field in ("p", "s"):
            with pytest.raises(ValueError):
                normalize(book(**{side: [{"p": "1", "s": "2", field: value}]}), received_at=RECEIPT)


@pytest.mark.parametrize("zero", [0, "0", "-0", Decimal("-0")])
def test_zero_prices_and_trade_size_reject(zero: object) -> None:
    for payload, fields in ((trade(), ("p", "s")), (quote(), ("bp", "ap"))):
        for field in fields:
            with pytest.raises(ValueError):
                normalize({**payload, field: zero}, received_at=RECEIPT)
    with pytest.raises(ValueError):
        normalize(book(b=[{"p": zero, "s": "1"}]), received_at=RECEIPT)


@pytest.mark.parametrize("value", [None, "", "buy", "SELL", "X", True, []])
def test_unknown_taker_side_rejects(value: object) -> None:
    with pytest.raises(ValueError, match="taker_side"):
        normalize(trade(tks=value), received_at=RECEIPT)


@pytest.mark.parametrize("value", [None, True, 123.0, Decimal(123), "123", "opaque"])
def test_trade_id_must_match_documented_integer_wire_type(value: object) -> None:
    with pytest.raises(ValueError, match="trade_id"):
        normalize(trade(i=value), received_at=RECEIPT)


@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD"])
def test_quote_preserves_all_fields(symbol: str) -> None:
    assert normalize(quote(S=symbol), received_at=RECEIPT).event == CryptoQuote(
        Instrument(symbol),
        Decimal("60000.01"),
        Decimal("60001.02"),
        Decimal("0.02"),
        Decimal("0.03"),
        UtcNanoseconds(1_700_000_000_123_456_789),
        RECEIPT,
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("alpaca:crypto:us", "UTC"),
    )


@pytest.mark.parametrize(
    ("ask", "state"),
    [
        ("60001", QuoteMarketState.NORMAL),
        ("60000.01", QuoteMarketState.LOCKED),
        ("59999", QuoteMarketState.CROSSED),
    ],
)
def test_quote_relationship_and_zero_sizes_preserved(ask: str, state: QuoteMarketState) -> None:
    event = normalize(quote(ap=ask, bs="0", **{"as": "-0"}), received_at=RECEIPT).event
    assert isinstance(event, CryptoQuote)
    assert event.market_state is state
    assert event.ask_price == Decimal(ask)
    assert event.bid_size == Decimal("0") and event.ask_size.as_tuple().sign == 1


@pytest.mark.parametrize(
    ("extra", "action"),
    [
        ({"r": True}, BookAction.RESET),
        ({"r": False}, BookAction.UPDATE),
        ({}, BookAction.UPDATE),
    ],
)
@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD"])
def test_book_action_and_exact_levels(
    extra: dict[str, object], action: BookAction, symbol: str
) -> None:
    event = normalize(book(S=symbol, **extra), received_at=RECEIPT).event
    assert event == CryptoBookEvent(
        Instrument(symbol),
        action,
        (BookLevel(Decimal("10"), Decimal("2")),),
        (BookLevel(Decimal("11"), Decimal("3")),),
        UtcNanoseconds(1_700_000_000_123_456_789),
        RECEIPT,
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("alpaca:crypto:us", "UTC"),
    )


def test_book_order_zero_deletion_and_empty_arrays_preserved() -> None:
    payload = book(b=[{"p": "9", "s": "0"}, {"p": "10", "s": "2"}], a=[])
    event = normalize(payload, received_at=RECEIPT).event
    assert isinstance(event, CryptoBookEvent)
    assert event.bids == (
        BookLevel(Decimal("9"), Decimal("0")),
        BookLevel(Decimal("10"), Decimal("2")),
    )
    assert event.asks == ()
    assert payload["b"] == [{"p": "9", "s": "0"}, {"p": "10", "s": "2"}]
    for reset in (True, False):
        empty = normalize(book(b=[], a=[], r=reset), received_at=RECEIPT).event
        assert isinstance(empty, CryptoBookEvent)
        assert empty.bids == empty.asks == ()


@pytest.mark.parametrize("side", ["b", "a"])
def test_missing_side_and_duplicate_numeric_price_reject(side: str) -> None:
    payload = book()
    del payload[side]
    with pytest.raises(ValueError, match="side array"):
        normalize(payload, received_at=RECEIPT)
    for reset in (True, False):
        with pytest.raises(ValueError, match="duplicate price"):
            normalize(
                book(r=reset, **{side: [{"p": "10", "s": "2"}, {"p": "10.0", "s": "0"}]}),
                received_at=RECEIPT,
            )


@pytest.mark.parametrize("value", [None, True, {}, (), [1], [[1, 2]], [{"p": "1"}], [{"s": "1"}]])
def test_bad_side_shapes_reject(value: object) -> None:
    for side in ("a", "b"):
        with pytest.raises(ValueError):
            normalize(book(**{side: value}), received_at=RECEIPT)


@pytest.mark.parametrize("reset", [None, "true", 0, 1, [], {}])
def test_non_boolean_reset_rejects(reset: object) -> None:
    with pytest.raises(ValueError, match="reset"):
        normalize(book(r=reset), received_at=RECEIPT)


@pytest.mark.parametrize("symbol", [None, "SOL/USD", "BTCUSD", "btc/usd", "SPY", [], True])
def test_unsupported_symbols_reject(symbol: object) -> None:
    for payload in (trade(S=symbol), quote(S=symbol), book(S=symbol)):
        with pytest.raises(ValueError, match="instrument"):
            normalize(payload, received_at=RECEIPT)


@pytest.mark.parametrize("kind", [None, "b", "success", "error", "subscription", "x", [], True])
def test_unknown_event_type_rejects(kind: object) -> None:
    with pytest.raises(ValueError, match="event_type"):
        normalize(trade(T=kind), received_at=RECEIPT)


@pytest.mark.parametrize("payload", [None, [], [trade()], "json", 1, True, {}])
def test_malformed_provider_shape_rejects(payload: object) -> None:
    with pytest.raises(ValueError):
        normalize(payload, received_at=RECEIPT)


def test_every_required_field_is_required() -> None:
    for example in (trade(), quote(), book()):
        for field in example:
            payload = example.copy()
            del payload[field]
            with pytest.raises(ValueError):
                normalize(payload, received_at=RECEIPT)


@pytest.mark.parametrize("location", ["us-1", "eu-1", "", "US"])
def test_only_us_location(location: str) -> None:
    with pytest.raises(ValueError, match="location"):
        normalize(trade(), received_at=RECEIPT, location=location)


@pytest.mark.parametrize("symbol", ["BTC/USD", "ETH/USD"])
def test_provider_object_to_book_snapshot_and_quote_isolation(symbol: str) -> None:
    reducer = CryptoBookReducer(source="alpaca:crypto:us", instrument=Instrument(symbol))
    reset = normalize(book(S=symbol, r=True), received_at=RECEIPT).event
    assert isinstance(reset, CryptoBookEvent)
    first = reducer.apply(reset)
    assert first.disposition is BookDisposition.RESET_APPLIED
    quoted = normalize(quote(S=symbol), received_at=RECEIPT).event
    assert isinstance(quoted, CryptoQuote)
    with pytest.raises(ValueError, match="CryptoBookEvent"):
        reducer.apply(quoted)  # type: ignore[arg-type]
    assert reducer.snapshot == first.snapshot
    update = normalize(
        book(
            S=symbol,
            t="2023-11-14T22:13:20.123456790Z",
            b=[{"p": "10", "s": "0"}],
            a=[{"p": "11", "s": "7"}],
        ),
        received_at=RECEIPT,
    ).event
    assert isinstance(update, CryptoBookEvent)
    result = reducer.apply(update)
    assert result.disposition is BookDisposition.UPDATE_APPLIED
    assert result.snapshot.bids == ()
    assert result.snapshot.asks == (BookLevel(Decimal("11"), Decimal("7")),)
    assert first.snapshot.bids == (BookLevel(Decimal("10"), Decimal("2")),)


@pytest.mark.parametrize(
    "module", ["shadow.adapters.alpaca.crypto_normalize", "shadow.data.crypto_book"]
)
def test_narrow_imports_and_transitive_authority_boundary(module: str) -> None:
    allowed = {
        "__future__",
        "re",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "shadow.domain.crypto_market",
        "shadow.domain.errors",
        "shadow.domain.market",
    }
    source = Path("src", *module.split(".")).with_suffix(".py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module in allowed
        elif isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; import {module}; "
            "assert not any(n.startswith(('shadow.execution', 'shadow.risk', 'shadow.strategies', "
            "'shadow.application', 'shadow.adapters.alpaca.paper', "
            "'shadow.adapters.alpaca.stream')) "
            "for n in sys.modules); "
            + (
                "assert not any(n.startswith('shadow.adapters') for n in sys.modules)"
                if module == "shadow.data.crypto_book"
                else ""
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
