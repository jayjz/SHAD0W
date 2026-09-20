# ruff: noqa: E501
"""One-shot P5A dispatch guard: commit first, then exactly one broker submit call."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
from shadow.execution.broker import (
    Broker,
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerError,
    BrokerSnapshot,
    Eligibility,
    OrderStatus,
    SessionState,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
)
from shadow.execution.journal import CommittedAttempt, ExecutionJournal, JournalError
from shadow.risk.models import RiskDecision, RiskDecisionStatus


class DispatchHalted(RuntimeError):
    """A fail-closed canary halt; it never authorizes a second POST."""


MAX_RISK_DECISION_AGE = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class RevalidationEvidence:
    account: BrokerAccount
    clock: BrokerClock
    asset: BrokerAsset
    snapshot: BrokerSnapshot


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    attempt: CommittedAttempt | None
    submission: SubmissionResult | None
    reconciliation: BrokerSnapshot | None
    halted_reason: str | None


class CanaryDispatcher:
    """Serialized canary dispatcher.  Existing attempts are reconciliation-only."""

    def __init__(
        self,
        *,
        broker: Broker,
        journal: ExecutionJournal,
        now: Callable[[], datetime],
        daily_submission_limit: int,
    ) -> None:
        if isinstance(daily_submission_limit, bool) or daily_submission_limit < 2:
            raise ValueError("entry canary needs a daily ceiling that reserves one later exit")
        self._broker = broker
        self._journal = journal
        self._now = now
        self._daily_submission_limit = daily_submission_limit

    def _halt(
        self,
        reason: str,
        intent_identity: str | None = None,
        *,
        attempt: CommittedAttempt | None = None,
        submission: SubmissionResult | None = None,
    ) -> DispatchOutcome:
        now = self._now().astimezone(UTC)
        try:
            self._journal.record_halt(
                occurred_at=now, reason=reason, intent_identity=intent_identity
            )
        except JournalError as exc:
            raise DispatchHalted("halt could not be durably recorded") from exc
        return DispatchOutcome(attempt, submission, None, reason)

    def _read(self, request: SubmitRequest) -> RevalidationEvidence:
        account = self._broker.read_account()
        clock = self._broker.read_clock()
        asset = self._broker.read_asset(request.instrument)
        snapshot = self._broker.read_snapshot()
        if any(isinstance(value, BrokerError) for value in (account, clock, asset, snapshot)):
            raise DispatchHalted("authoritative broker read failed")
        assert isinstance(account, BrokerAccount)
        assert isinstance(clock, BrokerClock)
        assert isinstance(asset, BrokerAsset)
        assert isinstance(snapshot, BrokerSnapshot)
        return RevalidationEvidence(account, clock, asset, snapshot)

    def _validate(
        self,
        *,
        request: SubmitRequest,
        decision: RiskDecision,
        evidence: RevalidationEvidence,
        deadline: datetime,
    ) -> None:
        now = self._now().astimezone(UTC)
        if decision.status is not RiskDecisionStatus.AUTHORIZED:
            raise DispatchHalted("risk authority is not authorized")
        if decision.decision_time > now:
            raise DispatchHalted("risk decision is from the future")
        if now - decision.decision_time > MAX_RISK_DECISION_AGE:
            raise DispatchHalted("risk decision is stale for the first canary")
        if (
            decision.intent.instrument != request.instrument
            or decision.intent.side != request.side
            or decision.intent.quantity != request.quantity
        ):
            raise DispatchHalted("risk authority/request mismatch")
        if (request.account_id, request.operational_scope) != (
            self._journal.identity.account_id,
            self._journal.identity.operational_scope,
        ):
            raise DispatchHalted("request/journal binding mismatch")
        account, clock, asset, snapshot = (
            evidence.account,
            evidence.clock,
            evidence.asset,
            evidence.snapshot,
        )
        if account.target.value != "paper" or account.eligibility is not Eligibility.ELIGIBLE:
            raise DispatchHalted("account is not eligible PAPER authority")
        if (
            asset.instrument != request.instrument
            or asset.tradable is not Eligibility.ELIGIBLE
            or asset.us_equity is not Eligibility.ELIGIBLE
        ):
            raise DispatchHalted("asset is not tradable US equity")
        if clock.state is not SessionState.REGULAR or clock.session_close is None:
            raise DispatchHalted("regular session is not open")
        if now >= deadline or now >= clock.session_close:
            raise DispatchHalted("dispatch deadline or session close reached")
        if not snapshot.complete:
            raise DispatchHalted("broker inventory is incomplete")
        if snapshot.positions:
            raise DispatchHalted("unexpected broker position")
        if snapshot.outstanding_orders:
            raise DispatchHalted("outstanding broker order")
        if request.quantity != Decimal(1):
            raise DispatchHalted("canary quantity changed")
        committed_today = sum(
            attempt.broker_trading_date == clock.trading_date.isoformat()
            for attempt in self._journal.committed_attempts()
        )
        if committed_today + 2 > self._daily_submission_limit:
            raise DispatchHalted("daily submission ceiling cannot reserve a later exit")

    def execute(
        self,
        *,
        source_opportunity_id: str,
        risk_decision: RiskDecision,
        request: SubmitRequest,
        dispatch_deadline: datetime,
    ) -> DispatchOutcome:
        """Attempt one logical intent once; recovery never invokes ``submit`` again."""
        intent_identity = risk_decision.intent.intent_identity
        try:
            existing = self._journal.attempt_for_intent(intent_identity)
        except JournalError as exc:
            raise DispatchHalted("journal cannot establish existing intent state") from exc
        if existing is not None:
            return self.reconcile(existing)
        try:
            # This is the submission-time reread, not reliance on the old P2A grant.
            evidence = self._read(request)
            self._validate(
                request=request,
                decision=risk_decision,
                evidence=evidence,
                deadline=dispatch_deadline,
            )
            client_identity = derive_paper_client_order_identity(
                stable_account_binding=request.account_id,
                operational_scope=request.operational_scope,
                intent_identity=intent_identity,
            )
            if client_identity.client_order_id != request.client_id:
                raise DispatchHalted("request client ID is not the deterministic PAPER identity")
            attempt = self._journal.commit_attempt(
                intent_identity=intent_identity,
                source_opportunity_id=source_opportunity_id,
                client_order_id=request.client_id,
                client_order_full_digest=client_identity.full_digest,
                broker_trading_date=evidence.clock.trading_date.isoformat(),
                committed_at=self._now().astimezone(UTC),
                dispatch_deadline=dispatch_deadline,
                request=request,
                risk_decision=risk_decision,
                account=evidence.account,
                clock=evidence.clock,
                asset=evidence.asset,
                snapshot=evidence.snapshot,
            )
        except (JournalError, DispatchHalted) as exc:
            return self._halt(str(exc), intent_identity)
        # The commit is the irreversible attempt boundary.  There is exactly one call site.
        if self._now().astimezone(UTC) >= dispatch_deadline:
            return self._halt("deadline expired after committed attempt", intent_identity)
        result = self._broker.submit(request)
        try:
            self._journal.persist_submission(intent_identity=intent_identity, result=result)
        except JournalError:
            # A POST without durable response evidence is uncertain; never retry it.
            return self._halt("submission response persistence failed", intent_identity)
        attempt = replace(attempt, submission=result)
        if result.status is SubmissionStatus.UNCERTAIN:
            return self._halt(
                "submission outcome is uncertain",
                intent_identity,
                attempt=attempt,
                submission=result,
            )
        return self.reconcile(attempt)

    def reconcile(self, attempt: CommittedAttempt) -> DispatchOutcome:
        """Lookup is evidence only: NOT_FOUND cannot authorize another POST."""
        result = attempt.submission
        order = self._broker.lookup_order(order_id=None, client_id=attempt.client_order_id)
        snapshot = self._broker.read_snapshot()
        if isinstance(order, BrokerError) or isinstance(snapshot, BrokerError):
            return self._halt(
                "reconciliation remains unresolved",
                attempt.intent_identity,
                attempt=attempt,
                submission=result,
            )
        if order.client_id != attempt.client_order_id or not snapshot.complete:
            return self._halt(
                "reconciliation evidence conflicts or is incomplete",
                attempt.intent_identity,
                attempt=attempt,
                submission=result,
            )
        if order.status is OrderStatus.UNKNOWN:
            return self._halt(
                "reconciliation order status is unknown",
                attempt.intent_identity,
                attempt=attempt,
                submission=result,
            )
        try:
            self._journal.persist_reconciliation(
                intent_identity=attempt.intent_identity, snapshot=snapshot
            )
        except JournalError:
            return self._halt(
                "reconciliation persistence failed",
                attempt.intent_identity,
                attempt=attempt,
                submission=result,
            )
        return DispatchOutcome(attempt, result, snapshot, None)
