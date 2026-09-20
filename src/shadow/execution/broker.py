"""P5A.1 evidence only: no admission, reconciliation, or transport authority.

Adapters must translate unknown/malformed provider data into UNKNOWN evidence or
BrokerError, never an empty complete snapshot. Constructors check local consistency;
agreement between independent observations belongs to P5A.3 reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from shadow.domain import Instrument
from shadow.risk.models import OrderSide, OrderTarget, OrderType, TimeInForce


class BrokerContractError(ValueError):
    """Invalid normalized evidence; consumers must halt rather than repair it."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BrokerContractError(message)


def _text(value: str) -> None:
    _require(
        isinstance(value, str) and bool(value) and value == value.strip(),
        "expected nonempty trimmed identity",
    )


def _utc(value: datetime) -> datetime:
    _require(isinstance(value, datetime), "expected datetime")
    _require(value.tzinfo is not None and value.utcoffset() is not None, "expected aware time")
    return value.astimezone(UTC)


def _decimal(value: Decimal, *, positive: bool = False) -> None:
    _require(isinstance(value, Decimal) and value.is_finite(), "expected finite Decimal")
    _require(value > 0 if positive else value >= 0, "invalid monetary value or quantity")


class Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


class SessionState(StrEnum):
    REGULAR = "regular"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class OrderStatus(StrEnum):
    ACCEPTED = "accepted"
    NEW = "new"
    PENDING_NEW = "pending_new"
    ACCEPTED_FOR_BIDDING = "accepted_for_bidding"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"
    EXPIRED = "expired"
    PENDING_CANCEL = "pending_cancel"
    PENDING_REPLACE = "pending_replace"
    REPLACED = "replaced"
    DONE_FOR_DAY = "done_for_day"
    SUSPENDED = "suspended"
    STOPPED = "stopped"
    CALCULATED = "calculated"
    HELD = "held"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """Whether Alpaca documents this order as receiving no further updates.

        ``filled``, ``canceled``, ``expired``, and ``rejected`` are the only
        states with that documented guarantee.  In particular, ``done_for_day``
        may update on the next trading day; ``calculated`` has pending settlement
        calculations; and ``replaced`` does not establish the replacement's
        state.  Every other state is therefore outstanding for this bounded
        dispatcher, including ``unknown``.
        """

        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }


class UpdateKind(StrEnum):
    STATUS = "status"
    FILL = "fill"
    CANCEL_REJECTED = "cancel_rejected"
    REPLACE_REJECTED = "replace_rejected"
    TRADE_CORRECTION = "trade_correction"
    TRADE_BUST = "trade_bust"
    UNKNOWN = "unknown"


class ErrorCategory(StrEnum):
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    NOT_FOUND = "not_found"
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"
    DEFINITIVE_REJECTION = "definitive_rejection"


@dataclass(frozen=True, slots=True)
class Evidence:
    """Source/receipt times and requested account/scope binding, not authentication."""

    reference: str
    account_id: str
    operational_scope: str
    observation_time: datetime
    availability_time: datetime

    def __post_init__(self) -> None:
        for value in (self.reference, self.account_id, self.operational_scope):
            _text(value)
        object.__setattr__(self, "observation_time", _utc(self.observation_time))
        object.__setattr__(self, "availability_time", _utc(self.availability_time))
        _require(self.availability_time >= self.observation_time, "receipt precedes observation")


def _evidence(value: Evidence) -> None:
    _require(isinstance(value, Evidence), "expected Evidence")


def _binding(parent: Evidence, child: Evidence) -> None:
    _require(
        (parent.account_id, parent.operational_scope)
        == (child.account_id, child.operational_scope),
        "evidence binding mismatch",
    )
    _require(child.availability_time <= parent.availability_time, "future child evidence")


@dataclass(frozen=True, slots=True)
class BrokerError:
    """Read failure or submission diagnostic; never an implicit retry instruction."""

    evidence: Evidence
    category: ErrorCategory
    reason: str

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _require(isinstance(self.category, ErrorCategory), "expected ErrorCategory")
        _text(self.reason)


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    evidence: Evidence
    target: OrderTarget
    eligibility: Eligibility
    buying_power: Decimal | None
    currency: str
    provider_account_id: str | None = None

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _require(self.target is OrderTarget.PAPER, "paper account required")
        _require(isinstance(self.eligibility, Eligibility), "expected Eligibility")
        _text(self.currency)
        if self.provider_account_id is not None:
            _text(self.provider_account_id)
        if self.buying_power is not None:
            _decimal(self.buying_power)
        _require(
            self.eligibility is not Eligibility.ELIGIBLE or self.buying_power is not None,
            "eligible account requires buying power",
        )


@dataclass(frozen=True, slots=True)
class BrokerClock:
    """Boundaries describe trading_date's regular session, including early closes.

    Closed holidays may have no boundaries. UNKNOWN is never a usable session.
    """

    evidence: Evidence
    state: SessionState
    trading_date: date
    session_open: datetime | None
    session_close: datetime | None

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _require(isinstance(self.state, SessionState), "expected SessionState")
        _require(type(self.trading_date) is date, "expected broker trading date")
        _require(
            (self.session_open is None) == (self.session_close is None),
            "session boundaries must be paired",
        )
        if self.session_open is not None and self.session_close is not None:
            object.__setattr__(self, "session_open", _utc(self.session_open))
            object.__setattr__(self, "session_close", _utc(self.session_close))
            _require(self.session_open < self.session_close, "invalid session bounds")
            within = self.session_open <= self.evidence.observation_time < self.session_close
            _require(
                self.state is SessionState.UNKNOWN
                or within == (self.state is SessionState.REGULAR),
                "contradictory session",
            )
        else:
            _require(self.state is not SessionState.REGULAR, "regular session needs boundaries")


@dataclass(frozen=True, slots=True)
class BrokerAsset:
    evidence: Evidence
    instrument: Instrument
    us_equity: Eligibility
    tradable: Eligibility

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _require(isinstance(self.instrument, Instrument), "expected Instrument")
        _require(
            isinstance(self.us_equity, Eligibility) and isinstance(self.tradable, Eligibility),
            "expected asset eligibility",
        )


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """Actual long quantity, including fractional residuals; never a simulation fill."""

    evidence: Evidence
    instrument: Instrument
    quantity: Decimal

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _require(isinstance(self.instrument, Instrument), "expected Instrument")
        _decimal(self.quantity, positive=True)


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """Cumulative fill evidence; terminal status does not imply zero exposure.

    Unsupported order semantics are represented by None, never coerced to market/DAY.
    Missing replacement successors remain visible and require reconciliation.
    """

    evidence: Evidence
    order_id: str
    client_id: str | None
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    filled_quantity: Decimal
    status: OrderStatus
    order_type: OrderType | None
    time_in_force: TimeInForce | None
    extended_hours: bool | None
    replaces: str | None
    replaced_by: str | None

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _text(self.order_id)
        for link in (self.client_id, self.replaces, self.replaced_by):
            if link is not None:
                _text(link)
        _require(self.order_id not in (self.replaces, self.replaced_by), "self replacement")
        _require(self.replaces is None or self.replaces != self.replaced_by, "replacement cycle")
        _require(isinstance(self.instrument, Instrument), "expected Instrument")
        _require(isinstance(self.side, OrderSide), "expected OrderSide")
        _require(isinstance(self.status, OrderStatus), "expected OrderStatus")
        _require(self.order_type is None or isinstance(self.order_type, OrderType), "order type")
        _require(self.time_in_force is None or isinstance(self.time_in_force, TimeInForce), "TIF")
        _require(self.extended_hours is None or type(self.extended_hours) is bool, "hours flag")
        _decimal(self.quantity, positive=True)
        _decimal(self.filled_quantity)
        _require(self.filled_quantity <= self.quantity, "overfill")
        if self.status is OrderStatus.FILLED:
            _require(self.filled_quantity == self.quantity, "filled quantity mismatch")
        elif self.status is OrderStatus.PARTIALLY_FILLED:
            _require(0 < self.filled_quantity < self.quantity, "partial quantity mismatch")
        elif self.status in (
            OrderStatus.ACCEPTED,
            OrderStatus.NEW,
            OrderStatus.PENDING_NEW,
            OrderStatus.REJECTED,
        ):
            _require(self.filled_quantity == 0, "unfilled status has fills")

    @property
    def is_terminal(self) -> bool:
        """Lifecycle classification derived from the normalized status."""

        return self.status.is_terminal


@dataclass(frozen=True, slots=True)
class BrokerFill:
    evidence: Evidence
    execution_id: str
    order_id: str
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _text(self.execution_id)
        _text(self.order_id)
        _require(isinstance(self.instrument, Instrument), "expected Instrument")
        _require(isinstance(self.side, OrderSide), "expected OrderSide")
        _decimal(self.quantity, positive=True)
        _decimal(self.price, positive=True)


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    evidence: Evidence
    event_id: str
    kind: UpdateKind
    order: BrokerOrder
    fill: BrokerFill | None
    affected_execution_id: str | None

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _text(self.event_id)
        _require(isinstance(self.kind, UpdateKind), "expected UpdateKind")
        _require(isinstance(self.order, BrokerOrder), "expected BrokerOrder")
        _binding(self.evidence, self.order.evidence)
        if self.affected_execution_id is not None:
            _text(self.affected_execution_id)
        if self.kind in (UpdateKind.TRADE_BUST, UpdateKind.TRADE_CORRECTION):
            _require(self.affected_execution_id is not None, "correction needs execution identity")
        if self.kind is UpdateKind.FILL:
            _require(self.fill is not None, "fill update needs execution")
        if self.fill is not None:
            _require(isinstance(self.fill, BrokerFill), "expected BrokerFill")
            _binding(self.evidence, self.fill.evidence)
            _require(
                (self.fill.order_id, self.fill.instrument, self.fill.side)
                == (self.order.order_id, self.order.instrument, self.order.side),
                "fill mismatch",
            )
            if self.kind is UpdateKind.FILL:
                _require(
                    self.fill.quantity <= self.order.filled_quantity, "fill exceeds cumulative"
                )


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    """Account-wide positions and orders, not an atomic reconciliation cut.

    orders_complete asserts all open orders AND history within the inclusive
    history_start/history_end interval, with pagination exhausted. ``orders``
    deliberately retains both terminal history and outstanding orders; use the
    derived views below when the distinction matters. False retains supplied rows
    but cannot establish absence. Cross-observation reconciliation, replacement
    chains, and order/position agreement belong to P5A.3.
    """

    evidence: Evidence
    positions: tuple[BrokerPosition, ...]
    orders: tuple[BrokerOrder, ...]
    positions_complete: bool
    orders_complete: bool
    history_start: datetime
    history_end: datetime

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _require(
            type(self.positions_complete) is bool and type(self.orders_complete) is bool,
            "explicit completeness required",
        )
        _require(
            isinstance(self.positions, tuple) and isinstance(self.orders, tuple),
            "immutable snapshot rows required",
        )
        _require(all(isinstance(row, BrokerPosition) for row in self.positions), "position rows")
        _require(all(isinstance(row, BrokerOrder) for row in self.orders), "order rows")
        rows: tuple[BrokerPosition | BrokerOrder, ...] = (*self.positions, *self.orders)
        for row in rows:
            _binding(self.evidence, row.evidence)
        _require(
            len({row.instrument for row in self.positions}) == len(self.positions),
            "duplicate position instrument",
        )
        _require(len({row.order_id for row in self.orders}) == len(self.orders), "duplicate order")
        clients = [row.client_id for row in self.orders if row.client_id is not None]
        _require(len(set(clients)) == len(clients), "duplicate client identity")
        object.__setattr__(
            self,
            "positions",
            tuple(sorted(self.positions, key=lambda row: row.instrument.identifier)),
        )
        object.__setattr__(self, "orders", tuple(sorted(self.orders, key=lambda row: row.order_id)))
        object.__setattr__(self, "history_start", _utc(self.history_start))
        object.__setattr__(self, "history_end", _utc(self.history_end))
        _require(
            self.history_start <= self.history_end <= self.evidence.observation_time,
            "invalid history coverage",
        )

    @property
    def complete(self) -> bool:
        return self.positions_complete and self.orders_complete

    @property
    def terminal_orders(self) -> tuple[BrokerOrder, ...]:
        """Historical rows whose documented lifecycle is complete."""

        return tuple(order for order in self.orders if order.is_terminal)

    @property
    def outstanding_orders(self) -> tuple[BrokerOrder, ...]:
        """Rows that cannot safely establish the absence of broker authority."""

        return tuple(order for order in self.orders if not order.is_terminal)


@dataclass(frozen=True, slots=True)
class SubmitRequest:
    """Exact paper payload, NOT a capability; only a future guarded dispatcher may send it."""

    account_id: str
    operational_scope: str
    client_id: str
    instrument: Instrument
    side: OrderSide
    quantity: Decimal
    target: OrderTarget
    order_type: OrderType
    time_in_force: TimeInForce
    extended_hours: bool

    def __post_init__(self) -> None:
        for value in (self.account_id, self.operational_scope, self.client_id):
            _text(value)
        _require(isinstance(self.instrument, Instrument), "expected Instrument")
        _require(isinstance(self.side, OrderSide), "expected OrderSide")
        _decimal(self.quantity, positive=True)
        _require(self.quantity == 1, "canary requires one share")
        _require(
            self.target is OrderTarget.PAPER
            and self.order_type is OrderType.MARKET
            and self.time_in_force is TimeInForce.DAY
            and self.extended_hours is False,
            "only regular-hours paper market/DAY supported",
        )


class SubmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    evidence: Evidence
    request: SubmitRequest
    status: SubmissionStatus
    order: BrokerOrder | None
    error: BrokerError | None

    def __post_init__(self) -> None:
        _evidence(self.evidence)
        _require(isinstance(self.request, SubmitRequest), "expected SubmitRequest")
        _require(isinstance(self.status, SubmissionStatus), "expected SubmissionStatus")
        _require(
            (self.evidence.account_id, self.evidence.operational_scope)
            == (self.request.account_id, self.request.operational_scope),
            "request binding",
        )
        if self.status is SubmissionStatus.ACCEPTED:
            _require(
                isinstance(self.order, BrokerOrder) and self.error is None,
                "acceptance requires order only",
            )
            assert self.order is not None
            _binding(self.evidence, self.order.evidence)
            _require(
                (
                    self.order.client_id,
                    self.order.instrument,
                    self.order.side,
                    self.order.quantity,
                    self.order.order_type,
                    self.order.time_in_force,
                    self.order.extended_hours,
                )
                == (
                    self.request.client_id,
                    self.request.instrument,
                    self.request.side,
                    self.request.quantity,
                    self.request.order_type,
                    self.request.time_in_force,
                    self.request.extended_hours,
                ),
                "accepted payload mismatch",
            )
            _require(
                self.order.status not in (OrderStatus.UNKNOWN, OrderStatus.REJECTED),
                "acceptance needs definitive order evidence",
            )
        else:
            _require(
                self.order is None and isinstance(self.error, BrokerError),
                "nonacceptance requires diagnostic only",
            )
            assert self.error is not None
            _binding(self.evidence, self.error.evidence)
            definitive = self.error.category is ErrorCategory.DEFINITIVE_REJECTION
            _require(
                definitive == (self.status is SubmissionStatus.REJECTED),
                "ambiguous failure cannot prove rejection",
            )


@dataclass(frozen=True, slots=True)
class CanaryConfig:
    """Explicit operator choices; configuration alone never enables dispatch.

    Asset evidence must independently establish US equity eligibility. The positive
    buying_power_buffer is an absolute amount in currency added to quote-side
    notional. Future entry checks must fit that estimate within buying power and
    maximum_entry_notional; neither bound guarantees a market execution price.
    """

    configuration_id: str
    account_id: str
    operational_scope: str
    enabled: bool
    provider: str
    target: OrderTarget
    instrument: Instrument
    quantity: Decimal
    order_type: OrderType
    time_in_force: TimeInForce
    regular_hours_only: bool
    maximum_concurrent_positions: int
    maximum_outstanding_orders: int
    daily_submission_limit: int
    maximum_entry_notional: Decimal
    buying_power_buffer: Decimal
    currency: str
    maximum_signal_age: timedelta
    maximum_feature_age: timedelta
    maximum_quote_age: timedelta
    maximum_broker_evidence_age: timedelta
    maximum_control_age: timedelta
    maximum_clock_skew: timedelta
    dispatch_deadline: timedelta
    close_guard: timedelta
    run_duration: timedelta

    def __post_init__(self) -> None:
        for value in (
            self.configuration_id,
            self.account_id,
            self.operational_scope,
            self.currency,
        ):
            _text(value)
        _require(type(self.enabled) is bool, "explicit enablement required")
        _require(
            self.provider == "alpaca" and self.target is OrderTarget.PAPER,
            "only Alpaca paper canary",
        )
        _require(isinstance(self.instrument, Instrument), "exactly one allowlisted instrument")
        _decimal(self.quantity, positive=True)
        _require(self.quantity == 1, "one whole share required")
        _require(
            self.order_type is OrderType.MARKET
            and self.time_in_force is TimeInForce.DAY
            and self.regular_hours_only is True,
            "regular market/DAY required",
        )
        for capacity in (self.maximum_concurrent_positions, self.maximum_outstanding_orders):
            _require(type(capacity) is int and capacity == 1, "one capacity slot required")
        _require(
            type(self.daily_submission_limit) is int and self.daily_submission_limit > 0,
            "positive integer daily ceiling required",
        )
        _decimal(self.maximum_entry_notional, positive=True)
        _decimal(self.buying_power_buffer, positive=True)
        ages = (
            self.maximum_signal_age,
            self.maximum_feature_age,
            self.maximum_quote_age,
            self.maximum_broker_evidence_age,
            self.maximum_control_age,
        )
        for duration in (*ages, self.dispatch_deadline, self.close_guard, self.run_duration):
            _require(
                isinstance(duration, timedelta) and duration > timedelta(0), "positive duration"
            )
        _require(
            isinstance(self.maximum_clock_skew, timedelta)
            and self.maximum_clock_skew >= timedelta(0),
            "nonnegative clock skew",
        )
        _require(
            self.dispatch_deadline <= min(*ages, self.run_duration),
            "dispatch bound exceeds validity/run window",
        )
        # Subtraction avoids overflow for adversarial timedelta.max inputs.
        _require(
            self.close_guard > self.dispatch_deadline
            and self.close_guard - self.dispatch_deadline > self.maximum_clock_skew,
            "close guard must exceed deadline plus skew",
        )


class Broker(Protocol):
    """Provider-neutral port. No production implementation or authority in P5A.1.

    A not-found lookup is a BrokerError, never proof of non-submission. Update
    batches preserve source order and duplicates and never assert completeness.
    submit is the future transport seam, callable only behind P5A.4 guards; request
    identity is not authorization. The sole current implementation is a script fake.
    """

    def read_account(self) -> BrokerAccount | BrokerError: ...
    def read_clock(self) -> BrokerClock | BrokerError: ...
    def read_asset(self, instrument: Instrument) -> BrokerAsset | BrokerError: ...
    def read_snapshot(self) -> BrokerSnapshot | BrokerError: ...
    def lookup_order(
        self, *, order_id: str | None, client_id: str | None
    ) -> BrokerOrder | BrokerError: ...
    def read_updates(self) -> tuple[TradeUpdate, ...] | BrokerError: ...
    def submit(self, request: SubmitRequest) -> SubmissionResult: ...
