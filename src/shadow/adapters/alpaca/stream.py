"""Bounded JSON stock-data stream. Only auth and market-data subscription messages."""

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

from shadow.adapters.alpaca.normalize import symbols_checked, translate
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
    if not isinstance(value, list) or not value or not all(isinstance(v, dict) for v in value):
        raise ValueError("invalid provider frame")
    return value


async def consume(
    socket: Socket, session: ShadowSession, credentials: DataCredentials,
    writer: EvidenceWriter, *, feed: str, clock: Callable[[], datetime],
) -> None:
    """Authenticate, confirm exact subscriptions, then process frames in receive order."""
    symbols = symbols_checked(tuple(c.instrument.identifier for c in session.config.strategies))

    async def expect(message: str) -> None:
        frame = decode_frame(await asyncio.wait_for(socket.recv(), 5))
        if frame != [{"T": "success", "msg": message}]:
            raise ValueError("provider authentication/connection rejected")

    await expect("connected")
    await socket.send(json.dumps({"action": "auth", "key": credentials.key, "secret": credentials.secret}))
    await expect("authenticated")
    await socket.send(json.dumps({"action": "subscribe", "bars": symbols, "quotes": symbols}))
    frame = decode_frame(await asyncio.wait_for(socket.recv(), 5))
    ack = frame[0]
    if (len(frame) != 1 or ack.get("T") != "subscription"
            or ack.get("bars") != list(symbols) and set_checked(ack.get("bars")) != set(symbols)
            or set_checked(ack.get("quotes")) != set(symbols)
            or any(ack.get(channel) for channel in ("trades", "dailyBars", "updatedBars"))):
        raise ValueError("provider subscription differs from requested scope")
    connected_at = clock()
    writer.write(session.control("connected", connected_at))
    while True:
        try:
            raw = await asyncio.wait_for(socket.recv(), 1)
        except TimeoutError:
            writer.write(session.control("tick", clock()))
        else:
            # Timestamp on application receipt, after any transport queue delay.
            received = clock()
            for payload in decode_frame(raw):
                if payload.get("T") not in ("b", "q"):
                    raise ValueError("unexpected provider message after subscription")
                observation = translate(payload, received_at=received, symbols=symbols, feed=feed)
                canonical = line(payload)
                reference = hashlib.sha256(canonical.encode()).hexdigest()
                writer.write({"provider_observation": payload, "received_at": received,
                              "delivery_reference": reference})
                writer.write(session.accept(observation, reference))
                if session.health is FeedHealth.FAILED:
                    return
        now = clock()
        writer.write(session.control("tick", now))
        # Recovery requires new observations from both channels for every symbol.
        # Quiet/closed markets are stale too; never fabricate a calendar heartbeat.
        if (session.health is FeedHealth.STALE
                and now - connected_at > session.config.maximum_bar_age):
            raise TimeoutError("stale market data")


def set_checked(value: object) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("invalid provider subscription acknowledgement")
    if len(value) != len(set(value)):
        raise ValueError("duplicate subscription acknowledgement")
    return set(value)


async def run_live(
    session: ShadowSession, credentials: DataCredentials, writer: EvidenceWriter,
    *, duration: float, feed: str,
) -> None:
    """Human-invoked production data connection; no configurable execution endpoint."""
    if not math.isfinite(duration) or not 0 < duration <= 3600:
        raise ValueError("duration must be within (0, 3600] seconds")
    if feed not in ("iex", "sip") or session.config.source != f"alpaca:{feed}":
        raise ValueError("feed and session source must match iex or sip")
    symbols_checked(tuple(c.instrument.identifier for c in session.config.strategies))
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    def clock() -> datetime:
        return datetime.now(UTC)

    # Dedicated disabled logger prevents websocket DEBUG frame logs exposing auth.
    logger = logging.Logger("shadow.market_data.private")
    logger.disabled = True

    async def attempts() -> None:
        for attempt in range(4):
            try:
                async with connect(
                    f"wss://stream.data.alpaca.markets/v2/{feed}", proxy=None,
                    open_timeout=5, close_timeout=2, ping_interval=20, ping_timeout=20,
                    max_queue=16, max_size=1_048_576, logger=logger,
                ) as socket:
                    await consume(socket, session, credentials, writer, feed=feed, clock=clock)
                    return
            except (OSError, TimeoutError, ConnectionClosed):
                writer.write(session.control("disconnected", clock(), "transport_or_stale"))
                if attempt == 3:
                    writer.write(session.control("failed", clock(), "reconnect_budget_exhausted"))
                    return
                await asyncio.sleep(2 ** attempt)

    try:
        async with asyncio.timeout(duration):
            await attempts()
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        if session.health not in (FeedHealth.FAILED, FeedHealth.STOPPED):
            writer.write(session.control("failed", clock(), "invalid_provider_or_application_state"))
        raise ValueError("shadow stream failed; inspect sanitized evidence") from None
    finally:
        if session.health not in (FeedHealth.FAILED, FeedHealth.STOPPED):
            writer.write(session.control("stopped", clock(), "duration_or_shutdown"))
