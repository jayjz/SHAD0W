"""Read-only crypto timing evidence. These tests grant no trading authority."""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

import pytest

from shadow.adapters.alpaca.crypto_normalize import normalize, parse_timestamp
from shadow.application.crypto_paper import _relay_live
from shadow.domain.crypto_market import UtcNanoseconds
from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import AvailabilitySemantics
from shadow.operations.crypto_timing import (
    JoinedTiming,
    ReceiveClocks,
    TimingCause,
    TimingEvidence,
    classify_timing,
    format_timing_report,
    load_timing_events,
    read_host_clock_diagnostics,
    validate_local_crypto_relay_url,
)

OBSERVATION = parse_timestamp("2026-09-25T12:00:00.000000000Z").value
LEAD = 300_000_000


class _Socket:
    def __init__(self, frames: list[str], on_market_recv: Callable[[], None]) -> None:
        self._frames = iter(frames)
        self._on_market_recv = on_market_recv
        self.receives = 0
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        self.receives += 1
        frame = next(self._frames)
        if self.receives == 2:
            self._on_market_recv()
        return frame

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


class _Context:
    def __init__(self, socket: _Socket) -> None:
        self.socket = socket
        self.exited = False

    async def __aenter__(self) -> _Socket:
        return self.socket

    async def __aexit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.exited = True


def _connect(context: _Context) -> Callable[..., _Context]:
    def connect(*_args: object, **_kwargs: object) -> _Context:
        return context

    return connect


def _quote(stamp: str, *, secret: str | None = None) -> str:
    payload: dict[str, object] = {
        "T": "q",
        "S": "BTC/USD",
        "t": stamp,
        "bp": "100",
        "ap": "101",
        "bs": "1",
        "as": "1",
    }
    if secret is not None:
        payload["secret"] = secret
    return json.dumps([payload])


def _evidence(path: Path, wall_ns: Callable[[], int]) -> TimingEvidence:
    return TimingEvidence(
        path,
        role="consumer",
        transport="downstream_relay_websocket",
        max_events=4,
        wall_ns=wall_ns,
        host_diagnostics={"sources": [], "trading_authority": False},
    )


def _ack() -> str:
    return json.dumps(
        [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"], "bars": []}]
    )


def test_consumer_receipt_is_after_downstream_receive(tmp_path: Path) -> None:
    phase: list[str] = []
    walls = iter((OBSERVATION + 50, OBSERVATION + 80))
    monos = iter((70,))

    def wall_ns() -> int:
        phase.append("wall")
        return next(walls)

    def monotonic_ns() -> int:
        phase.append("mono")
        return next(monos)

    def on_market() -> None:
        phase.append("recv_done")

    socket = _Socket([_ack(), _quote("2026-09-25T12:00:00.000000000Z")], on_market)
    evidence = _evidence(tmp_path / "consumer.jsonl", wall_ns)
    source = _relay_live(
        "ws://127.0.0.1:8766",
        1,
        connect=_connect(_Context(socket)),
        wall_ns=wall_ns,
        monotonic_ns=monotonic_ns,
        timing_evidence=evidence,
    )
    event = next(source)
    source.close()
    evidence.close()
    assert phase == ["recv_done", "wall", "mono", "wall"]
    assert event.availability_time.value == OBSERVATION + 50
    assert event.availability_semantics is AvailabilitySemantics.SYSTEM_RECEIVED
    recorded = load_timing_events(evidence.path)
    assert recorded[0]["wall_receive_ns"] == OBSERVATION + 50
    assert recorded[0]["monotonic_receive_ns"] == 70
    assert recorded[0]["host_utc_sample_ns"] == OBSERVATION + 80
    assert recorded[0]["causal_order_ok"] is True
    assert [json.loads(message)["action"] for message in socket.sent] == ["subscribe"]


def test_provider_lead_of_300ms_is_recorded_and_fail_closed(tmp_path: Path) -> None:
    stamp = "2026-09-25T12:00:00.300000000Z"
    observed = parse_timestamp(stamp).value
    assert observed - parse_timestamp("2026-09-25T12:00:00.000000000Z").value == LEAD

    def wall_ns() -> int:
        return observed - LEAD

    def monotonic_ns() -> int:
        return 10

    evidence = _evidence(tmp_path / "future.jsonl", wall_ns)
    socket = _Socket([_ack(), _quote(stamp, secret="super-secret-value")], lambda: None)
    source = _relay_live(
        "ws://127.0.0.1:8766",
        1,
        connect=_connect(_Context(socket)),
        wall_ns=wall_ns,
        monotonic_ns=monotonic_ns,
        timing_evidence=evidence,
    )
    with pytest.raises(MarketDataValidationError, match="must not precede observation_time"):
        next(source)
    source.close()
    evidence.close()
    recorded = load_timing_events(evidence.path)
    text = evidence.path.read_text(encoding="utf-8")
    assert recorded[0]["raw_provider_timestamp"] == stamp
    assert recorded[0]["observation_time_ns"] == observed
    assert recorded[0]["provider_to_wall_delta_ns"] == -LEAD
    assert recorded[0]["causal_order_ok"] is False
    assert recorded[0]["trading_authority"] is False
    assert "does not apply a tolerance" in str(recorded[0]["note"])
    assert "super-secret-value" not in text
    with pytest.raises(MarketDataValidationError, match="availability_time"):
        normalize(
            json.loads(_quote(stamp))[0],
            received_at=UtcNanoseconds(observed - 1),
        )
    accepted = normalize(json.loads(_quote(stamp))[0], received_at=UtcNanoseconds(observed))
    assert accepted.event.availability_time.value == observed


def test_equal_receipt_is_causal_and_stale_receipt_is_not_rewritten(tmp_path: Path) -> None:
    stamp = "2026-09-25T11:00:00.000000000Z"
    observed = parse_timestamp(stamp).value
    receipt = observed + 3_600_000_000_000

    def wall_ns() -> int:
        return receipt

    evidence = _evidence(tmp_path / "stale.jsonl", wall_ns)
    socket = _Socket([_ack(), _quote(stamp)], lambda: None)
    source = _relay_live(
        "ws://127.0.0.1:8766",
        1,
        connect=_connect(_Context(socket)),
        wall_ns=wall_ns,
        monotonic_ns=lambda: 3,
        timing_evidence=evidence,
    )
    event = next(source)
    source.close()
    evidence.close()
    assert event.observation_time.value == observed
    assert event.availability_time.value == receipt
    recorded = load_timing_events(evidence.path)[0]
    assert recorded["raw_provider_timestamp"] == stamp
    assert recorded["stale_evidence"] is True
    assert recorded["causal_order_ok"] is True
    assert recorded["provider_to_wall_delta_ns"] == 3_600_000_000_000


def test_malformed_timestamp_is_not_repaired(tmp_path: Path) -> None:
    def wall_ns() -> int:
        return OBSERVATION

    evidence = _evidence(tmp_path / "bad.jsonl", wall_ns)
    socket = _Socket([_ack(), _quote("not-a-timestamp")], lambda: None)
    source = _relay_live(
        "ws://127.0.0.1:8766",
        1,
        connect=_connect(_Context(socket)),
        wall_ns=wall_ns,
        monotonic_ns=lambda: 4,
        timing_evidence=evidence,
    )
    with pytest.raises(MarketDataValidationError):
        next(source)
    source.close()
    evidence.close()
    recorded = load_timing_events(evidence.path)[0]
    assert recorded["parse_error"] == "malformed timestamp"
    assert recorded["raw_provider_timestamp"] is None
    assert recorded["observation_time_ns"] is None
    assert "not-a-timestamp" not in evidence.path.read_text(encoding="utf-8")


def test_nanosecond_conversion_is_exact() -> None:
    base = parse_timestamp("2026-09-25T12:00:00.000000000Z").value
    lead = parse_timestamp("2026-09-25T12:00:00.300000000Z").value
    fractional = parse_timestamp("2026-09-25T12:55:24.059571000Z").value
    assert lead - base == 300_000_000
    assert fractional - parse_timestamp("2026-09-25T12:55:24Z").value == 59_571_000
    assert parse_timestamp("2026-09-25T12:00:00.300000000Z").value == lead


def test_no_causal_tolerance_was_added() -> None:
    source = Path("src/shadow/domain/crypto_market.py").read_text(encoding="utf-8")
    assert "tolerance" not in source
    paper = Path("src/shadow/application/crypto_paper.py").read_text(encoding="utf-8")
    assert "classify_timing" not in paper
    module = ast.parse(Path("src/shadow/operations/crypto_timing.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    assert not any(
        name.startswith(("shadow.execution", "shadow.risk", "shadow.strategies"))
        for name in imported
    )


def test_timing_evidence_serialization_has_no_trading_authority(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path / "round.jsonl", lambda: 5)
    evidence.observe_frame(
        _quote("2026-09-25T12:00:00.000000000Z"),
        ReceiveClocks(OBSERVATION + 5, 9),
    )
    evidence.close()
    text = evidence.path.read_text(encoding="utf-8")
    assert text.count('"trading_authority":false') >= 2
    assert '"trading_authority":true' not in text
    record = load_timing_events(evidence.path)[0]
    assert isinstance(record["wall_receive_ns"], int)
    assert record["schema"] == "shadow.crypto-timing.v1"
    report = format_timing_report((), classify_timing((), {"sources": []}))
    assert "trading_authority: false" in report
    with pytest.raises(ValueError, match="8766"):
        validate_local_crypto_relay_url("wss://stream.data.alpaca.markets/v1beta3/crypto/us")


def test_host_clock_diagnostics_parse_without_becoming_authority() -> None:
    def runner(argv: list[str]) -> str | None:
        if argv[-1] == "NTPSynchronized":
            return "NTP=yes\nNTPSynchronized=yes\n"
        return (
            "Server: ntp.ubuntu.com (91.189.91.157)\n"
            "Stratum: 2\n"
            "Offset: -66.669ms\n"
            "Delay: 98.075ms\n"
            "Jitter: 346.799ms\n"
            "Frequency: +142.470ppm\n"
        )

    diagnostics = read_host_clock_diagnostics(
        command_runner=runner,
        kernel_reader=lambda: {
            "source": "kernel-adjtimex",
            "available": True,
            "status_unsynchronized": False,
            "offset_ns": -66_669_000,
            "jitter_ns": 1_000,
            "maxerror_ns": 2_000_000,
        },
    )
    sources = diagnostics["sources"]
    assert isinstance(sources, list)
    timedate = sources[0]
    assert isinstance(timedate, dict)
    assert timedate["offset_ns"] == -66_669_000
    assert timedate["jitter_ns"] == 346_799_000
    assert timedate["ntp_synchronized"] is True
    assert diagnostics["trading_authority"] is False


def _joined(
    *,
    relay_delta: int,
    consumer_delta: int,
    mono: int,
    matches: bool = True,
    stamp: str = "2026-09-25T12:00:00.300000000Z",
) -> JoinedTiming:
    observed = parse_timestamp(stamp).value
    relay_wall = observed + relay_delta
    consumer_wall = observed + consumer_delta
    return JoinedTiming(
        "abc",
        "q",
        "BTC/USD",
        stamp,
        observed,
        matches,
        relay_wall,
        consumer_wall,
        1_000,
        1_000 + mono,
        relay_wall + 20,
        consumer_wall + 20,
    )


def _host(
    *,
    synchronized: bool | None,
    offset_ns: int | None,
    jitter_ns: int | None,
    unsync: bool = False,
    maxerror_ns: int | None = 1_000_000,
) -> dict[str, object]:
    timedate: dict[str, object] = {"source": "timedatectl", "available": synchronized is not None}
    if synchronized is not None:
        timedate["ntp_synchronized"] = synchronized
    if offset_ns is not None:
        timedate["offset_ns"] = offset_ns
    if jitter_ns is not None:
        timedate["jitter_ns"] = jitter_ns
    kernel: dict[str, object] = {
        "source": "kernel-adjtimex",
        "available": True,
        "status_unsynchronized": unsync,
    }
    if maxerror_ns is not None:
        kernel["maxerror_ns"] = maxerror_ns
    return {"sources": [timedate, kernel], "trading_authority": False}


def test_classification_preserves_fail_closed_causes() -> None:
    unsynchronized = classify_timing(
        (_joined(relay_delta=-LEAD, consumer_delta=-LEAD + 40_000, mono=40_000),),
        _host(
            synchronized=False,
            offset_ns=None,
            jitter_ns=None,
            unsync=True,
            maxerror_ns=16_000_000_000,
        ),
    )
    assert unsynchronized.cause is TimingCause.HOST_WALL_CLOCK
    assert unsynchronized.reproduced_inversion is True
    assert unsynchronized.trading_authority is False

    matched_offset = classify_timing(
        (_joined(relay_delta=-LEAD, consumer_delta=-LEAD + 10_000, mono=10_000),),
        _host(synchronized=True, offset_ns=250_000_000, jitter_ns=20_000_000),
    )
    assert matched_offset.cause is TimingCause.HOST_WALL_CLOCK
    assert matched_offset.confidence == "high"

    supplied = classify_timing(
        (
            _joined(relay_delta=-299_900_000, consumer_delta=-299_890_000, mono=200_000),
            _joined(relay_delta=-299_655_000, consumer_delta=-299_640_000, mono=180_000),
        ),
        _host(
            synchronized=True,
            offset_ns=-66_669_000,
            jitter_ns=346_799_000,
            maxerror_ns=5_000_000,
        ),
    )
    assert supplied.cause is TimingCause.INSUFFICIENT
    assert supplied.reproduced_inversion is True
    assert "jitter" in supplied.summary

    provider = classify_timing(
        (_joined(relay_delta=-LEAD, consumer_delta=-LEAD + 5_000, mono=5_000),),
        _host(synchronized=True, offset_ns=-200_000, jitter_ns=50_000, maxerror_ns=1_000_000),
    )
    assert provider.cause is TimingCause.PROVIDER_TIMESTAMP_SEMANTICS

    conversion = classify_timing(
        (_joined(relay_delta=-LEAD, consumer_delta=-LEAD, mono=1, matches=False),),
        _host(synchronized=True, offset_ns=250_000_000, jitter_ns=1_000),
    )
    assert conversion.cause is TimingCause.CONVERSION_PRECISION

    early_consumer = classify_timing(
        (_joined(relay_delta=-LEAD, consumer_delta=-LEAD - 2_000_000, mono=-2_000_000),),
        _host(synchronized=True, offset_ns=250_000_000, jitter_ns=1_000),
    )
    assert early_consumer.cause is TimingCause.CONSUMER_CAPTURE_DEFECT

    consumer_only = classify_timing(
        (_joined(relay_delta=1_000_000, consumer_delta=-LEAD, mono=20_000),),
        _host(synchronized=True, offset_ns=0, jitter_ns=1_000),
    )
    assert consumer_only.cause is TimingCause.CONSUMER_CAPTURE_DEFECT

    relay_only = classify_timing(
        (_joined(relay_delta=-LEAD, consumer_delta=1_000_000, mono=20_000),),
        _host(synchronized=True, offset_ns=0, jitter_ns=1_000),
    )
    assert relay_only.cause is TimingCause.RELAY_CAPTURE_DEFECT

    fresh = classify_timing(
        (_joined(relay_delta=1_000_000, consumer_delta=1_200_000, mono=20_000),),
        _host(synchronized=True, offset_ns=-66_669_000, jitter_ns=346_799_000),
    )
    assert fresh.cause is TimingCause.INSUFFICIENT
    assert fresh.reproduced_inversion is False
    assert classify_timing((), {"sources": []}).cause is TimingCause.INSUFFICIENT


def test_direct_provider_url_is_rejected_for_the_read_only_observer() -> None:
    with pytest.raises(ValueError, match="market-data relay"):
        validate_local_crypto_relay_url("ws://127.0.0.1:8765")
    assert validate_local_crypto_relay_url("ws://127.0.0.1:8766") == "ws://127.0.0.1:8766"
