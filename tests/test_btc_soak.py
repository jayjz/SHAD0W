"""Synthetic PAPER behavior experiments; no network or real order execution."""

import json
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from shadow.application.btc_history import HISTORICAL, LIVE, MarketEvidence
from shadow.application.btc_soak import NAMESPACE, BtcPaperSoak, parser
from shadow.domain.crypto_market import CryptoQuote, CryptoTrade, UtcNanoseconds
from shadow.execution.broker import (
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Evidence,
    OrderStatus,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
)
from shadow.execution.btc_journal import BtcJournal
from shadow.execution.btc_soak import SoakHalted, operational_state
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.execution.journal import ExecutionJournal
from shadow.execution.reconciliation import OperationalState, reconcile
from shadow.features.btc_trend import BTC, HOUR_NS
from shadow.risk.btc import utc_ns
from shadow.risk.models import OperatorControls, OrderSide, OrderTarget
from shadow.strategies.btc_trend import engineering_canary, paper_soak_canary
from tests.test_btc_journal import authority
from tests.test_crypto_paper import _Clock, _trade
from tests.test_crypto_paper import journal as journal
from tests.test_paper_reconciliation import NOW


class SoakFake:
    def __init__(self, journal: ExecutionJournal, clock: _Clock) -> None:
        self.journal, self.clock = journal, clock
        self.template = authority()[1]
        self.orders: tuple[BrokerOrder, ...] = ()
        self.fills: tuple[BrokerFill, ...] = ()
        self.exposure = Decimal(0)
        self.posts = 0
        self.pending = False
        self.uncertain = False
        self.disagreement = False
        self.stale = False
        self.foreign = False
        self.missing_fills = False
        self.before_post: Callable[[], None] = lambda: None

    def ev(self) -> Evidence:
        return Evidence(
            "synthetic",
            "other" if self.foreign else "paper-account",
            "scope",
            self.clock() - timedelta(seconds=60 if self.stale else 0),
            self.clock(),
        )

    def read_btc_account(self) -> BtcCashAccount:
        return replace(self.template.revalidation.account, evidence=self.ev())

    def read_btc_asset(self) -> BtcBrokerAsset:
        return replace(
            self.template.revalidation.asset,
            evidence=self.ev(),
            minimum_order_size=Decimal("0.000001"),
            minimum_trade_increment=Decimal("0.000001"),
        )

    def read_btc_activities(
        self, *, history_start: datetime, history_end: datetime, max_pages: int = 20
    ) -> CryptoActivityEvidence:
        return CryptoActivityEvidence(
            self.ev(),
            history_start,
            history_end,
            () if self.missing_fills else self.fills,
            (),
            (),
            True,
            False,
            (),
            None,
        )

    def read_reconciliation_snapshot(
        self,
        *,
        earliest_attempt: datetime,
        max_pages: int = 10,
        history_end: datetime | None = None,
    ) -> BrokerSnapshot:
        assert history_end is not None
        return BrokerSnapshot(
            replace(self.ev(), observation_time=history_end),
            () if not self.exposure else (BrokerPosition(self.ev(), BTC, self.exposure),),
            self.orders,
            True,
            True,
            earliest_attempt,
            history_end,
        )

    def read_btc_available(self) -> BrokerPosition:
        return BrokerPosition(
            self.ev(), BTC, self.exposure / 2 if self.disagreement else self.exposure
        )

    def submit(
        self, request: SubmitRequest, *, before_post: Callable[[], None] | None = None
    ) -> SubmissionResult:
        assert isinstance(request, BtcSubmitRequest)
        records = self.journal.application_events(NAMESPACE)
        assert any(
            isinstance(r, tuple) and r[0] == "attempt" and r[2][2] == request for r in records
        )
        assert not BtcJournal(self.journal).attempts
        assert before_post is not None
        self.before_post()
        before_post()
        self.posts += 1
        order = BrokerOrder(
            self.ev(),
            f"order-{self.posts}",
            request.client_id,
            BTC,
            request.side,
            request.quantity,
            Decimal(0) if self.pending else request.quantity,
            OrderStatus.NEW if self.pending else OrderStatus.FILLED,
            request.order_type,
            request.time_in_force,
            False,
            None,
            None,
        )
        self.orders += (order,)
        if not self.pending:
            self.fills += (
                BrokerFill(
                    self.ev(),
                    f"fill-{self.posts}",
                    order.order_id,
                    BTC,
                    request.side,
                    request.quantity,
                    Decimal(100000 if request.side is OrderSide.BUY else 101000),
                ),
            )
            # A smaller observed position is deliberately NOT accompanied by a fee.
            self.exposure += (
                request.quantity - Decimal("0.000001")
                if request.side is OrderSide.BUY
                else -request.quantity
            )
        if self.uncertain:
            raise TimeoutError("secret provider text")
        return SubmissionResult(self.ev(), request, SubmissionStatus.ACCEPTED, order, None)


def setup(
    journal: ExecutionJournal, tmp_path: Path, *, cycles: int = 1, duration: int = 86400
) -> tuple[BtcPaperSoak, SoakFake, _Clock]:
    clock = _Clock(NOW)
    broker = SoakFake(journal, clock)
    runner = BtcPaperSoak(
        broker=broker,
        journal=journal,
        market_evidence=MarketEvidence(tmp_path / "soak-market.jsonl", journal),
        policy=broker.template.evaluation.policy,
        quantity=broker.template.request.quantity,
        maximum_cycles=cycles,
        duration_seconds=duration,
        run_id="soak-run",
        code_revision="fixture",
        controls=lambda: OperatorControls(True, False, clock(), clock()),
        now=clock,
        monotonic=lambda: (clock() - NOW).total_seconds(),
    )
    return runner, broker, clock


def history(start: int, end: int) -> Iterable[tuple[CryptoTrade, ...]]:
    yield tuple(_trade(t, Decimal(100), HISTORICAL, end) for t in range(start, end, HOUR_NS))


def live(
    broker: SoakFake, clock: _Clock, prices: tuple[int, ...] = (106, 107, 90, 90, 94, 98, 90, 90)
) -> Iterable[CryptoTrade | CryptoQuote]:
    for i, price in enumerate(prices):
        if i:
            clock.value += timedelta(hours=1)
        ns = utc_ns(clock())
        yield replace(
            broker.template.revalidation.quote,
            observation_time=UtcNanoseconds(ns),
            availability_time=UtcNanoseconds(ns),
        )
        yield _trade(ns, Decimal(price), LIVE, ns)


def test_soak_round_trip_without_proof_upgrade(journal: ExecutionJournal, tmp_path: Path) -> None:
    runner, broker, clock = setup(journal, tmp_path)
    result = runner.run(history, lambda duration: live(broker, clock))
    assert result["halt_reason"] == "MAX_CYCLES", result
    assert broker.posts == 2
    assert result["completed_cycles"] == 1
    assert result["unresolved_cycles"] == 0
    assert broker.orders[1].quantity == broker.orders[0].quantity - Decimal("0.000001")
    assert result["observed_fees"] == [] and result["fee_accounting"] == "INCOMPLETE"
    assert result["gross_realized_paper_pnl_usd"] == "0.499000"
    assert not BtcJournal(journal).usable
    assert not journal.btc_events()
    assert runner.cut is not None
    activities = runner.cut.activities
    assert not activities.fee_complete_ids and activities.coverage_reference is None
    assert not activities.fees
    assert (
        reconcile(
            attempts=(),
            snapshot=runner.cut.snapshot,
            crypto_evidence=activities,
            require_crypto_evidence=True,
        ).state
        is not OperationalState.FLAT
    )
    states = [
        r[2][1]
        for r in journal.application_events(NAMESPACE)
        if isinstance(r, tuple) and r[0] == "operational_authority"
    ]
    assert "HOLDING" in states and states[-1] == "FLAT"
    assert result == runner.summary()


def test_next_cycle_requires_flat_and_no_second_buy_while_holding(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = setup(journal, tmp_path, cycles=2)
    result = runner.run(history, lambda duration: live(broker, clock))
    assert result["completed_cycles"] == 2, result
    assert [o.side for o in broker.orders] == [OrderSide.BUY, OrderSide.SELL] * 2
    assert len({o.client_id for o in broker.orders}) == 4
    assert result["halt_reason"] == "MAX_CYCLES"
    assert result["maximum_observed_exposure_btc"] == "0.000499"


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("pending", "SOURCE_ENDED"),
        ("uncertain", "SUBMISSION_HALTED"),
        ("disagreement", "POSITION_AVAILABLE_DISAGREEMENT"),
        ("stale", "STALE_OR_MISMATCHED_BROKER_EVIDENCE"),
        ("foreign", "STALE_OR_MISMATCHED_BROKER_EVIDENCE"),
        ("missing_fills", "SOURCE_ENDED"),
    ],
)
def test_faults_block_further_submission(
    journal: ExecutionJournal, tmp_path: Path, fault: str, reason: str
) -> None:
    runner, broker, clock = setup(journal, tmp_path, cycles=2)
    setattr(broker, fault, True)
    result = runner.run(history, lambda duration: live(broker, clock))
    assert result["halt_reason"] == reason, result
    assert broker.posts <= 1
    assert "secret provider" not in json.dumps(result)
    with pytest.raises(ValueError, match="fresh journal"):
        runner.run(history, lambda duration: live(broker, clock))
    assert broker.posts <= 1


@pytest.mark.parametrize("when", ["initial", "before_buy", "before_sell"])
def test_kill_switch_blocks_every_send(
    journal: ExecutionJournal, tmp_path: Path, when: str
) -> None:
    runner, broker, clock = setup(journal, tmp_path)
    killed = when == "initial"
    runner.controls = lambda: OperatorControls(True, killed, clock(), clock())

    def kill() -> None:
        nonlocal killed
        if when == "before_buy" or (when == "before_sell" and broker.posts == 1):
            killed = True

    broker.before_post = kill
    runner.run(history, lambda duration: live(broker, clock))
    assert broker.posts == (1 if when == "before_sell" else 0)


def test_duration_bound_no_forced_exit(journal: ExecutionJournal, tmp_path: Path) -> None:
    runner, broker, clock = setup(journal, tmp_path, duration=7200)
    result = runner.run(history, lambda duration: live(broker, clock))
    assert result["halt_reason"] == "DURATION_EXPIRED"
    assert broker.posts == 1 and result["unresolved_cycles"] == 1
    assert runner._expired() is not False


def test_fresh_account_cannot_adopt_existing_position(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = setup(journal, tmp_path)
    broker.exposure = Decimal("0.001")
    result = runner.run(history, lambda duration: live(broker, clock))
    assert result["halt_reason"] == "INITIAL_ACCOUNT_NOT_FLAT"
    assert broker.posts == 0


def test_canonical_profile_unchanged_and_minimal_non_degenerate_soak() -> None:
    canonical, soak = engineering_canary(), paper_soak_canary()
    assert (
        canonical.slow_horizon_ns,
        canonical.fast_horizon_ns,
        canonical.volatility_horizon_ns,
    ) == (72 * HOUR_NS, 6 * HOUR_NS, 24 * HOUR_NS)
    assert canonical.minimum_history == 73
    assert (soak.slow_horizon_ns, soak.fast_horizon_ns, soak.volatility_horizon_ns) == (
        3 * HOUR_NS,
        HOUR_NS,
        2 * HOUR_NS,
    )
    assert soak.minimum_history == 3
    assert soak.configuration_id != canonical.configuration_id
    assert soak.round_trip_cost == canonical.round_trip_cost


def test_soak_cannot_target_live() -> None:
    request = authority()[1].request
    with pytest.raises(ValueError, match="PAPER"):
        replace(request, target=cast(OrderTarget, "live"))
    with pytest.raises(SystemExit):
        parser().parse_args(["--paper-soak", "--paper-endpoint", "https://api.alpaca.markets"])


def test_duplicate_client_and_stale_available_halt(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = setup(journal, tmp_path, duration=7200)
    runner.run(history, lambda duration: live(broker, clock))
    assert runner.cut is not None and runner.cut.available is not None

    def state() -> None:
        assert runner.cut is not None
        operational_state(
            runner.cut,
            runner.attempts,
            binding=("paper-account", "scope"),
            start=runner.started,
            now_ns=utc_ns(clock()),
            maximum_age_ns=30_000_000_000,
        )

    runner.attempts += runner.attempts
    with pytest.raises(SoakHalted, match="DUPLICATE_CLIENT"):
        state()
    runner.attempts = runner.attempts[:1]
    runner.cut = replace(
        runner.cut,
        available=replace(
            runner.cut.available,
            evidence=replace(
                runner.cut.available.evidence, observation_time=clock() - timedelta(seconds=31)
            ),
        ),
    )
    with pytest.raises(SoakHalted, match="AVAILABLE_DISAGREEMENT"):
        state()


def test_no_reuse_of_proof_journal(journal: ExecutionJournal, tmp_path: Path) -> None:
    config, _ = authority()
    BtcJournal(journal).configure(config)
    before = journal.btc_events()
    with pytest.raises(ValueError, match="cannot adopt"):
        setup(journal, tmp_path)
    assert journal.btc_events() == before


def test_stale_quote_never_authorizes_buy(journal: ExecutionJournal, tmp_path: Path) -> None:
    runner, broker, clock = setup(journal, tmp_path)

    def source(duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        for event in live(broker, clock):
            if isinstance(event, CryptoQuote):
                event = replace(
                    event,
                    observation_time=UtcNanoseconds(event.observation_time.value - 31_000_000_000),
                )
            yield event

    result = runner.run(history, source)
    assert broker.posts == 0
    assert result["abstentions_by_reason"] == {
        "INVALID_MARKET_EVIDENCE": 4,
        "TREND_POSITIVE": 3,
        "MOMENTUM_POSITIVE": 3,
        "VOLATILITY_WITHIN_LIMIT": 3,
        "COST_HURDLE_PASSED": 3,
    }


def test_heartbeat_enforces_kill_and_polls_without_market_events(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = setup(journal, tmp_path)
    killed = False
    runner.controls = lambda: OperatorControls(True, killed, clock(), clock())

    def source(duration: float) -> Iterable[None]:
        nonlocal killed
        clock.value += timedelta(seconds=5)
        yield None
        killed = True
        clock.value += timedelta(seconds=5)
        yield None

    result = runner.run(history, source)
    assert result["halt_reason"] == "OPERATOR_DISABLED" and broker.posts == 0
    assert (
        len(
            [
                r
                for r in journal.application_events(NAMESPACE)
                if isinstance(r, tuple) and r[0] == "broker"
            ]
        )
        == 2
    )


def test_duration_includes_history_and_monotonic_expiry(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, _ = setup(journal, tmp_path, duration=60)
    elapsed = 0.0
    runner.monotonic = lambda: elapsed

    def slow_history(start: int, end: int) -> Iterable[tuple[CryptoTrade, ...]]:
        nonlocal elapsed
        elapsed = 60.0
        yield from history(start, end)

    def no_live(duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        pytest.fail("expired history budget must not open live source")

    result = runner.run(slow_history, no_live)
    assert result["halt_reason"] == "DURATION_EXPIRED" and broker.posts == 0


def test_evidence_and_summary_reproduce_across_independent_runs(tmp_path: Path) -> None:
    from shadow.execution.journal_codec import canonical_digest
    from shadow.execution.ownership import AccountOwner

    results = []
    decisions = []
    for i in range(2):
        root = tmp_path / str(i)
        root.mkdir()
        with AccountOwner.acquire(ownership_directory=root, account_id="paper-account") as owner:
            with ExecutionJournal.create(
                path=root / "journal.sqlite",
                owner=owner,
                account_id="paper-account",
                operational_scope="scope",
                created_at=NOW,
            ) as store:
                runner, broker, clock = setup(store, root, cycles=2)

                def source(
                    duration: float, broker: SoakFake = broker, clock: _Clock = clock
                ) -> Iterable[CryptoTrade | CryptoQuote]:
                    return live(broker, clock)

                results.append(runner.run(history, source))
                decisions.append(
                    canonical_digest(
                        tuple(
                            r
                            for r in store.application_events(NAMESPACE)
                            if isinstance(r, tuple)
                            and r[0]
                            in (
                                "decision",
                                "risk",
                                "submission",
                                "operational_authority",
                                "interval",
                            )
                        )
                    )
                )
                clock.value += timedelta(hours=1)
                assert runner.summary() == results[-1]
    assert results[0] == results[1]
    assert decisions[0] == decisions[1]


@pytest.mark.parametrize(
    "bound,value", [("cycles", 0), ("cycles", 25), ("duration", 0), ("duration", 86401)]
)
def test_invalid_bounds_rejected(
    journal: ExecutionJournal, tmp_path: Path, bound: str, value: int
) -> None:
    with pytest.raises(ValueError):
        setup(journal, tmp_path, **{bound: value})


def test_delayed_fills_continue_observing_before_exit(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    runner, broker, clock = setup(journal, tmp_path)
    broker.missing_fills = True

    def source(duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        for event in live(broker, clock):
            if clock() >= NOW + timedelta(hours=2):
                broker.missing_fills = False
            yield event

    result = runner.run(history, source)
    assert result["completed_cycles"] == 1
    states = [
        r[2][1]
        for r in journal.application_events(NAMESPACE)
        if isinstance(r, tuple) and r[0] == "operational_authority"
    ]
    assert "WAITING_FILLS" in states and states[-1] == "FLAT"


def test_fill_rewrite_halts(journal: ExecutionJournal, tmp_path: Path) -> None:
    runner, broker, clock = setup(journal, tmp_path)

    def source(duration: float) -> Iterable[CryptoTrade | CryptoQuote]:
        for event in live(broker, clock):
            if broker.fills:
                broker.fills = (replace(broker.fills[0], price=Decimal(1)),)
            yield event

    result = runner.run(history, source)
    assert result["halt_reason"] == "FILL_HISTORY_CHANGED"
    assert broker.posts == 1


def test_heartbeat_driver_closes_socket_on_silent_timeout() -> None:
    from shadow.adapters.alpaca.crypto_stream import CryptoDataCredentials
    from shadow.application.btc_soak_stream import heartbeat_events
    from shadow.application.crypto_paper import _direct_live
    from tests.test_crypto_paper import (
        _direct_connect,
        _DirectContext,
        _DirectSocket,
        _subscription_frame,
    )

    socket = _DirectSocket(
        [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            _subscription_frame(),
        ]
    )
    context = _DirectContext(socket)
    source = _direct_live(
        CryptoDataCredentials("test-key", "test-secret"), 0.05, connect=_direct_connect(context)
    )
    events = heartbeat_events(source)
    assert next(events) is None
    events.close()
    assert socket.closed and context.exited


def test_multiple_positions_halt(journal: ExecutionJournal, tmp_path: Path) -> None:
    from shadow.domain.market import Instrument

    runner, broker, clock = setup(journal, tmp_path, duration=7200)
    runner.run(history, lambda duration: live(broker, clock))
    assert runner.cut is not None
    cut = replace(
        runner.cut,
        snapshot=replace(
            runner.cut.snapshot,
            positions=(
                *runner.cut.snapshot.positions,
                BrokerPosition(broker.ev(), Instrument("ETH/USD"), Decimal(1)),
            ),
        ),
    )
    with pytest.raises(SoakHalted, match="FOREIGN_OR_MULTIPLE"):
        operational_state(
            cut,
            runner.attempts,
            binding=("paper-account", "scope"),
            start=runner.started,
            now_ns=utc_ns(clock()),
            maximum_age_ns=30_000_000_000,
        )


def test_forged_strategy_proposal_cannot_bypass_risk(
    journal: ExecutionJournal, tmp_path: Path
) -> None:
    from shadow.risk.btc_soak import evaluate_soak_risk

    runner, broker, clock = setup(journal, tmp_path, duration=7200)
    runner.run(history, lambda duration: live(broker, clock))
    assert runner.cut is not None and runner.quote is not None
    # Use the last recorded risk proposal, which was valid on its original cut.
    risk = next(
        r[2]
        for r in journal.application_events(NAMESPACE)
        if isinstance(r, tuple) and r[0] == "risk"
    )
    from shadow.strategies.btc_trend import BtcProposal

    original = risk[2]
    assert isinstance(original, BtcProposal)
    reasons = evaluate_soak_risk(
        policy=runner.policy,
        proposal=replace(original, reason="forged"),
        request=runner.attempts[0].request,
        intervals=runner.evidence.history.intervals,
        quote=runner.quote,
        cut=runner.cut,
        state=runner.state,
        controls=runner.controls(),
        now_ns=utc_ns(clock()),
        attempts=runner.attempts,
        start=runner.started,
    )
    assert "INVALID_STRATEGY_EVIDENCE" in reasons and "ENTRY_REQUIRES_FLAT" in reasons
