"""Bounded lifecycle composition; synthetic fee coverage is never provider evidence."""

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from shadow.application.btc_history import HISTORICAL, LIVE, MarketEvidence
from shadow.application.btc_session import BtcPaperSession
from shadow.domain.crypto_market import CryptoQuote, CryptoTrade, UtcNanoseconds
from shadow.execution.broker import (
    BrokerError,
    BrokerFill,
    BrokerPosition,
    ErrorCategory,
    OrderStatus,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
)
from shadow.execution.btc_journal import BtcJournal
from shadow.execution.crypto import BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoFeeActivity
from shadow.execution.journal import ExecutionJournal
from shadow.features.btc_trend import HOUR_NS
from shadow.risk.btc import utc_ns
from shadow.risk.models import OperatorControls, OrderSide
from tests.test_btc_dispatch import SimulatedCrash
from tests.test_btc_journal import authority
from tests.test_crypto_paper import _Clock, _FreshFake, _trade
from tests.test_crypto_paper import journal as journal
from tests.test_paper_reconciliation import NOW


class LifecycleBroker(_FreshFake):
    fills: tuple[BrokerFill, ...] = ()
    fees: tuple[CryptoFeeActivity, ...] = ()
    exposure = Decimal(0)
    fee_verified = True
    disagreement = False
    partial = False
    unavailable = False

    def read_btc_activities(self, **kwargs):  # type: ignore[no-untyped-def]
        value = super().read_btc_activities(**kwargs)
        return replace(
            value,
            executions=self.fills,
            fees=self.fees,
            fee_complete_ids=tuple(f.execution_id for f in self.fills) if self.fee_verified else (),
        )

    def read_reconciliation_snapshot(self, **kwargs):  # type: ignore[no-untyped-def]
        value = super().read_reconciliation_snapshot(**kwargs)
        quantity = self.exposure + (Decimal("0.0001") if self.disagreement else 0)
        return replace(
            value,
            positions=()
            if quantity == 0
            else (BrokerPosition(value.evidence, self.attempt.request.instrument, quantity),),
        )

    def read_btc_available(self) -> BrokerPosition:
        ev = replace(
            self.attempt.revalidation.snapshot.evidence,
            observation_time=self.clock(),
            availability_time=self.clock(),
        )
        return BrokerPosition(
            ev,
            self.attempt.request.instrument,
            self.exposure / 2 if self.unavailable else self.exposure,
        )

    def submit(
        self, request: SubmitRequest, *, before_post: Callable[[], None] | None = None
    ) -> SubmissionResult:
        prior = self.orders
        assert isinstance(request, BtcSubmitRequest)
        self.attempt = replace(
            self.attempt, evaluation=replace(self.attempt.evaluation, request=request)
        )
        # Preserve the crash/timeout seams and real journal-before-submit assertion.
        try:
            result = super().submit(request, before_post=before_post)
        except TimeoutError:
            raise
        order = replace(
            self.orders[-1],
            order_id=f"order-{self.posts}",
            evidence=replace(
                self.orders[-1].evidence,
                observation_time=self.clock(),
                availability_time=self.clock(),
            ),
        )
        if self.mode == "rejected":
            order = replace(order, status=OrderStatus.REJECTED)
        else:
            quantity = request.quantity / 2 if self.partial else request.quantity
            order = replace(
                order,
                filled_quantity=quantity,
                status=OrderStatus.PARTIALLY_FILLED if self.partial else OrderStatus.FILLED,
            )
            fill = BrokerFill(
                order.evidence,
                f"fill-{self.posts}",
                order.order_id,
                order.instrument,
                order.side,
                quantity,
                Decimal(100),
            )
            self.fills += (fill,)
            fee = Decimal("0.0001") if request.side is OrderSide.BUY else Decimal(0)
            self.fees += (
                CryptoFeeActivity(
                    order.evidence,
                    f"fee-{self.posts}",
                    "CFEE",
                    self.clock().date(),
                    fee,
                    "BTC",
                    fill.execution_id,
                ),
            )
            self.exposure += quantity - fee if request.side is OrderSide.BUY else -quantity
        self.orders = (*prior, order)
        if self.mode == "rejected":
            return SubmissionResult(
                order.evidence,
                request,
                SubmissionStatus.REJECTED,
                None,
                BrokerError(
                    order.evidence, ErrorCategory.DEFINITIVE_REJECTION, "synthetic rejection"
                ),
            )
        return replace(
            result,
            evidence=order.evidence,
            request=request,
            order=order,
            status=SubmissionStatus.ACCEPTED,
        )


def session(
    journal: ExecutionJournal, tmp_path: Path, *, probe: bool = True
) -> tuple[BtcPaperSession, LifecycleBroker, _Clock]:
    config, attempt = authority()
    clock = _Clock(NOW)
    policy = replace(attempt.evaluation.policy, maximum_entry_notional=Decimal(100))
    config = replace(config, risk_policy_id=policy.policy_id)
    broker = LifecycleBroker(BtcJournal(journal), attempt, clock=clock)
    runner = BtcPaperSession(
        plumbing_probe=probe,
        broker=broker,
        journal=journal,
        market_evidence=MarketEvidence(tmp_path / "session-market.jsonl", journal),
        run_config=config,
        risk_policy=policy,
        quantity=attempt.request.quantity,
        controls=lambda: OperatorControls(True, False, clock(), clock()),
        now=clock,
    )
    return runner, broker, clock


def events(broker: LifecycleBroker, clock: _Clock) -> Iterable[CryptoTrade | CryptoQuote]:
    for _ in range(6):
        clock.value += timedelta(seconds=1)
        ns = utc_ns(clock())
        yield replace(
            broker.attempt.revalidation.quote,
            observation_time=UtcNanoseconds(ns),
            availability_time=UtcNanoseconds(ns),
        )
        yield _trade(ns, Decimal(100), LIVE, ns)


def test_probe_net_fee_linked_exit_and_restart(journal: ExecutionJournal, tmp_path: Path) -> None:
    runner, broker, clock = session(journal, tmp_path)
    result = runner.run(lambda a, b: (), lambda duration: events(broker, clock), 60)
    assert result["stop_reason"] == "ROUND_TRIP_COMPLETE", result
    assert broker.posts == 2
    assert broker.orders[1].quantity == broker.orders[0].filled_quantity - Decimal("0.0001")
    assert result["reconciled_btc"] == "0"
    assert not result["unresolved"]
    decisions = result["decisions"]
    assert isinstance(decisions, list)
    assert all(d["mode"] == "probe" for d in decisions)
    result = runner.run(lambda a, b: (), lambda duration: events(broker, clock), 60)
    assert result["stop_reason"] == "RESTART_RECONCILIATION_ONLY"
    assert broker.posts == 2
    assert len(BtcJournal(journal).attempts) == 2


@pytest.mark.parametrize(
    "fault", ["partial", "fee_verified", "disagreement", "unavailable", "rejected", "timeout"]
)
def test_fault_never_creates_unbacked_exit(
    journal: ExecutionJournal, tmp_path: Path, fault: str
) -> None:
    runner, broker, clock = session(journal, tmp_path)
    if fault in ("rejected", "timeout"):
        broker.mode = fault
    else:
        setattr(broker, fault, fault != "fee_verified")
    result = runner.run(lambda a, b: (), lambda duration: events(broker, clock), 60)
    assert broker.posts <= 1
    assert not any(o.side is OrderSide.SELL for o in broker.orders)
    if fault == "partial":
        assert result["state"] == "entry_pending"
    elif fault == "rejected":
        assert result["stop_reason"] == "ENTRY_TERMINATED"
    else:
        assert result["unresolved"]


@pytest.mark.parametrize("mode", ["crash_before_post", "crash_after_post"])
def test_session_crash_restart_never_reposts(
    journal: ExecutionJournal, tmp_path: Path, mode: str
) -> None:
    runner, broker, clock = session(journal, tmp_path)
    broker.mode = mode
    with pytest.raises(SimulatedCrash):
        runner.run(lambda a, b: (), lambda duration: events(broker, clock), 60)
    posts = broker.posts
    journal.close()
    with ExecutionJournal.reopen(
        path=journal.path,
        owner=journal._owner,
        account_id="paper-account",
        operational_scope="scope",
    ) as reopened:
        runner.journal = reopened
        runner.store = BtcJournal(reopened)
        result = runner.run(lambda a, b: (), lambda duration: events(broker, clock), 60)
        assert result["committed_attempts"] == 1
        assert result["unresolved"]
        assert broker.posts == posts


def test_repeated_no_signal_trades_only_close_intervals(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = session(journal, tmp_path, probe=False)
    boundary = utc_ns(clock()) // HOUR_NS * HOUR_NS
    # Start at a boundary so all three captured hours are wholly live.
    clock.value -= timedelta(
        microseconds=clock.value.microsecond, seconds=clock.value.second, minutes=clock.value.minute
    )

    def historical(start: int, end: int) -> Iterable[tuple[CryptoTrade, ...]]:
        yield tuple(
            _trade(t, Decimal(100), HISTORICAL, boundary) for t in range(start, end, HOUR_NS)
        )

    def live(duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        yield _trade(boundary, Decimal(100), LIVE, boundary)
        for i in range(1, 4):
            clock.value += timedelta(hours=1)
            ns = utc_ns(clock())
            yield replace(
                broker.attempt.revalidation.quote,
                observation_time=UtcNanoseconds(ns),
                availability_time=UtcNanoseconds(ns),
            )
            assert len(journal.application_events("btc-session")) == i - 1
            yield _trade(ns, Decimal(100), LIVE, ns)

    result = runner.run(historical, live, 14400)
    assert result["intervals_evaluated"] == 3
    assert broker.posts == 0
    assert result["live_trades"] == 4 and result["live_quotes"] == 3
    decisions = result["decisions"]
    assert isinstance(decisions, list)
    assert all("COST_HURDLE" in d["reasons"] for d in decisions)


def test_strategy_enter_exit_uses_unchanged_hourly_filters(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = session(journal, tmp_path, probe=False)
    boundary = utc_ns(clock()) // HOUR_NS * HOUR_NS

    def historical(start: int, end: int) -> Iterable[tuple[CryptoTrade, ...]]:
        yield tuple(
            _trade(t, Decimal(100 + i), HISTORICAL, boundary)
            for i, t in enumerate(range(start, end, HOUR_NS))
        )

    def live(duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        yield _trade(boundary, Decimal(174), LIVE, boundary)
        for _ in range(3):
            clock.value += timedelta(hours=1)
            ns = utc_ns(clock())
            yield replace(
                broker.attempt.revalidation.quote,
                observation_time=UtcNanoseconds(ns),
                availability_time=UtcNanoseconds(ns),
            )
            yield _trade(ns, Decimal(50), LIVE, ns)

    result = runner.run(historical, live, 14400)
    assert result["stop_reason"] == "ROUND_TRIP_COMPLETE", result
    assert broker.posts == 2
    assert runner.config.slow_horizon_ns == 72 * HOUR_NS
    assert runner.config.round_trip_cost + runner.config.cost_safety_margin == Decimal("0.009")
    assert runner.store.attempts[0].evaluation.proposal.reason == "trend_confirmed"
    assert runner.store.attempts[1].evaluation.proposal.reason == "slow_trend_reversal"


def test_restart_verified_holding_can_only_exit(journal: ExecutionJournal, tmp_path: Path) -> None:
    runner, broker, clock = session(journal, tmp_path)

    def one_quote(duration: float) -> Iterable[CryptoQuote]:
        clock.value += timedelta(seconds=1)
        ns = UtcNanoseconds(utc_ns(clock()))
        yield replace(broker.attempt.revalidation.quote, observation_time=ns, availability_time=ns)

    result = runner.run(lambda a, b: (), one_quote, 60)
    assert result["state"] == "holding"
    assert broker.posts == 1
    journal.close()
    with ExecutionJournal.reopen(
        path=journal.path,
        owner=journal._owner,
        account_id="paper-account",
        operational_scope="scope",
    ) as reopened:
        runner.journal = reopened
        runner.store = BtcJournal(reopened)
        broker.store = runner.store
        result = runner.run(lambda a, b: (), lambda duration: events(broker, clock), 60)
        assert result["stop_reason"] == "ROUND_TRIP_COMPLETE", result
        assert broker.posts == 2


def test_stale_quote_and_duplicate_client_id_fail_closed(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = session(journal, tmp_path)
    quote = broker.attempt.revalidation.quote
    clock.value += timedelta(seconds=60)
    result = runner.run(lambda a, b: (), lambda duration: (quote,), 60)
    assert broker.posts == 0
    assert ["invalid_market_evidence"] in result["risk_rejections"]  # type: ignore[operator]
    result = runner.run(lambda a, b: (), lambda duration: events(broker, clock), 60)
    assert broker.posts == 2
    broker.orders += (replace(broker.orders[0], order_id="duplicate"),)
    result = runner.run(lambda a, b: (), lambda duration: events(broker, clock), 60)
    assert result["unresolved"]
    assert result["stop_reason"] == "SESSION_HALTED"
    assert broker.posts == 2
