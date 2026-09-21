"""Bounded, market-data-only Alpaca crypto capture into C1 evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from shadow.adapters.alpaca.crypto_normalize import normalize
from shadow.adapters.alpaca.crypto_stream import (
    ENDPOINT,
    CryptoDataCredentials,
    CryptoStreamError,
    auth_request,
    decode_frame,
    is_success,
    subscription_is_exact,
    subscription_request,
)
from shadow.application.crypto_evidence import (
    CryptoCaptureStatus,
    CryptoEvidenceHeader,
    CryptoEvidenceWriter,
    capture_sha256,
)
from shadow.application.crypto_replay import replay
from shadow.application.crypto_session import CryptoSession
from shadow.domain.crypto_market import CryptoBookEvent, UtcNanoseconds


class TransportState(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    SUBSCRIBED = "subscribed"
    CAPTURING = "capturing"
    STOPPED = "stopped"
    FAILED = "failed"


class Socket(Protocol):
    async def recv(self) -> str | bytes: ...
    async def send(self, message: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CaptureLimits:
    duration_seconds: float = 60.0
    max_records: int = 100_000
    reset_deadline_seconds: float = 20.0
    reconnect_attempts: int = 1

    def __post_init__(self) -> None:
        if not 0 < self.duration_seconds <= 3600:
            raise ValueError("duration must be within (0, 3600] seconds")
        if self.max_records < 1 or self.reset_deadline_seconds <= 0 or self.reconnect_attempts < 0:
            raise ValueError("invalid bounded capture limits")


@dataclass(frozen=True, slots=True)
class CaptureResult:
    status: CryptoCaptureStatus
    state: TransportState
    records: int
    btc_reset_seen: bool
    eth_reset_seen: bool
    capture_sha256: str
    replay_digest: str


async def capture_socket(
    socket: Socket,
    session: CryptoSession,
    writer: CryptoEvidenceWriter,
    credentials: CryptoDataCredentials,
    limits: CaptureLimits,
    *,
    wall_ns: Callable[[], int] = time.time_ns,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> tuple[int, set[str]]:
    """Capture one established socket. Receipt clocks are sampled before JSON decoding."""
    frame_sequence = 0
    records = 0
    resets: set[str] = set()
    if not is_success(decode_frame(await socket.recv()), "connected"):
        raise CryptoStreamError("connection acknowledgement rejected")
    await socket.send(auth_request(credentials))
    if not is_success(decode_frame(await socket.recv()), "authenticated"):
        raise CryptoStreamError("authentication rejected")
    await socket.send(subscription_request())
    if not subscription_is_exact(decode_frame(await socket.recv())):
        raise CryptoStreamError("subscription acknowledgement mismatch")
    session.establish_connection()
    capture_started = monotonic_ns()
    while records < limits.max_records:
        elapsed = monotonic_ns() - capture_started
        remaining = int(limits.duration_seconds * 1_000_000_000) - elapsed
        reset_remaining = int(limits.reset_deadline_seconds * 1_000_000_000) - elapsed
        if remaining <= 0:
            break
        if reset_remaining <= 0 and resets != {"BTC/USD", "ETH/USD"}:
            raise CryptoStreamError("reset acquisition deadline expired")
        wait_ns = (
            remaining
            if resets == {"BTC/USD", "ETH/USD"}
            else min(remaining, max(reset_remaining, 1))
        )
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=wait_ns / 1_000_000_000)
        except TimeoutError:
            if resets != {"BTC/USD", "ETH/USD"}:
                raise CryptoStreamError("reset acquisition deadline expired") from None
            break
        received_wall, received_monotonic = wall_ns(), monotonic_ns()
        frame = decode_frame(raw)
        for element_index, payload in enumerate(frame):
            if payload.get("T") == "error":
                raise CryptoStreamError("provider returned an error")
            if payload.get("T") not in ("t", "q", "o"):
                raise CryptoStreamError("unexpected provider control message")
            normalized = normalize(payload, received_at=UtcNanoseconds(received_wall))
            result = session.process(normalized.event)
            raw_bytes = raw.encode() if isinstance(raw, str) else raw
            writer.append(
                result,
                frame_sequence=frame_sequence,
                element_index=element_index,
                source_timestamp=normalized.source_timestamp,
                receipt_monotonic_ns=received_monotonic,
                raw_frame_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            )
            records += 1
            if (
                isinstance(normalized.event, CryptoBookEvent)
                and normalized.event.action.value == "reset"
            ):
                resets.add(normalized.event.instrument.identifier)
            if records >= limits.max_records:
                raise CryptoStreamError("record limit exhausted")
        frame_sequence += 1
    if resets != {"BTC/USD", "ETH/USD"}:
        raise CryptoStreamError("required book resets not observed")
    return records, resets


async def run_capture(
    path: Path,
    session_id: str,
    code_revision: str,
    limits: CaptureLimits,
    credentials: CryptoDataCredentials,
    *,
    connect: object | None = None,
) -> CaptureResult:
    """Run a finite capture with one bounded reconnect; no provider fallback."""
    if connect is None:
        from websockets.asyncio.client import connect as websocket_connect

        connect = websocket_connect
    writer = CryptoEvidenceWriter(
        path,
        CryptoEvidenceHeader(
            session_id,
            code_revision,
            {"crypto_capture": "1", "crypto_normalize": "1"},
            {
                "receipt_wall_clock": "time.time_ns",
                "receipt_monotonic": "time.monotonic_ns",
                "numbers": "json Decimal",
            },
            {
                "duration_seconds": limits.duration_seconds,
                "max_records": limits.max_records,
                "reset_deadline_seconds": limits.reset_deadline_seconds,
                "reconnect_attempts": limits.reconnect_attempts,
                "max_frame_bytes": 1_048_576,
                "max_queue": 16,
            },
        ),
    )
    session = CryptoSession()
    records, resets = 0, set()
    status = CryptoCaptureStatus.FAILED
    try:
        for attempt in range(limits.reconnect_attempts + 1):
            try:
                async with connect(  # type: ignore[operator]
                    ENDPOINT,
                    proxy=None,
                    max_size=1_048_576,
                    max_queue=16,
                    open_timeout=5,
                    close_timeout=2,
                ) as socket:
                    count, seen = await capture_socket(socket, session, writer, credentials, limits)
                    records += count
                    resets |= seen
                    status = CryptoCaptureStatus.COMPLETE_STOPPED
                    break
            except (OSError, TimeoutError, CryptoStreamError):
                session.disconnect()
                if attempt == limits.reconnect_attempts:
                    raise
        return CaptureResult(
            status,
            TransportState.STOPPED,
            records,
            "BTC/USD" in resets,
            "ETH/USD" in resets,
            "",
            "",
        )
    except Exception:
        status = CryptoCaptureStatus.FAILED
        return CaptureResult(
            status, TransportState.FAILED, records, "BTC/USD" in resets, "ETH/USD" in resets, "", ""
        )
    finally:
        writer.close(status)


def main() -> None:
    parser = argparse.ArgumentParser(description="SHAD0W bounded crypto market-data capture")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--evidence-path", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    args = parser.parse_args()
    credentials = CryptoDataCredentials.from_environment(os.environ)
    result = asyncio.run(
        run_capture(
            args.evidence_path,
            args.session_id,
            args.code_revision,
            CaptureLimits(duration_seconds=args.duration_seconds),
            credentials,
        )
    )
    replay_result = replay(args.evidence_path)
    print("SHAD0W CRYPTO MARKET-DATA ONLY")
    print(f"status: {result.status.value}")
    print(f"session: {args.session_id}")
    print(f"records: {result.records}")
    print(f"btc_reset_seen: {result.btc_reset_seen}")
    print(f"eth_reset_seen: {result.eth_reset_seen}")
    print(f"capture_sha256: {capture_sha256(args.evidence_path)}")
    print(f"replay_digest: {replay_result.digest}")
