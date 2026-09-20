"""P5A committed-attempt boundary: recovery cannot call submit a second time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
from shadow.domain import Instrument
from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerError,
    BrokerOrder,
    BrokerSnapshot,
    Eligibility,
    ErrorCategory,
    Evidence,
    OrderStatus,
    SessionState,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
    TradeUpdate,
)
from shadow.execution.dispatch import MAX_RISK_DECISION_AGE, CanaryDispatcher
from shadow.execution.journal import ExecutionJournal
from shadow.execution.ownership import AccountOwner
from shadow.risk.models import (
    OrderIntent,
    OrderSide,
    OrderTarget,
    OrderType,
    RiskDecision,
    RiskDecisionStatus,
    TimeInForce,
)
from tests.test_execution_journal_foundation import binding

NOW = datetime(2026, 9, 18, 15, tzinfo=UTC)
SPY = Instrument("SPY")


class OneShotBroker:
    def __init__(
        self,
        request: SubmitRequest,
        *,
        submission_status: SubmissionStatus = SubmissionStatus.ACCEPTED,
    ) -> None:
        self.evidence = Evidence("broker", "paper-account", "paper-scope", NOW, NOW)
        self.order = BrokerOrder(
            self.evidence,
            "broker-order",
            request.client_id,
            SPY,
            OrderSide.BUY,
            Decimal(1),
            Decimal(0),
            OrderStatus.NEW,
            OrderType.MARKET,
            TimeInForce.DAY,
            False,
            None,
            None,
        )
        self.request = request
        self.submission_status = submission_status
        self.post_count = 0
        self.snapshot_count = 0

    def read_account(self) -> BrokerAccount:
        return BrokerAccount(
            self.evidence, OrderTarget.PAPER, Eligibility.ELIGIBLE, Decimal(1000), "USD"
        )

    def read_clock(self) -> BrokerClock:
        return BrokerClock(
            self.evidence,
            SessionState.REGULAR,
            NOW.date(),
            NOW - timedelta(hours=1),
            NOW + timedelta(hours=1),
        )

    def read_asset(self, instrument: Instrument) -> BrokerAsset:
        assert instrument == SPY
        return BrokerAsset(self.evidence, SPY, Eligibility.ELIGIBLE, Eligibility.ELIGIBLE)

    def read_snapshot(self) -> BrokerSnapshot:
        self.snapshot_count += 1
        orders = () if self.snapshot_count == 1 else (self.order,)
        return BrokerSnapshot(
            self.evidence, (), orders, True, True, NOW - timedelta(minutes=1), NOW
        )

    def lookup_order(self, *, order_id: str | None, client_id: str | None) -> BrokerOrder:
        assert order_id is None and client_id == self.request.client_id
        return self.order

    def read_updates(self) -> tuple[TradeUpdate, ...]:
        return ()

    def submit(self, request: SubmitRequest) -> SubmissionResult:
        assert request == self.request
        self.post_count += 1
        if self.submission_status is SubmissionStatus.UNCERTAIN:
            error = BrokerError(self.evidence, ErrorCategory.UNAVAILABLE, "timeout")
            return SubmissionResult(self.evidence, request, SubmissionStatus.UNCERTAIN, None, error)
        return SubmissionResult(self.evidence, request, self.submission_status, self.order, None)


def decision(intent: OrderIntent, *, decision_time: datetime = NOW) -> RiskDecision:
    return RiskDecision(
        intent=intent,
        status=RiskDecisionStatus.AUTHORIZED,
        reasons=(),
        policy_id="policy",
        policy_fingerprint="policy-fingerprint",
        risk_state_id="state",
        risk_state_revision=1,
        risk_state_fingerprint="state-fingerprint",
        feature_reference="feature",
        quote_reference="quote",
        decision_time=decision_time,
    )


def test_one_logical_intent_has_at_most_one_provider_post_across_recovery(tmp_path: Path) -> None:
    source = binding()
    identity = derive_paper_client_order_identity(
        stable_account_binding="paper-account",
        operational_scope="paper-scope",
        intent_identity=source.intent.intent_identity,
    )
    request = SubmitRequest(
        "paper-account",
        "paper-scope",
        identity.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )
    broker = OneShotBroker(request)
    owner_path = tmp_path / "owner"
    owner_path.mkdir()
    with AccountOwner.acquire(ownership_directory=owner_path, account_id="paper-account") as owner:
        with ExecutionJournal.create(
            path=tmp_path / "journal.sqlite",
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
        ) as journal:
            dispatcher = CanaryDispatcher(
                broker=broker,
                journal=journal,
                now=lambda: NOW,
                daily_submission_limit=2,
            )
            first = dispatcher.execute(
                source_opportunity_id="source-opportunity",
                risk_decision=decision(source.intent),
                request=request,
                dispatch_deadline=NOW + timedelta(seconds=5),
            )
            journal.close()
            with ExecutionJournal.reopen(
                path=tmp_path / "journal.sqlite",
                owner=owner,
                account_id="paper-account",
                operational_scope="paper-scope",
            ) as reopened:
                second = CanaryDispatcher(
                    broker=broker,
                    journal=reopened,
                    now=lambda: NOW,
                    daily_submission_limit=2,
                ).execute(
                    source_opportunity_id="source-opportunity",
                    risk_decision=decision(source.intent),
                    request=request,
                    dispatch_deadline=NOW + timedelta(seconds=5),
                )
    assert first.halted_reason is None
    assert first.submission is not None
    assert first.submission.status is SubmissionStatus.ACCEPTED
    assert broker.post_count == 1
    assert second.halted_reason is not None  # repeated reconciliation evidence is immutable
    assert broker.post_count == 1


def test_stale_risk_decision_halts_before_commit_or_post(tmp_path: Path) -> None:
    source = binding()
    identity = derive_paper_client_order_identity(
        stable_account_binding="paper-account",
        operational_scope="paper-scope",
        intent_identity=source.intent.intent_identity,
    )
    request = SubmitRequest(
        "paper-account",
        "paper-scope",
        identity.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )
    broker = OneShotBroker(request)
    owner_path = tmp_path / "owner"
    owner_path.mkdir()
    with AccountOwner.acquire(ownership_directory=owner_path, account_id="paper-account") as owner:
        with ExecutionJournal.create(
            path=tmp_path / "journal.sqlite",
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
        ) as journal:
            dispatcher = CanaryDispatcher(
                broker=broker,
                journal=journal,
                now=lambda: NOW + MAX_RISK_DECISION_AGE + timedelta(seconds=1),
                daily_submission_limit=2,
            )
            outcome = dispatcher.execute(
                source_opportunity_id="stale-source",
                risk_decision=decision(source.intent),
                request=request,
                dispatch_deadline=NOW + timedelta(minutes=1),
            )
    assert outcome.halted_reason == "risk decision is stale for the first canary"
    assert broker.post_count == 0


def test_future_risk_decision_halts_before_commit_or_post(tmp_path: Path) -> None:
    source = binding()
    identity = derive_paper_client_order_identity(
        stable_account_binding="paper-account",
        operational_scope="paper-scope",
        intent_identity=source.intent.intent_identity,
    )
    request = SubmitRequest(
        "paper-account",
        "paper-scope",
        identity.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )
    broker = OneShotBroker(request)
    owner_path = tmp_path / "owner"
    owner_path.mkdir()
    with AccountOwner.acquire(ownership_directory=owner_path, account_id="paper-account") as owner:
        with ExecutionJournal.create(
            path=tmp_path / "journal.sqlite",
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
        ) as journal:
            outcome = CanaryDispatcher(
                broker=broker, journal=journal, now=lambda: NOW, daily_submission_limit=2
            ).execute(
                source_opportunity_id="future-source",
                risk_decision=decision(source.intent, decision_time=NOW + timedelta(seconds=1)),
                request=request,
                dispatch_deadline=NOW + timedelta(minutes=1),
            )
    assert outcome.halted_reason == "risk decision is from the future"
    assert broker.post_count == 0


def test_uncertain_submission_is_persisted_and_rerun_never_posts_again(tmp_path: Path) -> None:
    source = binding()
    identity = derive_paper_client_order_identity(
        stable_account_binding="paper-account",
        operational_scope="paper-scope",
        intent_identity=source.intent.intent_identity,
    )
    request = SubmitRequest(
        "paper-account",
        "paper-scope",
        identity.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )
    broker = OneShotBroker(request, submission_status=SubmissionStatus.UNCERTAIN)
    owner_path = tmp_path / "owner"
    owner_path.mkdir()
    with AccountOwner.acquire(ownership_directory=owner_path, account_id="paper-account") as owner:
        with ExecutionJournal.create(
            path=tmp_path / "journal.sqlite",
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
        ) as journal:
            dispatcher = CanaryDispatcher(
                broker=broker, journal=journal, now=lambda: NOW, daily_submission_limit=2
            )
            first = dispatcher.execute(
                source_opportunity_id="uncertain-source",
                risk_decision=decision(source.intent),
                request=request,
                dispatch_deadline=NOW + timedelta(minutes=1),
            )
            second = dispatcher.execute(
                source_opportunity_id="uncertain-source",
                risk_decision=decision(source.intent),
                request=request,
                dispatch_deadline=NOW + timedelta(minutes=1),
            )
    assert first.submission is not None
    assert first.submission.status is SubmissionStatus.UNCERTAIN
    assert second.submission is not None
    assert broker.post_count == 1


def test_response_persistence_failure_never_allows_a_second_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = binding()
    identity = derive_paper_client_order_identity(
        stable_account_binding="paper-account",
        operational_scope="paper-scope",
        intent_identity=source.intent.intent_identity,
    )
    request = SubmitRequest(
        "paper-account",
        "paper-scope",
        identity.client_order_id,
        SPY,
        OrderSide.BUY,
        Decimal(1),
        OrderTarget.PAPER,
        OrderType.MARKET,
        TimeInForce.DAY,
        False,
    )
    broker = OneShotBroker(request)
    owner_path = tmp_path / "owner"
    owner_path.mkdir()
    with AccountOwner.acquire(ownership_directory=owner_path, account_id="paper-account") as owner:
        with ExecutionJournal.create(
            path=tmp_path / "journal.sqlite",
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
        ) as journal:

            def fail_persistence(*_args: object, **_kwargs: object) -> None:
                from shadow.execution.journal import JournalError

                raise JournalError("injected response persistence failure")

            monkeypatch.setattr(ExecutionJournal, "persist_submission", fail_persistence)
            dispatcher = CanaryDispatcher(
                broker=broker, journal=journal, now=lambda: NOW, daily_submission_limit=2
            )
            first = dispatcher.execute(
                source_opportunity_id="persistence-failure-source",
                risk_decision=decision(source.intent),
                request=request,
                dispatch_deadline=NOW + timedelta(minutes=1),
            )
            second = dispatcher.execute(
                source_opportunity_id="persistence-failure-source",
                risk_decision=decision(source.intent),
                request=request,
                dispatch_deadline=NOW + timedelta(minutes=1),
            )
    assert first.halted_reason == "submission response persistence failed"
    assert second.submission is None
    assert broker.post_count == 1
