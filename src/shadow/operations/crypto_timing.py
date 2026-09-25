"""Read-only crypto transport timing evidence. No trading authority.

Relay and consumer stamps are different moments. They are not domain
observation or availability times, and nothing in this module may authorize,
submit, or rewrite an order or a provider timestamp.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import re
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from shadow.adapters.alpaca.crypto_normalize import parse_timestamp
from shadow.domain.errors import MarketDataValidationError

TIMING_SCHEMA = "shadow.crypto-timing.v1"
MAX_TIMING_EVENTS = 256
# Label only. This is not a causal tolerance and grants no trading freshness.
STALE_EVIDENCE_LABEL_NS = 5_000_000_000
_WALL_CLOCK_NOTE = (
    "local wall-clock receipt is earlier than provider observation; "
    "SHAD0W fails closed and does not apply a tolerance. "
    "Local wall-clock authority may be insufficient."
)
_SYMBOL = re.compile(r"^[A-Z0-9]{1,8}/[A-Z0-9]{1,8}$")
_KEY_FIELDS = (
    "T",
    "S",
    "t",
    "i",
    "p",
    "s",
    "tks",
    "bp",
    "ap",
    "bs",
    "as",
    "o",
    "h",
    "l",
    "c",
    "v",
)
_MARKET_KINDS = {"t", "q", "b"}
_DURATION = re.compile(r"([+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))\s*(ns|us|µs|ms|s)\b")
_PPM = re.compile(r"([+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))\s*ppm\b", re.IGNORECASE)


class Socket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class TimingCause(StrEnum):
    """Diagnostic classification only. It never relaxes a causal check."""

    HOST_WALL_CLOCK = "host_wall_clock_synchronization"
    PROVIDER_TIMESTAMP_SEMANTICS = "provider_timestamp_semantics"
    RELAY_CAPTURE_DEFECT = "relay_timestamp_capture_defect"
    CONSUMER_CAPTURE_DEFECT = "consumer_timestamp_capture_defect"
    CONVERSION_PRECISION = "timestamp_conversion_precision"
    OTHER = "another_demonstrated_cause"
    INSUFFICIENT = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class ReceiveClocks:
    """Wall and monotonic samples from one post-receive boundary."""

    wall_ns: int
    monotonic_ns: int

    @staticmethod
    def capture(
        wall_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> ReceiveClocks:
        # Wall is sampled first so availability can reuse this exact integer.
        return ReceiveClocks(wall_ns(), monotonic_ns())


@dataclass(frozen=True, slots=True)
class JoinedTiming:
    """One provider event observed at both the relay and the consumer."""

    event_key: str
    kind: str
    symbol: str | None
    raw_provider_timestamp: str
    observation_time_ns: int
    parsed_observation_matches: bool
    relay_wall_receive_ns: int
    consumer_wall_receive_ns: int
    relay_monotonic_receive_ns: int
    consumer_monotonic_receive_ns: int
    relay_host_utc_sample_ns: int
    consumer_host_utc_sample_ns: int

    @property
    def provider_to_relay_wall_ns(self) -> int:
        return self.relay_wall_receive_ns - self.observation_time_ns

    @property
    def provider_to_consumer_wall_ns(self) -> int:
        return self.consumer_wall_receive_ns - self.observation_time_ns

    @property
    def relay_to_consumer_monotonic_ns(self) -> int:
        return self.consumer_monotonic_receive_ns - self.relay_monotonic_receive_ns

    @property
    def relay_to_consumer_wall_ns(self) -> int:
        return self.consumer_wall_receive_ns - self.relay_wall_receive_ns


@dataclass(frozen=True, slots=True)
class TimingClassification:
    cause: TimingCause
    confidence: str
    reproduced_inversion: bool
    summary: str
    trading_authority: bool = False

    def __post_init__(self) -> None:
        if self.trading_authority:
            raise ValueError("timing classification has no trading authority")
        if self.confidence not in {"low", "medium", "high"}:
            raise ValueError("timing confidence must be low, medium, or high")


def validate_local_crypto_relay_url(url: str) -> str:
    """Accept only the fixed localhost crypto relay. No provider fallback."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("market-data relay must use a valid fixed localhost port") from error
    if (
        parsed.scheme != "ws"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port != 8766
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("market-data relay must be ws://127.0.0.1:8766 or ws://localhost:8766")
    return url


def _canonical_part(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if type(value) is int and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and len(value) <= 80 and "\n" not in value and "\r" not in value:
        return value
    return ""


def event_key_for_payload(payload: Mapping[str, object]) -> str:
    parts = [f"{field}={_canonical_part(payload.get(field))}" for field in _KEY_FIELDS]
    return hashlib.sha256("\n".join(parts).encode("ascii", "replace")).hexdigest()


def _safe_symbol(value: object) -> str | None:
    if isinstance(value, str) and _SYMBOL.fullmatch(value):
        return value
    return None


def _duration_to_ns(number: str, unit: str) -> int:
    scale = {"ns": 1, "us": 1_000, "µs": 1_000, "ms": 1_000_000, "s": 1_000_000_000}[unit]
    return int((Decimal(number) * scale).to_integral_value(rounding=ROUND_HALF_EVEN))


def _parse_show(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = raw.strip()
    return values


def _parse_timesync_text(text: str) -> dict[str, object]:
    """Parse timedatectl timesync-status without treating its numbers as authority."""
    result: dict[str, object] = {"source": "timedatectl-timesync-status"}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, raw = line.split(":", 1)
        key, value = label.strip().lower(), raw.strip()
        if key == "server":
            server = value.split(" (", 1)[0].strip()
            if server and len(server) <= 128 and " " not in server:
                result["server"] = server
        elif key == "stratum" and value.isdigit():
            result["stratum"] = int(value)
        elif key in {"offset", "delay", "jitter"}:
            match = _DURATION.search(value)
            if match is not None:
                result[f"{key}_ns"] = _duration_to_ns(match.group(1), match.group(2))
                result[f"{key}_text"] = match.group(0).replace(" ", "")
        elif key == "frequency":
            match = _PPM.search(value)
            if match is not None:
                result["frequency_ppm_text"] = match.group(0).replace(" ", "")
    return result


def _kernel_timex() -> dict[str, object]:
    """Read-only adjtimex(modes=0). Never steps or slews the clock."""

    class Timeval(ctypes.Structure):
        _fields_ = (("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long))

    class Timex(ctypes.Structure):
        _fields_ = (
            ("modes", ctypes.c_uint),
            ("_pad0", ctypes.c_int),
            ("offset", ctypes.c_longlong),
            ("freq", ctypes.c_longlong),
            ("maxerror", ctypes.c_longlong),
            ("esterror", ctypes.c_longlong),
            ("status", ctypes.c_int),
            ("_pad1", ctypes.c_int),
            ("constant", ctypes.c_longlong),
            ("precision", ctypes.c_longlong),
            ("tolerance", ctypes.c_longlong),
            ("time", Timeval),
            ("tick", ctypes.c_longlong),
            ("ppsfreq", ctypes.c_longlong),
            ("jitter", ctypes.c_longlong),
            ("shift", ctypes.c_int),
            ("_pad2", ctypes.c_int),
            ("stabil", ctypes.c_longlong),
            ("jitcnt", ctypes.c_longlong),
            ("calcnt", ctypes.c_longlong),
            ("errcnt", ctypes.c_longlong),
            ("stbcnt", ctypes.c_longlong),
            ("tai", ctypes.c_int),
            ("_pad3", ctypes.c_int * 11),
        )

    if ctypes.sizeof(Timex) != 208:
        return {"source": "kernel-adjtimex", "available": False, "detail": "unexpected timex size"}
    libc = ctypes.CDLL(None, use_errno=True)
    libc.adjtimex.argtypes = [ctypes.POINTER(Timex)]
    libc.adjtimex.restype = ctypes.c_int
    sample = Timex()
    if libc.adjtimex(ctypes.byref(sample)) < 0:
        error = ctypes.get_errno()
        return {
            "source": "kernel-adjtimex",
            "available": False,
            "detail": f"adjtimex failed: {os.strerror(error)}",
        }
    nano = bool(sample.status & 0x2000)
    offset_scale = 1 if nano else 1_000
    return {
        "source": "kernel-adjtimex",
        "available": True,
        "status_unsynchronized": bool(sample.status & 0x0040),
        "offset_nanosecond_resolution": nano,
        "offset_ns": int(sample.offset) * offset_scale,
        "jitter_ns": int(sample.jitter) * offset_scale,
        "maxerror_ns": int(sample.maxerror) * 1_000,
        "esterror_ns": int(sample.esterror) * 1_000,
        "frequency_ppm_text": format(Decimal(int(sample.freq)) / Decimal(65536), "f"),
        "tai": int(sample.tai),
        "detail": "kernel discipline only; not a systemd-timesyncd offset",
    }


def _default_command(argv: list[str]) -> str | None:
    try:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def read_host_clock_diagnostics(
    *,
    command_runner: Callable[[list[str]], str | None] | None = None,
    kernel_reader: Callable[[], dict[str, object]] | None = None,
) -> dict[str, object]:
    """Bounded host clock snapshot. Missing tools are reported, not invented."""
    runner = command_runner or _default_command
    show = runner(["timedatectl", "show", "-p", "NTP", "-p", "NTPSynchronized"])
    status = runner(["timedatectl", "timesync-status"])
    timedate: dict[str, object] = {"source": "timedatectl", "available": False}
    if show is None and status is None:
        timedate["detail"] = "timedatectl unavailable"
    else:
        timedate["available"] = True
        if show is not None:
            values = _parse_show(show)
            if "NTP" in values:
                timedate["ntp"] = values["NTP"] == "yes"
            if "NTPSynchronized" in values:
                timedate["ntp_synchronized"] = values["NTPSynchronized"] == "yes"
        if status is not None:
            timedate.update(_parse_timesync_text(status))
        else:
            timedate["detail"] = "timesync-status unavailable"
    try:
        kernel = (kernel_reader or _kernel_timex)()
    except (OSError, AttributeError, ctypes.ArgumentError):
        kernel = {"source": "kernel-adjtimex", "available": False, "detail": "adjtimex unavailable"}
    return {
        "trading_authority": False,
        "offset_sign": (
            "NTP/timesyncd offset is the correction added to the local clock. "
            "Positive means the local clock is behind the reference."
        ),
        "sources": [timedate, kernel],
    }


class TimingEvidence:
    """Append-only JSONL timing evidence with a hard event cap."""

    def __init__(
        self,
        path: Path,
        *,
        role: str,
        transport: str,
        max_events: int = 32,
        wall_ns: Callable[[], int] = time.time_ns,
        host_diagnostics: Mapping[str, object] | None = None,
        code_revision: str = "unspecified",
    ) -> None:
        if role not in {"relay", "consumer"}:
            raise ValueError("timing evidence role must be relay or consumer")
        if transport not in {
            "provider_websocket",
            "downstream_relay_websocket",
            "direct_provider_websocket",
        }:
            raise ValueError("unsupported timing transport")
        if not isinstance(max_events, int) or not 1 <= max_events <= MAX_TIMING_EVENTS:
            raise ValueError("timing evidence event bound must be within 1..256")
        self.path = path
        self.role = role
        self.transport = transport
        self.max_events = max_events
        self._wall_ns = wall_ns
        self._count = 0
        self._truncated = False
        self._closed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("x", encoding="utf-8")
        self._write(
            {
                "schema": TIMING_SCHEMA,
                "record": "header",
                "role": role,
                "transport": transport,
                "max_events": max_events,
                "code_revision": code_revision,
                "trading_authority": False,
                "clock_sources": {
                    "wall_receive": "time.time_ns immediately after websocket recv returns",
                    "monotonic_receive": "time.monotonic_ns at that same boundary",
                    "host_utc_sample": "time.time_ns when the diagnostic record is finalized",
                },
                "causal_invariant": "observation_time <= availability_time remains fail-closed",
                "host_clock": dict(host_diagnostics)
                if host_diagnostics is not None
                else read_host_clock_diagnostics(),
            }
        )

    def observe_frame(
        self, raw: str | bytes, clocks: ReceiveClocks
    ) -> tuple[dict[str, object], ...]:
        """Record market-data elements. Control and auth frames are ignored."""
        if self._closed or self._truncated:
            return ()
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        frame_sha = hashlib.sha256(encoded).hexdigest()
        try:
            text = raw if isinstance(raw, str) else raw.decode("utf-8")
            decoded = json.loads(text, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return (self._emit_malformed(clocks, frame_sha, "malformed provider frame"),)
        if not isinstance(decoded, list):
            return (self._emit_malformed(clocks, frame_sha, "malformed provider frame"),)
        records: list[dict[str, object]] = []
        for index, item in enumerate(decoded):
            if self._count >= self.max_events:
                self._truncated = True
                break
            if not isinstance(item, Mapping):
                records.append(
                    self._emit_malformed(clocks, frame_sha, "malformed provider element")
                )
                continue
            kind = item.get("T")
            if kind not in _MARKET_KINDS:
                continue
            records.append(self._emit_market(item, clocks, frame_sha, index, str(kind)))
        return tuple(records)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._write(
            {
                "schema": TIMING_SCHEMA,
                "record": "footer",
                "role": self.role,
                "events": self._count,
                "truncated": self._truncated,
                "trading_authority": False,
            }
        )
        self._handle.close()

    def _emit_malformed(
        self, clocks: ReceiveClocks, frame_sha: str, reason: str
    ) -> dict[str, object]:
        return self._emit(
            {
                "kind": "malformed",
                "symbol": None,
                "raw_provider_timestamp": None,
                "observation_time_ns": None,
                "element_index": -1,
                "frame_sha256": frame_sha,
                "event_key": frame_sha,
                "parse_error": reason,
                "provider_to_wall_delta_ns": None,
                "causal_order_ok": None,
                "stale_evidence": None,
                "note": None,
            },
            clocks,
        )

    def _emit_market(
        self,
        item: Mapping[str, object],
        clocks: ReceiveClocks,
        frame_sha: str,
        index: int,
        kind: str,
    ) -> dict[str, object]:
        raw_timestamp = item.get("t") if isinstance(item.get("t"), str) else None
        observation: int | None = None
        parse_error: str | None = None
        stored_timestamp: str | None = None
        if not isinstance(raw_timestamp, str):
            parse_error = "missing timestamp"
        else:
            try:
                observation = parse_timestamp(raw_timestamp).value
            except MarketDataValidationError:
                parse_error = "malformed timestamp"
            else:
                stored_timestamp = raw_timestamp
                again = parse_timestamp(raw_timestamp).value
                if again != observation:
                    parse_error = "timestamp conversion is not repeatable"
        delta = None if observation is None else clocks.wall_ns - observation
        causal = None if delta is None else delta >= 0
        note = _WALL_CLOCK_NOTE if delta is not None and delta < 0 else None
        return self._emit(
            {
                "kind": kind,
                "symbol": _safe_symbol(item.get("S")),
                "raw_provider_timestamp": stored_timestamp,
                "observation_time_ns": observation,
                "element_index": index,
                "frame_sha256": frame_sha,
                "event_key": event_key_for_payload(item),
                "parse_error": parse_error,
                "provider_to_wall_delta_ns": delta,
                "causal_order_ok": causal,
                "stale_evidence": None if delta is None else delta > STALE_EVIDENCE_LABEL_NS,
                "note": note,
            },
            clocks,
        )

    def _emit(self, fields: Mapping[str, object], clocks: ReceiveClocks) -> dict[str, object]:
        record: dict[str, object] = {
            "schema": TIMING_SCHEMA,
            "record": "event",
            "role": self.role,
            "transport": self.transport,
            "trading_authority": False,
            "wall_receive_ns": clocks.wall_ns,
            "monotonic_receive_ns": clocks.monotonic_ns,
            "host_utc_sample_ns": self._wall_ns(),
        }
        record.update(fields)
        self._write(record)
        self._count += 1
        if self._count >= self.max_events:
            self._truncated = True
        return record

    def _write(self, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
            default=_json_default,
        )
        lowered = encoded.lower()
        forbidden = ('"key"', '"secret"', '"authorization"', '"password"')
        if any(token in lowered for token in forbidden):
            raise ValueError("timing evidence refused a credential-like field")
        self._handle.write(encoded + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError("timing evidence values must be JSON-safe")


def load_timing_events(path: Path) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("schema") != TIMING_SCHEMA:
            raise ValueError("unexpected timing evidence record")
        if value.get("trading_authority") is not False:
            raise ValueError("timing evidence must not claim trading authority")
        if value.get("record") == "event":
            events.append(value)
    return tuple(events)


def _optional_int(value: object) -> int | None:
    if type(value) is int and not isinstance(value, bool):
        return value
    return None


def _require_int(value: object) -> int:
    number = _optional_int(value)
    if number is None:
        raise ValueError("timing evidence integer required")
    return number


def join_timing(
    relay_events: Sequence[Mapping[str, object]], consumer_events: Sequence[Mapping[str, object]]
) -> tuple[JoinedTiming, ...]:
    """Pair records in arrival order. Unmatched events are omitted, not repaired."""
    pending: dict[str, deque[Mapping[str, object]]] = {}
    for event in relay_events:
        if event.get("role") != "relay" or event.get("parse_error") not in (None,):
            continue
        if not isinstance(event.get("raw_provider_timestamp"), str):
            continue
        key = event.get("event_key")
        if not isinstance(key, str):
            continue
        pending.setdefault(key, deque()).append(event)
    joined: list[JoinedTiming] = []
    for event in consumer_events:
        if event.get("role") != "consumer" or event.get("parse_error") not in (None,):
            continue
        key = event.get("event_key")
        raw_timestamp = event.get("raw_provider_timestamp")
        if not isinstance(key, str) or not isinstance(raw_timestamp, str):
            continue
        queue = pending.get(key)
        if queue is None or not queue:
            continue
        relay = queue.popleft()
        try:
            parsed = parse_timestamp(raw_timestamp).value
        except MarketDataValidationError:
            parsed = None
        observation = _require_int(relay.get("observation_time_ns"))
        consumer_observation = _require_int(event.get("observation_time_ns"))
        matches = parsed == observation == consumer_observation
        kind = event.get("kind")
        symbol = event.get("symbol")
        joined.append(
            JoinedTiming(
                key,
                kind if isinstance(kind, str) else "",
                symbol if isinstance(symbol, str) else None,
                raw_timestamp,
                observation,
                matches,
                _require_int(relay.get("wall_receive_ns")),
                _require_int(event.get("wall_receive_ns")),
                _require_int(relay.get("monotonic_receive_ns")),
                _require_int(event.get("monotonic_receive_ns")),
                _require_int(relay.get("host_utc_sample_ns")),
                _require_int(event.get("host_utc_sample_ns")),
            )
        )
    return tuple(joined)


def _source_map(host: Mapping[str, object], name: str) -> Mapping[str, object]:
    sources = host.get("sources")
    if not isinstance(sources, list):
        return {}
    for source in sources:
        if isinstance(source, Mapping) and source.get("source") == name:
            return source
    return {}


def _spread(values: Sequence[int]) -> int:
    return max(values) - min(values) if values else 0


def classify_timing(
    joined: Sequence[JoinedTiming], host: Mapping[str, object]
) -> TimingClassification:
    """Classify transport evidence. This result must not be fed to trading code."""
    if not joined:
        return TimingClassification(
            TimingCause.INSUFFICIENT,
            "low",
            False,
            "no paired relay and consumer timing records",
        )
    if any(not item.parsed_observation_matches for item in joined):
        return TimingClassification(
            TimingCause.CONVERSION_PRECISION,
            "high",
            False,
            "parsed observation time does not exactly match raw provider t",
        )
    if any(item.relay_to_consumer_monotonic_ns < 0 for item in joined):
        if any(item.relay_to_consumer_wall_ns < 0 for item in joined):
            return TimingClassification(
                TimingCause.CONSUMER_CAPTURE_DEFECT,
                "high",
                False,
                "consumer receipt stamp precedes the relay receipt stamp",
            )
        return TimingClassification(
            TimingCause.OTHER,
            "medium",
            False,
            "monotonic relay-to-consumer order failed while wall clocks stayed ordered",
        )
    relay_deltas = [item.provider_to_relay_wall_ns for item in joined]
    consumer_deltas = [item.provider_to_consumer_wall_ns for item in joined]
    mono = [item.relay_to_consumer_monotonic_ns for item in joined]
    relay_negative = all(delta < 0 for delta in relay_deltas)
    consumer_negative = all(delta < 0 for delta in consumer_deltas)
    inverted = relay_negative and consumer_negative
    if not inverted:
        if any(delta < 0 for delta in consumer_deltas) and all(
            delta >= 0 for delta in relay_deltas
        ):
            return TimingClassification(
                TimingCause.CONSUMER_CAPTURE_DEFECT,
                "high",
                True,
                "only the consumer wall receipt is earlier than provider observation",
            )
        if any(delta < 0 for delta in relay_deltas) and all(
            delta >= 0 for delta in consumer_deltas
        ):
            return TimingClassification(
                TimingCause.RELAY_CAPTURE_DEFECT,
                "high",
                True,
                "only the relay wall receipt is earlier than provider observation",
            )
        return TimingClassification(
            TimingCause.INSUFFICIENT,
            "low",
            False,
            "paired receipts are not earlier than provider observation",
        )
    if _spread(relay_deltas) > 5_000_000 or _spread(consumer_deltas) > 5_000_000:
        return TimingClassification(
            TimingCause.INSUFFICIENT,
            "low",
            True,
            "provider-to-receipt inversion is present but not a tight cluster",
        )
    disagreed = any(
        abs(left - right) > 5_000_000
        for left, right in zip(relay_deltas, consumer_deltas, strict=True)
    )
    if disagreed:
        return TimingClassification(
            TimingCause.OTHER,
            "medium",
            True,
            "relay and consumer wall deltas disagree by more than 5 ms",
        )
    if any(delay > 50_000_000 for delay in mono):
        return TimingClassification(
            TimingCause.OTHER,
            "medium",
            True,
            "relay-to-consumer monotonic delay exceeds 50 ms",
        )
    return _classify_clock_or_provider(relay_deltas, host)


def _classify_clock_or_provider(
    relay_deltas: Sequence[int], host: Mapping[str, object]
) -> TimingClassification:
    lead = sorted(relay_deltas)[len(relay_deltas) // 2]
    timedate = _source_map(host, "timedatectl")
    kernel = _source_map(host, "kernel-adjtimex")
    synchronized = timedate.get("ntp_synchronized")
    kernel_unsync = kernel.get("status_unsynchronized") is True
    offset = _optional_int(timedate.get("offset_ns"))
    jitter = _optional_int(timedate.get("jitter_ns"))
    maxerror = _optional_int(kernel.get("maxerror_ns"))
    if synchronized is False or kernel_unsync:
        return TimingClassification(
            TimingCause.HOST_WALL_CLOCK,
            "high" if kernel_unsync or synchronized is False else "medium",
            True,
            "provider t leads both receipts and the host clock is not synchronized; "
            "no tolerance was applied",
        )
    trustworthy = (
        synchronized is True
        and not kernel_unsync
        and offset is not None
        and abs(offset) < 5_000_000
        and jitter is not None
        and jitter < 5_000_000
        and (maxerror is None or maxerror < 20_000_000)
    )
    if trustworthy:
        return TimingClassification(
            TimingCause.PROVIDER_TIMESTAMP_SEMANTICS,
            "medium",
            True,
            "provider t leads both local receipts while independent host synchronization "
            "is tight; provider timestamp meaning is not reclassified without documentation",
        )
    consistent = (
        offset is not None
        and offset != 0
        and ((lead < 0) == (offset > 0))
        and abs(offset) * 3 >= abs(lead)
    )
    if synchronized is True and consistent and (jitter is None or jitter <= abs(lead)):
        return TimingClassification(
            TimingCause.HOST_WALL_CLOCK,
            "high",
            True,
            "provider t leads both receipts by a similar amount and the NTP offset "
            "matches that local-clock error direction and scale",
        )
    if jitter is not None and jitter >= abs(lead):
        return TimingClassification(
            TimingCause.INSUFFICIENT,
            "low",
            True,
            "inversion reproduced, but NTP offset sign or scale does not demonstrate "
            "host-clock error and jitter is large enough that provider semantics stay possible",
        )
    return TimingClassification(
        TimingCause.INSUFFICIENT,
        "low",
        True,
        "inversion reproduced without a demonstrated capture, conversion, host-clock, "
        "or provider-semantic cause",
    )


def format_timing_report(
    joined: Sequence[JoinedTiming], classification: TimingClassification
) -> str:
    lines = [
        f"cause: {classification.cause.value}",
        f"confidence: {classification.confidence}",
        f"reproduced_inversion: {str(classification.reproduced_inversion).lower()}",
        "trading_authority: false",
        f"summary: {classification.summary}",
        f"paired_events: {len(joined)}",
    ]
    for index, item in enumerate(joined, start=1):
        lines.extend(
            [
                f"event_{index}.raw_provider_timestamp: {item.raw_provider_timestamp}",
                f"event_{index}.observation_time_ns: {item.observation_time_ns}",
                f"event_{index}.relay_wall_receive_ns: {item.relay_wall_receive_ns}",
                f"event_{index}.consumer_wall_receive_ns: {item.consumer_wall_receive_ns}",
                f"event_{index}.relay_to_consumer_monotonic_ns: "
                f"{item.relay_to_consumer_monotonic_ns}",
                f"event_{index}.provider_to_relay_wall_ns: {item.provider_to_relay_wall_ns}",
                f"event_{index}.provider_to_consumer_wall_ns: {item.provider_to_consumer_wall_ns}",
            ]
        )
    return "\n".join(lines) + "\n"


async def _observe_socket(
    socket: Socket,
    evidence: TimingEvidence,
    *,
    max_events: int,
    timeout_seconds: float,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    wall_ns: Callable[[], int] = time.time_ns,
) -> int:
    deadline = monotonic_ns() + int(timeout_seconds * 1_000_000_000)
    await socket.send(
        json.dumps({"action": "subscribe", "trades": ["BTC/USD"], "quotes": ["BTC/USD"]})
    )
    raw = await asyncio.wait_for(socket.recv(), timeout=timeout_seconds)
    acknowledgement = json.loads(raw)
    expected = [{"T": "subscription", "trades": ["BTC/USD"], "quotes": ["BTC/USD"], "bars": []}]
    if acknowledgement != expected:
        raise ValueError("local crypto relay subscription rejected")
    recorded = 0
    while recorded < max_events:
        remaining = deadline - monotonic_ns()
        if remaining <= 0:
            break
        raw = await asyncio.wait_for(socket.recv(), timeout=remaining / 1_000_000_000)
        clocks = ReceiveClocks.capture(wall_ns, monotonic_ns)
        recorded += len(evidence.observe_frame(raw, clocks))
    return recorded


async def observe_relay(
    relay_url: str,
    evidence_path: Path,
    *,
    max_events: int = 8,
    timeout_seconds: float = 20,
    connect: Callable[..., object] | None = None,
    code_revision: str = "unspecified",
) -> int:
    """Read BTC trades and quotes from the local relay. No broker and no orders."""
    validate_local_crypto_relay_url(relay_url)
    if connect is None:
        from websockets.asyncio.client import connect as websocket_connect

        connect = websocket_connect
    evidence = TimingEvidence(
        evidence_path,
        role="consumer",
        transport="downstream_relay_websocket",
        max_events=max_events,
        code_revision=code_revision,
    )
    try:
        websocket = cast(
            AbstractAsyncContextManager[Socket],
            connect(
                relay_url,
                proxy=None,
                max_size=1_048_576,
                max_queue=1,
                open_timeout=min(5, timeout_seconds),
            ),
        )
        async with websocket as socket:
            return await _observe_socket(
                socket, evidence, max_events=max_events, timeout_seconds=timeout_seconds
            )
    finally:
        evidence.close()


def _join_main(relay_path: Path, consumer_path: Path) -> int:
    relay_events = load_timing_events(relay_path)
    consumer_events = load_timing_events(consumer_path)
    header = json.loads(consumer_path.read_text(encoding="utf-8").splitlines()[0])
    host = header.get("host_clock")
    if not isinstance(host, dict):
        host = {"sources": []}
    joined = join_timing(relay_events, consumer_events)
    print(format_timing_report(joined, classify_timing(joined, host)), end="")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="bounded read-only BTC timing observation; no broker or order authority"
    )
    parser.add_argument("--relay", default="ws://127.0.0.1:8766")
    parser.add_argument("--evidence-path", type=Path)
    parser.add_argument("--max-events", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    parser.add_argument("--code-revision", default="unspecified")
    parser.add_argument("--join", action="store_true")
    parser.add_argument("--relay-evidence", type=Path)
    parser.add_argument("--consumer-evidence", type=Path)
    args = parser.parse_args()
    if args.join:
        if args.relay_evidence is None or args.consumer_evidence is None:
            raise SystemExit("--join requires --relay-evidence and --consumer-evidence")
        return _join_main(args.relay_evidence, args.consumer_evidence)
    if args.evidence_path is None:
        raise SystemExit("--evidence-path is required")
    if not 1 <= args.max_events <= MAX_TIMING_EVENTS or not 0 < args.timeout_seconds <= 60:
        raise SystemExit("timing observation exceeds its bounded limit")
    recorded = asyncio.run(
        observe_relay(
            args.relay,
            args.evidence_path,
            max_events=args.max_events,
            timeout_seconds=args.timeout_seconds,
            code_revision=args.code_revision,
        )
    )
    print(
        json.dumps(
            {
                "T": "timing_observation",
                "events": recorded,
                "trading_authority": False,
                "evidence_path": str(args.evidence_path),
            },
            separators=(",", ":"),
        )
    )
    return 0
