"""Bounded Alpaca market-data-only WebSocket client; it has no trading surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlparse

from shadow.adapters.alpaca.normalize import SUPPORTED_FEEDS, symbols_checked, translate
from shadow.application.evidence import EvidenceWriter, line
from shadow.application.shadow import FeedHealth, ShadowSession


@dataclass(frozen=True, slots=True)
class DataCredentials:
    key: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.secret.strip():
            raise ValueError("ALPACA_DATA_KEY and ALPACA_DATA_SECRET are required")

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> DataCredentials:
        return cls(env.get("ALPACA_DATA_KEY", ""), env.get("ALPACA_DATA_SECRET", ""))


class Socket(Protocol):
    async def recv(self) -> str | bytes: ...
    async def send(self, message: str) -> None: ...


def decode_frame(raw: str | bytes) -> list[dict[str, object]]:
    value = json.loads(raw, parse_float=Decimal, parse_constant=lambda _: None)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, dict) for item in value)
    ):
        raise ValueError("invalid provider frame")
    return value


def _reference(payload: object) -> str:
    return hashlib.sha256(line(payload).encode("utf-8")).hexdigest()


_OPTIONAL_SUBSCRIPTION_CHANNELS = (
    "trades",
    "updatedBars",
    "dailyBars",
    "statuses",
    "lulds",
    "corrections",
    "cancelErrors",
)


class _SetupFailure(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _subscription_is_exact(frame: list[dict[str, object]], symbols: tuple[str, ...]) -> bool:
    if len(frame) != 1 or frame[0].get("T") != "subscription":
        return False
    message = frame[0]
    if "bars" not in message or "quotes" not in message:
        return False
    if _string_set(message["bars"]) != set(symbols):
        return False
    if _string_set(message["quotes"]) != set(symbols):
        return False
    return all(
        channel not in message or not _string_set(message[channel])
        for channel in _OPTIONAL_SUBSCRIPTION_CHANNELS
    )


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("invalid provider subscription acknowledgement")
    if len(value) != len(set(value)):
        raise ValueError("duplicate provider subscription acknowledgement")
    return set(value)


async def _expect(socket: Socket, message: str, rejection_reason: str) -> None:
    try:
        frame = decode_frame(await asyncio.wait_for(socket.recv(), 5))
    except (TypeError, ValueError):
        raise _SetupFailure(rejection_reason) from None
    if frame == [{"T": "success", "msg": message}]:
        return
    if len(frame) == 1 and frame[0].get("T") == "error":
        if frame[0].get("code") == 406:
            raise _SetupFailure("authentication_connection_limit")
        raise _SetupFailure(rejection_reason)
    raise _SetupFailure(rejection_reason)


def _provider_error_disposition(payload: Mapping[str, object]) -> str:
    """Classify provider errors without retaining messages or authentication data."""
    code = payload.get("code")
    if code == 406:
        return "authentication_connection_limit"
    if code in (401, 402, 403):
        return "authentication_rejected"
    return "provider_error"


async def consume(
    socket: Socket,
    session: ShadowSession,
    credentials: DataCredentials,
    writer: EvidenceWriter,
    *,
    feed: str,
    clock: Callable[[], datetime],
) -> None:
    """Authenticate, confirm exact scope, and process data in receive order."""
    symbols = symbols_checked(
        tuple(strategy.instrument.identifier for strategy in session.config.strategies)
    )
    await _expect(socket, "connected", "connection_rejected")
    await socket.send(
        json.dumps({"action": "auth", "key": credentials.key, "secret": credentials.secret})
    )
    await _expect(socket, "authenticated", "authentication_rejected")
    await socket.send(json.dumps({"action": "subscribe", "bars": symbols, "quotes": symbols}))
    try:
        acknowledgement = decode_frame(await asyncio.wait_for(socket.recv(), 5))
        subscription_is_exact = _subscription_is_exact(acknowledgement, symbols)
    except (TypeError, ValueError):
        raise _SetupFailure("subscription_mismatch") from None
    if not subscription_is_exact:
        raise _SetupFailure("subscription_mismatch")
    writer.write(session.control("connected", clock()))

    while True:
        try:
            raw = await asyncio.wait_for(socket.recv(), 1)
        except TimeoutError:
            writer.write(session.control("tick", clock(), "receive_timeout"))
            continue
        received = clock()  # Application receipt, never a provider-time approximation.
        try:
            frame = decode_frame(raw)
        except (TypeError, ValueError):
            writer.write(session.invalid(received, "malformed_frame", _reference(str(raw))))
            continue
        for payload in frame:
            reference = _reference(payload)
            message_type = payload.get("T")
            if message_type == "error":
                writer.write(
                    session.control("failed", received, _provider_error_disposition(payload))
                )
                raise ValueError("provider returned a market-data error")
            if message_type not in ("b", "q"):
                writer.write(session.invalid(received, "unexpected_message", reference))
                continue
            try:
                observation = translate(payload, received_at=received, symbols=symbols, feed=feed)
            except ValueError as error:
                writer.write(session.invalid(received, str(error), reference))
                continue
            writer.write(session.accept(observation, reference))


def _local_feed_url_checked(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "ws" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("local feed URL must use ws and bind to localhost")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("local feed URL must not contain credentials or query data")
    if parsed.path not in ("", "/"):
        raise ValueError("local feed URL must use the relay data path")
    return url


async def consume_local(
    socket: Socket,
    session: ShadowSession,
    writer: EvidenceWriter,
    *,
    feed: str,
    clock: Callable[[], datetime],
) -> None:
    """Consume the relay's provider-frame transport without local credentials."""
    symbols = symbols_checked(
        tuple(strategy.instrument.identifier for strategy in session.config.strategies)
    )
    await socket.send(json.dumps({"action": "subscribe", "bars": symbols, "quotes": symbols}))
    try:
        acknowledgement = decode_frame(await asyncio.wait_for(socket.recv(), 5))
        subscription_is_exact = _subscription_is_exact(acknowledgement, symbols)
    except (TypeError, ValueError):
        raise _SetupFailure("local_subscription_mismatch") from None
    if not subscription_is_exact:
        raise _SetupFailure("local_subscription_mismatch")
    writer.write(session.control("connected", clock(), "local_relay"))

    while True:
        try:
            raw = await asyncio.wait_for(socket.recv(), 1)
        except TimeoutError:
            writer.write(session.control("tick", clock(), "receive_timeout"))
            continue
        received = clock()
        try:
            frame = decode_frame(raw)
        except (TypeError, ValueError):
            writer.write(session.invalid(received, "malformed_frame", _reference(str(raw))))
            continue
        for payload in frame:
            reference = _reference(payload)
            message_type = payload.get("T")
            if message_type == "relay":
                writer.write(session.control("disconnected", received, "local_relay_unavailable"))
                raise ValueError("local relay reported an unavailable upstream")
            if message_type not in ("b", "q"):
                writer.write(session.invalid(received, "unexpected_message", reference))
                continue
            try:
                observation = translate(payload, received_at=received, symbols=symbols, feed=feed)
            except ValueError as error:
                writer.write(session.invalid(received, str(error), reference))
                continue
            writer.write(session.accept(observation, reference))


async def run_live(
    session: ShadowSession,
    credentials: DataCredentials | None,
    writer: EvidenceWriter,
    *,
    duration: float,
    feed: str,
    local_feed_url: str | None = None,
) -> None:
    """Use direct Alpaca or an explicitly localhost-only provider-frame relay."""
    if not math.isfinite(duration) or not 0 < duration <= 3600:
        raise ValueError("duration must be within (0, 3600] seconds")
    if feed not in SUPPORTED_FEEDS or session.config.source != f"alpaca:{feed}":
        raise ValueError("feed and session source must match real-time iex or sip")
    symbols_checked(tuple(strategy.instrument.identifier for strategy in session.config.strategies))
    if local_feed_url is None and credentials is None:
        raise ValueError("direct Alpaca market data requires credentials")
    if local_feed_url is not None:
        local_feed_url = _local_feed_url_checked(local_feed_url)
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    def clock() -> datetime:
        return datetime.now(UTC)

    logger = logging.Logger("shadow.market_data.private")
    logger.disabled = True  # Avoid WebSocket debug logging of authentication frames.

    async def attempts() -> None:
        for attempt in range(4):
            try:
                async with connect(
                    local_feed_url or f"wss://stream.data.alpaca.markets/v2/{feed}",
                    proxy=None,
                    open_timeout=5,
                    close_timeout=2,
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=16,
                    max_size=1_048_576,
                    logger=logger,
                ) as socket:
                    if local_feed_url is None:
                        assert credentials is not None
                        await consume(socket, session, credentials, writer, feed=feed, clock=clock)
                    else:
                        await consume_local(socket, session, writer, feed=feed, clock=clock)
                    return
            except (OSError, TimeoutError, ConnectionClosed):
                writer.write(session.control("disconnected", clock(), "transport_disconnect"))
                if attempt == 3:
                    writer.write(session.control("failed", clock(), "reconnect_budget_exhausted"))
                    return
                await asyncio.sleep(2**attempt)

    try:
        async with asyncio.timeout(duration):
            await attempts()
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        raise
    except _SetupFailure as failure:
        if session.health not in (FeedHealth.FAILED, FeedHealth.STOPPED):
            writer.write(session.control("failed", clock(), failure.reason))
        raise ValueError("shadow stream failed; inspect sanitized evidence") from None
    except Exception:
        if session.health not in (FeedHealth.FAILED, FeedHealth.STOPPED):
            writer.write(session.control("failed", clock(), "application_error"))
        raise ValueError("shadow stream failed; inspect sanitized evidence") from None
    finally:
        if session.health not in (FeedHealth.FAILED, FeedHealth.STOPPED):
            writer.write(session.control("stopped", clock(), "duration_or_shutdown"))
