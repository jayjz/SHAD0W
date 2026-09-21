"""Strict, clock-free replay for C1 JSONL evidence."""

# ruff: noqa: E501, E701, E702
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from shadow.application.crypto_evidence import (
    CryptoCaptureStatus,
    CryptoEvidenceError,
    _snapshot,
    canonical_json,
    decode_event,
)
from shadow.application.crypto_session import CryptoSession
from shadow.data.crypto_book import BookSnapshot
from shadow.domain.crypto_market import CRYPTO_MARKET_SCHEMA
from shadow.domain.market import Instrument


@dataclass(frozen=True, slots=True)
class CryptoReplayResult:
    status: CryptoCaptureStatus
    btc: BookSnapshot | None
    eth: BookSnapshot | None
    digest: str


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CryptoEvidenceError("duplicate JSON key")
        result[key] = value
    return result


def replay(path: Path) -> CryptoReplayResult:
    raw = path.read_bytes()
    if not raw.endswith(b"\n"):
        return CryptoReplayResult(CryptoCaptureStatus.INCOMPLETE, None, None, "")
    try:
        lines = [
            json.loads(line, object_pairs_hook=_no_duplicates)
            for line in raw.decode("utf-8").splitlines()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError, CryptoEvidenceError) as exc:
        raise CryptoEvidenceError("invalid JSONL") from exc
    if (
        not lines
        or not isinstance(lines[0], dict)
        or lines[0].get("schema") != CRYPTO_MARKET_SCHEMA
    ):
        raise CryptoEvidenceError("unknown or missing schema")
    session = CryptoSession()
    expected = 0
    previous = ""
    terminal = False
    frame_by_epoch: dict[int, int] = {}
    lineage = hashlib.sha256(canonical_json(lines[0]).encode()).hexdigest()
    for record in lines[1:]:
        if not isinstance(record, dict):
            raise CryptoEvidenceError("record object required")
        if record.get("record_type") == "terminal":
            if terminal or record.get("previous_record_hash") != previous:
                raise CryptoEvidenceError("invalid terminal")
            terminal_status = record.get("status")
            if not isinstance(terminal_status, str):
                raise CryptoEvidenceError("terminal status required")
            terminal = True
            status = CryptoCaptureStatus(terminal_status)
            continue
        if terminal:
            raise CryptoEvidenceError("data after terminal")
        if record.get("record_type") != "event" or record.get("sequence") != expected:
            raise CryptoEvidenceError("sequence violation")
        if record.get("previous_record_hash") != previous:
            raise CryptoEvidenceError("bad previous hash")
        supplied = record.get("record_hash")
        unsigned = dict(record)
        unsigned.pop("record_hash", None)
        if (
            not isinstance(supplied, str)
            or hashlib.sha256(canonical_json(unsigned).encode()).hexdigest() != supplied
        ):
            raise CryptoEvidenceError("bad record hash")
        epoch, frame = record.get("connection_epoch"), record.get("frame_sequence")
        if (
            not isinstance(epoch, int)
            or not isinstance(frame, int)
            or epoch < 1
            or frame < 0
            or epoch < session.connection_epoch
            or frame < frame_by_epoch.get(epoch, 0)
        ):
            raise CryptoEvidenceError("impossible epoch/frame regression")
        while session.connection_epoch < epoch:
            session.establish_connection()
        if record.get("event") is None:
            actual = (
                session.establish_connection()
                if record.get("disposition") == "connection_established"
                else session.disconnect()
            )
        else:
            actual = session.process(decode_event(record.get("event")))
        if actual.disposition.value != record.get("disposition") or _snapshot(
            actual.snapshot
        ) != record.get("snapshot"):
            raise CryptoEvidenceError("derived result mismatch")
        frame_by_epoch[epoch] = frame
        previous = supplied
        expected += 1
        lineage = hashlib.sha256((lineage + supplied).encode()).hexdigest()
    if not terminal:
        return CryptoReplayResult(CryptoCaptureStatus.INCOMPLETE, None, None, "")
    return CryptoReplayResult(
        status,
        session.snapshot(Instrument("BTC/USD")),
        session.snapshot(Instrument("ETH/USD")),
        session.state_digest(lineage),
    )
