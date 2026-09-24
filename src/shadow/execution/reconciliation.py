"""Deterministic P5A.3 broker-authoritative lifecycle projection.

This module is deliberately a reducer.  Collecting snapshots and client-id
lookups is the caller's responsibility; reconciliation never owns a Broker and
cannot submit, cancel, or replace an order.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from shadow.execution.broker import BrokerOrder, BrokerSnapshot, OrderStatus
from shadow.execution.btc_authority import BtcAttempt
from shadow.execution.crypto import ExecutionAsset, execution_asset
from shadow.execution.crypto_accounting import CryptoActivityEvidence, inventory_effects
from shadow.execution.journal import CommittedAttempt
from shadow.risk.models import OrderSide


class OperationalState(StrEnum):
    """The only lifecycle states a future PAPER application may consume."""

    FLAT = "flat"
    ENTRY_PENDING = "entry_pending"
    HOLDING = "holding"
    EXIT_PENDING = "exit_pending"
    UNRESOLVED = "unresolved"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """A reproducible projection; ``reason`` is evidence, never permission."""

    state: OperationalState
    exposure: Decimal
    linked_order_ids: tuple[str, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, OperationalState):
            raise ValueError("invalid operational state")
        if not isinstance(self.exposure, Decimal) or not self.exposure.is_finite():
            raise ValueError("exposure must be finite Decimal")
        if self.exposure < 0:
            raise ValueError("short exposure is outside the PAPER envelope")
        if tuple(sorted(set(self.linked_order_ids))) != self.linked_order_ids:
            raise ValueError("linked order ids must be sorted and unique")
        if self.reason is not None and (not self.reason or self.reason != self.reason.strip()):
            raise ValueError("invalid reconciliation reason")

    @property
    def permits_entry_evaluation(self) -> bool:
        return self.state is OperationalState.FLAT

    @property
    def permits_exit_evaluation(self) -> bool:
        return self.state is OperationalState.HOLDING


def _result(
    state: OperationalState,
    exposure: Decimal = Decimal(0),
    reason: str | None = None,
    order_ids: set[str] | None = None,
) -> Reconciliation:
    return Reconciliation(state, exposure, tuple(sorted(order_ids or set())), reason)


def reconcile(
    *,
    attempts: tuple[CommittedAttempt | BtcAttempt, ...],
    snapshot: BrokerSnapshot,
    lookup_orders: tuple[BrokerOrder, ...] = (),
    crypto_evidence: CryptoActivityEvidence | None = None,
    require_crypto_evidence: bool = False,
) -> Reconciliation:
    """Reduce journal attempts and one authoritative broker cut.

    ``snapshot.complete`` and history covering every attempt are required before
    capacity can be released.  An uncertain submit additionally requires a
    linked broker order: a not-found lookup is not absence evidence.
    """
    if not isinstance(snapshot, BrokerSnapshot) or not isinstance(attempts, tuple):
        raise TypeError("typed snapshot and immutable attempts are required")
    if not all(isinstance(item, (CommittedAttempt, BtcAttempt)) for item in attempts):
        raise TypeError("attempts must be committed journal evidence")
    if not isinstance(lookup_orders, tuple) or not all(
        isinstance(item, BrokerOrder) for item in lookup_orders
    ):
        raise TypeError("lookup_orders must be typed broker evidence")
    if any(isinstance(attempt, BtcAttempt) for attempt in attempts):
        require_crypto_evidence = True
    binding = (snapshot.evidence.account_id, snapshot.evidence.operational_scope)
    if any(
        (attempt.request.account_id, attempt.request.operational_scope) != binding
        for attempt in attempts
    ):
        return _result(OperationalState.HALTED, reason="journal/broker binding conflict")
    if any(
        (order.evidence.account_id, order.evidence.operational_scope) != binding
        for order in lookup_orders
    ):
        return _result(OperationalState.HALTED, reason="lookup evidence binding conflict")
    if not snapshot.complete:
        return _result(OperationalState.UNRESOLVED, reason="broker snapshot is incomplete")
    snapshot_ids = {order.order_id for order in snapshot.orders}
    if any(order.order_id not in snapshot_ids for order in lookup_orders):
        return _result(OperationalState.HALTED, reason="lookup order absent from complete snapshot")

    # Same order id must describe precisely one broker fact.  A later, different
    # update cannot be guessed into a chronology without an explicit event cut.
    orders: dict[str, BrokerOrder] = {}
    for order in (*snapshot.orders, *lookup_orders):
        prior = orders.get(order.order_id)
        if prior is not None and any(
            getattr(prior, field.name) != getattr(order, field.name)
            for field in fields(BrokerOrder)
            if field.name != "evidence"
        ):
            return _result(OperationalState.HALTED, reason="contradictory broker order evidence")
        if prior is None:
            orders[order.order_id] = order

    if crypto_evidence is not None:
        if (
            crypto_evidence.evidence.account_id,
            crypto_evidence.evidence.operational_scope,
        ) != binding:
            return _result(OperationalState.HALTED, reason="activity binding conflict")
        if crypto_evidence.unsupported:
            return _result(OperationalState.HALTED, reason="unsupported broker activity")
    if not attempts:
        if crypto_evidence is not None and (crypto_evidence.executions or crypto_evidence.fees):
            return _result(OperationalState.HALTED, reason="unlinked broker activity")
        if snapshot.positions or orders:
            return _result(OperationalState.HALTED, reason="unlinked broker activity")
        if require_crypto_evidence and (
            crypto_evidence is None
            or not crypto_evidence.query_exhausted
            or not crypto_evidence.history_verified
        ):
            return _result(OperationalState.UNRESOLVED, reason="activity history unproven")
        return _result(OperationalState.FLAT)
    if len({attempt.request.instrument for attempt in attempts}) != 1:
        return _result(OperationalState.HALTED, reason="multiple journal instruments")

    assets = {execution_asset(attempt.request) for attempt in attempts}
    if len(assets) != 1:
        return _result(OperationalState.HALTED, reason="mixed execution asset contracts")

    earliest = min(item.committed_at for item in attempts)
    if snapshot.history_start > earliest:
        return _result(OperationalState.UNRESOLVED, reason="broker history does not cover attempts")

    by_client: dict[str, BrokerOrder] = {}
    for order in orders.values():
        if order.client_id is not None:
            if order.client_id in by_client:
                return _result(OperationalState.HALTED, reason="duplicate broker client identity")
            by_client[order.client_id] = order
    linked: dict[str, BrokerOrder] = {}
    for attempt in attempts:
        matched_order = by_client.get(attempt.client_order_id)
        if matched_order is not None:
            linked[matched_order.order_id] = matched_order

    # Replacement successors may legitimately carry a different client id, but
    # every declared edge must be visible and mutually corroborated.
    changed = True
    while changed:
        changed = False
        for order in tuple(linked.values()):
            if order.replaced_by is not None:
                successor = orders.get(order.replaced_by)
                if successor is None:
                    return _result(OperationalState.HALTED, reason="replacement successor missing")
                if successor.replaces != order.order_id:
                    return _result(OperationalState.HALTED, reason="replacement chain conflicts")
                if successor.order_id not in linked:
                    linked[successor.order_id] = successor
                    changed = True
            if order.replaces is not None:
                predecessor = orders.get(order.replaces)
                if predecessor is None or predecessor.replaced_by != order.order_id:
                    return _result(
                        OperationalState.HALTED, reason="replacement predecessor conflicts"
                    )
                if predecessor.order_id not in linked:
                    linked[predecessor.order_id] = predecessor
                    changed = True

    linked_ids = set(linked)
    if any(order.order_id not in linked_ids for order in orders.values()):
        return _result(
            OperationalState.HALTED, reason="unlinked broker activity", order_ids=linked_ids
        )

    for order in linked.values():
        candidates = [a for a in attempts if a.client_order_id == order.client_id]
        # Replacement children inherit the original request only through a
        # validated edge; their broker-side fields must still preserve envelope.
        request = candidates[0].request if candidates else None
        if request is not None and (
            order.instrument != request.instrument
            or order.side != request.side
            or order.quantity != request.quantity
            or order.order_type != request.order_type
            or order.time_in_force != request.time_in_force
            or order.extended_hours != request.extended_hours
        ):
            return _result(
                OperationalState.HALTED,
                reason="broker order conflicts with journal",
                order_ids=linked_ids,
            )
        if order.status is OrderStatus.UNKNOWN:
            return _result(
                OperationalState.HALTED, reason="broker order status unknown", order_ids=linked_ids
            )
        if request is None:
            # A replacement may have a provider-generated client id, but cannot
            # change the operational envelope of its linked predecessor.
            assert order.replaces is not None
            predecessor = linked[order.replaces]
            if (
                order.instrument != predecessor.instrument
                or order.side != predecessor.side
                or order.quantity != predecessor.quantity
                or order.order_type != predecessor.order_type
                or order.time_in_force != predecessor.time_in_force
                or order.extended_hours != predecessor.extended_hours
            ):
                return _result(
                    OperationalState.HALTED,
                    reason="replacement order conflicts with predecessor",
                    order_ids=linked_ids,
                )

    uncertain = [
        a for a in attempts if a.submission is None or a.submission.status.value == "uncertain"
    ]
    if any(a.client_order_id not in by_client for a in uncertain):
        return _result(
            OperationalState.UNRESOLVED,
            reason="uncertain submission lacks broker order",
            order_ids=linked_ids,
        )
    if any(a.client_order_id not in by_client for a in attempts):
        return _result(
            OperationalState.UNRESOLVED,
            reason="committed attempt lacks broker order",
            order_ids=linked_ids,
        )

    # Replacement filled_qty is a cumulative order-chain observation.  Each
    # successor must retain at least its predecessor cumulative fill; summing
    # both order rows would count executions twice.
    expected = Fraction(0)
    for order in linked.values():
        if order.replaces is not None:
            predecessor = linked[order.replaces]
            if order.filled_quantity < predecessor.filled_quantity:
                return _result(
                    OperationalState.HALTED,
                    reason="replacement cumulative fill regressed",
                    order_ids=linked_ids,
                )
        if order.replaced_by is None:
            expected += (
                Fraction(order.filled_quantity)
                if order.side is OrderSide.BUY
                else -Fraction(order.filled_quantity)
            )
    if assets == {ExecutionAsset.BTC_SPOT} and (
        require_crypto_evidence or crypto_evidence is not None
    ):
        if crypto_evidence is None:
            return _result(OperationalState.UNRESOLVED, reason="BTC activity evidence missing")
        if (
            crypto_evidence.history_start > earliest
            or crypto_evidence.history_end < snapshot.history_end
        ):
            return _result(OperationalState.UNRESOLVED, reason="BTC activity history truncated")
        if crypto_evidence.evidence.availability_time > snapshot.evidence.availability_time:
            return _result(OperationalState.UNRESOLVED, reason="activities newer than position cut")
        if any(fill.order_id not in linked for fill in crypto_evidence.executions):
            return _result(OperationalState.HALTED, reason="unlinked broker execution")
        try:
            effects = inventory_effects(crypto_evidence)
        except ValueError as exc:
            return _result(OperationalState.HALTED, reason=str(exc))
        except LookupError as exc:
            return _result(OperationalState.UNRESOLVED, reason=str(exc))
        if any(order.replaces or order.replaced_by for order in linked.values()):
            return _result(OperationalState.HALTED, reason="BTC replacement accounting unsupported")
        totals = dict.fromkeys(linked, Fraction(0))
        for effect in effects:
            fill = effect.execution
            if fill.side != linked[fill.order_id].side:
                return _result(OperationalState.HALTED, reason="execution side conflict")
            totals[fill.order_id] += Fraction(fill.quantity)
        if any(totals[key] > order.filled_quantity for key, order in linked.items()):
            return _result(
                OperationalState.HALTED, reason="gross executions exceed linked order fills"
            )
        if any(totals[key] != order.filled_quantity for key, order in linked.items()):
            return _result(OperationalState.UNRESOLVED, reason="gross execution coverage mismatch")
        expected = sum((Fraction(effect.net_btc) for effect in effects), Fraction(0))
    position = next(
        (row for row in snapshot.positions if row.instrument == attempts[0].request.instrument),
        None,
    )
    if len(snapshot.positions) > (1 if position is not None else 0):
        return _result(
            OperationalState.HALTED, reason="unlinked broker position", order_ids=linked_ids
        )
    actual = Decimal(0) if position is None else position.quantity
    if actual != expected:
        return _result(
            OperationalState.HALTED,
            actual,
            "broker position conflicts with linked fills",
            linked_ids,
        )
    if assets == {ExecutionAsset.US_EQUITY} and actual != actual.to_integral_value():
        return _result(OperationalState.HALTED, actual, "fractional broker residue", linked_ids)

    outstanding = [
        order for order in linked.values() if order.replaced_by is None and not order.is_terminal
    ]
    if outstanding:
        if {order.side for order in outstanding} == {OrderSide.BUY, OrderSide.SELL}:
            return _result(
                OperationalState.HALTED,
                actual,
                "simultaneous outstanding buy and sell",
                linked_ids,
            )
        if any(order.side is OrderSide.SELL for order in outstanding):
            return _result(OperationalState.EXIT_PENDING, actual, order_ids=linked_ids)
        return _result(OperationalState.ENTRY_PENDING, actual, order_ids=linked_ids)
    if actual == 0:
        return _result(OperationalState.FLAT, order_ids=linked_ids)
    return _result(OperationalState.HOLDING, actual, order_ids=linked_ids)
