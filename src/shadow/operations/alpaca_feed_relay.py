"""A bounded localhost-only fanout for Alpaca IEX market-data frames.

This module deliberately contains no broker, account, order, or execution imports.
Downstream clients receive provider event objects (``b`` and ``q``) unchanged inside
deterministic JSON array frames.  The small local control protocol is documented by
``RelaySubscription`` below.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from websockets.asyncio.client import connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

ALPACA_IEX_URL = "wss://stream.data.alpaca.markets/v2/iex"
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8765
CLIENT_QUEUE_LIMIT = 32


class Socket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


@dataclass(frozen=True, slots=True)
class MarketDataCredentials:
    """Credentials are intentionally relay-private and never serialized."""

    key: str = field(repr=False)
    secret: str = field(repr=False)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> MarketDataCredentials:
        key = (env.get("ALPACA_DATA_KEY") or env.get("ALPACA_API_KEY_ID") or "").strip()
        secret = (env.get("ALPACA_DATA_SECRET") or env.get("ALPACA_API_SECRET_KEY") or "").strip()
        if not key or not secret:
            raise ValueError("ALPACA_DATA_KEY and ALPACA_DATA_SECRET are required by the relay")
        return cls(key, secret)


@dataclass(frozen=True, slots=True)
class RelaySubscription:
    """The only downstream command; no authentication or broker command exists."""

    bars: tuple[str, ...]
    quotes: tuple[str, ...]

    @classmethod
    def parse(cls, raw: str | bytes) -> RelaySubscription:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid local relay subscription") from error
        if not isinstance(value, dict) or value.get("action") != "subscribe":
            raise ValueError("local relay accepts only subscribe")
        bars = cls._symbols(value.get("bars", []))
        quotes = cls._symbols(value.get("quotes", []))
        if not bars and not quotes:
            raise ValueError("local relay subscriptions require a market-data channel")
        return cls(bars, quotes)

    @staticmethod
    def _symbols(value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.isascii() and item.isupper() and item.isalnum()
            for item in value
        ):
            raise ValueError("local relay subscriptions require uppercase symbols")
        if len(value) != len(set(value)):
            raise ValueError("local relay subscriptions may not duplicate symbols")
        return tuple(value)

    def contains(self, event: Mapping[str, object]) -> bool:
        symbol = event.get("S")
        event_type = event.get("T")
        return isinstance(symbol, str) and (
            event_type == "b" and symbol in self.bars or event_type == "q" and symbol in self.quotes
        )

    def acknowledgement(self) -> str:
        return _frame([{"T": "subscription", "bars": list(self.bars), "quotes": list(self.quotes)}])


def _frame(value: object) -> str:
    """Canonical local framing; provider event object key/value content is unchanged."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _decode_frame(raw: str | bytes) -> list[dict[str, object]]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid provider frame") from error
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("invalid provider frame")
    return value


@dataclass(slots=True, eq=False)
class _Client:
    socket: ServerConnection
    subscription: RelaySubscription
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(CLIENT_QUEUE_LIMIT))
    ready: asyncio.Future[None] | None = None


class AlpacaFeedRelay:
    """One upstream IEX socket and bounded, isolated local fanout consumers."""

    def __init__(
        self,
        credentials: MarketDataCredentials,
        *,
        host: str = LOCAL_HOST,
        port: int = LOCAL_PORT,
        upstream_connect: Callable[[], Awaitable[Socket]] | None = None,
        reconnect_delay: Callable[[int], float] | None = None,
    ) -> None:
        if host != LOCAL_HOST:
            raise ValueError("relay must bind only to 127.0.0.1")
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("relay port must be valid")
        self.credentials = credentials
        self.host = host
        self.port = port
        self._upstream_connect = upstream_connect or self._connect_upstream
        self._reconnect_delay = reconnect_delay or (lambda attempt: min(2**attempt, 30))
        self._server: Server | None = None
        self._upstream_task: asyncio.Task[None] | None = None
        self._clients: set[_Client] = set()
        self._subscription_changed = asyncio.Event()
        self._stopping = False
        self._state = "starting"
        self._reason: str | None = None
        self._attempts = 0
        self._last_provider_frame_at: str | None = None
        self._logger = logging.getLogger("shadow.alpaca_feed_relay")

    async def _connect_upstream(self) -> Socket:
        logger = logging.Logger("shadow.alpaca_feed_relay.private")
        logger.disabled = True
        return await connect(
            ALPACA_IEX_URL,
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
            raise RuntimeError("relay already started")
        self._server = await serve(
            self._handle_client,
            self.host,
            self.port,
            max_queue=16,
            max_size=1_048_576,
            ping_interval=20,
            ping_timeout=20,
        )
        self._upstream_task = asyncio.create_task(self._run_upstream(), name="alpaca-iex-relay")

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
        self._state = "stopped"

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
            subscription = RelaySubscription.parse(await asyncio.wait_for(socket.recv(), 5))
        except (TimeoutError, ConnectionClosed, ValueError):
            await socket.close(code=1008, reason="invalid_subscription")
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
            # _close_clients has already sent the explicit upstream-loss close.
            # Do not let that expected ready-future failure escape a server task.
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

    def _desired_subscription(self) -> RelaySubscription | None:
        if not self._clients:
            return None
        bars = tuple(
            sorted({symbol for client in self._clients for symbol in client.subscription.bars})
        )
        quotes = tuple(
            sorted({symbol for client in self._clients for symbol in client.subscription.quotes})
        )
        return RelaySubscription(bars, quotes)

    @staticmethod
    def _upstream_request(subscription: RelaySubscription) -> str:
        request: dict[str, object] = {"action": "subscribe"}
        if subscription.bars:
            request["bars"] = list(subscription.bars)
        if subscription.quotes:
            request["quotes"] = list(subscription.quotes)
        return _frame(request)

    async def _run_upstream(self) -> None:
        attempt = 0
        while not self._stopping:
            self._attempts += 1
            self._state, self._reason = "connecting", None
            try:
                socket = await self._upstream_connect()
                await self._expect(socket, "connected")
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
                self._state = "connected"
                attempt = 0
                await self._serve_upstream(socket)
            except asyncio.CancelledError:
                raise
            except ValueError as error:
                self._state, self._reason = "failed", str(error)
            except (OSError, TimeoutError, ConnectionClosed):
                self._state, self._reason = "disconnected", "transport_disconnect"
            await self._close_clients(1011, self._reason or "upstream_disconnect")
            if self._stopping:
                return
            await asyncio.sleep(self._reconnect_delay(attempt))
            attempt += 1

    async def _expect(self, socket: Socket, message: str) -> None:
        frame = _decode_frame(await asyncio.wait_for(socket.recv(), 5))
        if frame == [{"T": "success", "msg": message}]:
            return
        if len(frame) == 1 and frame[0].get("T") == "error":
            code = frame[0].get("code")
            if code == 406:
                raise ValueError("authentication_connection_limit")
            raise ValueError("authentication_rejected")
        raise ValueError("upstream_protocol_failure")

    async def _serve_upstream(self, socket: Socket) -> None:
        active: RelaySubscription | None = None
        while not self._stopping:
            desired = self._desired_subscription()
            if desired is not None and desired == active:
                for client in tuple(self._clients):
                    if client.ready is not None and not client.ready.done():
                        client.ready.set_result(None)
            if desired is not None and desired != active:
                await socket.send(self._upstream_request(desired))
                while True:
                    frame = _decode_frame(await socket.recv())
                    if len(frame) == 1 and self._subscription_acknowledges(frame[0], desired):
                        active = desired
                        for client in tuple(self._clients):
                            if client.ready is not None and not client.ready.done():
                                client.ready.set_result(None)
                        break
                await self._broadcast(frame)
                continue
            receive = asyncio.create_task(socket.recv())
            changed = asyncio.create_task(self._subscription_changed.wait())
            done, pending = await asyncio.wait(
                (receive, changed), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if changed in done:
                self._subscription_changed.clear()
            if receive in done:
                await self._broadcast(_decode_frame(receive.result()))

    @staticmethod
    def _subscription_acknowledges(
        message: Mapping[str, object], desired: RelaySubscription
    ) -> bool:
        if message.get("T") != "subscription":
            return False
        try:
            bars = set(RelaySubscription._symbols(message.get("bars", [])))
            quotes = set(RelaySubscription._symbols(message.get("quotes", [])))
        except ValueError:
            return False
        return set(desired.bars).issubset(bars) and set(desired.quotes).issubset(quotes)

    async def _broadcast(self, frame: list[dict[str, object]]) -> None:
        data = [item for item in frame if item.get("T") in ("b", "q")]
        if not data:
            return
        self._last_provider_frame_at = datetime.now(UTC).isoformat()
        for client in tuple(self._clients):
            selected = [item for item in data if client.subscription.contains(item)]
            if not selected:
                continue
            try:
                client.queue.put_nowait(_frame(selected))
            except asyncio.QueueFull:
                self._logger.warning("local consumer dropped: slow_consumer_queue_full")
                asyncio.create_task(
                    client.socket.close(code=1013, reason="slow_consumer_queue_full")
                )

    async def _close_clients(self, code: int, reason: str) -> None:
        clients = tuple(self._clients)
        for client in clients:
            if client.ready is not None and not client.ready.done():
                client.ready.set_exception(ConnectionError(reason))
            await client.socket.close(code=code, reason=reason[:123])


async def _run(arguments: argparse.Namespace) -> None:
    relay = AlpacaFeedRelay(MarketDataCredentials.from_environment(os.environ), port=arguments.port)
    await relay.start()
    print(_frame({"T": "relay_started", "host": LOCAL_HOST, "port": arguments.port}), flush=True)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    try:
        await stopped.wait()
    finally:
        await relay.stop()


async def _health(port: int) -> None:
    """Read the unauthenticated, sanitized localhost health frame."""
    async with connect(f"ws://{LOCAL_HOST}:{port}/health", proxy=None, open_timeout=2) as socket:
        print(await socket.recv())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="localhost-only Alpaca IEX market-data relay; no trading authority"
    )
    parser.add_argument("--port", type=int, default=LOCAL_PORT)
    parser.add_argument(
        "--health", action="store_true", help="read sanitized localhost relay health"
    )
    arguments = parser.parse_args()
    if arguments.health:
        asyncio.run(_health(arguments.port))
    else:
        asyncio.run(_run(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
