"""Bounded BTC PAPER experiment composition.

This module owns the application sequence only.  Strategy, risk, reconciliation,
and the single guarded Alpaca POST remain in their existing boundaries.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from shadow.adapters.alpaca.btc_history import historical_trades
from shadow.adapters.alpaca.crypto_normalize import normalize
from shadow.adapters.alpaca.crypto_stream import (
    ENDPOINT,
    CryptoDataCredentials,
    auth_request,
    decode_frame,
    is_success,
    subscription_is_exact,
    subscription_request,
)
from shadow.adapters.alpaca.paper_broker import (
    AlpacaPaperBroker,
    PaperCredentials,
    UrllibTransport,
)
from shadow.application.btc_history import MarketEvidence
from shadow.domain.crypto_market import CryptoQuote, CryptoTrade, UtcNanoseconds
from shadow.execution.broker import BrokerSnapshot, SubmissionResult
from shadow.execution.btc_authority import BtcRunConfig, btc_intent_identity
from shadow.execution.btc_classification import (
    AuthorityClassification,
    classify_btc_authority,
    proof_reconcile,
)
from shadow.execution.btc_dispatch import BtcBroker, BtcDispatchAuthority, BtcDispatcher
from shadow.execution.btc_journal import BtcJournal
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.execution.dispatch import DispatchHalted
from shadow.execution.journal import ExecutionJournal
from shadow.execution.ownership import AccountOwner
from shadow.features.btc_trend import BTC, HOUR_NS, BtcTrendFeatures, CompletedBtcInterval
from shadow.risk.btc import evaluate_btc_risk, utc_ns
from shadow.risk.btc_models import BtcLifecycleAuthority, BtcRiskEvaluation, BtcRiskPolicy
from shadow.risk.models import OperatorControls, OrderSide, OrderTarget, OrderType, TimeInForce
from shadow.strategies.btc_trend import BtcProposal, engineering_canary, propose

_ACKNOWLEDGEMENT = "I-UNDERSTAND-THIS-SUBMITS-ONE-BTC-ALPACA-PAPER-ORDER"


class PaperApplicationError(RuntimeError):
    """The bounded application cannot establish a safe experiment cut."""


HistoricalSource = Callable[[int, int], Iterable[tuple[CryptoTrade, ...]]]
LiveSource = Callable[[float], Iterable[CryptoTrade | CryptoQuote]]


@dataclass(frozen=True, slots=True)
class PreflightResult:
    warm_start_ready: bool
    fresh_live_ready: bool
    journal_ready: bool
    execution_authority_ready: bool
    broker_ready: bool
    btc_asset_ready: bool
    strict_lifecycle: str
    experiment_ready: bool
    proof_ready: bool
    proof_blocker: str | None
    experiment_blockers: tuple[str, ...]
    submission_budget: int
    classification: AuthorityClassification

    def payload(self) -> dict[str, object]:
        return {
            "warm_start_ready": self.warm_start_ready,
            "fresh_live_ready": self.fresh_live_ready,
            "journal_ready": self.journal_ready,
            "execution_authority_ready": self.execution_authority_ready,
            "broker_ready": self.broker_ready,
            "btc_asset_ready": self.btc_asset_ready,
            "strict_lifecycle": self.strict_lifecycle,
            "experiment_ready": self.experiment_ready,
            "proof_ready": self.proof_ready,
            "proof_blocker": self.proof_blocker,
            "experiment_blockers": list(self.experiment_blockers),
            "submission_budget": self.submission_budget,
            "authority_classification": self.classification.value,
        }


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    status: str
    stop_reason: str
    preflight: PreflightResult
    proposal: BtcProposal | None
    evaluation: BtcRiskEvaluation | None
    submission: SubmissionResult | None
    observed_snapshot: BrokerSnapshot | None
    observed_activities: CryptoActivityEvidence | None

    def payload(self) -> dict[str, object]:
        features: BtcTrendFeatures | None = (
            None if self.proposal is None else self.proposal.features
        )
        return {
            "experiment_status": self.status,
            "final_stop_reason": self.stop_reason,
            **self.preflight.payload(),
            "proposal": None if self.proposal is None else self.proposal.action.value,
            "proposal_reason": None if self.proposal is None else self.proposal.reason,
            "trend_distance": None if features is None else str(features.trend_distance),
            "momentum_6h": None if features is None else str(features.fast_return),
            "volatility": None if features is None else str(features.volatility),
            "modeled_cost_hurdle": None
            if self.proposal is None
            else str(self.proposal.round_trip_cost + self.proposal.safety_margin),
            "risk_authorized": None if self.evaluation is None else self.evaluation.authorized,
            "risk_reasons": None if self.evaluation is None else list(self.evaluation.reasons),
            "submission": None if self.submission is None else self.submission.status.value,
            "client_order_id": None
            if self.submission is None
            else self.submission.request.client_id,
            "observed_broker_orders": None
            if self.observed_snapshot is None
            else len(self.observed_snapshot.orders),
            "observed_broker_position": None
            if self.observed_snapshot is None or len(self.observed_snapshot.positions) != 1
            else str(self.observed_snapshot.positions[0].quantity),
            "observed_activity_executions": None
            if self.observed_activities is None
            else len(self.observed_activities.executions),
            "observed_activity_fees": None
            if self.observed_activities is None
            else len(self.observed_activities.fees),
        }


class BtcPaperExperiment:
    """One account-owned, one-entry BTC PAPER experiment.

    ``live_source`` is injected so a future local crypto relay can replace direct
    WebSocket ownership without changing any strategy, risk, or execution code.
    """

    def __init__(
        self,
        *,
        broker: BtcBroker,
        journal: ExecutionJournal,
        market_evidence: MarketEvidence,
        run_config: BtcRunConfig,
        risk_policy: BtcRiskPolicy,
        quantity: Decimal,
        controls: Callable[[], OperatorControls],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if quantity <= 0 or not quantity.is_finite():
            raise PaperApplicationError("positive BTC quantity required")
        if run_config.maximum_per_run != 1 or run_config.maximum_per_period != 1:
            raise PaperApplicationError("BTC experiment submission ceiling must be exactly one")
        self.broker = broker
        self.journal = journal
        self.store = BtcJournal(journal)
        self.evidence = market_evidence
        self.config = engineering_canary()
        if run_config.strategy_configuration_id != self.config.configuration_id:
            raise PaperApplicationError("run configuration does not bind BTC trend configuration")
        if run_config.risk_policy_id != risk_policy.policy_id:
            raise PaperApplicationError("run configuration does not bind BTC risk policy")
        self.run_config = run_config
        self.policy = risk_policy
        self.quantity = quantity
        self.controls = controls
        self.now = now
        self.quote: CryptoQuote | None = None

    def _clock(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise PaperApplicationError("application clock must be timezone-aware")
        return value.astimezone(UTC)

    def _market(self) -> tuple[tuple[CompletedBtcInterval, ...], CryptoQuote]:
        if self.quote is None:
            raise DispatchHalted("no fresh BTC quote")
        return self.evidence.history.intervals, self.quote

    def _dispatcher(self) -> BtcDispatcher:
        return BtcDispatcher(
            broker=self.broker,
            journal=self.store,
            now=self._clock,
            controls=self.controls,
            market=self._market,
        )

    def _reads(
        self,
    ) -> tuple[BtcCashAccount, BtcBrokerAsset, BrokerSnapshot, CryptoActivityEvidence] | None:
        start = self.journal.identity.created_at
        if self.store.attempts:
            start = min(start, self.store.attempts[0].committed_at)
        cut = self._clock()
        account = self.broker.read_btc_account()
        asset = self.broker.read_btc_asset()
        activities = self.broker.read_btc_activities(history_start=start, history_end=cut)
        snapshot = self.broker.read_reconciliation_snapshot(earliest_attempt=start, history_end=cut)
        values = (account, asset, snapshot, activities)
        if not all(
            isinstance(
                value, (BtcCashAccount, BtcBrokerAsset, BrokerSnapshot, CryptoActivityEvidence)
            )
            for value in values
        ):
            return None
        assert isinstance(account, BtcCashAccount)
        assert isinstance(asset, BtcBrokerAsset)
        assert isinstance(snapshot, BrokerSnapshot)
        assert isinstance(activities, CryptoActivityEvidence)
        binding = (self.journal.identity.account_id, self.journal.identity.operational_scope)
        if any(
            (value.evidence.account_id, value.evidence.operational_scope) != binding
            for value in values
        ):
            return None
        if (
            activities.history_start > start
            or activities.history_end < cut
            or snapshot.history_start > start
            or snapshot.history_end != cut
        ):
            return None
        return account, asset, snapshot, activities

    def preflight(self) -> PreflightResult:
        """Perform all real read-side work and persist only reconciliation evidence."""
        self.store.configure(self.run_config)
        read = self._reads()
        if read is None:
            return PreflightResult(
                len(self.evidence.history.intervals) >= self.config.minimum_history,
                self.evidence.history.last_fresh_end is not None,
                False,
                False,
                False,
                False,
                "unavailable",
                False,
                False,
                None,
                ("broker_evidence_unavailable",),
                max(0, 1 - len(self.store.attempts)),
                AuthorityClassification.BLOCKED,
            )
        account, asset, snapshot, activities = read
        strict = proof_reconcile(
            attempts=self.store.attempts, snapshot=snapshot, activities=activities
        )
        self.store.record_reconciliation(snapshot, activities)
        status = classify_btc_authority(
            account=account,
            asset=asset,
            snapshot=snapshot,
            activities=activities,
            strict_lifecycle=strict,
            account_binding=(
                self.journal.identity.account_id,
                self.journal.identity.operational_scope,
            ),
            journal_usable=self.store.execution_authority_ready,
            risk_fresh=True,
            unresolved_prior_intent=bool(self.store.attempts),
        )
        return PreflightResult(
            len(self.evidence.history.intervals) >= self.config.minimum_history,
            self.evidence.history.last_fresh_end is not None,
            self.store.usable,
            self.store.execution_authority_ready,
            True,
            asset.tradable.value == "eligible",
            strict.state.value,
            status.classification is AuthorityClassification.EXPERIMENT_READY,
            status.proof_status is AuthorityClassification.PROOF_READY,
            status.proof_blocker,
            status.experiment_blockers,
            max(0, 1 - len(self.store.attempts)),
            status.classification,
        )

    def _persist(self, result: ExperimentResult) -> None:
        """Journal immutable experiment evidence without granting another attempt."""
        self.journal.append_application_event(
            "btc-experiment",
            (
                "shadow.btc-experiment.v1",
                self.run_config,
                self.evidence.digest,
                result.stop_reason,
                result.proposal,
                result.evaluation,
                result.preflight.classification.value,
                result.preflight.strict_lifecycle,
                result.preflight.submission_budget,
                result.submission,
                result.observed_snapshot,
                result.observed_activities,
                result.preflight.proof_ready,
                result.preflight.proof_blocker,
            ),
        )

    def _finish(
        self,
        *,
        status: str,
        stop_reason: str,
        preflight: PreflightResult,
        proposal: BtcProposal | None = None,
        evaluation: BtcRiskEvaluation | None = None,
        submission: SubmissionResult | None = None,
    ) -> ExperimentResult:
        # These are observations only.  They cannot clear an uncertain attempt.
        read = self._reads()
        snapshot = None if read is None else read[2]
        activities = None if read is None else read[3]
        observed_preflight = preflight
        if read is None:
            observed_preflight = replace(
                preflight,
                broker_ready=False,
                btc_asset_ready=False,
                strict_lifecycle="unavailable",
                experiment_ready=False,
                proof_ready=False,
                proof_blocker=None,
                experiment_blockers=("broker_evidence_unavailable",),
                classification=AuthorityClassification.BLOCKED,
            )
        else:
            account, asset, snapshot, activities = read
            strict = proof_reconcile(
                attempts=self.store.attempts, snapshot=snapshot, activities=activities
            )
            authority = classify_btc_authority(
                account=account,
                asset=asset,
                snapshot=snapshot,
                activities=activities,
                strict_lifecycle=strict,
                account_binding=(
                    self.journal.identity.account_id,
                    self.journal.identity.operational_scope,
                ),
                journal_usable=self.store.execution_authority_ready,
                risk_fresh=True,
                unresolved_prior_intent=bool(self.store.attempts),
            )
            observed_preflight = replace(
                preflight,
                journal_ready=self.store.usable,
                execution_authority_ready=self.store.execution_authority_ready,
                broker_ready=True,
                btc_asset_ready=asset.tradable.value == "eligible",
                strict_lifecycle=strict.state.value,
                experiment_ready=authority.classification
                is AuthorityClassification.EXPERIMENT_READY,
                proof_ready=authority.proof_status is AuthorityClassification.PROOF_READY,
                proof_blocker=authority.proof_blocker,
                experiment_blockers=authority.experiment_blockers,
                submission_budget=max(0, 1 - len(self.store.attempts)),
                classification=authority.classification,
            )
        result = ExperimentResult(
            status,
            stop_reason,
            observed_preflight,
            proposal,
            evaluation,
            submission,
            snapshot,
            activities,
        )
        self._persist(result)
        return result

    def warm_start(self, historical: HistoricalSource) -> None:
        if self.evidence.events:
            return
        boundary = (utc_ns(self._clock()) // HOUR_NS) * HOUR_NS
        start = boundary - (self.config.minimum_history + 1) * HOUR_NS
        self.evidence.begin(start_ns=start, live_boundary_ns=boundary)
        for page in historical(start, boundary):
            self.evidence.trades(page)

    def _evaluate(self, proposal: BtcProposal) -> BtcRiskEvaluation | None:
        read = self._reads()
        if read is None or self.quote is None:
            return None
        account, asset, snapshot, activities = read
        quote = self.quote
        temporary = BtcSubmitRequest(
            self.policy.account_id,
            self.policy.operational_scope,
            "btc-experiment-draft",
            BTC,
            OrderSide.BUY,
            self.quantity,
            OrderTarget.PAPER,
            OrderType.MARKET,
            TimeInForce.GTC,
            False,
        )
        lifecycle = (
            BtcLifecycleAuthority.INITIAL_EXPERIMENT
            if self.store.reconciliation is not None and not self.store.usable
            else BtcLifecycleAuthority.PROOF
        )
        controls = self.controls()
        now_ns = utc_ns(self._clock())

        def evaluate(request: BtcSubmitRequest) -> BtcRiskEvaluation:
            return evaluate_btc_risk(
                policy=self.policy,
                config=self.config,
                proposal=proposal,
                request=request,
                intervals=self.evidence.history.intervals,
                quote=quote,
                account=account,
                asset=asset,
                snapshot=snapshot,
                attempts=self.store.attempts,
                controls=controls,
                now_ns=now_ns,
                crypto_evidence=activities,
                require_crypto_evidence=True,
                lifecycle_authority=lifecycle,
            )

        initial = evaluate(temporary)
        intent = btc_intent_identity(initial, self.run_config.source_market_id)
        from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity

        identity = derive_paper_client_order_identity(
            stable_account_binding=self.policy.account_id,
            operational_scope=self.policy.operational_scope,
            intent_identity=intent,
        )
        return evaluate(
            BtcSubmitRequest(
                temporary.account_id,
                temporary.operational_scope,
                identity.client_order_id,
                temporary.instrument,
                temporary.side,
                temporary.quantity,
                temporary.target,
                temporary.order_type,
                temporary.time_in_force,
                temporary.extended_hours,
            )
        )

    def experiment(
        self, historical: HistoricalSource, live: LiveSource, duration_seconds: float
    ) -> ExperimentResult:
        if duration_seconds <= 0:
            raise PaperApplicationError("positive experiment duration required")
        initial = self.preflight()
        if initial.submission_budget == 0:
            return self._finish(
                status="complete", stop_reason="SUBMISSION_BUDGET_SPENT", preflight=initial
            )
        self.warm_start(historical)
        events = live(duration_seconds)
        try:
            for event in events:
                if isinstance(event, CryptoQuote):
                    if event.instrument == BTC:
                        self.quote = event
                    continue
                if not isinstance(event, CryptoTrade) or event.instrument != BTC:
                    continue
                previous_fresh = self.evidence.history.last_fresh_end
                self.evidence.trades((event,))
                if self.evidence.history.last_fresh_end is None or (
                    self.evidence.history.last_fresh_end == previous_fresh
                ):
                    continue
                features = self.config.features(
                    self.evidence.history.intervals, utc_ns(self._clock())
                )
                proposal = propose(
                    self.config, features, now_ns=utc_ns(self._clock()), holding=False
                )
                fresh = self.preflight()
                if proposal is None:
                    return self._finish(status="complete", stop_reason="NO_SIGNAL", preflight=fresh)
                if fresh.classification not in (
                    AuthorityClassification.EXPERIMENT_READY,
                    AuthorityClassification.PROOF_READY,
                ):
                    return self._finish(
                        status="complete", stop_reason="BLOCKED", preflight=fresh, proposal=proposal
                    )
                evaluation = self._evaluate(proposal)
                if evaluation is None or not evaluation.authorized:
                    return self._finish(
                        status="complete",
                        stop_reason="RISK_REJECTED",
                        preflight=fresh,
                        proposal=proposal,
                        evaluation=evaluation,
                    )
                authority = (
                    BtcDispatchAuthority.INITIAL_EXPERIMENT
                    if fresh.classification is AuthorityClassification.EXPERIMENT_READY
                    else BtcDispatchAuthority.PROOF
                )
                try:
                    dispatcher = self._dispatcher()
                    # Establish the dispatcher-owned recovery cut immediately before
                    # dispatch.  Its execute path then repeats all fresh reads and
                    # authority/risk checks before the durable commit and POST guard.
                    dispatcher.recover()
                    submission = dispatcher.execute(
                        evaluation,
                        dispatch_deadline=self._clock() + timedelta(seconds=10),
                        expected_revision=self.store.revision,
                        authority=authority,
                    )
                except DispatchHalted:
                    return self._finish(
                        status="complete",
                        stop_reason="DISPATCH_BLOCKED",
                        preflight=fresh,
                        proposal=proposal,
                        evaluation=evaluation,
                    )
                if not isinstance(submission, SubmissionResult):
                    return self._finish(
                        status="complete",
                        stop_reason="RESTART_RECONCILIATION_ONLY",
                        preflight=fresh,
                        proposal=proposal,
                        evaluation=evaluation,
                    )
                return self._finish(
                    status="complete",
                    stop_reason="POST_ATTEMPT_OBSERVED",
                    preflight=fresh,
                    proposal=proposal,
                    evaluation=evaluation,
                    submission=submission,
                )
            final = self.preflight()
            return self._finish(
                status="complete", stop_reason="LIVE_INTERVAL_TIMEOUT", preflight=final
            )
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()


def _historical_source(
    credentials: CryptoDataCredentials, duration_seconds: float
) -> HistoricalSource:
    def fetch(start_ns: int, end_ns: int) -> Iterator[tuple[CryptoTrade, ...]]:
        started = time.monotonic()
        yield from historical_trades(
            transport=UrllibTransport(),
            credentials=credentials,
            start_ns=start_ns,
            end_ns=end_ns,
            now_ns=time.time_ns,
            remaining_seconds=lambda: duration_seconds - (time.monotonic() - started),
        )

    return fetch


class _DirectLive(Iterator[CryptoTrade | CryptoQuote]):
    """A closeable synchronous view of one direct Alpaca crypto WebSocket.

    The synchronous experiment drives this iterator one event at a time.  Its
    private event loop retains the socket between ``next`` calls, so returning
    from the experiment can close it immediately without a producer task or a
    session-sized event buffer.
    """

    def __init__(
        self,
        credentials: CryptoDataCredentials,
        duration_seconds: float,
        connect: Callable[..., Any],
    ) -> None:
        self.credentials = credentials
        self.duration_seconds = duration_seconds
        self._connect = connect
        self._deadline: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._context: Any | None = None
        self._socket: Any | None = None
        # This contains only decoded BTC events from the current provider frame;
        # ``max_size`` bounds that frame to one MiB.
        self._pending: deque[CryptoTrade | CryptoQuote] = deque()
        self._closed = False

    def __iter__(self) -> Iterator[CryptoTrade | CryptoQuote]:
        return self

    def _remaining(self) -> float:
        assert self._deadline is not None
        return self._deadline - time.monotonic()

    def _receive(self) -> str | bytes | None:
        if self._loop is None or self._socket is None:
            raise PaperApplicationError("crypto stream is not connected")
        remaining = self._remaining()
        if remaining <= 0:
            self.close()
            return None
        try:
            raw = self._loop.run_until_complete(
                asyncio.wait_for(self._socket.recv(), timeout=remaining)
            )
        except TimeoutError:
            self.close()
            return None
        if not isinstance(raw, (str, bytes)):
            raise PaperApplicationError("crypto stream returned invalid frame")
        return raw

    def _protocol_frame(self) -> list[dict[str, object]]:
        raw = self._receive()
        if raw is None:
            raise PaperApplicationError("crypto stream deadline expired during setup")
        return decode_frame(raw)

    def _open(self) -> None:
        self._deadline = time.monotonic() + self.duration_seconds
        self._loop = asyncio.new_event_loop()
        try:
            self._context = self._connect(
                ENDPOINT,
                proxy=None,
                max_size=1_048_576,
                max_queue=1,
                open_timeout=min(5, self.duration_seconds),
            )
            self._socket = self._loop.run_until_complete(self._context.__aenter__())
            if not is_success(self._protocol_frame(), "connected"):
                raise PaperApplicationError("crypto stream connection rejected")
            self._loop.run_until_complete(self._socket.send(auth_request(self.credentials)))
            if not is_success(self._protocol_frame(), "authenticated"):
                raise PaperApplicationError("crypto stream authentication rejected")
            self._loop.run_until_complete(self._socket.send(subscription_request()))
            if not subscription_is_exact(self._protocol_frame()):
                raise PaperApplicationError("crypto stream subscription rejected")
        except BaseException:
            self.close()
            raise

    def __next__(self) -> CryptoTrade | CryptoQuote:
        if self._closed:
            raise StopIteration
        if self._loop is None:
            self._open()
        while True:
            if self._pending:
                return self._pending.popleft()
            raw = self._receive()
            if raw is None:
                raise StopIteration
            received = UtcNanoseconds(time.time_ns())
            for frame in decode_frame(raw):
                if frame.get("T") not in ("t", "q"):
                    continue
                event = normalize(frame, received_at=received).event
                if event.instrument == BTC:
                    assert isinstance(event, (CryptoTrade, CryptoQuote))
                    self._pending.append(event)

    async def _shutdown(self) -> None:
        try:
            if self._socket is not None:
                await self._socket.close()
        finally:
            if self._context is not None:
                await self._context.__aexit__(None, None, None)

    def close(self) -> None:
        """Close the socket and private event loop; safe on every exit path."""
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        if self._loop is None:
            return
        try:
            self._loop.run_until_complete(self._shutdown())
        finally:
            self._loop.close()


def _direct_live(
    credentials: CryptoDataCredentials,
    duration_seconds: float,
    *,
    connect: Callable[..., Any] | None = None,
) -> _DirectLive:
    """Return one bounded direct source; relays can implement ``LiveSource`` instead."""
    if duration_seconds <= 0:
        raise PaperApplicationError("positive experiment duration required")
    if connect is None:
        from websockets.asyncio.client import connect as websocket_connect

        connect = websocket_connect
    return _DirectLive(credentials, duration_seconds, connect)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="bounded SHAD0W BTC PAPER experiment")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--experiment", action="store_true")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--operational-scope", required=True)
    parser.add_argument("--quantity", required=True, type=Decimal)
    parser.add_argument("--journal-path", required=True, type=Path)
    parser.add_argument("--market-evidence-path", required=True, type=Path)
    parser.add_argument("--experiment-evidence-path", required=True, type=Path)
    parser.add_argument("--ownership-directory", required=True, type=Path)
    parser.add_argument("--duration-seconds", required=True, type=float)
    parser.add_argument("--paper-acknowledgement", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-market-id", required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--maximum-entry-notional", required=True, type=Decimal)
    parser.add_argument("--cash-buffer", required=True, type=Decimal)
    parser.add_argument("--trading-enabled", action="store_true")
    parser.add_argument("--kill-switch", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 0 < args.duration_seconds <= 7_200:
        raise SystemExit("duration must be within (0, 7200] seconds")
    if args.experiment and args.paper_acknowledgement != _ACKNOWLEDGEMENT:
        raise SystemExit("explicit BTC PAPER acknowledgement is required")
    if args.experiment and not args.trading_enabled:
        raise SystemExit("--trading-enabled is required for an experiment")
    credentials = PaperCredentials.from_environment()
    args.ownership_directory.mkdir(parents=True, exist_ok=True)
    with AccountOwner.acquire(
        ownership_directory=args.ownership_directory, account_id=args.account_id
    ) as owner:
        journal = (
            ExecutionJournal.reopen(
                path=args.journal_path,
                owner=owner,
                account_id=args.account_id,
                operational_scope=args.operational_scope,
            )
            if args.journal_path.exists()
            else ExecutionJournal.create(
                path=args.journal_path,
                owner=owner,
                account_id=args.account_id,
                operational_scope=args.operational_scope,
                created_at=datetime.now(UTC),
            )
        )
        with journal:
            config = engineering_canary()
            policy = BtcRiskPolicy(
                args.account_id,
                args.operational_scope,
                args.quantity,
                args.maximum_entry_notional,
                args.cash_buffer,
                30_000_000_000,
                30_000_000_000,
                30_000_000_000,
            )
            runner = BtcPaperExperiment(
                broker=AlpacaPaperBroker(
                    credentials=credentials,
                    account_id=args.account_id,
                    operational_scope=args.operational_scope,
                ),
                journal=journal,
                market_evidence=MarketEvidence(args.market_evidence_path, journal),
                run_config=BtcRunConfig(
                    args.run_id,
                    args.source_market_id,
                    config.configuration_id,
                    policy.policy_id,
                    args.code_revision,
                    1,
                    1,
                    86_400,
                ),
                risk_policy=policy,
                quantity=args.quantity,
                controls=lambda: OperatorControls(
                    args.trading_enabled, args.kill_switch, datetime.now(UTC), datetime.now(UTC)
                ),
            )
            if args.preflight:
                payload = runner.preflight().payload()
            else:
                data_credentials = CryptoDataCredentials.from_environment(os.environ)
                payload = runner.experiment(
                    _historical_source(data_credentials, args.duration_seconds),
                    lambda duration: _direct_live(data_credentials, duration),
                    args.duration_seconds,
                ).payload()
            args.experiment_evidence_path.parent.mkdir(parents=True, exist_ok=True)
            args.experiment_evidence_path.write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
