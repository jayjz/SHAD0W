"""Versioned canonical serialization for existing P5A domain evidence.

This module is deliberately a codec, not a journal or an authority.  It knows
how to preserve the immutable contracts that already cross the risk and broker
boundaries; it neither creates an order nor grants permission to submit one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from shadow.domain import (
    AvailabilitySemantics,
    Bar,
    BarInterval,
    Instrument,
    Provenance,
    Quote,
)
from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    CanaryConfig,
    Eligibility,
    ErrorCategory,
    Evidence,
    OrderStatus,
    SessionState,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
    TradeUpdate,
    UpdateKind,
)
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.features import (
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
    FeatureUnavailableReason,
)
from shadow.features.btc_trend import BtcTrendFeatures, CompletedBtcInterval
from shadow.risk.btc_models import BtcRiskEvaluation, BtcRiskPolicy
from shadow.risk.models import (
    OpenLongPosition,
    OperationalQuantityConfig,
    OperatorControls,
    OrderIntent,
    OrderSide,
    OrderTarget,
    OrderType,
    OutstandingOrder,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
    RiskRejectionReason,
    RiskState,
    TimeInForce,
)
from shadow.strategies import Signal, SignalReason, SignalType
from shadow.strategies.btc_trend import BtcAction, BtcProposal, BtcTrendConfig

CODEC_VERSION = "shadow.execution.journal-codec.v1"


class JournalCodecError(ValueError):
    """A journal payload is unknown, malformed, or not canonical."""


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise JournalCodecError("expected finite Decimal")
    sign, digits, exponent = value.as_tuple()
    if all(digit == 0 for digit in digits):
        return "0:0:0"
    assert isinstance(exponent, int)
    while len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    return f"{sign}:{''.join(str(digit) for digit in digits)}:{exponent}"


def _decimal_from_text(value: object) -> Decimal:
    if not isinstance(value, str):
        raise JournalCodecError("decimal payload must be text")
    pieces = value.split(":")
    if len(pieces) != 3 or pieces[0] not in {"0", "1"}:
        raise JournalCodecError("invalid decimal payload")
    try:
        sign = int(pieces[0])
        digits = tuple(int(character) for character in pieces[1])
        exponent = int(pieces[2])
    except ValueError as exc:
        raise JournalCodecError("invalid decimal payload") from exc
    if not digits or any(digit < 0 or digit > 9 for digit in digits):
        raise JournalCodecError("invalid decimal payload")
    result = Decimal((sign, digits, exponent))
    if not result.is_finite() or _decimal_text(result) != value:
        raise JournalCodecError("noncanonical decimal payload")
    return result


def _duration_microseconds(value: timedelta) -> int:
    if not isinstance(value, timedelta):
        raise JournalCodecError("expected timedelta")
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _duration_from_microseconds(value: object) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalCodecError("duration payload must be an integer")
    try:
        return timedelta(microseconds=value)
    except OverflowError as exc:
        raise JournalCodecError("duration payload overflows") from exc


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is not UTC:
        raise JournalCodecError("journal timestamps must use datetime.UTC")
    return value.isoformat()


def _utc_from_text(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("+00:00"):
        raise JournalCodecError("timestamp payload must be canonical UTC text")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise JournalCodecError("invalid timestamp payload") from exc
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise JournalCodecError("timestamp payload must be UTC")
    result = result.astimezone(UTC)
    if result.isoformat() != value:
        raise JournalCodecError("noncanonical timestamp payload")
    return result


_ENUMS: tuple[type[StrEnum], ...] = (
    AvailabilitySemantics,
    BtcAction,
    Eligibility,
    ErrorCategory,
    FeatureInput,
    FeatureName,
    FeatureState,
    FeatureUnavailableReason,
    OrderSide,
    OrderStatus,
    OrderTarget,
    OrderType,
    RiskDecisionStatus,
    RiskRejectionReason,
    SessionState,
    SignalReason,
    SignalType,
    SubmissionStatus,
    TimeInForce,
    UpdateKind,
)
_ENUM_BY_NAME = {item.__name__: item for item in _ENUMS}

_DATACLASSES: tuple[type[Any], ...] = (
    Bar,
    BarInterval,
    BtcBrokerAsset,
    BtcCashAccount,
    BtcRiskEvaluation,
    BtcRiskPolicy,
    BtcTrendFeatures,
    BtcTrendConfig,
    BtcProposal,
    CompletedBtcInterval,
    BtcSubmitRequest,
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerError,
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    CanaryConfig,
    Evidence,
    FeatureSnapshot,
    Instrument,
    OpenLongPosition,
    OperationalQuantityConfig,
    OperatorControls,
    OrderIntent,
    OutstandingOrder,
    Provenance,
    Quote,
    RiskDecision,
    RiskPolicy,
    RiskState,
    Signal,
    SubmissionResult,
    SubmitRequest,
    TradeUpdate,
)
_DATACLASS_BY_NAME = {item.__name__: item for item in _DATACLASSES}


def _encode(value: object) -> object:
    if isinstance(value, StrEnum):
        if type(value) not in _ENUMS:
            raise JournalCodecError(f"unsupported enum {type(value).__name__}")
        return {"t": "enum", "n": type(value).__name__, "v": value.value}
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        raise JournalCodecError("floats are not journal values")
    if isinstance(value, Decimal):
        return {"t": "decimal", "v": _decimal_text(value)}
    if isinstance(value, datetime):
        return {"t": "datetime", "v": _utc_text(value)}
    if isinstance(value, date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, timedelta):
        return {"t": "timedelta", "v": _duration_microseconds(value)}
    if isinstance(value, tuple):
        return {"t": "tuple", "v": [_encode(item) for item in value]}
    if is_dataclass(value) and type(value) in _DATACLASSES:
        return {
            "t": "dataclass",
            "n": type(value).__name__,
            "v": {
                item.name: _encode(getattr(value, item.name)) for item in fields(value) if item.init
            },
        }
    raise JournalCodecError(f"unsupported journal value {type(value).__name__}")


def _object(value: object, *, required: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != required:
        raise JournalCodecError("malformed tagged journal value")
    if not all(isinstance(key, str) for key in value):
        raise JournalCodecError("journal object keys must be strings")
    return value


def _decode(value: object) -> object:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, list):
        raise JournalCodecError("untagged list is not a journal value")
    if not isinstance(value, dict):
        raise JournalCodecError("malformed journal value")
    tag = value.get("t")
    if tag == "decimal":
        return _decimal_from_text(_object(value, required={"t", "v"})["v"])
    if tag == "datetime":
        return _utc_from_text(_object(value, required={"t", "v"})["v"])
    if tag == "date":
        text = _object(value, required={"t", "v"})["v"]
        if not isinstance(text, str):
            raise JournalCodecError("date payload must be text")
        try:
            result = date.fromisoformat(text)
        except ValueError as exc:
            raise JournalCodecError("invalid date payload") from exc
        if result.isoformat() != text:
            raise JournalCodecError("noncanonical date payload")
        return result
    if tag == "timedelta":
        return _duration_from_microseconds(_object(value, required={"t", "v"})["v"])
    if tag == "tuple":
        payload = _object(value, required={"t", "v"})["v"]
        if not isinstance(payload, list):
            raise JournalCodecError("tuple payload must be a list")
        return tuple(_decode(item) for item in payload)
    if tag == "enum":
        tagged = _object(value, required={"t", "n", "v"})
        name, raw = tagged["n"], tagged["v"]
        if not isinstance(name, str) or not isinstance(raw, str) or name not in _ENUM_BY_NAME:
            raise JournalCodecError("unknown enum payload")
        try:
            return _ENUM_BY_NAME[name](raw)
        except ValueError as exc:
            raise JournalCodecError("invalid enum payload") from exc
    if tag == "dataclass":
        tagged = _object(value, required={"t", "n", "v"})
        name, payload = tagged["n"], tagged["v"]
        if (
            not isinstance(name, str)
            or name not in _DATACLASS_BY_NAME
            or not isinstance(payload, dict)
        ):
            raise JournalCodecError("unknown dataclass payload")
        cls = _DATACLASS_BY_NAME[name]
        expected = {item.name for item in fields(cls) if item.init}
        if set(payload) != expected or not all(isinstance(key, str) for key in payload):
            raise JournalCodecError("dataclass fields do not match codec schema")
        try:
            return cls(**{key: _decode(item) for key, item in payload.items()})
        except (TypeError, ValueError) as exc:
            raise JournalCodecError(f"invalid {name} payload") from exc
    raise JournalCodecError("unknown journal value tag")


def canonical_bytes(value: object) -> bytes:
    """Return stable UTF-8 bytes for a supported immutable domain value."""
    payload = {"codec_version": CODEC_VERSION, "value": _encode(value)}
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def decode_canonical(data: bytes) -> object:
    """Decode one exact codec payload and reject noncanonical encodings."""
    if not isinstance(data, bytes):
        raise JournalCodecError("journal payload must be bytes")
    try:
        loaded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalCodecError("invalid journal JSON") from exc
    if not isinstance(loaded, dict) or set(loaded) != {"codec_version", "value"}:
        raise JournalCodecError("malformed codec envelope")
    if loaded["codec_version"] != CODEC_VERSION:
        raise JournalCodecError("unsupported codec version")
    decoded = _decode(loaded["value"])
    if canonical_bytes(decoded) != data:
        raise JournalCodecError("journal payload is not canonical")
    return decoded


def canonical_digest(value: object) -> str:
    """SHA-256 identity of canonical evidence, never an authorization token."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
