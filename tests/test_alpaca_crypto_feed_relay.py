"""Offline integration tests for the localhost-only shared crypto relay."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from shadow.operations.alpaca_crypto_feed_relay import (
    BTC_USD,
    CLIENT_QUEUE_LIMIT,
    AlpacaCryptoFeedRelay,
    CryptoRelayCredentials,
    CryptoRelaySubscription,
)
from shadow.operations.crypto_timing import (
    ReceiveClocks,
    TimingEvidence,
    join_timing,
    load_timing_events,
)


class _Connection:
    def __init__(self, upstream: _Upstream) -> None:
        self.upstream = upstream
        self.received: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def recv(self) -> str:
        value = await self.received.get()
        if isinstance(value, BaseException):
            raise value
        hook = self.upstream.on_receive_return
        if hook is not None:
            hook(value)
        return value

    async def send(self, message: str | bytes) -> None:
        value = json.loads(message)
        self.sent.append(value)
        if value["action"] == "auth":
            await self.received.put(self.upstream.auth_response)
        elif value["action"] == "subscribe":
            if self.upstream.pre_ack_market_data is not None:
                await self.received.put(self.upstream.pre_ack_market_data)
            await self.received.put(
                json.dumps(
                    [
                        {
                            "T": "subscription",
                            "trades": value.get("trades", []),
                            "quotes": value.get("quotes", []),
                            "bars": value.get("bars", []),
                            "updatedBars": [],
                            "dailyBars": [],
                            "orderbooks": [],
                        }
                    ]
                )
                if self.upstream.acknowledge_subscriptions
                else '[{"T":"error","code":403}]'
            )

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        if self.upstream.close_with_receive_error:
            await self.received.put(OSError("upstream closed"))


class _Upstream:
    def __init__(self) -> None:
        self.connections: list[_Connection] = []
        self.auth_response = '[{"T":"success","msg":"authenticated"}]'
        self.acknowledge_subscriptions = True
        self.pre_ack_market_data: str | None = None
        self.close_with_receive_error = False
        self.on_receive_return: Callable[[str], None] | None = None

    async def connect(self) -> _Connection:
        connection = _Connection(self)
        self.connections.append(connection)
        await connection.received.put('[{"T":"success","msg":"connected"}]')
        return connection

    async def emit(self, frame: str | BaseException, index: int = -1) -> None:
        await self.connections[index].received.put(frame)


async def _until(predicate: bool) -> None:
    for _ in range(100):
        if predicate:
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


async def _relay(
    delay: float = 0,
    *,
    timing: TimingEvidence | None = None,
    wall_ns: Callable[[], int] | None = None,
    monotonic_ns: Callable[[], int] | None = None,
) -> tuple[AlpacaCryptoFeedRelay, _Upstream, str]:
    upstream = _Upstream()
    relay = AlpacaCryptoFeedRelay(
        CryptoRelayCredentials("relay-key", "relay-secret"),
        port=0,
        upstream_connect=upstream.connect,
        reconnect_delay=lambda _: delay,
        timing=timing,
        wall_ns=wall_ns,
        monotonic_ns=monotonic_ns,
    )
    await relay.start()
    for _ in range(100):
        if relay.health()["upstream_state"] == "subscribed":
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("relay did not subscribe")
    assert relay._server is not None  # test-only ephemeral-port discovery
    port = next(iter(relay._server.sockets)).getsockname()[1]
    return relay, upstream, f"ws://127.0.0.1:{port}"


async def _subscribe(url: str, request: dict[str, object]) -> ClientConnection:
    socket = await connect(url)
    await socket.send(json.dumps(request))
    acknowledgement = json.loads(await socket.recv())
    assert acknowledgement[0]["T"] == "subscription"
    return socket


def _bar() -> str:
    # Whitespace and the precise provider timestamp/numeric spelling are deliberate.
    return (
        '[ { "T" : "b" , "S" : "BTC/USD" , "o" : 100.00 , "h" : 102.50 , '
        '"l" : 99.50 , "c" : 101.25 , "v" : 7.0 , '
        '"t" : "2026-09-24T12:00:00.000000000Z" } ]'
    )


def test_one_upstream_authenticates_once_and_fans_the_same_provider_bar_to_two_clients() -> None:
    async def scenario() -> None:
        relay, upstream, url = await _relay()
        first = await _subscribe(url, {"action": "subscribe", "bars": [BTC_USD]})
        second = await _subscribe(url, {"action": "subscribe", "bars": [BTC_USD]})
        try:
            await upstream.emit(_bar())
            assert await first.recv() == _bar()
            assert await second.recv() == _bar()
            assert len(upstream.connections) == 1
            assert upstream.connections[0].sent[0] == {
                "action": "auth",
                "key": "relay-key",
                "secret": "relay-secret",
            }
            assert upstream.connections[0].sent[1] == {"action": "subscribe", "bars": [BTC_USD]}
        finally:
            await first.close()
            await second.close()
            await relay.stop()

    asyncio.run(scenario())


def test_invalid_local_requests_fail_closed() -> None:
    async def scenario() -> None:
        relay, _, url = await _relay()
        try:
            socket = await connect(url)
            await socket.send("not-json")
            with pytest.raises(ConnectionClosed):
                await socket.recv()
            for request in (
                {"action": "subscribe", "bars": ["ETH/USD"]},
                {"action": "subscribe", "bars": ["*"]},
                {"action": "subscribe", "orderbooks": [BTC_USD]},
                {"action": "auth", "key": "not-allowed"},
                {"action": "subscribe", "bars": [BTC_USD, BTC_USD]},
            ):
                socket = await connect(url)
                await socket.send(json.dumps(request))
                with pytest.raises(ConnectionClosed):
                    await socket.recv()
        finally:
            await relay.stop()

    asyncio.run(scenario())


def test_shad_trade_quote_client_adds_only_fixed_channels_and_never_exposes_credentials() -> None:
    async def scenario() -> None:
        relay, upstream, url = await _relay()
        client = await _subscribe(
            url, {"action": "subscribe", "trades": [BTC_USD], "quotes": [BTC_USD]}
        )
        try:
            for _ in range(100):
                if len(upstream.connections[0].sent) == 3:
                    break
                await asyncio.sleep(0)
            assert upstream.connections[0].sent[2] == {
                "action": "subscribe",
                "trades": [BTC_USD],
                "quotes": [BTC_USD],
                "bars": [BTC_USD],
            }
            await upstream.emit(
                '[{"T":"t","S":"BTC/USD","i":7,"p":101,"s":1,"tks":"B","t":"2026-09-24T12:00:00Z"}]'
            )
            assert json.loads(await client.recv())[0]["T"] == "t"
        finally:
            await client.close()
            await asyncio.sleep(0)
            assert len(upstream.connections[0].sent) == 3
            await relay.stop()

    asyncio.run(scenario())


def test_bad_upstream_auth_subscription_and_data_are_explicit_unavailable_failures() -> None:
    async def scenario() -> None:
        for auth_response, acknowledge in (
            ('[{"T":"error","code":403}]', True),
            ('[{"T":"success","msg":"authenticated"}]', False),
        ):
            upstream = _Upstream()
            upstream.auth_response, upstream.acknowledge_subscriptions = auth_response, acknowledge
            relay = AlpacaCryptoFeedRelay(
                CryptoRelayCredentials("key", "secret"), port=0, upstream_connect=upstream.connect
            )
            await relay.start()
            for _ in range(100):
                if relay.health()["upstream_state"] == "failed":
                    break
                await asyncio.sleep(0)
            assert relay.health()["reason"] in {
                "upstream_authentication_rejected",
                "upstream_subscription_rejected",
            }
            await relay.stop()

        relay, upstream, url = await _relay()
        client = await _subscribe(url, {"action": "subscribe", "bars": [BTC_USD]})
        try:
            await upstream.emit('[{"T":"b","S":"ETH/USD"}]')
            with pytest.raises(ConnectionClosed):
                await client.recv()
            for _ in range(100):
                if relay.health()["upstream_state"] == "failed":
                    break
                await asyncio.sleep(0)
            assert relay.health()["reason"] == "unexpected_upstream_market_data"
        finally:
            await relay.stop()

    asyncio.run(scenario())


def test_406_is_clear_bounded_reconnecting_state_without_overlapping_sockets() -> None:
    async def scenario() -> None:
        upstream = _Upstream()
        upstream.auth_response = '[{"T":"error","code":406}]'
        relay = AlpacaCryptoFeedRelay(
            CryptoRelayCredentials("key", "secret"),
            port=0,
            upstream_connect=upstream.connect,
            reconnect_delay=lambda _: 60,
        )
        await relay.start()
        try:
            for _ in range(100):
                if relay.health()["upstream_state"] == "reconnecting":
                    break
                await asyncio.sleep(0)
            assert relay.health()["reason"] == "upstream_connection_limit"
            assert len(upstream.connections) == 1 and upstream.connections[0].closed
        finally:
            await relay.stop()

    asyncio.run(scenario())


def test_provider_empty_orderbooks_acknowledgement_is_accepted_but_nonempty_is_rejected() -> None:
    desired = CryptoRelaySubscription(bars=True)
    acknowledgement = {
        "T": "subscription",
        "trades": [],
        "quotes": [],
        "bars": [BTC_USD],
        "updatedBars": [],
        "dailyBars": [],
        "orderbooks": [],
    }

    assert AlpacaCryptoFeedRelay._subscription_acknowledges(acknowledgement, desired)
    assert not AlpacaCryptoFeedRelay._subscription_acknowledges(
        {**acknowledgement, "orderbooks": [BTC_USD]}, desired
    )


def test_expected_provider_event_before_subscription_ack_is_dropped_until_local_ack() -> None:
    async def scenario() -> None:
        upstream = _Upstream()
        upstream.pre_ack_market_data = _bar()
        relay = AlpacaCryptoFeedRelay(
            CryptoRelayCredentials("relay-key", "relay-secret"),
            port=0,
            upstream_connect=upstream.connect,
        )
        await relay.start()
        assert relay._server is not None
        port = next(iter(relay._server.sockets)).getsockname()[1]
        try:
            client = await _subscribe(
                f"ws://127.0.0.1:{port}", {"action": "subscribe", "bars": [BTC_USD]}
            )
            await client.close()
            assert relay.health()["upstream_state"] == "subscribed"
        finally:
            await relay.stop()

    asyncio.run(scenario())


def test_stop_collects_pending_upstream_receive_task() -> None:
    async def scenario() -> None:
        relay, upstream, _ = await _relay()
        upstream.close_with_receive_error = True
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, object]] = []
        prior_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            await relay.stop()
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            loop.set_exception_handler(prior_handler)

    asyncio.run(scenario())


def test_reconnect_closes_old_socket_before_next_and_never_synthesizes_a_bar() -> None:
    async def scenario() -> None:
        relay, upstream, url = await _relay()
        client = await _subscribe(url, {"action": "subscribe", "bars": [BTC_USD]})
        try:
            await upstream.emit(_bar())
            assert await client.recv() == _bar()
            await upstream.emit(OSError("simulated upstream loss"))
            with pytest.raises(ConnectionClosed):
                await client.recv()
            for _ in range(100):
                if len(upstream.connections) == 2:
                    break
                await asyncio.sleep(0)
            assert upstream.connections[0].closed and not upstream.connections[1].closed
        finally:
            await relay.stop()

    asyncio.run(scenario())


def test_slow_client_shutdown_and_localhost_only_lifecycle() -> None:
    async def scenario() -> None:
        relay, upstream, _ = await _relay()

        class _Blocked:
            closed: tuple[int, str] | None = None

            async def send(self, _: str | bytes) -> None:
                await asyncio.Future()

            async def close(self, code: int = 1000, reason: str = "") -> None:
                self.closed = (code, reason)

        blocked = _Blocked()
        from shadow.operations.alpaca_crypto_feed_relay import _Client

        client = _Client(blocked, CryptoRelaySubscription(bars=True))  # type: ignore[arg-type]
        relay._clients.add(client)
        try:
            for _ in range(CLIENT_QUEUE_LIMIT + 1):
                await relay._broadcast(_bar(), json.loads(_bar()))
            await asyncio.sleep(0)
            assert blocked.closed == (1013, "slow_consumer_queue_full")
            assert client.queue.maxsize == CLIENT_QUEUE_LIMIT
        finally:
            await relay.stop()
            assert upstream.connections[0].closed

    asyncio.run(scenario())
    with pytest.raises(ValueError, match="127.0.0.1"):
        AlpacaCryptoFeedRelay(CryptoRelayCredentials("key", "secret"), host="0.0.0.0")


def test_relay_receipt_is_after_provider_frame_and_frame_is_unchanged(tmp_path: Path) -> None:
    async def scenario() -> None:
        order: list[str] = []
        clock = {"wall": 1, "mono": 1}

        def wall_ns() -> int:
            order.append("wall")
            return clock["wall"]

        def monotonic_ns() -> int:
            order.append("mono")
            return clock["mono"]

        evidence = TimingEvidence(
            tmp_path / "relay.jsonl",
            role="relay",
            transport="provider_websocket",
            max_events=4,
            wall_ns=wall_ns,
            host_diagnostics={"sources": [], "trading_authority": False},
        )
        relay, upstream, url = await _relay(
            timing=evidence, wall_ns=wall_ns, monotonic_ns=monotonic_ns
        )

        def arm(value: str) -> None:
            if value == _bar():
                order.append("recv_done")
                clock["wall"] = 5_000
                clock["mono"] = 9_000

        upstream.on_receive_return = arm
        client = await _subscribe(url, {"action": "subscribe", "bars": [BTC_USD]})
        try:
            await upstream.emit(_bar())
            assert await client.recv() == _bar()
            done = order.index("recv_done")
            assert order[done : done + 3] == ["recv_done", "wall", "mono"]
        finally:
            await client.close()
            await relay.stop()
            evidence.close()
        events = load_timing_events(evidence.path)
        text = evidence.path.read_text(encoding="utf-8")
        assert len(events) == 1
        assert events[0]["wall_receive_ns"] == 5_000
        assert events[0]["monotonic_receive_ns"] == 9_000
        assert events[0]["raw_provider_timestamp"] == "2026-09-24T12:00:00.000000000Z"
        assert events[0]["trading_authority"] is False
        assert "relay-secret" not in text
        assert "relay-key" not in text

    asyncio.run(scenario())


def test_downstream_monotonic_receipt_is_not_before_relay_receipt(tmp_path: Path) -> None:
    async def scenario() -> None:
        diagnostics = {"sources": [], "trading_authority": False}
        relay_evidence = TimingEvidence(
            tmp_path / "relay.jsonl",
            role="relay",
            transport="provider_websocket",
            host_diagnostics=diagnostics,
        )
        consumer_evidence = TimingEvidence(
            tmp_path / "consumer.jsonl",
            role="consumer",
            transport="downstream_relay_websocket",
            host_diagnostics=diagnostics,
        )
        relay, upstream, url = await _relay(timing=relay_evidence)
        quote = (
            '[{"T":"q","S":"BTC/USD","t":"2020-01-01T00:00:00.000000000Z",'
            '"bp":1,"ap":2,"bs":1,"as":1}]'
        )
        client = await _subscribe(url, {"action": "subscribe", "quotes": [BTC_USD]})
        try:
            for _ in range(100):
                if len(upstream.connections[0].sent) == 3:
                    break
                await asyncio.sleep(0)
            else:
                raise AssertionError("relay did not add the quote subscription")
            await upstream.emit(quote)
            raw = await client.recv()
            consumer_evidence.observe_frame(raw, ReceiveClocks.capture())
            assert raw == quote
        finally:
            await client.close()
            await relay.stop()
            relay_evidence.close()
            consumer_evidence.close()
        joined = join_timing(
            load_timing_events(relay_evidence.path), load_timing_events(consumer_evidence.path)
        )
        assert len(joined) == 1
        assert joined[0].relay_to_consumer_monotonic_ns >= 0
        assert joined[0].raw_provider_timestamp == "2020-01-01T00:00:00.000000000Z"
        assert joined[0].parsed_observation_matches is True

    asyncio.run(scenario())
