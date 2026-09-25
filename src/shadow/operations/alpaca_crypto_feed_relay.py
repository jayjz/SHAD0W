"""Bounded localhost fanout for one Alpaca ``crypto/us`` market-data socket.

This is market-data infrastructure only.  It intentionally imports no broker,
account, order, strategy, risk, or execution implementation.  The relay always
owns the fixed BTC/USD bars subscription needed by the durable BTC worker.  A
local SHAD0W BTC PAPER client can additionally request the fixed BTC/USD trade
and quote channels it already consumes; no caller can select a provider URL,
symbol, or arbitrary channel.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from websockets.asyncio.client import connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from shadow.operations.crypto_timing import ReceiveClocks, TimingEvidence

ALPACA_CRYPTO_URL = "wss://stream.data.alpaca.markets/v1beta3/crypto/us"
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8766
BTC_USD = "BTC/USD"
CLIENT_QUEUE_LIMIT = 32
_CHANNELS = ("trades", "quotes", "bars")


class Socket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str | bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


@dataclass(frozen=True, slots=True)
class CryptoRelayCredentials:
    """Relay-private market-data credentials; their representation is redacted."""

    key: str = field(repr=False)
    secret: str = field(repr=False)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> CryptoRelayCredentials:
        key = (env.get("ALPACA_DATA_KEY") or env.get("ALPACA_API_KEY_ID") or "").strip()
        secret = (env.get("ALPACA_DATA_SECRET") or env.get("ALPACA_API_SECRET_KEY") or "").strip()
        if not key or not secret:
            raise ValueError(
                "ALPACA_DATA_KEY and ALPACA_DATA_SECRET are required by the crypto relay"
            )
        return cls(key, secret)


def _loads(raw: str | bytes) -> list[dict[str, object]]:
    try:
        value = json.loads(raw, parse_float=Decimal)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("malformed upstream frame") from error
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("malformed upstream frame")
    return value


def _frame(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str)


@dataclass(frozen=True, slots=True)
class CryptoRelaySubscription:
    """One strict local subscription; every requested channel is BTC/USD only."""

    trades: bool = False
    quotes: bool = False
    bars: bool = False

    @classmethod
    def parse(cls, raw: str | bytes) -> CryptoRelaySubscription:
        try:
            value = json.loads(raw)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid local crypto relay subscription") from error
        if not isinstance(value, dict) or set(value) - {"action", *_CHANNELS}:
            raise ValueError("local crypto relay accepts only fixed BTC/USD subscriptions")
        if value.get("action") != "subscribe":
            raise ValueError("local crypto relay accepts only subscribe")
        requested: dict[str, bool] = {}
        for channel in _CHANNELS:
            symbols = value.get(channel, [])
            if not isinstance(symbols, list) or symbols not in ([], [BTC_USD]):
                raise ValueError("local crypto relay permits only exact BTC/USD")
            requested[channel] = symbols == [BTC_USD]
        if not any(requested.values()):
            raise ValueError("local crypto relay requires a BTC/USD channel")
        return cls(**requested)

    def requested(self, channel: str) -> bool:
        if channel == "trades":
            return self.trades
        if channel == "quotes":
            return self.quotes
        if channel == "bars":
            return self.bars
        raise ValueError("unsupported crypto relay channel")

    def acknowledgement(self) -> str:
        return _frame(
            [
                {
                    "T": "subscription",
                    **{
                        channel: [BTC_USD] if self.requested(channel) else []
                        for channel in _CHANNELS
                    },
                }
            ]
        )


@dataclass(slots=True, eq=False)
class _Client:
    socket: ServerConnection
    subscription: CryptoRelaySubscription
    queue: asyncio.Queue[str | bytes] = field(
        default_factory=lambda: asyncio.Queue(CLIENT_QUEUE_LIMIT)
    )
    ready: asyncio.Future[None] | None = None


class AlpacaCryptoFeedRelay:
    """Exactly one upstream crypto socket, with strict local fanout and backoff."""

    def __init__(
        self,
        credentials: CryptoRelayCredentials,
        *,
        host: str = LOCAL_HOST,
        port: int = LOCAL_PORT,
        upstream_connect: Callable[[], Awaitable[Socket]] | None = None,
        reconnect_delay: Callable[[int], float] | None = None,
        timing: TimingEvidence | None = None,
        wall_ns: Callable[[], int] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
    ) -> None:
        if host != LOCAL_HOST:
            raise ValueError("crypto relay must bind only to 127.0.0.1")
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("crypto relay port must be valid")
        self.credentials = credentials
        self.host = host
        self.port = port
        self._upstream_connect = upstream_connect or self._connect_upstream
        self._reconnect_delay = reconnect_delay or (lambda attempt: min(2**attempt, 30))
        self._timing = timing
        self._wall_ns = wall_ns or time.time_ns
        self._monotonic_ns = monotonic_ns or time.monotonic_ns
        self._server: Server | None = None
        self._upstream_task: asyncio.Task[None] | None = None
        self._clients: set[_Client] = set()
        self._subscription_changed = asyncio.Event()
        self._stopping = False
        self._state = "connecting"
        self._reason: str | None = None
        self._attempts = 0
        self._last_provider_frame_at: str | None = None
        self._logger = logging.getLogger("shadow.alpaca_crypto_feed_relay")

    async def _connect_upstream(self) -> Socket:
        logger = logging.Logger("shadow.alpaca_crypto_feed_relay.private")
        logger.disabled = True
        return await connect(
            ALPACA_CRYPTO_URL,
            proxy=None,
            open_timeout=5,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            max_queue=16,
            max_size=1_048_576,
            logger=logger,
        )

    def health(self) -> dict[str, object]:
        return {
            "T": "relay_health",
            "upstream_state": self._state,
            "reason": self._reason,
            "connection_attempts": self._attempts,
            "clients": len(self._clients),
            "last_provider_frame_at": self._last_provider_frame_at,
        }

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("crypto relay already started")
        self._server = await serve(
            self._handle_client,
            self.host,
            self.port,
            max_queue=16,
            max_size=1_048_576,
            ping_interval=20,
            ping_timeout=20,
        )
        self._upstream_task = asyncio.create_task(self._run_upstream(), name="alpaca-crypto-relay")

    async def stop(self) -> None:
        self._stopping = True
        if self._upstream_task is not None:
            self._upstream_task.cancel()
            await asyncio.gather(self._upstream_task, return_exceptions=True)
        await self._close_clients(1001, "relay_shutdown")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._state, self._reason = "stopped", None

    async def _handle_client(self, socket: ServerConnection) -> None:
        path = socket.request.path if socket.request is not None else ""
        if path == "/health":
            await socket.send(_frame(self.health()))
            await socket.close(code=1000, reason="health_complete")
            return
        if path != "/":
            await socket.close(code=1008, reason="unknown_local_path")
            return
        try:
            subscription = CryptoRelaySubscription.parse(await asyncio.wait_for(socket.recv(), 5))
        except (TimeoutError, ConnectionClosed, ValueError):
            await socket.close(code=1008, reason="invalid_subscription")
            return
        if self._state == "failed":
            await socket.close(code=1013, reason="relay_unavailable")
            return
        client = _Client(socket, subscription)
        client.ready = asyncio.get_running_loop().create_future()
        self._clients.add(client)
        self._subscription_changed.set()
        writer = asyncio.create_task(self._write_client(client))
        try:
            await asyncio.wait_for(client.ready, 10)
            await client.queue.put(subscription.acknowledgement())
            await socket.wait_closed()
        except TimeoutError:
            await socket.close(code=1013, reason="upstream_subscription_timeout")
        except ConnectionError:
            pass
        finally:
            self._clients.discard(client)
            self._subscription_changed.set()
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def _write_client(self, client: _Client) -> None:
        try:
            while True:
                await client.socket.send(await client.queue.get())
        except ConnectionClosed:
            return

    def _desired_subscription(self) -> CryptoRelaySubscription:
        # Bars are intentionally always owned, even before a downstream worker connects.
        return CryptoRelaySubscription(
            trades=any(client.subscription.trades for client in self._clients),
            quotes=any(client.subscription.quotes for client in self._clients),
            bars=True,
        )

    @staticmethod
    def _upstream_request(subscription: CryptoRelaySubscription) -> str:
        return _frame(
            {
                "action": "subscribe",
                **{channel: [BTC_USD] for channel in _CHANNELS if subscription.requested(channel)},
            }
        )

    async def _run_upstream(self) -> None:
        attempt = 0
        while not self._stopping:
            socket: Socket | None = None
            self._attempts += 1
            self._state, self._reason = ("connecting" if attempt == 0 else "reconnecting"), None
            retry = True
            try:
                socket = await self._upstream_connect()
                await self._expect(socket, "connected")
                self._state = "connected"
                await socket.send(
                    _frame(
                        {
                            "action": "auth",
                            "key": self.credentials.key,
                            "secret": self.credentials.secret,
                        }
                    )
                )
                await self._expect(socket, "authenticated")
                self._state = "authenticated"
                await self._serve_upstream(socket)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except ValueError as error:
                self._state, self._reason = "failed", str(error)
                retry = self._reason == "upstream_connection_limit"
            except (OSError, TimeoutError, ConnectionClosed):
                self._state, self._reason = "reconnecting", "transport_disconnect"
            finally:
                if socket is not None:
                    await socket.close(code=1001, reason="relay_reconnect")
            await self._close_clients(1011, self._reason or "upstream_disconnect")
            if self._stopping or not retry:
                return
            self._state = "reconnecting"
            await asyncio.sleep(self._reconnect_delay(attempt))
            attempt += 1

    async def _expect(self, socket: Socket, message: str) -> None:
        frame = _loads(await asyncio.wait_for(socket.recv(), 5))
        if frame == [{"T": "success", "msg": message}]:
            return
        if len(frame) == 1 and frame[0].get("T") == "error":
            if frame[0].get("code") == 406:
                raise ValueError("upstream_connection_limit")
            raise ValueError("upstream_authentication_rejected")
        raise ValueError("upstream_protocol_failure")

    async def _recv_timed(self, socket: Socket) -> tuple[str | bytes, ReceiveClocks]:
        """Sample clocks only after the provider frame receive completes."""
        raw = await socket.recv()
        return raw, ReceiveClocks.capture(self._wall_ns, self._monotonic_ns)

    async def _serve_upstream(self, socket: Socket) -> None:
        active: CryptoRelaySubscription | None = None
        while not self._stopping:
            desired = self._desired_subscription()
            if active is not None:
                # Alpaca's subscribe action is additive. Keep fixed channels
                # already accepted by this one upstream socket until it
                # reconnects; local fanout remains subscription-filtered.
                desired = CryptoRelaySubscription(
                    trades=active.trades or desired.trades,
                    quotes=active.quotes or desired.quotes,
                    bars=True,
                )
            if desired != active:
                await socket.send(self._upstream_request(desired))
                while True:
                    raw = await socket.recv()
                    frame = _loads(raw)
                    if len(frame) == 1 and self._subscription_acknowledges(frame[0], desired):
                        active = desired
                        self._state, self._reason = "subscribed", None
                        self._ready_clients(active)
                        break
                    if self._is_expected_market_data(frame):
                        # Alpaca may deliver a valid event before the matching
                        # subscription acknowledgement. Do not put it ahead of
                        # a local client's acknowledgement.
                        self._last_provider_frame_at = datetime.now(UTC).isoformat()
                        continue
                    raise ValueError("upstream_subscription_rejected")
                continue
            self._ready_clients(active)
            receive = asyncio.create_task(self._recv_timed(socket))
            changed = asyncio.create_task(self._subscription_changed.wait())
            try:
                done, _ = await asyncio.wait(
                    (receive, changed), return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (receive, changed):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(receive, changed, return_exceptions=True)
            if changed in done:
                self._subscription_changed.clear()
            if receive in done:
                raw, clocks = receive.result()
                if self._timing is not None:
                    self._timing.observe_frame(raw, clocks)
                await self._broadcast(raw, _loads(raw))

    @staticmethod
    def _subscription_acknowledges(
        message: Mapping[str, object], desired: CryptoRelaySubscription
    ) -> bool:
        if message.get("T") != "subscription" or set(message) - {
            "T",
            *_CHANNELS,
            "updatedBars",
            "dailyBars",
            "orderbooks",
        }:
            return False
        for channel in _CHANNELS:
            expected = [BTC_USD] if desired.requested(channel) else []
            if message.get(channel, []) != expected:
                return False
        return (
            message.get("updatedBars", []) == []
            and message.get("dailyBars", []) == []
            # Alpaca crypto/us includes this empty acknowledgement field even
            # when the relay never requests order books.  Accepting only the
            # empty form preserves the fixed BTC/USD b/t/q subscription.
            and message.get("orderbooks", []) == []
        )

    def _ready_clients(self, active: CryptoRelaySubscription) -> None:
        for client in tuple(self._clients):
            if (
                client.ready is not None
                and not client.ready.done()
                and all(
                    not client.subscription.requested(channel) or active.requested(channel)
                    for channel in _CHANNELS
                )
            ):
                client.ready.set_result(None)

    @staticmethod
    def _is_expected_market_data(frame: list[dict[str, object]]) -> bool:
        kinds = {"t", "q", "b"}
        return bool(frame) and all(
            isinstance(item.get("T"), str) and item.get("T") in kinds and item.get("S") == BTC_USD
            for item in frame
        )

    async def _broadcast(self, raw: str | bytes, frame: list[dict[str, object]]) -> None:
        kinds = {"t": "trades", "q": "quotes", "b": "bars"}
        if not self._is_expected_market_data(frame):
            raise ValueError("unexpected_upstream_market_data")
        selected: list[tuple[dict[str, object], str]] = []
        for item in frame:
            event_type = item.get("T")
            kind = kinds.get(event_type) if isinstance(event_type, str) else None
            assert kind is not None
            selected.append((item, kind))
        self._last_provider_frame_at = datetime.now(UTC).isoformat()
        for client in tuple(self._clients):
            items = [item for item, channel in selected if client.subscription.requested(channel)]
            if not items:
                continue
            # Preserve a provider frame byte-for-byte whenever it already contains
            # exactly this client's allowed events (the normal Alpaca bar case).
            payload: str | bytes = raw if len(items) == len(frame) else _frame(items)
            try:
                client.queue.put_nowait(payload)
            except asyncio.QueueFull:
                self._logger.warning("local crypto consumer dropped: slow_consumer_queue_full")
                asyncio.create_task(
                    client.socket.close(code=1013, reason="slow_consumer_queue_full")
                )

    async def _close_clients(self, code: int, reason: str) -> None:
        for client in tuple(self._clients):
            if client.ready is not None and not client.ready.done():
                client.ready.set_exception(ConnectionError(reason))
            await client.socket.close(code=code, reason=reason[:123])


async def _run(arguments: argparse.Namespace) -> None:
    timing: TimingEvidence | None = None
    if arguments.timing_evidence is not None:
        timing = TimingEvidence(
            arguments.timing_evidence,
            role="relay",
            transport="provider_websocket",
            max_events=arguments.timing_max_events,
            code_revision=arguments.code_revision,
        )
    relay = AlpacaCryptoFeedRelay(
        CryptoRelayCredentials.from_environment(os.environ),
        port=arguments.port,
        timing=timing,
    )
    try:
        await relay.start()
        try:
            print(
                _frame({"T": "relay_started", "host": LOCAL_HOST, "port": arguments.port}),
                flush=True,
            )
            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stopped.set)
            await stopped.wait()
        finally:
            await relay.stop()
    finally:
        if timing is not None:
            timing.close()


async def _health(port: int) -> None:
    async with connect(f"ws://{LOCAL_HOST}:{port}/health", proxy=None, open_timeout=2) as socket:
        print(await socket.recv())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="localhost-only Alpaca crypto BTC/USD relay; no broker or trading authority"
    )
    parser.add_argument("--port", type=int, default=LOCAL_PORT)
    parser.add_argument(
        "--health", action="store_true", help="read sanitized localhost relay health"
    )
    parser.add_argument(
        "--timing-evidence",
        type=Path,
        help="write bounded read-only receive timing JSONL; it has no trading authority",
    )
    parser.add_argument("--timing-max-events", type=int, default=32)
    parser.add_argument("--code-revision", default="unspecified")
    arguments = parser.parse_args()
    if arguments.health:
        asyncio.run(_health(arguments.port))
    else:
        asyncio.run(_run(arguments))
    return 0
