"""No external transport: committed BTC attempts are one-use across every crash seam."""

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from shadow.adapters.alpaca.paper_broker import AlpacaPaperBroker
from shadow.domain.crypto_market import UtcNanoseconds
from shadow.execution.broker import (
    BrokerFill,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    OrderStatus,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
)
from shadow.execution.btc_authority import BtcAttempt
from shadow.execution.btc_dispatch import BtcBroker, BtcDispatchAuthority, BtcDispatcher
from shadow.execution.btc_journal import BtcJournal
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount
from shadow.execution.crypto_accounting import CryptoActivityEvidence, CryptoFeeActivity
from shadow.execution.dispatch import DispatchHalted
from shadow.execution.journal import ExecutionJournal
from shadow.execution.reconciliation import OperationalState, Reconciliation
from shadow.risk.btc_models import BtcLifecycleAuthority
from tests.test_btc_journal import authority
from tests.test_btc_journal import journal as journal
from tests.test_paper_reconciliation import NOW


class SimulatedCrash(BaseException):
    pass


class FakeBtcBroker:
    def __init__(self, store: BtcJournal, attempt: BtcAttempt) -> None:
        self.store = store
        self.attempt = attempt
        self.orders: tuple[BrokerOrder, ...] = ()
        self.posts = 0
        self.mode = "accepted"
        self.before_send: Callable[[], None] = lambda: None

    def read_btc_account(self) -> BtcCashAccount:
        return self.attempt.revalidation.account

    def read_btc_asset(self) -> BtcBrokerAsset:
        return self.attempt.revalidation.asset

    def read_btc_activities(
        self, *, history_start: datetime, history_end: datetime, max_pages: int = 20
    ) -> CryptoActivityEvidence:
        return replace(
            self.attempt.revalidation.activities,
            history_start=history_start,
            history_end=history_end,
            evidence=replace(
                self.attempt.revalidation.activities.evidence,
                observation_time=history_end,
                availability_time=history_end,
            ),
        )

    def read_reconciliation_snapshot(
        self,
        *,
        earliest_attempt: datetime,
        max_pages: int = 10,
        history_end: datetime | None = None,
    ) -> BrokerSnapshot:
        assert history_end is not None
        return replace(
            self.attempt.revalidation.snapshot,
            orders=self.orders,
            history_start=earliest_attempt,
            history_end=history_end,
            evidence=replace(
                self.attempt.revalidation.snapshot.evidence,
                observation_time=history_end,
                availability_time=history_end,
            ),
        )

    def submit(
        self, request: SubmitRequest, *, before_post: Callable[[], None] | None = None
    ) -> SubmissionResult:
        assert BtcJournal(self.store.journal).attempts[-1].request == request
        if self.mode == "crash_before_post":
            raise SimulatedCrash
        self.before_send()
        assert before_post is not None
        before_post()
        self.posts += 1
        ev = self.attempt.revalidation.snapshot.evidence
        order = BrokerOrder(
            ev,
            "original",
            request.client_id,
            request.instrument,
            request.side,
            request.quantity,
            Decimal(0),
            OrderStatus.NEW,
            request.order_type,
            request.time_in_force,
            False,
            None,
            None,
        )
        self.orders = (order,)
        if self.mode == "timeout":
            raise TimeoutError("accepted, response lost")
        if self.mode == "disconnect":
            raise ConnectionError("response disconnected")
        if self.mode == "crash_after_post":
            raise SimulatedCrash
        if self.mode == "malformed":
            return cast(SubmissionResult, object())
        return SubmissionResult(ev, self.attempt.request, SubmissionStatus.ACCEPTED, order, None)


def setup(journal: ExecutionJournal) -> tuple[BtcDispatcher, FakeBtcBroker, BtcAttempt]:
    config, attempt = authority()
    store = BtcJournal(journal)
    store.configure(config)
    broker = FakeBtcBroker(store, attempt)
    dispatcher = BtcDispatcher(
        broker=broker,
        journal=store,
        now=lambda: NOW + timedelta(microseconds=1),
        controls=lambda: attempt.revalidation.controls,
        market=lambda: (attempt.revalidation.intervals, attempt.revalidation.quote),
    )
    assert dispatcher.recover().state is OperationalState.FLAT
    return dispatcher, broker, attempt


def execute(dispatcher: BtcDispatcher, attempt: BtcAttempt) -> SubmissionResult | Reconciliation:
    return dispatcher.execute(
        attempt.evaluation,
        dispatch_deadline=attempt.dispatch_deadline,
        expected_revision=dispatcher.store.revision,
    )


def initial_experiment_setup(
    journal: ExecutionJournal,
) -> tuple[BtcDispatcher, FakeBtcBroker, BtcAttempt]:
    """The sole initial-entry seam: strict proof unresolved only for fee finality."""
    config, attempt = authority()
    store = BtcJournal(journal)
    store.configure(config)
    activities = replace(
        attempt.revalidation.activities, history_verified=False, coverage_reference=None
    )
    broker = FakeBtcBroker(
        store, replace(attempt, revalidation=replace(attempt.revalidation, activities=activities))
    )
    dispatcher = BtcDispatcher(
        broker=broker,
        journal=store,
        now=lambda: NOW + timedelta(microseconds=1),
        controls=lambda: broker.attempt.revalidation.controls,
        market=lambda: (broker.attempt.revalidation.intervals, broker.attempt.revalidation.quote),
    )
    assert dispatcher.recover().state is OperationalState.UNRESOLVED
    return dispatcher, broker, attempt


def test_pristine_experiment_reaches_guarded_submit_and_is_spent(journal: ExecutionJournal) -> None:
    from shadow.execution.btc_classification import (
        AuthorityClassification,
        classify_btc_authority,
        proof_reconcile,
    )
    from shadow.risk.btc import evaluate_btc_risk, utc_ns

    config, attempt = authority()
    store = BtcJournal(journal)
    store.configure(config)
    activities = replace(
        attempt.revalidation.activities, history_verified=False, coverage_reference=None
    )
    broker = FakeBtcBroker(
        store, replace(attempt, revalidation=replace(attempt.revalidation, activities=activities))
    )
    dispatcher = BtcDispatcher(
        broker=broker,
        journal=store,
        now=lambda: NOW + timedelta(microseconds=1),
        controls=lambda: broker.attempt.revalidation.controls,
        market=lambda: (broker.attempt.revalidation.intervals, broker.attempt.revalidation.quote),
    )
    assert dispatcher.recover().state is OperationalState.UNRESOLVED
    assert not store.usable and store.execution_authority_ready
    strict = proof_reconcile(
        attempts=(), snapshot=broker.attempt.revalidation.snapshot, activities=activities
    )
    classified = classify_btc_authority(
        account=broker.attempt.revalidation.account,
        asset=broker.attempt.revalidation.asset,
        snapshot=broker.attempt.revalidation.snapshot,
        activities=activities,
        strict_lifecycle=strict,
        account_binding=(attempt.request.account_id, attempt.request.operational_scope),
        journal_usable=store.execution_authority_ready,
        risk_fresh=True,
        unresolved_prior_intent=False,
    )
    assert classified.classification is AuthorityClassification.EXPERIMENT_READY
    assert classified.proof_status is AuthorityClassification.BLOCKED
    risk = evaluate_btc_risk(
        policy=attempt.evaluation.policy,
        config=attempt.evaluation.config,
        proposal=attempt.evaluation.proposal,
        request=attempt.evaluation.request,
        intervals=broker.attempt.revalidation.intervals,
        quote=broker.attempt.revalidation.quote,
        account=broker.attempt.revalidation.account,
        asset=broker.attempt.revalidation.asset,
        snapshot=broker.attempt.revalidation.snapshot,
        attempts=(),
        controls=broker.attempt.revalidation.controls,
        now_ns=utc_ns(NOW + timedelta(microseconds=1)),
        crypto_evidence=activities,
        require_crypto_evidence=True,
        lifecycle_authority=BtcLifecycleAuthority.INITIAL_EXPERIMENT,
    )
    assert risk.authorized
    broker.attempt = replace(broker.attempt, evaluation=risk)
    result = dispatcher.execute(
        risk,
        dispatch_deadline=attempt.dispatch_deadline,
        expected_revision=store.revision,
        authority=BtcDispatchAuthority.INITIAL_EXPERIMENT,
    )
    assert isinstance(result, SubmissionResult) and broker.posts == 1
    assert store.attempts[0].submission == result
    assert not store.usable and not store.execution_authority_ready
    second = dispatcher.execute(
        risk,
        dispatch_deadline=attempt.dispatch_deadline,
        expected_revision=store.revision,
        authority=BtcDispatchAuthority.INITIAL_EXPERIMENT,
    )
    assert isinstance(second, Reconciliation) and second.state is OperationalState.UNRESOLVED
    assert broker.posts == 1
    journal.close()
    with ExecutionJournal.reopen(
        path=journal.path,
        owner=journal._owner,
        account_id="paper-account",
        operational_scope="scope",
    ) as reopened:
        restarted_store = BtcJournal(reopened)
        restarted = BtcDispatcher(
            broker=broker,
            journal=restarted_store,
            now=lambda: NOW + timedelta(microseconds=1),
            controls=dispatcher.controls,
            market=dispatcher.market,
        )
        state = restarted.execute(
            risk,
            dispatch_deadline=attempt.dispatch_deadline,
            expected_revision=restarted_store.revision,
            authority=BtcDispatchAuthority.INITIAL_EXPERIMENT,
        )
        assert isinstance(state, Reconciliation)
        assert broker.posts == 1


@pytest.mark.parametrize(
    "failure",
    [
        "prior_committed_attempt",
        "broker_position",
        "broker_order",
        "execution_activity",
        "fee_activity",
        "unsupported_activity",
        "activity_query_incomplete",
        "stale_market_evidence",
        "stale_risk_authorization",
        "kill_switch_active",
        "trading_disabled",
        "account_scope_mismatch",
        "journal_revision_conflict",
        "incomplete_broker_snapshot",
    ],
)
def test_initial_experiment_negative_matrix_never_posts(
    journal: ExecutionJournal, failure: str
) -> None:
    dispatcher, broker, attempt = initial_experiment_setup(journal)
    evaluation = broker.attempt.evaluation
    expected_revision = dispatcher.store.revision
    if failure == "prior_committed_attempt":
        dispatcher.store.commit(attempt, expected_revision=expected_revision)
        expected_revision = dispatcher.store.revision
    elif failure == "broker_position":
        snapshot = broker.attempt.revalidation.snapshot
        broker.attempt = replace(
            broker.attempt,
            revalidation=replace(
                broker.attempt.revalidation,
                snapshot=replace(
                    snapshot,
                    positions=(
                        BrokerPosition(
                            snapshot.evidence, attempt.request.instrument, Decimal("0.01")
                        ),
                    ),
                ),
            ),
        )
    elif failure == "broker_order":
        ev = broker.attempt.revalidation.snapshot.evidence
        broker.orders = (
            BrokerOrder(
                ev,
                "existing",
                "existing-client",
                attempt.request.instrument,
                attempt.request.side,
                attempt.request.quantity,
                Decimal(0),
                OrderStatus.NEW,
                attempt.request.order_type,
                attempt.request.time_in_force,
                False,
                None,
                None,
            ),
        )
    elif failure == "execution_activity":
        activities = broker.attempt.revalidation.activities
        broker.attempt = replace(
            broker.attempt,
            revalidation=replace(
                broker.attempt.revalidation,
                activities=replace(
                    activities,
                    executions=(
                        BrokerFill(
                            activities.evidence,
                            "prior-execution",
                            "prior-order",
                            attempt.request.instrument,
                            attempt.request.side,
                            attempt.request.quantity,
                            Decimal("100"),
                        ),
                    ),
                ),
            ),
        )
    elif failure == "fee_activity":
        activities = broker.attempt.revalidation.activities
        broker.attempt = replace(
            broker.attempt,
            revalidation=replace(
                broker.attempt.revalidation,
                activities=replace(
                    activities,
                    fees=(
                        CryptoFeeActivity(
                            activities.evidence,
                            "prior-fee",
                            "FEE",
                            date(2025, 1, 1),
                            Decimal("0.001"),
                            "USD",
                            None,
                        ),
                    ),
                ),
            ),
        )
    elif failure == "unsupported_activity":
        activities = broker.attempt.revalidation.activities
        broker.attempt = replace(
            broker.attempt,
            revalidation=replace(
                broker.attempt.revalidation, activities=replace(activities, unsupported=("x",))
            ),
        )
    elif failure == "activity_query_incomplete":
        activities = broker.attempt.revalidation.activities
        broker.attempt = replace(
            broker.attempt,
            revalidation=replace(
                broker.attempt.revalidation, activities=replace(activities, query_exhausted=False)
            ),
        )
    elif failure == "stale_market_evidence":
        dispatcher.market = lambda: (
            broker.attempt.revalidation.intervals,
            replace(
                broker.attempt.revalidation.quote,
                observation_time=UtcNanoseconds(
                    broker.attempt.revalidation.quote.observation_time.value - 31_000_000_000
                ),
                availability_time=UtcNanoseconds(
                    broker.attempt.revalidation.quote.availability_time.value - 31_000_000_000
                ),
            ),
        )
    elif failure == "stale_risk_authorization":
        evaluation = replace(evaluation, evaluated_ns=evaluation.evaluated_ns - 31_000_000_000)
    elif failure == "kill_switch_active":
        dispatcher.controls = lambda: replace(
            broker.attempt.revalidation.controls, kill_switch_active=True
        )
    elif failure == "trading_disabled":
        dispatcher.controls = lambda: replace(
            broker.attempt.revalidation.controls, trading_enabled=False
        )
    elif failure == "account_scope_mismatch":
        account = broker.attempt.revalidation.account
        broker.attempt = replace(
            broker.attempt,
            revalidation=replace(
                broker.attempt.revalidation,
                account=replace(
                    account,
                    evidence=replace(account.evidence, operational_scope="other-scope"),
                ),
            ),
        )
    elif failure == "journal_revision_conflict":
        expected_revision -= 1
    else:
        snapshot = broker.attempt.revalidation.snapshot
        broker.attempt = replace(
            broker.attempt,
            revalidation=replace(
                broker.attempt.revalidation, snapshot=replace(snapshot, orders_complete=False)
            ),
        )
    if failure == "prior_committed_attempt":
        result = dispatcher.execute(
            evaluation,
            dispatch_deadline=attempt.dispatch_deadline,
            expected_revision=expected_revision,
            authority=BtcDispatchAuthority.INITIAL_EXPERIMENT,
        )
        assert isinstance(result, Reconciliation)
    else:
        with pytest.raises(DispatchHalted):
            dispatcher.execute(
                evaluation,
                dispatch_deadline=attempt.dispatch_deadline,
                expected_revision=expected_revision,
                authority=BtcDispatchAuthority.INITIAL_EXPERIMENT,
            )
    assert broker.posts == 0


def test_authorized_commit_precedes_exactly_one_post(journal: ExecutionJournal) -> None:
    dispatcher, broker, attempt = setup(journal)
    result = execute(dispatcher, attempt)
    assert isinstance(result, SubmissionResult) and result.status is SubmissionStatus.ACCEPTED
    assert broker.posts == 1
    assert dispatcher.store.attempts[0].submission == result
    assert isinstance(execute(dispatcher, attempt), Reconciliation)
    assert broker.posts == 1


@pytest.mark.parametrize(
    "mode", ["timeout", "disconnect", "malformed", "crash_before_post", "crash_after_post"]
)
@pytest.mark.parametrize("experimental", [False, True])
def test_uncertainty_restart_never_reposts(
    journal: ExecutionJournal, mode: str, experimental: bool
) -> None:
    dispatcher, broker, attempt = (
        initial_experiment_setup(journal) if experimental else setup(journal)
    )

    def dispatch_once(target: BtcDispatcher) -> SubmissionResult | Reconciliation:
        return target.execute(
            broker.attempt.evaluation,
            dispatch_deadline=attempt.dispatch_deadline,
            expected_revision=target.store.revision,
            authority=(
                BtcDispatchAuthority.INITIAL_EXPERIMENT
                if experimental
                else BtcDispatchAuthority.PROOF
            ),
        )

    broker.mode = mode
    if mode.startswith("crash"):
        with pytest.raises(SimulatedCrash):
            dispatch_once(dispatcher)
    else:
        result = dispatch_once(dispatcher)
        assert isinstance(result, SubmissionResult) and result.status is SubmissionStatus.UNCERTAIN
        assert dispatcher.store.halted
    count = broker.posts
    journal.close()
    with ExecutionJournal.reopen(
        path=journal.path,
        owner=journal._owner,
        account_id="paper-account",
        operational_scope="scope",
    ) as reopened:
        restarted = BtcDispatcher(
            broker=broker,
            journal=BtcJournal(reopened),
            now=lambda: NOW + timedelta(microseconds=1),
            controls=dispatcher.controls,
            market=dispatcher.market,
        )
        state = dispatch_once(restarted)
        assert isinstance(state, Reconciliation)
        if not experimental:
            assert state.state is (
                OperationalState.UNRESOLVED
                if mode == "crash_before_post"
                else OperationalState.ENTRY_PENDING
            )
        assert broker.posts == count
        assert len(restarted.store.attempts) == 1


@pytest.mark.parametrize(
    "failure", ["kill", "expired", "revision", "startup", "market", "cash", "source_conflict"]
)
def test_preflight_failures_never_post(journal: ExecutionJournal, failure: str) -> None:
    dispatcher, broker, attempt = setup(journal)
    if failure == "kill":
        dispatcher.controls = lambda: replace(
            attempt.revalidation.controls, kill_switch_active=True
        )
    elif failure == "expired":
        attempt = replace(
            attempt,
            evaluation=replace(
                attempt.evaluation, evaluated_ns=attempt.evaluation.evaluated_ns - 31_000_000_000
            ),
        )
    elif failure == "startup":
        dispatcher._recovered = False
    elif failure == "market":
        dispatcher.market = lambda: ((), attempt.revalidation.quote)
    elif failure == "cash":
        broker.attempt = replace(
            attempt,
            revalidation=replace(
                attempt.revalidation,
                account=replace(attempt.revalidation.account, available_cash=Decimal(0)),
            ),
        )
    elif failure == "source_conflict":
        execute(dispatcher, attempt)
        attempt = replace(
            attempt,
            evaluation=replace(
                attempt.evaluation, request=replace(attempt.request, quantity=Decimal("0.0013"))
            ),
        )
    with pytest.raises(DispatchHalted):
        dispatcher.execute(
            attempt.evaluation,
            dispatch_deadline=attempt.dispatch_deadline,
            expected_revision=dispatcher.store.revision - (1 if failure == "revision" else 0),
        )
    assert broker.posts == (1 if failure == "source_conflict" else 0)


@pytest.mark.parametrize(
    "failure", ["kill", "deadline", "revision", "ownership", "monotonic", "legacy_halt"]
)
def test_final_guard_spends_attempt_without_post(journal: ExecutionJournal, failure: str) -> None:
    dispatcher, broker, attempt = setup(journal)

    def intervene() -> None:
        if failure == "kill":
            dispatcher.controls = lambda: replace(
                attempt.revalidation.controls, kill_switch_active=True
            )
        elif failure == "deadline":
            dispatcher.now = lambda: attempt.dispatch_deadline
        elif failure == "legacy_halt":
            journal.record_halt(occurred_at=NOW, reason="account-wide halt")
        elif failure == "monotonic":
            dispatcher.monotonic = lambda: 1e20
        elif failure == "revision":
            dispatcher.store.halt(NOW, "concurrent halt")
        else:
            journal.close()

    broker.before_send = intervene
    with pytest.raises((DispatchHalted, RuntimeError)):
        execute(dispatcher, attempt)
    assert broker.posts == 0
    if failure != "ownership":
        assert len(BtcJournal(journal).attempts) == 1
        assert BtcJournal(journal).halted


def test_failed_result_persistence_does_not_restore_capacity(
    journal: ExecutionJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, broker, attempt = setup(journal)

    def crash(_result: SubmissionResult) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(dispatcher.store, "persist_submission", crash)
    with pytest.raises(SimulatedCrash):
        execute(dispatcher, attempt)
    assert broker.posts == 1
    assert BtcJournal(journal).attempts[0].submission is None
    execute(dispatcher, attempt)
    assert broker.posts == 1


def test_real_adapter_conforms_to_guarded_protocol() -> None:
    # Static conformance only: do not instantiate a network transport.
    def conforms(broker: AlpacaPaperBroker) -> BtcBroker:
        return broker

    assert callable(conforms)


def test_adapter_final_guard_runs_after_asset_read_and_before_transport() -> None:
    from shadow.adapters.alpaca.paper_broker import PaperCredentials
    from tests.test_alpaca_paper_broker import RecordingTransport, response
    from tests.test_btc_execution import asset_payload, btc_request

    transport = RecordingTransport([response(200, asset_payload())])
    broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="scope",
        transport=transport,
    )

    def stop() -> None:
        assert len(transport.calls) == 1 and transport.calls[0][0] == "GET"
        raise DispatchHalted("kill after asset read")

    with pytest.raises(DispatchHalted):
        broker.submit(btc_request(), before_post=stop)
    assert len(transport.calls) == 1


def test_malformed_alpaca_post_is_uncertain() -> None:
    from shadow.adapters.alpaca.paper_broker import PaperCredentials
    from tests.test_alpaca_paper_broker import RecordingTransport, response
    from tests.test_btc_execution import asset_payload, btc_request

    transport = RecordingTransport([response(200, asset_payload()), response(201, {})])
    broker = AlpacaPaperBroker(
        credentials=PaperCredentials("key", "secret"),
        account_id="paper-account",
        operational_scope="scope",
        transport=transport,
    )
    result = broker.submit(btc_request(), before_post=lambda: None)
    assert result.status is SubmissionStatus.UNCERTAIN
    assert sum(call[0] == "POST" for call in transport.calls) == 1


def test_authoritative_recovery_allows_distinct_opportunity_but_never_reuses_original(
    journal: ExecutionJournal,
) -> None:
    from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
    from shadow.domain.crypto_market import UtcNanoseconds
    from shadow.execution.btc_authority import btc_intent_identity
    from shadow.risk.btc import utc_ns

    dispatcher, broker, first = setup(journal)
    broker.mode = "timeout"
    execute(dispatcher, first)
    broker.orders = (replace(broker.orders[0], status=OrderStatus.REJECTED),)
    assert dispatcher.recover().state is OperationalState.FLAT
    assert dispatcher.store.halted
    dispatcher.store.resume(expected_revision=dispatcher.store.revision)
    later = NOW + timedelta(hours=1)
    shift = 3_600_000_000_000
    old = first.revalidation
    intervals = tuple(
        replace(
            row,
            start_ns=row.start_ns + shift,
            end_ns=row.end_ns + shift,
            available_ns=row.available_ns + shift,
        )
        for row in old.intervals
    )
    ev = replace(old.snapshot.evidence, observation_time=later, availability_time=later)
    evidence = replace(
        old,
        intervals=intervals,
        quote=replace(
            old.quote,
            observation_time=UtcNanoseconds(utc_ns(later) + 1),
            availability_time=UtcNanoseconds(utc_ns(later) + 1),
        ),
        account=replace(old.account, evidence=ev),
        asset=replace(old.asset, evidence=ev),
        snapshot=replace(old.snapshot, evidence=ev, history_end=later),
        controls=replace(old.controls, observation_time=later, availability_time=later),
    )
    features = first.evaluation.config.features(intervals, utc_ns(later) + 1)
    assert features is not None
    evaluation = replace(
        first.evaluation,
        evaluated_ns=utc_ns(later) + 1,
        proposal=replace(first.evaluation.proposal, features=features),
    )
    config = dispatcher.store.config
    assert config is not None
    intent = btc_intent_identity(evaluation, config.source_market_id)
    identity = derive_paper_client_order_identity(
        stable_account_binding="paper-account", operational_scope="scope", intent_identity=intent
    )
    evaluation = replace(
        evaluation, request=replace(evaluation.request, client_id=identity.client_order_id)
    )
    second = replace(
        first,
        intent_identity=intent,
        client_order_full_digest=identity.full_digest,
        evaluation=evaluation,
        revalidation=evidence,
        committed_at=later,
        dispatch_deadline=later + timedelta(seconds=5),
    )
    broker.attempt = second
    broker.mode = "accepted"
    # A new dispatcher is not ready merely because an earlier process resumed.
    restarted = BtcDispatcher(
        broker=broker,
        journal=BtcJournal(journal),
        now=lambda: later + timedelta(microseconds=1),
        controls=lambda: evidence.controls,
        market=lambda: (evidence.intervals, evidence.quote),
    )
    assert restarted.recover().state is OperationalState.FLAT
    result = execute(restarted, second)
    assert isinstance(result, SubmissionResult) and result.status is SubmissionStatus.ACCEPTED
    assert broker.posts == 2 and len(restarted.store.attempts) == 2
    execute(restarted, first)
    assert broker.posts == 2


def test_unproven_provider_coverage_cannot_activate(
    journal: ExecutionJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher, broker, attempt = setup(journal)
    original = broker.read_btc_activities

    def unproven(
        *, history_start: datetime, history_end: datetime, max_pages: int = 20
    ) -> CryptoActivityEvidence:
        return replace(
            original(history_start=history_start, history_end=history_end, max_pages=max_pages),
            history_verified=False,
            coverage_reference=None,
        )

    monkeypatch.setattr(broker, "read_btc_activities", unproven)
    assert dispatcher.recover().state is OperationalState.UNRESOLVED
    with pytest.raises(DispatchHalted):
        execute(dispatcher, attempt)
    assert broker.posts == 0 and not dispatcher.store.attempts
