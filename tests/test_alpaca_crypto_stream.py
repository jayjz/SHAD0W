import json
from decimal import Decimal

import pytest

from shadow.adapters.alpaca.crypto_stream import (
    ENDPOINT,
    CryptoDataCredentials,
    CryptoStreamError,
    auth_request,
    decode_frame,
    subscription_is_exact,
    subscription_request,
)


def test_endpoint_requests_and_credentials_are_data_only() -> None:
    credentials = CryptoDataCredentials("key", "secret")
    assert ENDPOINT == "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
    assert json.loads(auth_request(credentials)) == {
        "action": "auth",
        "key": "key",
        "secret": "secret",
    }
    assert credentials.secret not in repr(credentials)
    assert json.loads(subscription_request()) == {
        "action": "subscribe",
        "trades": ["BTC/USD", "ETH/USD"],
        "quotes": ["BTC/USD", "ETH/USD"],
        "orderbooks": ["BTC/USD", "ETH/USD"],
    }


def test_decode_is_decimal_preserving_and_rejects_bad_frame_shapes() -> None:
    frame = decode_frame('[{"T":"q","bp":1.000000000000000001}]')
    assert frame[0]["bp"] == Decimal("1.000000000000000001")
    for raw in ('{"T":"q"}', '[{"T":"q","T":"x"}]', "[1]"):
        with pytest.raises(CryptoStreamError):
            decode_frame(raw)


def test_subscription_acknowledgement_must_be_exact() -> None:
    exact: list[dict[str, object]] = [
        {
            "T": "subscription",
            "trades": ["BTC/USD", "ETH/USD"],
            "quotes": ["BTC/USD", "ETH/USD"],
            "orderbooks": ["BTC/USD", "ETH/USD"],
            "bars": [],
            "updatedBars": [],
            "dailyBars": [],
        }
    ]
    assert subscription_is_exact(exact)
    assert not subscription_is_exact([{**exact[0], "orderbooks": ["BTC/USD"]}])
    assert not subscription_is_exact([{**exact[0], "bars": ["BTC/USD"]}])
