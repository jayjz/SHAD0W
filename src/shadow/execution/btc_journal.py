"""BTC authority projected from append-only events in the existing SQLite journal."""

from dataclasses import replace
from datetime import datetime

from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
from shadow.execution.broker import BrokerSnapshot, SubmissionResult, SubmissionStatus
from shadow.execution.btc_authority import BtcAttempt, BtcRunConfig, btc_intent_identity
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.execution.journal import ExecutionJournal, JournalError
from shadow.execution.journal_codec import canonical_digest
from shadow.execution.reconciliation import OperationalState, Reconciliation, reconcile
from shadow.risk.models import OrderSide


class BtcJournal:
    """Same owner, connection and durability as ExecutionJournal; no second store."""

    def __init__(self, journal: ExecutionJournal) -> None:
        self.journal = journal
        self.revision = 0
        self.config: BtcRunConfig | None = None
        self.attempts: tuple[BtcAttempt, ...] = ()
        self.reconciliation: Reconciliation | None = None
        self.reconciliation_revision = 0
        self.reconciled_attempts = -1
        self.halted = False
        self.halt_revision = 0
        self.refresh()

    def refresh(self) -> None:
        self.revision = 0
        self.config = None
        self.attempts = ()
        self.reconciliation = None
        self.reconciliation_revision = 0
        self.reconciled_attempts = -1
        self.halted = False
        self.halt_revision = 0
        for revision, kind, payload in self.journal.btc_events():
            if revision != self.revision + 1:
                raise JournalError("BTC event revision gap")
            self._apply(kind, payload, revision)
            self.revision = revision

    def _apply(self, kind: str, payload: object, revision: int) -> None:
        if kind == "configure":
            if self.config is not None or not isinstance(payload, BtcRunConfig):
                raise JournalError("BTC run binding conflict")
            self.config = payload
        elif kind == "attempt":
            if not isinstance(payload, BtcAttempt):
                raise JournalError("malformed BTC attempt")
            self._validate_attempt(payload)
            self.attempts += (payload,)
        elif kind == "submission":
            if not isinstance(payload, SubmissionResult):
                raise JournalError("malformed BTC submission")
            matching = [a for a in self.attempts if a.request == payload.request]
            if len(matching) != 1 or matching[0].submission is not None:
                raise JournalError("BTC submission transition conflict")
            self.attempts = tuple(
                replace(a, submission=payload) if a == matching[0] else a for a in self.attempts
            )
            if payload.status is SubmissionStatus.UNCERTAIN:
                self.halted = True
                self.halt_revision = revision
        elif kind == "reconcile":
            if (
                not isinstance(payload, tuple)
                or len(payload) != 2
                or not isinstance(payload[0], BrokerSnapshot)
                or not isinstance(payload[1], CryptoActivityEvidence)
            ):
                raise JournalError("malformed BTC reconciliation")
            self._binding(payload[0].evidence.account_id, payload[0].evidence.operational_scope)
            self.reconciliation = reconcile(
                attempts=self.attempts,
                snapshot=payload[0],
                crypto_evidence=payload[1],
                require_crypto_evidence=True,
            )
            self.reconciliation_revision = revision
            self.reconciled_attempts = len(self.attempts)
            # A pristine initial entry may be operationally usable even when the
            # strict proof reducer cannot verify provider fee/history finality.
            # Keep `usable` proof-grade; expose that narrow seam separately.
            pristine_experiment_cut = (
                not self.attempts
                and self.reconciliation.state is OperationalState.UNRESOLVED
                and self.reconciliation.reason == "activity history unproven"
                and payload[1].query_exhausted
                and not payload[1].history_verified
                and not payload[1].executions
                and not payload[1].fees
                and not payload[1].unsupported
                and payload[0].complete
                and payload[0].orders_complete
                and payload[0].positions_complete
                and not payload[0].orders
                and not payload[0].positions
            )
            if self.reconciliation.state is OperationalState.HALTED or (
                self.reconciliation.state is OperationalState.UNRESOLVED
                and not pristine_experiment_cut
            ):
                self.halted = True
                self.halt_revision = revision
        elif kind == "halt":
            if (
                not isinstance(payload, tuple)
                or len(payload) != 2
                or not isinstance(payload[0], datetime)
                or not isinstance(payload[1], str)
                or not payload[1]
            ):
                raise JournalError("malformed BTC halt")
            self.halted = True
            self.halt_revision = revision
        elif kind == "resume":
            if (
                payload != self.reconciliation_revision
                or not self.usable
                or self.reconciliation_revision <= self.halt_revision
            ):
                raise JournalError("resume requires current authoritative reconciliation")
            self.halted = False
        else:
            raise JournalError("unknown BTC authority event")

    @property
    def usable(self) -> bool:
        return (
            self.reconciled_attempts == len(self.attempts)
            and self.reconciliation is not None
            and self.reconciliation.state in (OperationalState.FLAT, OperationalState.HOLDING)
        )

    @property
    def execution_authority_ready(self) -> bool:
        """Proof-grade readiness or the exact pristine initial-entry cut."""
        if self.halted or self.reconciled_attempts != len(self.attempts):
            return False
        if self.usable:
            return True
        if self.attempts or self.reconciliation is None:
            return False
        if (
            self.reconciliation.state is not OperationalState.UNRESOLVED
            or self.reconciliation.reason != "activity history unproven"
        ):
            return False
        events = self.journal.btc_events()
        if not events or events[-1][1] != "reconcile":
            return False
        return self._pristine_experiment_reconciliation(events[-1][2])

    @staticmethod
    def _pristine_experiment_reconciliation(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], BrokerSnapshot)
            and isinstance(value[1], CryptoActivityEvidence)
            and value[1].query_exhausted
            and not value[1].history_verified
            and not value[1].executions
            and not value[1].fees
            and not value[1].unsupported
            and value[0].complete
            and value[0].orders_complete
            and value[0].positions_complete
            and not value[0].orders
            and not value[0].positions
        )

    def _binding(self, account: str, scope: str) -> None:
        if (account, scope) != (
            self.journal.identity.account_id,
            self.journal.identity.operational_scope,
        ):
            raise JournalError("BTC journal/account binding conflict")

    def _validate_attempt(self, attempt: BtcAttempt) -> None:
        config = self.config
        events = self.journal.btc_events()
        latest_reconciliation = events[-1][2] if events and events[-1][1] == "reconcile" else None
        if (
            len(events) >= 2
            and events[-1][1] == "attempt"
            and events[-1][2] == attempt
            and events[-2][1] == "reconcile"
        ):
            latest_reconciliation = events[-2][2]
        pristine_initial_authority = not self.attempts and (
            self._pristine_experiment_reconciliation(latest_reconciliation)
            or (
                self.reconciliation is not None
                and self.reconciliation.state is OperationalState.UNRESOLVED
                and self.reconciliation.reason == "activity history unproven"
                and self._pristine_experiment_reconciliation(
                    (attempt.revalidation.snapshot, attempt.revalidation.activities)
                )
            )
        )
        if config is None or self.halted or not (self.usable or pristine_initial_authority):
            raise JournalError("BTC authority not ready")
        if attempt.submission is not None:
            raise JournalError("attempt must start without submission result")
        self._binding(attempt.request.account_id, attempt.request.operational_scope)
        evaluation = attempt.evaluation
        session = self.journal.application_events("btc-session-binding")
        if not session and evaluation.proposal.reason == "paper_plumbing_probe":
            raise JournalError("probe requires bounded session journal")
        if session:
            binding = session[0]
            if not isinstance(binding, tuple) or len(binding) != 3:
                raise JournalError("invalid session binding")
            probe, quantity, deadline = binding
            expected_side = OrderSide.BUY if not self.attempts else OrderSide.SELL
            if (
                len(session) != 1
                or len(self.attempts) >= 2
                or config.maximum_per_run != 2
                or config.maximum_per_period != 2
                or evaluation.policy.maximum_entry_notional > 100
                or attempt.request.side is not expected_side
                or (evaluation.proposal.reason == "paper_plumbing_probe") != probe
                or not isinstance(deadline, datetime)
                or attempt.dispatch_deadline > deadline
                or (not self.attempts and attempt.request.quantity != quantity)
                or (
                    self.attempts
                    and (
                        self.reconciliation is None
                        or self.reconciliation.state is not OperationalState.HOLDING
                        or attempt.request.quantity != self.reconciliation.exposure
                    )
                )
            ):
                raise JournalError("bounded session authority conflict")
        if (
            evaluation.config.configuration_id != config.strategy_configuration_id
            or evaluation.policy.policy_id != config.risk_policy_id
            or btc_intent_identity(evaluation, config.source_market_id) != attempt.intent_identity
            or attempt.reconciliation_revision != self.reconciliation_revision
        ):
            raise JournalError("BTC authority identity/reconciliation conflict")
        identity = derive_paper_client_order_identity(
            stable_account_binding=attempt.request.account_id,
            operational_scope=attempt.request.operational_scope,
            intent_identity=attempt.intent_identity,
        )
        if (identity.client_order_id, identity.full_digest) != (
            attempt.client_order_id,
            attempt.client_order_full_digest,
        ):
            raise JournalError("BTC client identity conflict")
        if any(
            a.intent_identity == attempt.intent_identity
            or a.client_order_id == attempt.client_order_id
            for a in self.attempts
        ):
            raise JournalError("BTC logical attempt permanently spent")
        if self.attempts and attempt.committed_at < self.attempts[-1].committed_at:
            raise JournalError("BTC clock moved backwards")
        if len(self.attempts) >= config.maximum_per_run:
            raise JournalError("BTC run submission ceiling reached")
        count = sum(
            config.period(a.committed_at) == config.period(attempt.committed_at)
            for a in self.attempts
        )
        if count >= config.maximum_per_period:
            raise JournalError("BTC period submission ceiling reached")
        if self.journal.committed_attempts():
            raise JournalError("mixed equity/BTC journal authority")

    def _append(self, kind: str, payload: object, expected_revision: int) -> int:
        self.refresh()
        if self.revision != expected_revision:
            raise JournalError("BTC journal revision conflict")
        # Validate before persistence; refresh below reconstructs exact committed state.
        try:
            self._apply(kind, payload, self.revision + 1)
            revision = self.journal.append_btc_event(
                expected_revision=expected_revision, kind=kind, payload=payload
            )
        finally:
            self.refresh()
        return revision

    def configure(self, config: BtcRunConfig) -> None:
        self.refresh()
        if self.config is not None:
            if self.config != config:
                raise JournalError("BTC run configuration is immutable")
            return
        self._append("configure", config, self.revision)

    def commit(self, attempt: BtcAttempt, *, expected_revision: int) -> None:
        self._append("attempt", attempt, expected_revision)

    def persist_submission(self, result: SubmissionResult) -> None:
        self._append("submission", result, self.revision)

    def record_reconciliation(
        self, snapshot: BrokerSnapshot, activities: CryptoActivityEvidence
    ) -> Reconciliation:
        self._append("reconcile", (snapshot, activities), self.revision)
        assert self.reconciliation is not None
        return self.reconciliation

    def halt(self, now: datetime, reason: str) -> None:
        self.refresh()
        self._append("halt", (now, reason), self.revision)

    def resume(self, *, expected_revision: int) -> None:
        """Explicit operator action after fresh reconciliation; cannot unspend attempts."""
        self._append("resume", self.reconciliation_revision, expected_revision)

    def evidence_identity(self, attempt: BtcAttempt) -> str:
        return canonical_digest(attempt.evaluation)
