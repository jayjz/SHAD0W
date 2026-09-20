"""Immutable, content-addressed P0.1 bar artifacts for sealed studies."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from shadow.data.fingerprint import canonical_dataset_bytes
from shadow.data.validation import validate_bars
from shadow.domain.market import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    DatasetMetadata,
    Instrument,
    Provenance,
    ValidationStatus,
)


class FrozenDatasetError(ValueError):
    """A frozen artifact is malformed, misplaced, or disagrees with its identity."""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _path(root: Path, digest: str) -> Path:
    return root / "frozen-bars" / digest[:2] / f"{digest}.json"


def _provenance_id(document_metadata: dict[str, Any]) -> str:
    """Identify the P0.1 identity-bearing provenance declaration exactly."""
    return _digest(
        json.dumps(document_metadata, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    )


@dataclass(frozen=True, slots=True)
class FrozenBarDatasetRef:
    """Reference to exact validated P0.1 bar bytes, not a mutable data label."""

    dataset_id: str
    sha256: str
    instrument_universe: tuple[str, ...]
    source: str
    bar_interval_microseconds: int
    coverage_start: datetime
    coverage_end: datetime
    record_count: int
    provenance_id: str
    retrieval_method: str | None
    source_timezone: str | None
    session: str | None
    adjustment_policy: str | None
    schema_version: str

    def __post_init__(self) -> None:
        if not self.dataset_id or self.dataset_id != self.dataset_id.strip():
            raise FrozenDatasetError("dataset_id must be a nonempty trimmed string")
        for value, name in ((self.sha256, "sha256"), (self.provenance_id, "provenance_id")):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise FrozenDatasetError(f"{name} must be a SHA-256 hexadecimal digest")
        if (
            not self.instrument_universe
            or tuple(sorted(self.instrument_universe)) != self.instrument_universe
            or any(not value or value != value.strip() for value in self.instrument_universe)
        ):
            raise FrozenDatasetError("instrument universe must be a sorted nonempty tuple")
        if (
            not isinstance(self.coverage_start, datetime)
            or not isinstance(self.coverage_end, datetime)
            or self.coverage_start.tzinfo is None
            or self.coverage_start.utcoffset() is None
            or self.coverage_end.tzinfo is None
            or self.coverage_end.utcoffset() is None
        ):
            raise FrozenDatasetError("frozen bar coverage must use timezone-aware instants")
        object.__setattr__(self, "coverage_start", self.coverage_start.astimezone(UTC))
        object.__setattr__(self, "coverage_end", self.coverage_end.astimezone(UTC))
        if (
            isinstance(self.bar_interval_microseconds, bool)
            or not isinstance(self.bar_interval_microseconds, int)
            or self.bar_interval_microseconds <= 0
            or self.coverage_end < self.coverage_start
        ):
            raise FrozenDatasetError("invalid frozen bar coverage or interval")
        if self.record_count <= 0 or not self.source or not self.schema_version:
            raise FrozenDatasetError("invalid frozen bar reference metadata")


@dataclass(frozen=True, slots=True)
class FrozenBarDataset:
    """A verified immutable reference together with decoded P0.1 values."""

    reference: FrozenBarDatasetRef
    metadata: DatasetMetadata
    bars: tuple[Bar, ...]


def _reference(document: dict[str, Any]) -> FrozenBarDatasetRef:
    metadata = document["metadata"]
    records = document["records"]
    if (
        not isinstance(metadata, dict)
        or metadata.get("observation_kind") != "bar"
        or not isinstance(records, list)
    ):
        raise FrozenDatasetError("artifact is not a canonical bar dataset")
    interval = metadata.get("interval_microseconds")
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise FrozenDatasetError("bar artifact has no valid interval")
    try:
        start = datetime.fromisoformat(str(metadata["coverage_start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(metadata["coverage_end"]).replace("Z", "+00:00"))
        instruments = tuple(metadata["instruments"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FrozenDatasetError("malformed canonical bar metadata") from exc
    return FrozenBarDatasetRef(
        dataset_id=str(metadata["dataset_id"]),
        sha256="0" * 64,  # populated by the caller after bytes are verified
        instrument_universe=instruments,
        source=str(metadata["source"]),
        bar_interval_microseconds=interval,
        coverage_start=start,
        coverage_end=end,
        record_count=len(records),
        provenance_id=_provenance_id(metadata),
        retrieval_method=metadata.get("retrieval_method"),
        source_timezone=metadata.get("source_timezone"),
        session=metadata.get("session"),
        adjustment_policy=metadata.get("adjustment_policy"),
        schema_version=str(metadata["schema_version"]),
    )


def _decoded(document: dict[str, Any]) -> tuple[DatasetMetadata, tuple[Bar, ...]]:
    metadata_value = document["metadata"]
    interval = BarInterval(timedelta(microseconds=int(metadata_value["interval_microseconds"])))
    metadata = DatasetMetadata(
        source=str(metadata_value["source"]),
        dataset_id=str(metadata_value["dataset_id"]),
        instruments=tuple(Instrument(str(item)) for item in metadata_value["instruments"]),
        coverage_start=datetime.fromisoformat(
            str(metadata_value["coverage_start"]).replace("Z", "+00:00")
        ),
        coverage_end=datetime.fromisoformat(
            str(metadata_value["coverage_end"]).replace("Z", "+00:00")
        ),
        interval=interval,
        retrieval_method=metadata_value.get("retrieval_method"),
        source_timezone=metadata_value.get("source_timezone"),
        session=metadata_value.get("session"),
        adjustment_policy=metadata_value.get("adjustment_policy"),
        validation_status=ValidationStatus.VALIDATED,
        schema_version=str(metadata_value["schema_version"]),
    )
    bars = tuple(
        Bar(
            instrument=Instrument(str(record["instrument"])),
            interval=BarInterval(timedelta(microseconds=int(record["interval_microseconds"]))),
            observation_time=datetime.fromisoformat(
                str(record["observation_time"]).replace("Z", "+00:00")
            ),
            availability_time=datetime.fromisoformat(
                str(record["availability_time"]).replace("Z", "+00:00")
            ),
            availability_semantics=AvailabilitySemantics(str(record["availability_semantics"])),
            open=Decimal(str(record["open"])),
            high=Decimal(str(record["high"])),
            low=Decimal(str(record["low"])),
            close=Decimal(str(record["close"])),
            volume=None if record["volume"] is None else Decimal(str(record["volume"])),
            provenance=Provenance(
                str(record["provenance"]["source"]),
                record["provenance"].get("source_timezone"),
                record["provenance"].get("session"),
            ),
        )
        for record in document["records"]
    )
    validate_bars(bars, metadata)
    return metadata, bars


def _ref_for_bytes(contents: bytes) -> FrozenBarDatasetRef:
    try:
        document = json.loads(contents)
        if not isinstance(document, dict):
            raise TypeError
        partial = _reference(document)
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FrozenDatasetError("malformed frozen bar artifact") from exc
    return FrozenBarDatasetRef(
        dataset_id=partial.dataset_id,
        sha256=_digest(contents),
        instrument_universe=partial.instrument_universe,
        source=partial.source,
        bar_interval_microseconds=partial.bar_interval_microseconds,
        coverage_start=partial.coverage_start,
        coverage_end=partial.coverage_end,
        record_count=partial.record_count,
        provenance_id=partial.provenance_id,
        retrieval_method=partial.retrieval_method,
        source_timezone=partial.source_timezone,
        session=partial.session,
        adjustment_policy=partial.adjustment_policy,
        schema_version=partial.schema_version,
    )


def freeze_bars(root: Path, bars: Sequence[Bar], metadata: DatasetMetadata) -> FrozenBarDatasetRef:
    """Validate and exclusively persist exact P0.1 canonical bytes by their digest."""
    validate_bars(bars, metadata)
    contents = canonical_dataset_bytes(bars, metadata)
    reference = _ref_for_bytes(contents)
    target = _path(root, reference.sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError as exc:
        if target.read_bytes() != contents:
            raise FrozenDatasetError(
                "existing frozen artifact conflicts with its content identity"
            ) from exc
        return reference
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        # An incomplete exclusive file is never a usable release; remove only this new path.
        target.unlink(missing_ok=True)
        raise
    return reference


def load_frozen_bars(root: Path, reference: FrozenBarDatasetRef) -> FrozenBarDataset:
    """Load only exact canonical bytes at their exact content-addressed location."""
    target = _path(root, reference.sha256)
    if not target.is_file():
        raise FrozenDatasetError("frozen artifact is absent from its expected identity path")
    contents = target.read_bytes()
    if _digest(contents) != reference.sha256:
        raise FrozenDatasetError("frozen artifact digest disagrees with its path identity")
    derived = _ref_for_bytes(contents)
    if derived != reference:
        raise FrozenDatasetError("frozen artifact metadata disagrees with its reference")
    try:
        document = json.loads(contents)
        metadata, bars = _decoded(document)
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FrozenDatasetError("frozen artifact cannot decode to valid P0.1 bars") from exc
    if canonical_dataset_bytes(bars, metadata) != contents:
        raise FrozenDatasetError("decoded frozen bars do not reproduce canonical bytes")
    return FrozenBarDataset(reference, metadata, bars)
