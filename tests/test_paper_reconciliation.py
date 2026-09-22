"""P5A.3 reducer tests: typed evidence only, with no broker transport."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from shadow.domain import Instrument
from shadow.execution.broker import (
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Evidence,
    OrderStatus,
)
from shadow.execution.journal import ExecutionJournal
from shadow.execution.ownership import AccountOwner
from shadow.execution.reconciliation import OperationalState, reconcile
from shadow.risk.models import OrderSide
from tests.test_paper_dispatch import _dispatch_with_historical_status

NOW = datetime(2026, 9, 18, 15, tzinfo=UTC)
SPY = Instrument("SPY")


def test_clean_state_is_flat() -> None:
    evidence = Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
    state = reconcile(
        attempts=(),
        snapshot=BrokerSnapshot(evidence, (), (), True, True, NOW - timedelta(minutes=1), NOW),
    )
    assert state.state is OperationalState.FLAT


def test_entry_exit_uncertainty_and_inconsistency(tmp_path: Path) -> None:
    outcome, broker = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    del broker
    assert outcome.attempt is not None
    attempt = outcome.attempt
    ev = Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
    filled = BrokerOrder(
        ev,
        "order",
        attempt.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        Decimal(1),
        OrderStatus.FILLED,
        attempt.request.order_type,
        attempt.request.time_in_force,
        False,
        None,
        None,
    )
    holding = reconcile(
        attempts=(attempt,),
        snapshot=BrokerSnapshot(
            ev,
            (BrokerPosition(ev, SPY, Decimal(1)),),
            (filled,),
            True,
            True,
            NOW - timedelta(minutes=1),
            NOW,
        ),
    )
    assert (holding.state, holding.exposure) == (OperationalState.HOLDING, Decimal(1))
    assert (
        reconcile(
            attempts=(attempt,),
            snapshot=BrokerSnapshot(ev, (), (), True, True, NOW - timedelta(minutes=1), NOW),
        ).state
        is OperationalState.UNRESOLVED
    )
    assert (
        reconcile(
            attempts=(attempt,),
            snapshot=BrokerSnapshot(ev, (), (filled,), True, True, NOW - timedelta(minutes=1), NOW),
        ).state
        is OperationalState.HALTED
    )


def test_linked_exit_pending_and_full_exit_are_flat(tmp_path: Path) -> None:
    outcome, broker = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    del broker
    assert outcome.attempt is not None
    buy = outcome.attempt
    sell_request = replace(buy.request, client_id="exit-client", side=OrderSide.SELL)
    sell = replace(
        buy, intent_identity="exit-intent", client_order_id="exit-client", request=sell_request
    )
    ev = Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
    buy_order = BrokerOrder(
        ev,
        "buy-order",
        buy.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        Decimal(1),
        OrderStatus.FILLED,
        buy.request.order_type,
        buy.request.time_in_force,
        False,
        None,
        None,
    )
    open_exit = BrokerOrder(
        ev,
        "exit-order",
        sell.client_order_id,
        SPY,
        OrderSide.SELL,
        Decimal(1),
        Decimal(0),
        OrderStatus.NEW,
        sell.request.order_type,
        sell.request.time_in_force,
        False,
        None,
        None,
    )
    pending = BrokerSnapshot(
        ev,
        (BrokerPosition(ev, SPY, Decimal(1)),),
        (buy_order, open_exit),
        True,
        True,
        NOW - timedelta(minutes=1),
        NOW,
    )
    assert reconcile(attempts=(buy, sell), snapshot=pending).state is OperationalState.EXIT_PENDING
    filled_exit = replace(open_exit, status=OrderStatus.FILLED, filled_quantity=Decimal(1))
    flat = BrokerSnapshot(
        ev, (), (buy_order, filled_exit), True, True, NOW - timedelta(minutes=1), NOW
    )
    assert reconcile(attempts=(buy, sell), snapshot=flat).state is OperationalState.FLAT


def test_fractional_unlinked_replacement_and_idempotence(tmp_path: Path) -> None:
    outcome, broker = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    del broker
    assert outcome.attempt is not None
    attempt = outcome.attempt
    ev = Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
    old = BrokerOrder(
        ev,
        "old",
        attempt.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        Decimal(0),
        OrderStatus.REPLACED,
        attempt.request.order_type,
        attempt.request.time_in_force,
        False,
        None,
        "new",
    )
    new = BrokerOrder(
        ev,
        "new",
        "replacement-client",
        SPY,
        OrderSide.BUY,
        Decimal(1),
        Decimal(0),
        OrderStatus.NEW,
        attempt.request.order_type,
        attempt.request.time_in_force,
        False,
        "old",
        None,
    )
    snap = BrokerSnapshot(ev, (), (old, new), True, True, NOW - timedelta(minutes=1), NOW)
    assert reconcile(attempts=(attempt,), snapshot=snap).state is OperationalState.ENTRY_PENDING
    assert reconcile(attempts=(attempt,), snapshot=snap) == reconcile(
        attempts=(attempt,), snapshot=snap
    )
    contradictory = replace(new, side=OrderSide.SELL)
    assert (
        reconcile(
            attempts=(attempt,),
            snapshot=BrokerSnapshot(
                ev,
                (),
                (old, contradictory),
                True,
                True,
                NOW - timedelta(minutes=1),
                NOW,
            ),
        ).state
        is OperationalState.HALTED
    )
    assert (
        reconcile(
            attempts=(attempt,),
            snapshot=BrokerSnapshot(ev, (), (old,), True, True, NOW - timedelta(minutes=1), NOW),
        ).state
        is OperationalState.HALTED
    )
    filled = BrokerOrder(
        ev,
        "fill",
        attempt.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        Decimal(1),
        OrderStatus.FILLED,
        attempt.request.order_type,
        attempt.request.time_in_force,
        False,
        None,
        None,
    )
    fractional = BrokerSnapshot(
        ev,
        (BrokerPosition(ev, SPY, Decimal("0.1")),),
        (filled,),
        True,
        True,
        NOW - timedelta(minutes=1),
        NOW,
    )
    assert reconcile(attempts=(attempt,), snapshot=fractional).state is OperationalState.HALTED


def test_restart_replays_journal_attempt_to_same_broker_projection(tmp_path: Path) -> None:
    outcome, broker = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    del broker
    assert outcome.attempt is not None
    original = outcome.attempt
    owner_dir = tmp_path / "reopen-owner"
    owner_dir.mkdir()
    path = tmp_path / "reopen.sqlite"
    with AccountOwner.acquire(ownership_directory=owner_dir, account_id="paper-account") as owner:
        with ExecutionJournal.create(
            path=path,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
        ) as journal:
            journal.commit_attempt(
                intent_identity=original.intent_identity,
                source_opportunity_id=original.source_opportunity_id,
                client_order_id=original.client_order_id,
                client_order_full_digest=original.client_order_full_digest,
                broker_trading_date=original.broker_trading_date,
                committed_at=original.committed_at,
                dispatch_deadline=original.dispatch_deadline,
                request=original.request,
                risk_decision=original.risk_decision,
                account=original.account,
                clock=original.clock,
                asset=original.asset,
                snapshot=original.snapshot,
            )
        with ExecutionJournal.reopen(
            path=path, owner=owner, account_id="paper-account", operational_scope="paper-scope"
        ) as journal:
            restored = journal.committed_attempts()
    ev = Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
    filled = BrokerOrder(
        ev,
        "order",
        original.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        Decimal(1),
        OrderStatus.FILLED,
        original.request.order_type,
        original.request.time_in_force,
        False,
        None,
        None,
    )
    snap = BrokerSnapshot(
        ev,
        (BrokerPosition(ev, SPY, Decimal(1)),),
        (filled,),
        True,
        True,
        NOW - timedelta(minutes=1),
        NOW,
    )
    assert reconcile(attempts=(original,), snapshot=snap) == reconcile(
        attempts=restored, snapshot=snap
    )


def test_replacement_cumulative_fill_and_duplicate_lookup(tmp_path: Path) -> None:
    outcome, _ = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    assert outcome.attempt is not None
    base = outcome.attempt
    request = base.request
    attempt = base
    ev = Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
    old = BrokerOrder(
        ev,
        "old",
        attempt.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        Decimal("0.25"),
        OrderStatus.REPLACED,
        request.order_type,
        request.time_in_force,
        False,
        None,
        "new",
    )
    new = replace(
        old,
        order_id="new",
        client_id="replacement-client",
        filled_quantity=Decimal(1),
        status=OrderStatus.FILLED,
        replaces="old",
        replaced_by=None,
    )
    snapshot = BrokerSnapshot(
        ev,
        (BrokerPosition(ev, SPY, Decimal(1)),),
        (old, new),
        True,
        True,
        NOW - timedelta(minutes=1),
        NOW,
    )
    lookup_ev = Evidence("lookup-request", "paper-account", "paper-scope", NOW, NOW)
    result = reconcile(
        attempts=(attempt,),
        snapshot=snapshot,
        lookup_orders=(replace(old, evidence=lookup_ev),),
    )
    assert (result.state, result.exposure) == (OperationalState.HOLDING, Decimal(1))
    regressed = replace(new, filled_quantity=Decimal(0), status=OrderStatus.NEW)
    bad_snapshot = replace(snapshot, orders=(old, regressed), positions=())
    assert reconcile(attempts=(attempt,), snapshot=bad_snapshot).state is OperationalState.HALTED
    contradictory = replace(old, evidence=lookup_ev, filled_quantity=Decimal(0))
    assert (
        reconcile(attempts=(attempt,), snapshot=snapshot, lookup_orders=(contradictory,)).state
        is OperationalState.HALTED
    )
    assert (
        reconcile(
            attempts=(attempt,),
            snapshot=replace(snapshot, orders=(old,)),
            lookup_orders=(replace(new, evidence=lookup_ev),),
        ).state
        is OperationalState.HALTED
    )


def test_simultaneous_outstanding_buy_and_sell_halts(tmp_path: Path) -> None:
    outcome, _ = _dispatch_with_historical_status(tmp_path, OrderStatus.FILLED)
    assert outcome.attempt is not None
    buy = outcome.attempt
    sell_request = replace(buy.request, client_id="exit-client", side=OrderSide.SELL)
    sell = replace(
        buy, intent_identity="exit-intent", client_order_id="exit-client", request=sell_request
    )
    ev = Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
    buy_order = BrokerOrder(
        ev,
        "buy",
        buy.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        Decimal(0),
        OrderStatus.NEW,
        buy.request.order_type,
        buy.request.time_in_force,
        False,
        None,
        None,
    )
    sell_order = replace(
        buy_order, order_id="sell", client_id=sell.client_order_id, side=OrderSide.SELL
    )
    snapshot = BrokerSnapshot(
        ev, (), (buy_order, sell_order), True, True, NOW - timedelta(minutes=1), NOW
    )
    result = reconcile(attempts=(buy, sell), snapshot=snapshot)
    assert result.state is OperationalState.HALTED
    assert result.reason == "simultaneous outstanding buy and sell"
