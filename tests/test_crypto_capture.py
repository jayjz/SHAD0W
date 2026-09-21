import asyncio
import json
from pathlib import Path

import pytest

from shadow.adapters.alpaca.crypto_stream import CryptoDataCredentials, CryptoStreamError
from shadow.application.crypto_capture import CaptureLimits, capture_socket
from shadow.application.crypto_evidence import CryptoEvidenceHeader, CryptoEvidenceWriter
from shadow.application.crypto_replay import replay
from shadow.application.crypto_session import CryptoSession


class FakeSocket:
    def __init__(self, frames: list[str]) -> None:
        self.frames = iter(frames)
        self.sent: list[str] = []

    async def recv(self) -> str:
        try:
            return next(self.frames)
        except StopIteration as exc:
            raise TimeoutError from exc

    async def send(self, message: str) -> None:
        self.sent.append(message)


def reset(symbol: str) -> dict[str, object]:
    return {
        "T": "o",
        "S": symbol,
        "t": "2025-01-01T00:00:00Z",
        "r": True,
        "b": [{"p": 1.1, "s": 2.2}],
        "a": [{"p": 1.2, "s": 3.3}],
    }


def test_capture_receipts_precede_decode_and_replay_is_offline(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    writer = CryptoEvidenceWriter(path, CryptoEvidenceHeader("s", "r", {}, {}))
    socket = FakeSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","trades":["BTC/USD","ETH/USD"],"quotes":["BTC/USD","ETH/USD"],"orderbooks":["BTC/USD","ETH/USD"],"bars":[],"updatedBars":[],"dailyBars":[]}]',
            json.dumps([reset("BTC/USD"), reset("ETH/USD")]),
        ]
    )
    records, resets = asyncio.run(
        capture_socket(
            socket,
            CryptoSession(),
            writer,
            CryptoDataCredentials("k", "s"),
            CaptureLimits(duration_seconds=1),
            wall_ns=lambda: 1_735_689_601_000_000_000,
            monotonic_ns=lambda: 8,
        )
    )
    writer.close()
    assert records == 2 and resets == {"BTC/USD", "ETH/USD"}
    lines = path.read_text().splitlines()
    assert json.loads(lines[1])["event"]["availability_time"] == 1_735_689_601_000_000_000
    assert replay(path).btc is not None and replay(path).eth is not None


def test_update_before_reset_is_not_a_successful_capture(tmp_path: Path) -> None:
    writer = CryptoEvidenceWriter(
        tmp_path / "capture.jsonl", CryptoEvidenceHeader("s", "r", {}, {})
    )
    socket = FakeSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","trades":["BTC/USD","ETH/USD"],"quotes":["BTC/USD","ETH/USD"],"orderbooks":["BTC/USD","ETH/USD"],"bars":[],"updatedBars":[],"dailyBars":[]}]',
            json.dumps([{**reset("BTC/USD"), "r": False}]),
        ]
    )
    with pytest.raises(CryptoStreamError, match="reset"):
        asyncio.run(
            capture_socket(
                socket,
                CryptoSession(),
                writer,
                CryptoDataCredentials("k", "s"),
                CaptureLimits(duration_seconds=1),
            )
        )
    writer.close()
