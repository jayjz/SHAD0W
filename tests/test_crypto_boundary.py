from decimal import Decimal

import pytest

from shadow.application.crypto_session import CryptoSession, SessionDisposition
from shadow.domain.crypto_market import CryptoQuote, CryptoTrade, TakerSide, UtcNanoseconds
from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import AvailabilitySemantics, Instrument, Provenance


def context(
    symbol: str,
) -> tuple[Instrument, UtcNanoseconds, UtcNanoseconds, AvailabilitySemantics, Provenance]:
    return (
        Instrument(symbol),
        UtcNanoseconds(1),
        UtcNanoseconds(2),
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("alpaca:crypto:us", "UTC"),
    )


def test_trade_quote_are_observations_and_books_are_isolated() -> None:
    session = CryptoSession()
    session.establish_connection()
    btc = CryptoTrade(
        context("BTC/USD")[0],
        Decimal("1"),
        Decimal("1"),
        "t",
        TakerSide.BUY,
        *context("BTC/USD")[1:],
    )
    eth = CryptoQuote(
        context("ETH/USD")[0],
        Decimal("1"),
        Decimal("2"),
        Decimal("0"),
        Decimal("0"),
        *context("ETH/USD")[1:],
    )
    assert session.process(btc).disposition is SessionDisposition.TRADE_OBSERVED
    assert session.process(eth).disposition is SessionDisposition.QUOTE_OBSERVED
    btc_snapshot = session.snapshot(Instrument("BTC/USD"))
    eth_snapshot = session.snapshot(Instrument("ETH/USD"))
    assert btc_snapshot is not None and btc_snapshot.bids == ()
    assert eth_snapshot is not None and eth_snapshot.bids == ()


def test_disconnect_requires_new_connection_before_events() -> None:
    session = CryptoSession()
    session.establish_connection()
    session.disconnect()
    event = CryptoTrade(
        context("BTC/USD")[0],
        Decimal("1"),
        Decimal("1"),
        "t",
        TakerSide.BUY,
        *context("BTC/USD")[1:],
    )
    with pytest.raises(MarketDataValidationError, match="connection"):
        session.process(event)
