"""Independent expected depth/state transitions, without provider dependencies."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from shadow.data.crypto_book import BookDisposition, CryptoBookReducer
from shadow.domain.crypto_market import (
    BookAction,
    BookLevel,
    BookQuality,
    CryptoBookEvent,
    UtcNanoseconds,
)
from shadow.domain.market import AvailabilitySemantics, Instrument, Provenance, QuoteMarketState


def levels(*pairs: tuple[str, str]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(Decimal(price), Decimal(size)) for price, size in pairs)


def event(
    time: int = 10,
    *,
    action: BookAction = BookAction.RESET,
    bids: tuple[BookLevel, ...] = (),
    asks: tuple[BookLevel, ...] = (),
    symbol: str = "BTC/USD",
    source: str = "synthetic:crypto:us",
) -> CryptoBookEvent:
    return CryptoBookEvent(
        Instrument(symbol),
        action,
        bids,
        asks,
        UtcNanoseconds(time),
        UtcNanoseconds(time + 1),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance(source, "UTC"),
    )


def reducer(symbol: str = "BTC/USD", source: str = "synthetic:crypto:us") -> CryptoBookReducer:
    return CryptoBookReducer(source=source, instrument=Instrument(symbol))


def seeded() -> CryptoBookReducer:
    book = reducer()
    book.apply(event(bids=levels(("10", "2"), ("9", "3")), asks=levels(("11", "4"), ("12", "5"))))
    return book


def test_initial_unknown_and_update_before_reset() -> None:
    book = reducer()
    original = book.snapshot
    assert original.instrument == Instrument("BTC/USD")
    assert original.source == "synthetic:crypto:us"
    assert original.quality is BookQuality.UNKNOWN
    assert original.bids == original.asks == ()
    assert original.accepted_time is None and original.reset_epoch == 0
    for _ in range(2):
        result = book.apply(event(action=BookAction.UPDATE, bids=levels(("10", "2"))))
        assert result.disposition is BookDisposition.AWAITING_RESET
        assert result.snapshot == original


def test_reset_constructs_sorted_exact_state_without_changing_event() -> None:
    book = reducer()
    incoming = event(
        bids=levels(("9", "3"), ("10", "2"), ("8", "0")),
        asks=levels(("12", "5"), ("11", "4"), ("13", "-0")),
    )
    result = book.apply(incoming)
    assert result.disposition is BookDisposition.RESET_APPLIED
    assert result.snapshot.quality is BookQuality.RESET_SNAPSHOT
    assert result.snapshot.bids == levels(("10", "2"), ("9", "3"))
    assert result.snapshot.asks == levels(("11", "4"), ("12", "5"))
    assert result.snapshot.accepted_time == UtcNanoseconds(10)
    assert result.snapshot.reset_epoch == 1
    assert incoming.bids == levels(("9", "3"), ("10", "2"), ("8", "0"))


def test_reset_replaces_every_old_level_even_with_empty_sides() -> None:
    book = seeded()
    result = book.apply(event(20, bids=levels(("7", "9")), asks=levels(("11", "0"))))
    assert result.snapshot.bids == levels(("7", "9")) and result.snapshot.asks == ()
    assert result.snapshot.reset_epoch == 2
    assert result.snapshot.quality is BookQuality.RESET_SNAPSHOT
    empty = book.apply(event(21)).snapshot
    assert empty.bids == empty.asks == () and empty.reset_epoch == 3
    assert empty.market_state is None


def test_update_add_replace_delete_absolute_sizes_and_sorted_projection() -> None:
    book = seeded()
    result = book.apply(
        event(
            11,
            action=BookAction.UPDATE,
            bids=levels(("8", "6"), ("10", "7"), ("9", "0")),
            asks=levels(("14", "8"), ("12", "9"), ("11", "0")),
        )
    )
    assert result.disposition is BookDisposition.UPDATE_APPLIED
    assert result.snapshot.quality is BookQuality.RECONSTRUCTED_UNVERIFIED
    assert result.snapshot.bids == levels(("10", "7"), ("8", "6"))
    assert result.snapshot.asks == levels(("12", "9"), ("14", "8"))
    assert result.snapshot.accepted_time == UtcNanoseconds(11) and result.snapshot.reset_epoch == 1


@pytest.mark.parametrize("reconstructed", [False, True])
def test_absent_delete_and_identical_assignment_are_evidenced_noops(reconstructed: bool) -> None:
    book = seeded()
    if reconstructed:
        book.apply(event(11, action=BookAction.UPDATE, bids=levels(("8", "1"))))
    before = book.snapshot
    result = book.apply(
        event(
            12,
            action=BookAction.UPDATE,
            bids=levels(("10.0", "2.0"), ("7", "-0")),
            asks=levels(("11", "4"), ("15", "0")),
        )
    )
    assert result.disposition is BookDisposition.NO_CHANGE
    assert result.absent_deletions == 2 and result.repeated_assignments == 2
    assert result.snapshot.bids == before.bids and result.snapshot.asks == before.asks
    assert result.snapshot.quality is before.quality
    assert result.snapshot.accepted_time == UtcNanoseconds(12)
    empty = book.apply(event(13, action=BookAction.UPDATE))
    assert empty.disposition is BookDisposition.NO_CHANGE
    assert empty.absent_deletions == empty.repeated_assignments == 0


@pytest.mark.parametrize(
    ("ask", "state"),
    [
        ("11", QuoteMarketState.NORMAL),
        ("10", QuoteMarketState.LOCKED),
        ("9", QuoteMarketState.CROSSED),
    ],
)
def test_locked_crossed_depth_preserved(ask: str, state: QuoteMarketState) -> None:
    book = reducer()
    reset = book.apply(event(bids=levels(("10", "2")), asks=levels((ask, "4"), ("12", "6"))))
    assert reset.snapshot.market_state is state
    assert reset.snapshot.asks == levels((ask, "4"), ("12", "6"))
    update = book.apply(event(11, action=BookAction.UPDATE, bids=levels(("10", "3"))))
    assert update.snapshot.market_state is state
    assert update.snapshot.quality is BookQuality.RECONSTRUCTED_UNVERIFIED


def test_source_location_and_instrument_are_completely_isolated() -> None:
    btc, eth = seeded(), reducer("ETH/USD")
    other_location = reducer(source="synthetic:crypto:eu")
    other_provider = reducer(source="another:crypto:us")
    btc_before = btc.snapshot
    for instance, symbol, source in (
        (eth, "ETH/USD", "synthetic:crypto:us"),
        (other_location, "BTC/USD", "synthetic:crypto:eu"),
        (other_provider, "BTC/USD", "another:crypto:us"),
    ):
        incoming = event(symbol=symbol, source=source, bids=levels(("99", "1")))
        instance.apply(incoming)
        with pytest.raises(ValueError, match="scope"):
            btc.apply(incoming)
        assert btc.snapshot == btc_before
    btc.apply(event(11, action=BookAction.UPDATE, bids=levels(("10", "0"))))
    assert (
        eth.snapshot.bids
        == other_location.snapshot.bids
        == other_provider.snapshot.bids
        == levels(("99", "1"))
    )


@pytest.mark.parametrize("action", list(BookAction))
def test_older_event_never_regresses_watermark_and_invalidates(action: BookAction) -> None:
    book = seeded()
    before = book.snapshot
    result = book.apply(event(9, action=action, bids=levels(("99", "7"))))
    assert result.disposition is BookDisposition.UNTRUSTED_OLDER_EVENT
    assert result.snapshot == replace(before, quality=BookQuality.UNTRUSTED)
    ignored = book.apply(event(20, action=BookAction.UPDATE, bids=levels(("99", "8"))))
    assert ignored.disposition is BookDisposition.AWAITING_RESET
    assert ignored.snapshot == result.snapshot
    recovered = book.apply(event(21, bids=levels(("7", "4"))))
    assert recovered.disposition is BookDisposition.RESET_APPLIED
    assert recovered.snapshot.quality is BookQuality.RESET_SNAPSHOT
    assert recovered.snapshot.reset_epoch == 2
    assert recovered.snapshot.bids == levels(("7", "4")) and recovered.snapshot.asks == ()


@pytest.mark.parametrize("action", list(BookAction))
def test_same_time_conflict_retains_depth_and_requires_later_reset(action: BookAction) -> None:
    book = seeded()
    before = book.snapshot
    result = book.apply(event(action=action, bids=levels(("10", "99"))))
    assert result.disposition is BookDisposition.UNTRUSTED_SAME_TIME_CONFLICT
    assert result.snapshot == replace(before, quality=BookQuality.UNTRUSTED)
    # Returning to the original reset at the disputed timestamp proves no ordering.
    retry = book.apply(
        event(bids=levels(("10", "2"), ("9", "3")), asks=levels(("11", "4"), ("12", "5")))
    )
    assert retry.disposition is BookDisposition.UNTRUSTED_SAME_TIME_CONFLICT
    assert retry.snapshot == result.snapshot
    assert book.apply(event(11)).snapshot.quality is BookQuality.RESET_SNAPSHOT


def test_same_timestamp_different_update_payload_even_if_both_are_noops_is_ambiguous() -> None:
    book = seeded()
    accepted = book.apply(event(11, action=BookAction.UPDATE, bids=levels(("10", "2"))))
    assert accepted.disposition is BookDisposition.NO_CHANGE
    conflict = book.apply(event(11, action=BookAction.UPDATE, asks=levels(("11", "4"))))
    assert conflict.disposition is BookDisposition.UNTRUSTED_SAME_TIME_CONFLICT


@pytest.mark.parametrize("action", list(BookAction))
def test_immediate_repeated_payload_has_no_second_transition(action: BookAction) -> None:
    book = seeded()
    incoming = event(11, action=action, bids=levels(("10", "6")))
    first = book.apply(incoming)
    repeated = book.apply(replace(incoming, availability_time=UtcNanoseconds(100)))
    assert repeated.disposition is BookDisposition.IMMEDIATE_REPEAT
    assert repeated.snapshot is first.snapshot
    assert book.apply(incoming).snapshot is first.snapshot


def test_nonconsecutive_identical_payload_is_not_globally_deduplicated() -> None:
    book = seeded()
    incoming = event(11, action=BookAction.UPDATE, bids=levels(("10", "6")))
    book.apply(incoming)
    book.apply(event(12, action=BookAction.UPDATE, bids=levels(("10", "7"))))
    result = book.apply(incoming)
    assert result.disposition is BookDisposition.UNTRUSTED_OLDER_EVENT
    assert result.snapshot.bids == levels(("10", "7"), ("9", "3"))
    # A later delivery with identical depth assignments still follows chronology.
    recovery = book.apply(event(13, bids=levels(("10", "6"))))
    assert recovery.disposition is BookDisposition.RESET_APPLIED
    later = book.apply(
        replace(incoming, observation_time=UtcNanoseconds(14), availability_time=UtcNanoseconds(15))
    )
    assert later.disposition is BookDisposition.NO_CHANGE
    assert later.snapshot.accepted_time == UtcNanoseconds(14)


@pytest.mark.parametrize(
    "field,value",
    [
        ("asks", None),
        ("asks", []),
        ("asks", ("bad",)),
        ("asks", levels(("11", "4"), ("11.0", "5"))),
        ("action", "update"),
        ("observation_time", 11),
        ("availability_time", UtcNanoseconds(1)),
        ("availability_semantics", AvailabilitySemantics.MODELED),
        ("provenance", None),
    ],
)
@pytest.mark.parametrize("action", list(BookAction))
def test_malformed_event_never_partially_applies(
    field: str, value: object, action: BookAction
) -> None:
    book = seeded()
    before = book.snapshot
    malformed = event(11, action=action, bids=levels(("10", "99")))
    object.__setattr__(malformed, field, value)
    result = book.apply(malformed)
    assert result.disposition is BookDisposition.UNTRUSTED_MALFORMED_EVENT
    assert result.error
    assert result.snapshot == replace(before, quality=BookQuality.UNTRUSTED)
    assert before.quality is BookQuality.RESET_SNAPSHOT


@pytest.mark.parametrize(
    "field,value", [("price", Decimal("sNaN")), ("size", Decimal("-1")), ("price", 1.0)]
)
def test_malformed_nested_level_is_validated_before_any_commit(field: str, value: object) -> None:
    book = seeded()
    before = book.snapshot
    malformed = event(11, bids=levels(("100", "99")), asks=levels(("101", "2")))
    object.__setattr__(malformed.asks[0], field, value)
    result = book.apply(malformed)
    assert result.disposition is BookDisposition.UNTRUSTED_MALFORMED_EVENT
    assert result.snapshot == replace(before, quality=BookQuality.UNTRUSTED)


def test_normalizer_failure_can_invalidate_attributed_book_without_partial_mutation() -> None:
    book = seeded()
    before = book.snapshot
    result = book.invalidate("missing provider ask side")
    assert result.error == "missing provider ask side"
    assert result.snapshot == replace(before, quality=BookQuality.UNTRUSTED)
    assert (
        book.apply(event(11, action=BookAction.UPDATE)).disposition
        is BookDisposition.AWAITING_RESET
    )
    assert book.apply(event(12)).snapshot.quality is BookQuality.RESET_SNAPSHOT


def test_returned_snapshots_and_levels_remain_immutable_across_future_updates() -> None:
    book = seeded()
    old = book.snapshot
    book.apply(
        event(11, action=BookAction.UPDATE, bids=levels(("10", "99")), asks=levels(("11", "0")))
    )
    book.apply(event(12))
    assert old.bids == levels(("10", "2"), ("9", "3"))
    assert old.asks == levels(("11", "4"), ("12", "5"))
    assert old.accepted_time == UtcNanoseconds(10) and old.reset_epoch == 1
    with pytest.raises(FrozenInstanceError):
        old.quality = BookQuality.UNKNOWN  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        old.bids[0].size = Decimal("0")  # type: ignore[misc]


def test_hostile_decimal_context_does_not_change_reconstruction() -> None:
    with localcontext() as context:
        context.prec = 1
        context.Emax = 1
        context.Emin = -1
        for signal in context.traps:
            context.traps[signal] = True
        book = reducer()
        book.apply(event(bids=levels(("60000.000000000000001", "0.000000000123"), ("60000", "2"))))
        result = book.apply(
            event(
                11,
                action=BookAction.UPDATE,
                bids=levels(("60000.000000000000001", "0.000000000456")),
                asks=levels(("60001.000000000000001", "0.000000000789")),
            )
        )
    assert result.snapshot.bids == levels(
        ("60000.000000000000001", "0.000000000456"), ("60000", "2")
    )
    assert result.snapshot.asks == levels(("60001.000000000000001", "0.000000000789"))


def test_quotes_cannot_enter_reducer_or_change_book() -> None:
    book = seeded()
    before = book.snapshot
    with pytest.raises(ValueError, match="CryptoBookEvent"):
        book.apply(object())  # type: ignore[arg-type]
    assert book.snapshot == before


@pytest.mark.parametrize("symbol", ["SPY", "SOL/USD", "BTCUSD"])
def test_unsupported_reducer_instrument_rejects(symbol: str) -> None:
    with pytest.raises(ValueError, match="instrument"):
        reducer(symbol)
