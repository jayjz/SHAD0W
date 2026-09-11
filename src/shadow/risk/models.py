"""Immutable provider-neutral contracts for P2A paper risk authorization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from shadow.domain import Instrument, Quote
from shadow.features import FeatureSnapshot
from shadow.strategies import Signal, SignalType

RISK_MODEL_VERSION = "shadow.risk.paper.v2"


class RiskContractError(ValueError):
    """Programming or typed-contract misuse at the P2A boundary."""


class OrderTarget(StrEnum):
    PAPER = "paper"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"


class TimeInForce(StrEnum):
    DAY = "day"


class RiskDecisionStatus(StrEnum):
    AUTHORIZED = "authorized"
    REJECTED = "rejected"


class RiskRejectionReason(StrEnum):
    POLICY_DISABLED = "policy_disabled"
    TRADING_DISABLED = "trading_disabled"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    UNSUPPORTED_INSTRUMENT = "unsupported_instrument"
    INVALID_QUANTITY = "invalid_quantity"
    MAX_QUANTITY_EXCEEDED = "max_quantity_exceeded"
    INTENT_SIGNAL_MISMATCH = "intent_signal_mismatch"
    LINEAGE_MISMATCH = "lineage_mismatch"
    FUTURE_SIGNAL = "future_signal"
    STALE_SIGNAL = "stale_signal"
    FUTURE_FEATURE = "future_feature"
    STALE_FEATURE = "stale_feature"
    QUOTE_INSTRUMENT_MISMATCH = "quote_instrument_mismatch"
    FUTURE_QUOTE = "future_quote"
    STALE_QUOTE = "stale_quote"
    CROSSED_QUOTE = "crossed_quote"
    NONPOSITIVE_QUOTE = "nonpositive_quote"
    INCOMPLETE_INVENTORY = "incomplete_inventory"
    FUTURE_OPERATIONAL_STATE = "future_operational_state"
    STALE_OPERATIONAL_STATE = "stale_operational_state"
    FUTURE_CONTROLS = "future_controls"
    STALE_CONTROLS = "stale_controls"
    INCONSISTENT_OPERATIONAL_STATE = "inconsistent_operational_state"
    POSITION_ALREADY_OPEN = "position_already_open"
    OUTSTANDING_ORDER_EXISTS = "outstanding_order_exists"
    MAX_CONCURRENT_POSITIONS = "max_concurrent_positions"
    POSITION_NOT_OPEN = "position_not_open"
    PARTIAL_EXIT = "partial_exit"
    OVERSELL = "oversell"
    DUPLICATE_INTENT = "duplicate_intent"
    INTENT_IDENTITY_CONFLICT = "intent_identity_conflict"


def _canonical_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RiskContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _nonempty(value: str, field_name: str) -> None:
    if not value or value != value.strip():
        raise RiskContractError(f"{field_name} must be a non-empty trimmed string")


def _finite(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RiskContractError(f"{field_name} must be a finite Decimal")


def _decimal_text(value: Decimal) -> str:
    """Encode numerical value canonically without ambient Decimal arithmetic."""
    sign, digits, exponent = value.as_tuple()
    if all(digit == 0 for digit in digits):
        return "0:0:0"
    assert isinstance(exponent, int)
    while len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    return f"{sign}:{''.join(str(digit) for digit in digits)}:{exponent}"


def _duration_text(value: timedelta) -> str:
    return str(value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds)


def _digest(label: str, parts: tuple[str, ...]) -> str:
    encoded = json.dumps((label, *parts), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _signal_business_parts(signal: Signal) -> tuple[str, ...]:
    return (
        signal.instrument.identifier,
        signal.strategy_id,
        signal.strategy_version,
        signal.configuration_id,
        signal.signal_type.value,
        signal.feature_name.value,
        signal.feature_input.value,
        signal.feature_implementation_version,
        str(signal.feature_window),
        signal.feature_observation_time.isoformat(),
        signal.feature_availability_time.isoformat(),
        signal.source_dataset_id,
    )


def _signal_payload_parts(signal: Signal) -> tuple[str, ...]:
    return (
        *_signal_business_parts(signal),
        signal.availability_time.isoformat(),
        _decimal_text(signal.observed_feature_value),
        _decimal_text(signal.entry_threshold),
        _decimal_text(signal.exit_threshold),
        _duration_text(signal.maximum_feature_age),
        signal.reason.value,
    )


@dataclass(frozen=True, slots=True)
class OperationalQuantityConfig:
    """Operator-declared paper quantity; it has no strategy sizing authority."""

    instrument: Instrument
    configuration_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise RiskContractError("instrument must be an Instrument")
        _nonempty(self.configuration_id, "configuration_id")
        _finite(self.quantity, "quantity")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A provider-neutral paper market-order proposal without submission authority."""

    operational_scope: str
    source_signal: Signal
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    intent_time: datetime
    quantity_configuration_id: str
    target: OrderTarget = OrderTarget.PAPER
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY

    def __post_init__(self) -> None:
        _nonempty(self.operational_scope, "operational_scope")
        if not isinstance(self.source_signal, Signal):
            raise RiskContractError("source_signal must be a Signal")
        if not isinstance(self.instrument, Instrument):
            raise RiskContractError("instrument must be an Instrument")
        if not isinstance(self.side, OrderSide):
            raise RiskContractError("side must be an OrderSide")
        _finite(self.quantity, "quantity")
        _nonempty(self.quantity_configuration_id, "quantity_configuration_id")
        object.__setattr__(self, "intent_time", _canonical_utc(self.intent_time, "intent_time"))
        if self.target is not OrderTarget.PAPER:
            raise RiskContractError("only the paper target is supported")
        if self.order_type is not OrderType.MARKET:
            raise RiskContractError("only market order semantics are supported")
        if self.time_in_force is not TimeInForce.DAY:
            raise RiskContractError("only DAY time-in-force is supported")

    @classmethod
    def from_signal(
        cls,
        *,
        operational_scope: str,
        signal: Signal,
        quantity_config: OperationalQuantityConfig,
    ) -> OrderIntent:
        if not isinstance(signal, Signal):
            raise RiskContractError("signal must be a Signal")
        if not isinstance(quantity_config, OperationalQuantityConfig):
            raise RiskContractError("quantity_config must be an OperationalQuantityConfig")
        if signal.instrument != quantity_config.instrument:
            raise RiskContractError("quantity configuration instrument must match signal")
        side = OrderSide.BUY if signal.signal_type is SignalType.LONG_ENTRY else OrderSide.SELL
        return cls(
            operational_scope=operational_scope,
            source_signal=signal,
            instrument=signal.instrument,
            side=side,
            quantity=quantity_config.quantity,
            intent_time=signal.decision_time,
            quantity_configuration_id=quantity_config.configuration_id,
        )

    @property
    def intent_identity(self) -> str:
        return _digest(
            "shadow.order-intent.business.v1",
            (self.operational_scope, *_signal_business_parts(self.source_signal)),
        )

    @property
    def payload_fingerprint(self) -> str:
        return _digest(
            "shadow.order-intent.payload.v1",
            (
                self.operational_scope,
                *_signal_payload_parts(self.source_signal),
                self.instrument.identifier,
                self.side.value,
                _decimal_text(self.quantity),
                self.intent_time.isoformat(),
                self.quantity_configuration_id,
                self.target.value,
                self.order_type.value,
                self.time_in_force.value,
            ),
        )


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """One explicit fail-closed P2A paper policy."""

    policy_id: str
    enabled: bool
    allowed_instruments: tuple[Instrument, ...]
    maximum_quantity_per_order: Decimal
    maximum_concurrent_positions: int
    maximum_signal_age: timedelta
    maximum_feature_age: timedelta
    maximum_quote_age: timedelta
    maximum_operational_state_age: timedelta
    risk_model_version: str = RISK_MODEL_VERSION

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "policy_id")
        if not isinstance(self.enabled, bool):
            raise RiskContractError("enabled must be bool")
        if not all(isinstance(instrument, Instrument) for instrument in self.allowed_instruments):
            raise RiskContractError("allowed_instruments must contain Instrument values")
        if len(set(self.allowed_instruments)) != len(self.allowed_instruments):
            raise RiskContractError("allowed_instruments must not contain duplicates")
        object.__setattr__(
            self,
            "allowed_instruments",
            tuple(sorted(self.allowed_instruments, key=lambda item: item.identifier)),
        )
        _finite(self.maximum_quantity_per_order, "maximum_quantity_per_order")
        if (
            self.maximum_quantity_per_order <= 0
            or self.maximum_quantity_per_order
            != self.maximum_quantity_per_order.to_integral_value()
        ):
            raise RiskContractError("maximum_quantity_per_order must be whole and positive")
        if (
            isinstance(self.maximum_concurrent_positions, bool)
            or not isinstance(self.maximum_concurrent_positions, int)
            or self.maximum_concurrent_positions <= 0
        ):
            raise RiskContractError("maximum_concurrent_positions must be a positive integer")
        for name in (
            "maximum_signal_age",
            "maximum_feature_age",
            "maximum_quote_age",
            "maximum_operational_state_age",
        ):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value < timedelta(0):
                raise RiskContractError(f"{name} must be a non-negative timedelta")
        if self.risk_model_version != RISK_MODEL_VERSION:
            raise RiskContractError("risk_model_version is not supported")

    @property
    def fingerprint(self) -> str:
        return _digest(
            "shadow.risk-policy.v1",
            (
                self.policy_id,
                str(self.enabled),
                "allowed_instruments",
                str(len(self.allowed_instruments)),
                *(instrument.identifier for instrument in self.allowed_instruments),
                "limits",
                _decimal_text(self.maximum_quantity_per_order),
                str(self.maximum_concurrent_positions),
                _duration_text(self.maximum_signal_age),
                _duration_text(self.maximum_feature_age),
                _duration_text(self.maximum_quote_age),
                _duration_text(self.maximum_operational_state_age),
                self.risk_model_version,
            ),
        )


@dataclass(frozen=True, slots=True)
class OperatorControls:
    trading_enabled: bool
    kill_switch_active: bool
    observation_time: datetime
    availability_time: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.trading_enabled, bool) or not isinstance(
            self.kill_switch_active, bool
        ):
            raise RiskContractError("operator controls must be bool")
        observation = _canonical_utc(self.observation_time, "controls observation_time")
        availability = _canonical_utc(self.availability_time, "controls availability_time")
        if availability < observation:
            raise RiskContractError("controls availability_time must not precede observation_time")
        object.__setattr__(self, "observation_time", observation)
        object.__setattr__(self, "availability_time", availability)


@dataclass(frozen=True, slots=True)
class OpenLongPosition:
    instrument: Instrument
    quantity: Decimal
    position_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise RiskContractError("position instrument must be an Instrument")
        _finite(self.quantity, "position quantity")
        if self.quantity <= 0 or self.quantity != self.quantity.to_integral_value():
            raise RiskContractError("position quantity must be whole and positive")
        _nonempty(self.position_id, "position_id")


@dataclass(frozen=True, slots=True)
class OutstandingOrder:
    instrument: Instrument
    intent_identity: str
    side: OrderSide
    quantity: Decimal
    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Instrument):
            raise RiskContractError("outstanding instrument must be an Instrument")
        _nonempty(self.intent_identity, "intent_identity")
        if not isinstance(self.side, OrderSide):
            raise RiskContractError("outstanding side must be an OrderSide")
        _finite(self.quantity, "outstanding quantity")
        if self.quantity <= 0 or self.quantity != self.quantity.to_integral_value():
            raise RiskContractError("outstanding quantity must be whole and positive")
        _nonempty(self.reference, "reference")


@dataclass(frozen=True, slots=True)
class RiskState:
    """Explicit operational inventory/order/control evidence supplied to risk."""

    operational_scope: str
    state_id: str
    revision: int
    inventory_complete: bool
    open_positions: tuple[OpenLongPosition, ...]
    outstanding_orders: tuple[OutstandingOrder, ...]
    observation_time: datetime
    availability_time: datetime
    controls: OperatorControls

    def __post_init__(self) -> None:
        _nonempty(self.operational_scope, "operational_scope")
        _nonempty(self.state_id, "state_id")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise RiskContractError("revision must be a non-negative integer")
        if not isinstance(self.inventory_complete, bool):
            raise RiskContractError("inventory_complete must be bool")
        if not all(isinstance(position, OpenLongPosition) for position in self.open_positions):
            raise RiskContractError("open_positions must contain OpenLongPosition values")
        if not all(isinstance(order, OutstandingOrder) for order in self.outstanding_orders):
            raise RiskContractError("outstanding_orders must contain OutstandingOrder values")
        if not isinstance(self.controls, OperatorControls):
            raise RiskContractError("controls must be OperatorControls")
        object.__setattr__(
            self,
            "open_positions",
            tuple(
                sorted(
                    self.open_positions,
                    key=lambda item: (item.instrument.identifier, item.position_id),
                )
            ),
        )
        object.__setattr__(
            self,
            "outstanding_orders",
            tuple(
                sorted(
                    self.outstanding_orders,
                    key=lambda item: (
                        item.instrument.identifier,
                        item.intent_identity,
                        item.reference,
                    ),
                )
            ),
        )
        observation = _canonical_utc(self.observation_time, "state observation_time")
        availability = _canonical_utc(self.availability_time, "state availability_time")
        if availability < observation:
            raise RiskContractError("state availability_time must not precede observation_time")
        object.__setattr__(self, "observation_time", observation)
        object.__setattr__(self, "availability_time", availability)

    @property
    def fingerprint(self) -> str:
        position_parts = tuple(
            part
            for item in self.open_positions
            for part in (
                "position",
                item.instrument.identifier,
                _decimal_text(item.quantity),
                item.position_id,
            )
        )
        order_parts = tuple(
            part
            for item in self.outstanding_orders
            for part in (
                "outstanding_order",
                item.instrument.identifier,
                item.intent_identity,
                item.side.value,
                _decimal_text(item.quantity),
                item.reference,
            )
        )
        return _digest(
            "shadow.risk-state.v1",
            (
                self.operational_scope,
                self.state_id,
                str(self.revision),
                str(self.inventory_complete),
                "positions",
                str(len(self.open_positions)),
                *position_parts,
                "orders",
                str(len(self.outstanding_orders)),
                *order_parts,
                "state_times_and_controls",
                self.observation_time.isoformat(),
                self.availability_time.isoformat(),
                str(self.controls.trading_enabled),
                str(self.controls.kill_switch_active),
                self.controls.observation_time.isoformat(),
                self.controls.availability_time.isoformat(),
            ),
        )


def feature_reference(snapshot: FeatureSnapshot) -> str:
    value = "none" if snapshot.value is None else _decimal_text(snapshot.value)
    reason = "none" if snapshot.unavailable_reason is None else snapshot.unavailable_reason.value
    return _digest(
        "shadow.feature-reference.v1",
        (
            snapshot.instrument.identifier,
            snapshot.feature_name.value,
            snapshot.input_value.value,
            snapshot.implementation_version,
            value,
            snapshot.observation_time.isoformat(),
            snapshot.availability_time.isoformat(),
            str(snapshot.window),
            snapshot.state.value,
            reason,
            snapshot.source_dataset_id,
        ),
    )


def quote_reference(quote: Quote) -> str:
    return _digest(
        "shadow.quote-reference.v1",
        (
            quote.instrument.identifier,
            _decimal_text(quote.bid_price),
            _decimal_text(quote.ask_price),
            "none" if quote.bid_size is None else _decimal_text(quote.bid_size),
            "none" if quote.ask_size is None else _decimal_text(quote.ask_size),
            quote.observation_time.isoformat(),
            quote.availability_time.isoformat(),
            quote.availability_semantics.value,
            quote.provenance.source,
            quote.provenance.source_timezone or "",
            quote.provenance.session or "",
        ),
    )


@dataclass(frozen=True, slots=True)
class RiskDecision:
    intent: OrderIntent
    status: RiskDecisionStatus
    reasons: tuple[RiskRejectionReason, ...]
    policy_id: str
    policy_fingerprint: str
    risk_state_id: str
    risk_state_revision: int
    risk_state_fingerprint: str
    feature_reference: str
    quote_reference: str
    decision_time: datetime
    prior_decision_reference: str | None = None
    risk_model_version: str = RISK_MODEL_VERSION
    decision_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.intent, OrderIntent):
            raise RiskContractError("decision intent must be an OrderIntent")
        if not isinstance(self.status, RiskDecisionStatus):
            raise RiskContractError("status must be a RiskDecisionStatus")
        if not all(isinstance(reason, RiskRejectionReason) for reason in self.reasons):
            raise RiskContractError("reasons must contain RiskRejectionReason values")
        canonical_reasons = tuple(sorted(set(self.reasons), key=lambda reason: reason.value))
        object.__setattr__(self, "reasons", canonical_reasons)
        if self.status is RiskDecisionStatus.AUTHORIZED and canonical_reasons:
            raise RiskContractError("authorized decisions cannot contain rejection reasons")
        if self.status is RiskDecisionStatus.REJECTED and not canonical_reasons:
            raise RiskContractError("rejected decisions require a rejection reason")
        for name in (
            "policy_id",
            "policy_fingerprint",
            "risk_state_id",
            "risk_state_fingerprint",
            "feature_reference",
            "quote_reference",
            "risk_model_version",
        ):
            _nonempty(getattr(self, name), name)
        if self.prior_decision_reference is not None:
            _nonempty(self.prior_decision_reference, "prior_decision_reference")
        decision_time = _canonical_utc(self.decision_time, "decision_time")
        object.__setattr__(self, "decision_time", decision_time)
        identity = _digest(
            "shadow.risk-decision.v1",
            (
                self.intent.intent_identity,
                self.intent.payload_fingerprint,
                self.status.value,
                "reasons",
                str(len(canonical_reasons)),
                *(reason.value for reason in canonical_reasons),
                "decision_evidence",
                self.policy_id,
                self.policy_fingerprint,
                self.risk_state_id,
                str(self.risk_state_revision),
                self.risk_state_fingerprint,
                self.feature_reference,
                self.quote_reference,
                decision_time.isoformat(),
                self.prior_decision_reference or "",
                self.risk_model_version,
            ),
        )
        object.__setattr__(self, "decision_id", identity)


_AUTHORIZATION_ISSUER = object()


@dataclass(frozen=True, slots=True)
class AuthorizedOrder:
    """One gate-issued admission-time capability, insufficient for external submission.

    There is no expiry or assurance of current controls/freshness at consumption.
    A copied private token shares the original grant's single claim, never a new grant.
    """

    grant_id: str
    intent_identity: str
    payload_fingerprint: str
    decision_id: str
    operational_scope: str
    _gate_token: object = field(repr=False, compare=False)
    _issuer: InitVar[object] = None

    def __post_init__(self, _issuer: object) -> None:
        if _issuer is not _AUTHORIZATION_ISSUER:
            raise RiskContractError("AuthorizedOrder values may only be issued by RiskGate")
        for name in (
            "grant_id",
            "intent_identity",
            "payload_fingerprint",
            "decision_id",
            "operational_scope",
        ):
            _nonempty(getattr(self, name), name)


def _issue_authorized_order(
    *,
    grant_id: str,
    intent: OrderIntent,
    decision: RiskDecision,
    gate_token: object,
) -> AuthorizedOrder:
    return AuthorizedOrder(
        grant_id=grant_id,
        intent_identity=intent.intent_identity,
        payload_fingerprint=intent.payload_fingerprint,
        decision_id=decision.decision_id,
        operational_scope=intent.operational_scope,
        _gate_token=gate_token,
        _issuer=_AUTHORIZATION_ISSUER,
    )
