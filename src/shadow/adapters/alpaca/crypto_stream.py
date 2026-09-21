"""Strict Alpaca ``crypto/us`` stream protocol helpers; no trading surface."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

ENDPOINT = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
SYMBOLS = ("BTC/USD", "ETH/USD")
CHANNELS = ("trades", "quotes", "orderbooks")


class CryptoStreamError(ValueError):
    """Sanitized protocol failure; never include a provider payload."""


@dataclass(frozen=True, slots=True)
class CryptoDataCredentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.secret.strip():
            raise CryptoStreamError("market-data credentials are required")

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> CryptoDataCredentials:
        return cls(
            env.get("ALPACA_DATA_KEY", env.get("ALPACA_API_KEY_ID", "")),
            env.get("ALPACA_DATA_SECRET", env.get("ALPACA_API_SECRET_KEY", "")),
        )


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CryptoStreamError("duplicate JSON key")
        result[key] = value
    return result


def decode_frame(raw: str | bytes) -> list[dict[str, object]]:
    """Decode a provider frame exactly, preserving Decimal fractional values."""
    try:
        value = json.loads(raw, parse_float=Decimal, object_pairs_hook=_no_duplicates)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError, CryptoStreamError) as exc:
        raise CryptoStreamError("malformed provider frame") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CryptoStreamError("provider frame must be an array of objects")
    return value


def auth_request(credentials: CryptoDataCredentials) -> str:
    return json.dumps({"action": "auth", "key": credentials.key, "secret": credentials.secret})


def subscription_request() -> str:
    return json.dumps({"action": "subscribe", **{channel: list(SYMBOLS) for channel in CHANNELS}})


def is_success(frame: list[dict[str, object]], message: str) -> bool:
    return frame == [{"T": "success", "msg": message}]


def subscription_is_exact(frame: list[dict[str, object]]) -> bool:
    if len(frame) != 1 or frame[0].get("T") != "subscription":
        return False
    message = frame[0]
    expected = set(SYMBOLS)
    allowed = {"T", *CHANNELS, "bars", "updatedBars", "dailyBars"}
    if set(message) - allowed:
        return False
    for channel in CHANNELS:
        value = message.get(channel)
        if not isinstance(value, list) or set(value) != expected or len(value) != len(expected):
            return False
    return all(message.get(channel, []) == [] for channel in ("bars", "updatedBars", "dailyBars"))
