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
from shadow.domain.market import AvailabilitySemantics, Bar, BarInterval, Instrument, Provenance, Quote


def encode(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: encode(getattr(value, field.name)) for field in dataclasses.fields(value)}
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
        else:
            session.control(record.action, record.time, record.reason)
    return session.records


def decode_observation(value: dict[str, object]) -> Bar | Quote:
    """Read the normalized observation portion of a v1 JSONL evidence record."""
    instrument_data = value["instrument"]
    provenance_data = value["provenance"]
    if not isinstance(instrument_data, dict) or not isinstance(provenance_data, dict):
        raise ValueError("invalid normalized observation")
    instrument = Instrument(str(instrument_data["identifier"]))
    provenance = Provenance(**provenance_data)
    observation = datetime.fromisoformat(str(value["observation_time"]))
    availability = datetime.fromisoformat(str(value["availability_time"]))
    semantics = AvailabilitySemantics(str(value["availability_semantics"]))

    def decimal(name: str) -> Decimal:
        return Decimal(str(value[name]))

    def optional(name: str) -> Decimal | None:
        return None if value[name] is None else decimal(name)

    if "interval" in value:
        interval = value["interval"]
        if not isinstance(interval, dict):
            raise ValueError("invalid normalized interval")
        return Bar(instrument, BarInterval(timedelta(microseconds=interval["duration"])),
                   observation, availability, semantics, decimal("open"), decimal("high"),
                   decimal("low"), decimal("close"), optional("volume"), provenance)
    return Quote(instrument, decimal("bid_price"), decimal("ask_price"), optional("bid_size"),
                 optional("ask_size"), observation, availability, semantics, provenance)
