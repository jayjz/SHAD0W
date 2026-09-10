"""Canonical serialization and SHA-256 identity for validated market datasets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from shadow.data.validation import validate_bars, validate_quotes
from shadow.domain.errors import MarketDataValidationError
from shadow.domain.market import Bar, DatasetMetadata, Provenance, Quote


def _timestamp(value: datetime) -> str:
    """Serialize an already-validated UTC instant at microsecond precision."""
    utc_value = value.astimezone(UTC)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _metadata(metadata: DatasetMetadata, observation_kind: str) -> dict[str, Any]:
    """Return identity-bearing metadata in the documented field order."""
    return {
        "schema_version": metadata.schema_version,
        "observation_kind": observation_kind,
        "source": metadata.source,
        "dataset_id": metadata.dataset_id,
        "retrieval_method": metadata.retrieval_method,
        "instruments": [instrument.identifier for instrument in metadata.instruments],
        "interval_microseconds": None
        if metadata.interval is None
        else metadata.interval.microseconds,
        "coverage_start": _timestamp(metadata.coverage_start),
        "coverage_end": _timestamp(metadata.coverage_end),
        "source_timezone": metadata.source_timezone,
        "session": metadata.session,
        "adjustment_policy": metadata.adjustment_policy,
    }


def _provenance(source: Provenance) -> dict[str, str | None]:
    return {
        "source": source.source,
        "source_timezone": source.source_timezone,
        "session": source.session,
    }


def _bar_record(bar: Bar) -> dict[str, Any]:
    return {
        "instrument": bar.instrument.identifier,
        "interval_microseconds": bar.interval.microseconds,
        "observation_time": _timestamp(bar.observation_time),
        "availability_time": _timestamp(bar.availability_time),
        "availability_semantics": bar.availability_semantics.value,
        "open": _decimal(bar.open),
        "high": _decimal(bar.high),
        "low": _decimal(bar.low),
        "close": _decimal(bar.close),
        "volume": _decimal(bar.volume),
        "provenance": _provenance(bar.provenance),
    }


def _quote_record(quote: Quote) -> dict[str, Any]:
    return {
        "instrument": quote.instrument.identifier,
        "observation_time": _timestamp(quote.observation_time),
        "availability_time": _timestamp(quote.availability_time),
        "availability_semantics": quote.availability_semantics.value,
        "bid_price": _decimal(quote.bid_price),
        "ask_price": _decimal(quote.ask_price),
        "bid_size": _decimal(quote.bid_size),
        "ask_size": _decimal(quote.ask_size),
        "provenance": _provenance(quote.provenance),
    }


def _encoded_record(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=False).encode(
        "ascii"
    )


def canonical_dataset_bytes(
    observations: Sequence[Bar] | Sequence[Quote], metadata: DatasetMetadata
) -> bytes:
    """Validate and serialize one homogeneous dataset into canonical UTF-8 JSON bytes.

    Input must already be chronological. Equal-time records are then sorted by their
    full canonical record representation so equivalent legal input order has one identity.
    """
    if not observations:
        raise MarketDataValidationError("dataset_records", "must contain at least one observation")
    first = observations[0]
    if isinstance(first, Bar):
        if not all(isinstance(item, Bar) for item in observations):
            raise MarketDataValidationError(
                "observation_kind", "dataset must not mix bars and quotes"
            )
        bars = cast(Sequence[Bar], observations)
        validate_bars(bars, metadata)
        observation_kind = "bar"
        records = [_bar_record(item) for item in bars]
    elif isinstance(first, Quote):
        if not all(isinstance(item, Quote) for item in observations):
            raise MarketDataValidationError(
                "observation_kind", "dataset must not mix bars and quotes"
            )
        quotes = cast(Sequence[Quote], observations)
        validate_quotes(quotes, metadata)
        observation_kind = "quote"
        records = [_quote_record(item) for item in quotes]
    else:
        raise MarketDataValidationError("observation_kind", "must contain Bar or Quote values")
    records.sort(key=_encoded_record)
    document = {"metadata": _metadata(metadata, observation_kind), "records": records}
    return json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=False).encode(
        "ascii"
    )


def dataset_fingerprint(
    observations: Sequence[Bar] | Sequence[Quote], metadata: DatasetMetadata
) -> str:
    """Return the SHA-256 hex digest of validated canonical dataset bytes."""
    return hashlib.sha256(canonical_dataset_bytes(observations, metadata)).hexdigest()
