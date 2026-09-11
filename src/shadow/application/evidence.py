"""Versioned local append-only shadow capture and bounded deterministic replay."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TextIO

from shadow.application.shadow import ShadowConfig, ShadowRecord, ShadowSession


def encode(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: encode(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value // timedelta(microseconds=1)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


def line(value: object) -> str:
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


class EvidenceWriter:
    """Exclusive creation prevents accidental overwrite or mixing sessions."""

    def __init__(self, path: Path, config: ShadowConfig) -> None:
        self._file: TextIO = path.open("x", encoding="utf-8")
        self.write({"schema": "shadow.live.v1", "config": config})

    def write(self, value: object) -> None:
        self._file.write(line(value) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def replay(config: ShadowConfig, captured: Iterable[ShadowRecord]) -> tuple[ShadowRecord, ...]:
    """Replay immutable normalized inputs and controls without importing an adapter."""
    session = ShadowSession(config)
    for record in captured:
        if record.observation is not None:
            session.accept(record.observation, record.delivery_reference)
        elif record.action == "observation":
            session.invalid(record.time, record.disposition, record.delivery_reference)
        else:
            session.control(record.action, record.time, record.disposition)
    return session.records
