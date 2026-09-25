"""Serialized BTC PAPER dispatch: exact durable intent, at most one POST invocation."""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
from shadow.domain.crypto_market import CryptoQuote
from shadow.execution.broker import (
    BrokerError,
    BrokerSnapshot,
    ErrorCategory,
    Evidence,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
)
from shadow.execution.btc_authority import BtcAttempt, BtcRevalidation, btc_intent_identity
from shadow.execution.btc_classification import AuthorityClassification, classify_btc_authority
from shadow.execution.btc_journal import BtcJournal
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.execution.dispatch import DispatchHalted
from shadow.execution.reconciliation import Reconciliation, reconcile
from shadow.features.btc_trend import CompletedBtcInterval
from shadow.risk.btc import evaluate_btc_risk, utc_ns
from shadow.risk.btc_models import BtcLifecycleAuthority, BtcRiskEvaluation
from shadow.risk.models import OperatorControls


class BtcBroker(Protocol):
    def read_btc_account(self) -> BtcCashAccount | BrokerError: ...
    def read_btc_asset(self) -> BtcBrokerAsset | BrokerError: ...
    def read_btc_activities(
        self, *, history_start: datetime, history_end: datetime, max_pages: int = 20
    ) -> CryptoActivityEvidence | BrokerError: ...
    def read_reconciliation_snapshot(
        self,
        *,
        earliest_attempt: datetime,
        max_pages: int = 10,
        history_end: datetime | None = None,
    ) -> BrokerSnapshot | BrokerError: ...
    def submit(
        self, request: SubmitRequest, *, before_post: Callable[[], None] | None = None
    ) -> SubmissionResult: ...


class BtcDispatchAuthority(StrEnum):
    PROOF = "proof"
    INITIAL_EXPERIMENT = "initial_experiment"


class BtcDispatcher:
    """Single-thread owner; restart requires explicit fresh read-only recovery.

    An existing attempt always takes the reconciliation-only branch. A local
    committed/no-response attempt cannot prove whether any transport occurred.
    """

    def __init__(
        self,
        *,
        broker: BtcBroker,
        journal: BtcJournal,
        now: Callable[[], datetime],
        controls: Callable[[], OperatorControls],
        market: Callable[[], tuple[tuple[CompletedBtcInterval, ...], CryptoQuote]],
        monotonic: Callable[[], float] = time.monotonic,
        session_guard: Callable[[SubmitRequest], None] | None = None,
    ) -> None:
        self.broker = broker
        self.store = journal
        self.now = now
        self.controls = controls
        self.market = market
        self.monotonic = monotonic
        self.session_guard = session_guard
        self._thread = threading.get_ident()
        self._busy = False
        self._recovered = False

    def _clock(self) -> datetime:
        now = self.now()
        if now.tzinfo is not UTC:
            raise DispatchHalted("UTC dispatcher clock required")
        return now

    def _held(self) -> None:
        if threading.get_ident() != self._thread:
            raise DispatchHalted("BTC dispatcher requires serialized owner thread")
        self.store.journal.assert_held()

    def _cut(self) -> tuple[BtcCashAccount, BtcBrokerAsset, BrokerSnapshot, CryptoActivityEvidence]:
        self._held()
        start = self.store.journal.identity.created_at
        if self.store.attempts:
            start = min(start, self.store.attempts[0].committed_at)
        cut = self._clock()
        account = self.broker.read_btc_account()
        asset = self.broker.read_btc_asset()
        activities = self.broker.read_btc_activities(history_start=start, history_end=cut)
        snapshot = self.broker.read_reconciliation_snapshot(earliest_attempt=start, history_end=cut)
        if (
            not isinstance(account, BtcCashAccount)
            or not isinstance(asset, BtcBrokerAsset)
            or not isinstance(activities, CryptoActivityEvidence)
            or not isinstance(snapshot, BrokerSnapshot)
        ):
            raise DispatchHalted("BTC broker evidence unavailable")
        binding = (
            self.store.journal.identity.account_id,
            self.store.journal.identity.operational_scope,
        )
        if any(
            (item.evidence.account_id, item.evidence.operational_scope) != binding
            for item in (account, asset, snapshot, activities)
        ):
            raise DispatchHalted("BTC broker binding conflict")
        if (
            activities.history_start > start
            or activities.history_end < cut
            or snapshot.history_start > start
            or snapshot.history_end != cut
        ):
            raise DispatchHalted("BTC activity history does not cover requested cut")
        return account, asset, snapshot, activities

    def recover(self) -> Reconciliation:
        """Read and persist authoritative recovery; never submit or auto-resume."""
        self._held()
        if self._busy:
            raise DispatchHalted("BTC dispatch is already active")
        try:
            self.store.refresh()
            _, _, snapshot, activities = self._cut()
            result = self.store.record_reconciliation(snapshot, activities)
            self._recovered = True
            return result
        except Exception as exc:
            self.store.halt(self._clock(), f"BTC recovery failed: {type(exc).__name__}")
            raise DispatchHalted("BTC recovery failed") from exc

    def execute(
        self,
        evaluation: BtcRiskEvaluation,
        *,
        dispatch_deadline: datetime,
        expected_revision: int,
        independent_risk_halt: bool = False,
        authority: BtcDispatchAuthority = BtcDispatchAuthority.PROOF,
        plumbing_probe: bool = False,
    ) -> SubmissionResult | Reconciliation:
        self._held()
        if self._busy:
            raise DispatchHalted("BTC dispatch is already active")
        self.store.refresh()
        config = self.store.config
        if config is None:
            raise DispatchHalted("BTC run is not configured")
        if plumbing_probe:
            binding = self.store.journal.application_events("btc-session-binding")
            if (
                len(binding) != 1
                or not isinstance(binding[0], tuple)
                or len(binding[0]) != 3
                or binding[0][0] is not True
                or config.maximum_per_run != 2
                or config.maximum_per_period != 2
            ):
                raise DispatchHalted("plumbing probe requires bounded session authority")
        intent = btc_intent_identity(evaluation, config.source_market_id)
        existing = next((a for a in self.store.attempts if a.intent_identity == intent), None)
        if existing is not None:
            if (
                existing.request != evaluation.request
                or existing.evaluation.proposal != evaluation.proposal
                or existing.evaluation.policy != evaluation.policy
            ):
                self.store.halt(self._clock(), "BTC source opportunity content conflict")
                raise DispatchHalted("BTC source opportunity content conflict")
            return self.recover()
        self._busy = True
        try:
            started = self._clock()
            mono_started = self.monotonic()
            if (
                self.store.journal.has_legacy_halts()
                or not self._recovered
                or self.store.halted
                or not (
                    self.store.usable
                    if authority is BtcDispatchAuthority.PROOF
                    else self.store.execution_authority_ready
                )
                or self.store.revision != expected_revision
            ):
                raise DispatchHalted("BTC startup/halt/reconciliation revision guard")
            if (
                not evaluation.authorized
                or evaluation.evaluated_ns > utc_ns(started)
                or utc_ns(started) - evaluation.evaluated_ns > 30_000_000_000
            ):
                raise DispatchHalted("BTC risk authorization expired or unauthorized")
            account, asset, snapshot, activities = self._cut()
            strict_state = reconcile(
                attempts=self.store.attempts,
                snapshot=snapshot,
                crypto_evidence=activities,
                require_crypto_evidence=True,
            )
            if authority is BtcDispatchAuthority.INITIAL_EXPERIMENT:
                classified = classify_btc_authority(
                    account=account,
                    asset=asset,
                    snapshot=snapshot,
                    activities=activities,
                    strict_lifecycle=strict_state,
                    account_binding=(
                        self.store.journal.identity.account_id,
                        self.store.journal.identity.operational_scope,
                    ),
                    journal_usable=self.store.execution_authority_ready,
                    risk_fresh=True,
                    unresolved_prior_intent=bool(self.store.attempts),
                )
                if classified.classification is not AuthorityClassification.EXPERIMENT_READY:
                    raise DispatchHalted("BTC experiment classification rejected")
            intervals, quote = self.market()
            controls = self.controls()
            fresh = evaluate_btc_risk(
                policy=evaluation.policy,
                config=evaluation.config,
                proposal=evaluation.proposal,
                request=evaluation.request,
                intervals=intervals,
                quote=quote,
                account=account,
                asset=asset,
                snapshot=snapshot,
                attempts=self.store.attempts,
                controls=controls,
                now_ns=utc_ns(self._clock()),
                fills=tuple({fill.execution_id: fill for fill in activities.executions}.values()),
                independent_risk_halt=independent_risk_halt,
                crypto_evidence=activities,
                require_crypto_evidence=True,
                lifecycle_authority=(
                    BtcLifecycleAuthority.INITIAL_EXPERIMENT
                    if authority is BtcDispatchAuthority.INITIAL_EXPERIMENT
                    else BtcLifecycleAuthority.PROOF
                ),
                plumbing_probe=plumbing_probe,
            )
            if not fresh.authorized:
                raise DispatchHalted("BTC revalidation rejected: " + ",".join(fresh.reasons))
            # The fresh cut must agree with the expected committed lifecycle.
            fresh_state = reconcile(
                attempts=self.store.attempts,
                snapshot=snapshot,
                crypto_evidence=activities,
                require_crypto_evidence=True,
            )
            if authority is BtcDispatchAuthority.PROOF and fresh_state != self.store.reconciliation:
                raise DispatchHalted("BTC lifecycle changed since reconciliation")
            if authority is BtcDispatchAuthority.INITIAL_EXPERIMENT and (
                strict_state != self.store.reconciliation
                or self.store.usable
                or self.store.attempts
            ):
                raise DispatchHalted("BTC experiment lifecycle changed since reconciliation")
            policy = evaluation.policy
            if self.session_guard is not None:
                self.session_guard(evaluation.request)
            expiry_ns = min(
                evaluation.evaluated_ns + 30_000_000_000,
                quote.observation_time.value + policy.maximum_market_age_ns,
                utc_ns(controls.observation_time) + policy.maximum_control_age_ns,
                *(
                    utc_ns(item.evidence.observation_time) + policy.maximum_broker_age_ns
                    for item in (account, asset, snapshot)
                ),
                utc_ns(snapshot.history_end) + policy.maximum_broker_age_ns,
                evaluation.proposal.features.end_ns + evaluation.config.maximum_evidence_age_ns,
            )
            if dispatch_deadline.tzinfo is not UTC or utc_ns(dispatch_deadline) > expiry_ns:
                raise DispatchHalted("BTC deadline exceeds evidence validity")
            identity = derive_paper_client_order_identity(
                stable_account_binding=evaluation.request.account_id,
                operational_scope=evaluation.request.operational_scope,
                intent_identity=intent,
            )
            evidence = BtcRevalidation(
                intervals,
                quote,
                account,
                asset,
                snapshot,
                activities,
                controls,
                independent_risk_halt,
            )
            attempt = BtcAttempt(
                intent,
                identity.full_digest,
                evaluation,
                evidence,
                self._clock(),
                dispatch_deadline,
                self.store.reconciliation_revision,
            )
            self.store.commit(attempt, expected_revision=expected_revision)
            committed_revision = self.store.revision
            checked = False

            def final_check() -> None:
                nonlocal checked
                if checked:
                    raise DispatchHalted("BTC transport attempted a second send")
                checked = True
                if self.session_guard is not None:
                    self.session_guard(attempt.request)
                self._held()
                self.store.refresh()
                current_controls = self.controls()
                current = self._clock()
                elapsed = self.monotonic() - mono_started
                if (
                    self.store.journal.has_legacy_halts()
                    or self.store.revision != committed_revision
                    or self.store.halted
                    or current_controls != controls
                    or current_controls.kill_switch_active
                    or not current_controls.trading_enabled
                    or current < started
                    or current >= dispatch_deadline
                    or elapsed < 0
                    or elapsed >= (dispatch_deadline - started).total_seconds()
                ):
                    raise DispatchHalted("BTC final local send guard")

            # Adapter must invoke callback after its last read, immediately before
            # POST. No retry and no unbounded queue follows this seam.
            try:
                result = self.broker.submit(attempt.request, before_post=final_check)
                if (
                    not checked
                    or not isinstance(result, SubmissionResult)
                    or result.request != attempt.request
                ):
                    raise ValueError("malformed BTC submission result")
            except DispatchHalted:
                raise
            except Exception as exc:
                ev = Evidence(
                    "btc:submit:uncertain",
                    attempt.request.account_id,
                    attempt.request.operational_scope,
                    self._clock(),
                    self._clock(),
                )
                result = SubmissionResult(
                    ev,
                    attempt.request,
                    SubmissionStatus.UNCERTAIN,
                    None,
                    BrokerError(ev, ErrorCategory.UNAVAILABLE, type(exc).__name__),
                )
            self.store.persist_submission(result)
            return result
        except Exception as exc:
            self.store.halt(self._clock(), f"BTC dispatch halted: {type(exc).__name__}: {exc}")
            raise DispatchHalted(str(exc)) from exc
        finally:
            self._busy = False
