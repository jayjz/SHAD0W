"""Offline integration checks for the bounded local market-data relay."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from shadow.operations.alpaca_feed_relay import (
    CLIENT_QUEUE_LIMIT,
    AlpacaFeedRelay,
    MarketDataCredentials,
    RelaySubscription,
)


class _Upstream:
    def __init__(self) -> None:
        self.received: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.connections = 0
        self.acknowledge_subscriptions = True

    async def connect(self) -> _Upstream:
        self.connections += 1
        await self.received.put(json.dumps([{"T": "success", "msg": "connected"}]))
        return self

    async def recv(self) -> str:
        item = await self.received.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(self, message: str) -> None:
        value = json.loads(message)
        self.sent.append(value)
        if value["action"] == "auth":
            await self.received.put(json.dumps([{"T": "success", "msg": "authenticated"}]))
        elif value["action"] == "subscribe" and self.acknowledge_subscriptions:
            await self.received.put(
                json.dumps(
                    [
                        {
                            "T": "subscription",
                            "bars": value.get("bars", []),
                            "quotes": value.get("quotes", []),
                        }
                    ]
                )
            )

    async def emit(self, frame: list[dict[str, object]]) -> None:
        await self.received.put(json.dumps(frame))


async def _relay(delay: float = 0) -> tuple[AlpacaFeedRelay, _Upstream, str]:
    upstream = _Upstream()
    relay = AlpacaFeedRelay(
        MarketDataCredentials("relay-key", "relay-secret"),
        port=0,
        upstream_connect=upstream.connect,
        reconnect_delay=lambda _: delay,
    )
    await relay.start()
    assert relay._server is not None  # test-only address discovery for an ephemeral port
    port = next(iter(relay._server.sockets)).getsockname()[1]
    return relay, upstream, f"ws://127.0.0.1:{port}"


async def _subscribe(url: str, *, quotes: bool = True) -> ClientConnection:
    socket = await connect(url)
    await socket.send(
        json.dumps({"action": "subscribe", "bars": ["SPY"], "quotes": ["SPY"] if quotes else []})
    )
    assert json.loads(await socket.recv()) == [
        {"T": "subscription", "bars": ["SPY"], "quotes": ["SPY"] if quotes else []}
    ]
    return socket


def test_one_upstream_event_reaches_two_consumers_in_observed_order_without_mutation() -> None:
    async def scenario() -> None:
        relay, upstream, url = await _relay()
        first = await _subscribe(url)
        second = await _subscribe(url)
        provider_event = {
            "T": "b",
            "S": "SPY",
            "o": 100,
            "h": 102,
            "l": 99,
            "c": 101,
            "v": 7,
            "t": "2026-09-21T14:30:00Z",
        }
        original = copy.deepcopy(provider_event)
        try:
            await upstream.emit([provider_event])
            assert json.loads(await first.recv()) == [original]
            assert json.loads(await second.recv()) == [original]
            assert provider_event == original
            await upstream.emit([{**provider_event, "c": 102, "t": "2026-09-21T14:31:00Z"}])
            assert json.loads(await first.recv())[0]["t"] == "2026-09-21T14:31:00Z"
            assert json.loads(await second.recv())[0]["t"] == "2026-09-21T14:31:00Z"
            assert upstream.connections == 1
            assert all("relay-secret" not in json.dumps(frame) for frame in [original])
        finally:
            await first.close()
            await second.close()
            await relay.stop()

    asyncio.run(scenario())


def test_consumer_disconnect_does_not_interrupt_another_consumer() -> None:
    async def scenario() -> None:
        relay, upstream, url = await _relay()
        first = await _subscribe(url)
        second = await _subscribe(url)
        try:
            await first.close()
            await upstream.emit(
                [
                    {
                        "T": "b",
                        "S": "SPY",
                        "o": 1,
                        "h": 2,
                        "l": 1,
                        "c": 2,
                        "v": 1,
                        "t": "2026-09-21T14:30:00Z",
                    }
                ]
            )
            assert json.loads(await second.recv())[0]["S"] == "SPY"
            assert relay.health()["upstream_state"] == "connected"
        finally:
            await second.close()
            await relay.stop()

    asyncio.run(scenario())


def test_slow_consumer_queue_is_bounded_and_upstream_disconnect_is_health_evidence() -> None:
    async def scenario() -> None:
        relay, upstream, _ = await _relay(60)
        try:
            subscription = RelaySubscription(("SPY",), ())

            class BlockedSocket:
                closed: tuple[int, str] | None = None

                async def send(self, message: str) -> None:
                    await asyncio.Future()

                async def close(self, code: int, reason: str) -> None:
                    self.closed = (code, reason)

            blocked = BlockedSocket()
            from shadow.operations.alpaca_feed_relay import _Client

            client = _Client(blocked, subscription)  # type: ignore[arg-type]
            relay._clients.add(client)
            event = {
                "T": "b",
                "S": "SPY",
                "o": 1,
                "h": 2,
                "l": 1,
                "c": 2,
                "v": 1,
                "t": "2026-09-21T14:30:00Z",
            }
            for _ in range(CLIENT_QUEUE_LIMIT + 1):
                await relay._broadcast([event])
            await asyncio.sleep(0)
            assert blocked.closed == (1013, "slow_consumer_queue_full")
            assert client.queue.maxsize == CLIENT_QUEUE_LIMIT
            await upstream.received.put(OSError("test disconnect"))
            for _ in range(20):
                if relay.health()["upstream_state"] in {"disconnected", "connecting"}:
                    break
                await asyncio.sleep(0)
            assert relay.health()["upstream_state"] == "disconnected"
        finally:
            await relay.stop()

    asyncio.run(scenario())


def test_upstream_failure_while_client_waits_for_ready_is_a_clean_server_shutdown() -> None:
    async def scenario() -> None:
        relay, upstream, url = await _relay(60)
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, object]] = []
        prior_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        client = await connect(url)
        try:
            upstream.acknowledge_subscriptions = False
            await client.send(json.dumps({"action": "subscribe", "bars": ["SPY"], "quotes": []}))
            await asyncio.sleep(0)
            await upstream.received.put(
                OSError("upstream failed before subscription acknowledgement")
            )
            with pytest.raises(ConnectionClosed):
                await client.recv()
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            loop.set_exception_handler(prior_handler)
            await client.close()
            await relay.stop()

    asyncio.run(scenario())


def test_relay_module_has_no_trading_or_order_surface() -> None:
    from pathlib import Path

    source = Path("src/shadow/operations/alpaca_feed_relay.py").read_text(encoding="utf-8").lower()
    assert "shadow.execution" not in source
    assert "paper_broker" not in source
    assert "alpaca_paper" not in source


def test_relay_accepts_data_credential_names_or_relay_private_api_names() -> None:
    assert MarketDataCredentials.from_environment(
        {"ALPACA_DATA_KEY": "key", "ALPACA_DATA_SECRET": "secret"}
    )
    assert MarketDataCredentials.from_environment(
        {"ALPACA_API_KEY_ID": "key", "ALPACA_API_SECRET_KEY": "secret"}
    )
