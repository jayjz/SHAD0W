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
from shadow.domain import AvailabilitySemantics, Bar, BarInterval, Instrument, Provenance, Quote
from shadow.features import FeatureInput, FeatureName
from shadow.risk import (
    OpenLongPosition,
    OperationalQuantityConfig,
    OperatorControls,
    OrderSide,
    OutstandingOrder,
    RiskPolicy,
    RiskState,
)
from shadow.strategies import MeanReversionConfig

EVIDENCE_SCHEMA = "shadow.live.v1"


class CaptureStatus(Enum):
    COMPLETE_STOPPED = "complete_stopped"
    COMPLETE_FAILED = "complete_failed"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class CaptureError(ValueError):
    """A persisted capture is malformed or cannot be deterministically verified."""

    status = CaptureStatus.INVALID


@dataclasses.dataclass(frozen=True, slots=True)
class _ReplayInput:
    observation: Bar | Quote | None
    action: str
    time: datetime
    disposition: str
    delivery_reference: str
    persisted: dict[str, object]


@dataclasses.dataclass(frozen=True, slots=True)
class ShadowCapture:
    """Parsed normalized P4A evidence, prior to derived-output verification."""

    config: ShadowConfig
    inputs: tuple[_ReplayInput, ...]
    status: CaptureStatus
    diagnostic: str | None = None


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
        self.write({"schema": EVIDENCE_SCHEMA, "config": config})

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
            if not record.disposition.startswith("invalid:"):
                raise ValueError("observation without payload must be invalid evidence")
            session.invalid(
                record.time,
                record.disposition.removeprefix("invalid:"),
                record.delivery_reference,
            )
        else:
            session.control(record.action, record.time, record.disposition)
    return session.records


def _mapping(value: object, context: str, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CaptureError(f"invalid {context} shape")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise CaptureError(f"invalid {context}")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaptureError(f"invalid {context}")
    return value


def _decimal(value: object, context: str) -> Decimal:
    if not isinstance(value, str):
        raise CaptureError(f"invalid {context}")
    try:
        result = Decimal(value)
    except Exception as error:
        raise CaptureError(f"invalid {context}") from error
    if not result.is_finite():
        raise CaptureError(f"invalid {context}")
    return result


def _time(value: object, context: str) -> datetime:
    if not isinstance(value, str):
        raise CaptureError(f"invalid {context}")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise CaptureError(f"invalid {context}") from error
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise CaptureError(f"invalid {context}")
    return result


def _duration(value: object, context: str) -> timedelta:
    return timedelta(microseconds=_integer(value, context))


def _instrument(value: object) -> Instrument:
    item = _mapping(value, "instrument", {"identifier"})
    try:
        return Instrument(_text(item["identifier"], "instrument identifier"))
    except ValueError as error:
        raise CaptureError("invalid instrument") from error


def _provenance(value: object) -> Provenance:
    item = _mapping(value, "provenance", {"source", "source_timezone", "session"})
    source_timezone = item["source_timezone"]
    session = item["session"]
    if source_timezone is not None and not isinstance(source_timezone, str):
        raise CaptureError("invalid provenance source timezone")
    if session is not None and not isinstance(session, str):
        raise CaptureError("invalid provenance session")
    try:
        return Provenance(_text(item["source"], "provenance source"), source_timezone, session)
    except ValueError as error:
        raise CaptureError("invalid provenance") from error


def _observation(value: object) -> Bar | Quote:
    if not isinstance(value, dict):
        raise CaptureError("invalid observation")
    if "interval" in value:
        item = _mapping(
            value,
            "bar",
            {
                "instrument",
                "interval",
                "observation_time",
                "availability_time",
                "availability_semantics",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "provenance",
            },
        )
        interval = _mapping(item["interval"], "bar interval", {"duration"})
        volume = item["volume"]
        if volume is not None:
            volume = _decimal(volume, "bar volume")
        try:
            return Bar(
                _instrument(item["instrument"]),
                BarInterval(_duration(interval["duration"], "bar interval")),
                _time(item["observation_time"], "bar observation time"),
                _time(item["availability_time"], "bar availability time"),
                AvailabilitySemantics(
                    _text(item["availability_semantics"], "bar availability semantics")
                ),
                _decimal(item["open"], "bar open"),
                _decimal(item["high"], "bar high"),
                _decimal(item["low"], "bar low"),
                _decimal(item["close"], "bar close"),
                volume,
                _provenance(item["provenance"]),
            )
        except ValueError as error:
            raise CaptureError("invalid bar") from error
    item = _mapping(
        value,
        "quote",
        {
            "instrument",
            "bid_price",
            "ask_price",
            "bid_size",
            "ask_size",
            "observation_time",
            "availability_time",
            "availability_semantics",
            "provenance",
        },
    )
    bid_size = item["bid_size"]
    ask_size = item["ask_size"]
    if bid_size is not None:
        bid_size = _decimal(bid_size, "quote bid size")
    if ask_size is not None:
        ask_size = _decimal(ask_size, "quote ask size")
    try:
        return Quote(
            _instrument(item["instrument"]),
            _decimal(item["bid_price"], "quote bid price"),
            _decimal(item["ask_price"], "quote ask price"),
            bid_size,
            ask_size,
            _time(item["observation_time"], "quote observation time"),
            _time(item["availability_time"], "quote availability time"),
            AvailabilitySemantics(
                _text(item["availability_semantics"], "quote availability semantics")
            ),
            _provenance(item["provenance"]),
        )
    except ValueError as error:
        raise CaptureError("invalid quote") from error


def _strategy(value: object) -> MeanReversionConfig:
    item = _mapping(
        value,
        "strategy",
        {
            "instrument",
            "configuration_id",
            "rolling_window",
            "entry_threshold",
            "exit_threshold",
            "maximum_feature_age",
            "feature_name",
            "feature_input",
            "feature_implementation_version",
        },
    )
    try:
        return MeanReversionConfig(
            _instrument(item["instrument"]),
            _text(item["configuration_id"], "strategy configuration id"),
            _integer(item["rolling_window"], "strategy rolling window"),
            _decimal(item["entry_threshold"], "strategy entry threshold"),
            _decimal(item["exit_threshold"], "strategy exit threshold"),
            _duration(item["maximum_feature_age"], "strategy maximum feature age"),
            FeatureName(_text(item["feature_name"], "strategy feature name")),
            FeatureInput(_text(item["feature_input"], "strategy feature input")),
            _text(item["feature_implementation_version"], "strategy feature version"),
        )
    except ValueError as error:
        raise CaptureError("invalid strategy") from error


def _quantity(value: object) -> OperationalQuantityConfig:
    item = _mapping(value, "quantity", {"instrument", "configuration_id", "quantity"})
    try:
        return OperationalQuantityConfig(
            _instrument(item["instrument"]),
            _text(item["configuration_id"], "quantity configuration id"),
            _decimal(item["quantity"], "quantity"),
        )
    except ValueError as error:
        raise CaptureError("invalid quantity") from error


def _risk_policy(value: object) -> RiskPolicy:
    item = _mapping(
        value,
        "risk policy",
        {
            "policy_id",
            "enabled",
            "allowed_instruments",
            "maximum_quantity_per_order",
            "maximum_concurrent_positions",
            "maximum_signal_age",
            "maximum_feature_age",
            "maximum_quote_age",
            "maximum_operational_state_age",
            "risk_model_version",
        },
    )
    allowed = item["allowed_instruments"]
    if not isinstance(allowed, list) or not isinstance(item["enabled"], bool):
        raise CaptureError("invalid risk policy")
    try:
        return RiskPolicy(
            _text(item["policy_id"], "risk policy id"),
            item["enabled"],
            tuple(_instrument(entry) for entry in allowed),
            _decimal(item["maximum_quantity_per_order"], "risk maximum quantity"),
            _integer(item["maximum_concurrent_positions"], "risk maximum positions"),
            _duration(item["maximum_signal_age"], "risk maximum signal age"),
            _duration(item["maximum_feature_age"], "risk maximum feature age"),
            _duration(item["maximum_quote_age"], "risk maximum quote age"),
            _duration(item["maximum_operational_state_age"], "risk maximum state age"),
            _text(item["risk_model_version"], "risk model version"),
        )
    except ValueError as error:
        raise CaptureError("invalid risk policy") from error


def _risk_state(value: object) -> RiskState | None:
    if value is None:
        return None
    item = _mapping(
        value,
        "risk state",
        {
            "operational_scope",
            "state_id",
            "revision",
            "inventory_complete",
            "open_positions",
            "outstanding_orders",
            "observation_time",
            "availability_time",
            "controls",
        },
    )
    controls = _mapping(
        item["controls"],
        "operator controls",
        {"trading_enabled", "kill_switch_active", "observation_time", "availability_time"},
    )
    positions = item["open_positions"]
    orders = item["outstanding_orders"]
    if (
        not isinstance(item["inventory_complete"], bool)
        or not isinstance(controls["trading_enabled"], bool)
        or not isinstance(controls["kill_switch_active"], bool)
        or not isinstance(positions, list)
        or not isinstance(orders, list)
    ):
        raise CaptureError("invalid risk state")
    try:
        return RiskState(
            _text(item["operational_scope"], "risk state scope"),
            _text(item["state_id"], "risk state id"),
            _integer(item["revision"], "risk state revision"),
            item["inventory_complete"],
            tuple(_position(position) for position in positions),
            tuple(_outstanding_order(order) for order in orders),
            _time(item["observation_time"], "risk state observation time"),
            _time(item["availability_time"], "risk state availability time"),
            OperatorControls(
                controls["trading_enabled"],
                controls["kill_switch_active"],
                _time(controls["observation_time"], "controls observation time"),
                _time(controls["availability_time"], "controls availability time"),
            ),
        )
    except ValueError as error:
        raise CaptureError("invalid risk state") from error


def _position(value: object) -> OpenLongPosition:
    item = _mapping(value, "position", {"instrument", "quantity", "position_id"})
    try:
        return OpenLongPosition(
            _instrument(item["instrument"]),
            _decimal(item["quantity"], "position quantity"),
            _text(item["position_id"], "position id"),
        )
    except ValueError as error:
        raise CaptureError("invalid position") from error


def _outstanding_order(value: object) -> OutstandingOrder:
    item = _mapping(
        value,
        "outstanding order",
        {"instrument", "intent_identity", "side", "quantity", "reference"},
    )
    try:
        return OutstandingOrder(
            _instrument(item["instrument"]),
            _text(item["intent_identity"], "outstanding intent identity"),
            OrderSide(_text(item["side"], "outstanding side")),
            _decimal(item["quantity"], "outstanding quantity"),
            _text(item["reference"], "outstanding reference"),
        )
    except ValueError as error:
        raise CaptureError("invalid outstanding order") from error


def _config(value: object) -> ShadowConfig:
    item = _mapping(
        value,
        "capture config",
        {
            "session_id",
            "code_revision",
            "strategies",
            "quantities",
            "risk_policy",
            "source",
            "maximum_bar_age",
            "maximum_quote_age",
            "observability_state",
            "maximum_records",
        },
    )
    strategies = item["strategies"]
    quantities = item["quantities"]
    if not isinstance(strategies, list) or not isinstance(quantities, list):
        raise CaptureError("invalid capture config")
    try:
        return ShadowConfig(
            _text(item["session_id"], "capture session id"),
            _text(item["code_revision"], "capture code revision"),
            tuple(_strategy(entry) for entry in strategies),
            tuple(_quantity(entry) for entry in quantities),
            _risk_policy(item["risk_policy"]),
            _text(item["source"], "capture source"),
            _duration(item["maximum_bar_age"], "capture maximum bar age"),
            _duration(item["maximum_quote_age"], "capture maximum quote age"),
            _risk_state(item["observability_state"]),
            _integer(item["maximum_records"], "capture maximum records"),
        )
    except ValueError as error:
        raise CaptureError("invalid capture config") from error


def _input(value: object, config: ShadowConfig, sequence: int) -> _ReplayInput:
    item = _mapping(
        value,
        "shadow record",
        {
            "sequence",
            "session_id",
            "time",
            "action",
            "disposition",
            "health",
            "observation",
            "delivery_reference",
            "feature",
            "signal",
            "intent",
            "risk_observability",
        },
    )
    if _integer(item["sequence"], "record sequence") != sequence:
        raise CaptureError("non-sequential record sequence")
    if _text(item["session_id"], "record session id") != config.session_id:
        raise CaptureError("record session id differs from config")
    action = _text(item["action"], "record action")
    disposition = _text(item["disposition"], "record disposition")
    reference = _text(item["delivery_reference"], "record delivery reference")
    try:
        from shadow.application.shadow import FeedHealth

        FeedHealth(_text(item["health"], "record health"))
    except ValueError as error:
        raise CaptureError("invalid record health") from error
    observation_value = item["observation"]
    if action == "observation":
        if observation_value is None:
            if not disposition.startswith("invalid:") or not disposition.removeprefix("invalid:"):
                raise CaptureError("invalid observation disposition")
            observation = None
        else:
            observation = _observation(observation_value)
    elif action in ("connected", "disconnected", "failed", "stopped", "tick"):
        if observation_value is not None:
            raise CaptureError("control record cannot contain an observation")
        observation = None
    else:
        raise CaptureError("invalid record action")
    return _ReplayInput(
        observation,
        action,
        _time(item["time"], "record time"),
        disposition,
        reference,
        item,
    )


def load_capture(path: Path) -> ShadowCapture:
    """Parse one local ``shadow.live.v1`` capture without trusting derived evidence.

    A final incomplete JSON line is recoverable evidence interruption. Any malformed
    complete line is corruption and fails closed.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CaptureError("unable to read capture") from error
    physical_lines = content.splitlines(keepends=True)
    if not physical_lines:
        raise CaptureError("capture is empty")
    if not physical_lines[0].endswith(("\n", "\r")):
        raise CaptureError("capture header is incomplete")
    try:
        header: object = json.loads(physical_lines[0])
    except json.JSONDecodeError as error:
        raise CaptureError("malformed capture header") from error
    header_map = _mapping(header, "capture header", {"schema", "config"})
    if header_map["schema"] != EVIDENCE_SCHEMA:
        raise CaptureError("unsupported capture schema")
    config = _config(header_map["config"])
    inputs: list[_ReplayInput] = []
    diagnostic = None
    for index, raw_line in enumerate(physical_lines[1:], start=1):
        is_final = index == len(physical_lines) - 1
        complete_line = raw_line.endswith(("\n", "\r"))
        try:
            parsed: object = json.loads(raw_line)
        except json.JSONDecodeError as error:
            if is_final and not complete_line:
                diagnostic = "truncated final JSON line"
                break
            raise CaptureError(f"malformed complete record line {index + 1}") from error
        inputs.append(_input(parsed, config, len(inputs)))
    if diagnostic is not None:
        status = CaptureStatus.INCOMPLETE
    elif inputs and inputs[-1].action == "stopped":
        status = CaptureStatus.COMPLETE_STOPPED
    elif inputs and inputs[-1].action == "failed":
        status = CaptureStatus.COMPLETE_FAILED
    else:
        status = CaptureStatus.INCOMPLETE
        if diagnostic is None:
            diagnostic = "capture ended without terminal evidence"
    return ShadowCapture(config, tuple(inputs), status, diagnostic)


def verify_capture(capture: ShadowCapture) -> ShadowSession:
    """Recompute and compare every persisted record from normalized P4A inputs."""
    if capture.status is CaptureStatus.INVALID:
        raise CaptureError("invalid captures cannot be verified")
    session = ShadowSession(capture.config)
    for input_record in capture.inputs:
        try:
            if input_record.observation is not None:
                actual = session.accept(input_record.observation, input_record.delivery_reference)
            elif input_record.action == "observation":
                actual = session.invalid(
                    input_record.time,
                    input_record.disposition.removeprefix("invalid:"),
                    input_record.delivery_reference,
                )
            else:
                actual = session.control(
                    input_record.action, input_record.time, input_record.disposition
                )
        except ValueError as error:
            raise CaptureError(f"normalized input cannot replay: {error}") from error
        if line(actual) != line(input_record.persisted):
            raise CaptureError(f"persisted derived evidence differs at sequence {actual.sequence}")
    if capture.status is CaptureStatus.COMPLETE_STOPPED and session.health.value != "stopped":
        raise CaptureError("stopped capture did not replay terminally")
    if capture.status is CaptureStatus.COMPLETE_FAILED and session.health.value != "failed":
        raise CaptureError("failed capture did not replay terminally")
    return session
