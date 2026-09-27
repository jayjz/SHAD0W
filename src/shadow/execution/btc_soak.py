"""Operational PAPER-only lifecycle evidence; never proof inventory or fee authority."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Protocol

from shadow.execution.broker import (
    BrokerError,
    BrokerPosition,
    BrokerSnapshot,
    Evidence,
    OrderStatus,
    SubmissionResult,
    SubmissionStatus,
)
from shadow.execution.btc_dispatch import BtcBroker
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.features.btc_trend import BTC
from shadow.risk.btc import utc_ns
from shadow.risk.models import OrderSide

AUTHORITY = "PAPER_SOAK_OPERATIONAL"


class SoakHalted(RuntimeError):
    """A stable local reason code; provider exception text must not be persisted."""


class SoakBroker(BtcBroker, Protocol):
    def read_btc_available(self) -> BrokerPosition | BrokerError: ...


@dataclass(frozen=True)
class SoakAttempt:
    request: BtcSubmitRequest
    committed_at: datetime
    result: SubmissionResult | None = None


@dataclass(frozen=True)
class SoakCut:
    account: BtcCashAccount
    asset: BtcBrokerAsset
    snapshot: BrokerSnapshot
    activities: CryptoActivityEvidence
    available: BrokerPosition | None

    def payload(self) -> tuple[object, ...]:
        return self.account, self.asset, self.snapshot, self.activities, self.available


@dataclass(frozen=True)
class SoakState:
    state: str
    quantity: Decimal = Decimal(0)
    entry_ns: int | None = None
    completed_cycles: int = 0


def fresh(evidence: Evidence, binding: tuple[str, str], now_ns: int, age_ns: int) -> bool:
    return (
        (evidence.account_id, evidence.operational_scope) == binding
        and utc_ns(evidence.observation_time) <= utc_ns(evidence.availability_time) <= now_ns
        and 0 <= now_ns - utc_ns(evidence.observation_time) <= age_ns
    )


def operational_state(
    cut: SoakCut,
    attempts: tuple[SoakAttempt, ...],
    *,
    binding: tuple[str, str],
    start: datetime,
    now_ns: int,
    maximum_age_ns: int,
) -> SoakState:
    """Validate actual orders/fills; position is ONLY operational sellable authority.

    No net inventory equation, fee guess, coverage flag, or proof reducer mutation.
    Entire account must start empty. All subsequent orders must belong to this run.
    """
    snapshot, activities = cut.snapshot, cut.activities
    for ev in (
        cut.account.evidence,
        cut.asset.evidence,
        snapshot.evidence,
        activities.evidence,
        *(p.evidence for p in snapshot.positions),
    ):
        if not fresh(ev, binding, now_ns, maximum_age_ns):
            raise SoakHalted("STALE_OR_MISMATCHED_BROKER_EVIDENCE")
    if (
        not snapshot.complete
        or not activities.query_exhausted
        or activities.unsupported
        or snapshot.history_start > start
        or activities.history_start > start
        or snapshot.history_end != activities.history_end
        or not 0 <= now_ns - utc_ns(snapshot.history_end) <= maximum_age_ns
    ):
        raise SoakHalted("INCOMPLETE_BROKER_EVIDENCE")
    if len(snapshot.positions) > 1 or any(p.instrument != BTC for p in snapshot.positions):
        raise SoakHalted("FOREIGN_OR_MULTIPLE_POSITIONS")
    clients = [a.request.client_id for a in attempts]
    if len(set(clients)) != len(clients):
        raise SoakHalted("DUPLICATE_CLIENT_ID")
    if any(a.result is None or a.result.status is not SubmissionStatus.ACCEPTED for a in attempts):
        raise SoakHalted("UNCERTAIN_OR_REJECTED_SUBMISSION")
    orders = {o.client_id: o for o in snapshot.orders}
    if len(orders) != len(snapshot.orders) or set(orders) != set(clients):
        raise SoakHalted("UNLINKED_OR_MISSING_ORDER")
    for i, attempt in enumerate(attempts):
        request, order = attempt.request, orders[attempt.request.client_id]
        request.__post_init__()
        assert attempt.result is not None and attempt.result.order is not None
        if (
            (request.account_id, request.operational_scope) != binding
            or request.side is not (OrderSide.BUY if i % 2 == 0 else OrderSide.SELL)
            or order.order_id != attempt.result.order.order_id
            or (
                order.instrument,
                order.side,
                order.quantity,
                order.order_type,
                order.time_in_force,
                order.extended_hours,
            )
            != (
                request.instrument,
                request.side,
                request.quantity,
                request.order_type,
                request.time_in_force,
                request.extended_hours,
            )
            or order.replaces is not None
            or order.replaced_by is not None
            or (order.evidence.account_id, order.evidence.operational_scope) != binding
            or order.evidence.availability_time > snapshot.evidence.availability_time
            or order.filled_quantity < attempt.result.order.filled_quantity
        ):
            raise SoakHalted("ORDER_CONFLICT")
        if i < len(attempts) - 1 and order.status is not OrderStatus.FILLED:
            raise SoakHalted("PRIOR_ORDER_NOT_FILLED")
    fills = activities.executions
    if len({f.execution_id for f in fills}) != len(fills):
        raise SoakHalted("DUPLICATE_FILL")
    by_id = {o.order_id: o for o in snapshot.orders}
    commits = {orders[a.request.client_id].order_id: a.committed_at for a in attempts}
    totals = dict.fromkeys(by_id, Fraction(0))
    for fill in fills:
        fill_order = by_id.get(fill.order_id)
        if (
            fill_order is None
            or fill.instrument != BTC
            or fill.side is not fill_order.side
            or (fill.evidence.account_id, fill.evidence.operational_scope) != binding
            or not commits[fill.order_id]
            <= fill.evidence.observation_time
            <= activities.history_end
            or fill.evidence.availability_time > activities.evidence.availability_time
        ):
            raise SoakHalted("FILL_CONFLICT")
        totals[fill.order_id] += Fraction(fill.quantity)
    if any(totals[key] > order.filled_quantity for key, order in by_id.items()):
        raise SoakHalted("FILL_TOTAL_CONFLICT")
    # A later order cannot execute before its preceding completed order.
    for previous, following in zip(attempts, attempts[1:], strict=False):
        previous_id = orders[previous.request.client_id].order_id
        following_id = orders[following.request.client_id].order_id
        previous_times = [f.evidence.observation_time for f in fills if f.order_id == previous_id]
        following_times = [f.evidence.observation_time for f in fills if f.order_id == following_id]
        if previous_times and following_times and max(previous_times) > min(following_times):
            raise SoakHalted("FILL_CHRONOLOGY_CONFLICT")
    quantity = snapshot.positions[0].quantity if snapshot.positions else Decimal(0)
    completed = max(0, (len(attempts) - 1) // 2)
    if not attempts:
        if quantity or fills:
            raise SoakHalted("INITIAL_ACCOUNT_NOT_FLAT")
        return SoakState("FLAT")
    last = orders[clients[-1]]
    if not last.is_terminal:
        return SoakState("PENDING", quantity, completed_cycles=completed)
    if last.status is not OrderStatus.FILLED:
        raise SoakHalted("ORDER_TERMINATED_WITHOUT_FULL_FILL")
    if any(totals[key] != order.filled_quantity for key, order in by_id.items()):
        return SoakState("WAITING_FILLS", quantity, completed_cycles=completed)
    if len(attempts) % 2 == 0:
        if quantity:
            raise SoakHalted("SELL_POSITION_RESIDUE")
        return SoakState("FLAT", completed_cycles=len(attempts) // 2)
    available = cut.available
    if (
        quantity <= 0
        or quantity > last.filled_quantity
        or available is None
        or available.instrument != BTC
        or available.quantity != quantity
        or not fresh(available.evidence, binding, now_ns, maximum_age_ns)
    ):
        raise SoakHalted("POSITION_AVAILABLE_DISAGREEMENT")
    entry = min(utc_ns(f.evidence.observation_time) for f in fills if f.order_id == last.order_id)
    return SoakState("HOLDING", quantity, entry, completed)
