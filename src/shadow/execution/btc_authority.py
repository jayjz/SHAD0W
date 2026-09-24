"""Immutable BTC journal evidence, separate from equity session authority."""

from dataclasses import dataclass
from datetime import UTC, datetime

from shadow.domain.crypto_market import CryptoQuote
from shadow.execution.broker import BrokerSnapshot, SubmissionResult
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.features.btc_trend import CompletedBtcInterval
from shadow.risk.btc_models import BtcRiskEvaluation
from shadow.risk.models import OperatorControls


@dataclass(frozen=True, slots=True)
class BtcRunConfig:
    """One durable run per journal; restart cannot rename it or change limits.

    Periods are half-open epoch-aligned UTC intervals, unrelated to equity days.
    A new run/configuration needs an explicit future migration, not a new name.
    """

    run_id: str
    source_market_id: str
    strategy_configuration_id: str
    risk_policy_id: str
    code_revision: str
    maximum_per_run: int
    maximum_per_period: int
    period_seconds: int

    def __post_init__(self) -> None:
        for value in (
            self.run_id,
            self.source_market_id,
            self.strategy_configuration_id,
            self.risk_policy_id,
            self.code_revision,
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("stable run identity required")
        for limit in (self.maximum_per_run, self.maximum_per_period, self.period_seconds):
            if type(limit) is not int or limit <= 0:
                raise ValueError("positive submission ceilings/period required")

    def period(self, now: datetime) -> int:
        if now.tzinfo is not UTC:
            raise ValueError("UTC period timestamp required")
        delta = now - datetime(1970, 1, 1, tzinfo=UTC)
        return (delta.days * 86400 + delta.seconds) // self.period_seconds


@dataclass(frozen=True, slots=True)
class BtcRevalidation:
    intervals: tuple[CompletedBtcInterval, ...]
    quote: CryptoQuote
    account: BtcCashAccount
    asset: BtcBrokerAsset
    snapshot: BrokerSnapshot
    activities: CryptoActivityEvidence
    controls: OperatorControls
    independent_risk_halt: bool = False


@dataclass(frozen=True, slots=True)
class BtcAttempt:
    """Presence is the dispatch-start marker. Every such attempt is spent forever."""

    intent_identity: str
    client_order_full_digest: str
    evaluation: BtcRiskEvaluation
    revalidation: BtcRevalidation
    committed_at: datetime
    dispatch_deadline: datetime
    reconciliation_revision: int
    submission: SubmissionResult | None = None

    @property
    def client_order_id(self) -> str:
        return self.request.client_id

    @property
    def request(self) -> BtcSubmitRequest:
        return self.evaluation.request

    def __post_init__(self) -> None:
        if (
            not self.intent_identity
            or len(self.client_order_full_digest) != 64
            or not self.evaluation.authorized
            or self.committed_at.tzinfo is not UTC
            or self.dispatch_deadline.tzinfo is not UTC
            or self.committed_at >= self.dispatch_deadline
            or type(self.reconciliation_revision) is not int
            or self.reconciliation_revision <= 0
        ):
            raise ValueError("invalid committed BTC authority")
        if self.submission is not None and self.submission.request != self.request:
            raise ValueError("submission request differs from committed authority")


def btc_intent_identity(evaluation: BtcRiskEvaluation, source_market_id: str) -> str:
    """Stable completed-bar opportunity; receipt, run and request sizing are excluded.

    Changed material under this identity is a conflict, never another attempt.
    """
    from shadow.execution.journal_codec import canonical_digest

    return canonical_digest(
        (
            "shadow.btc-intent.v1",
            evaluation.policy.account_id,
            evaluation.policy.operational_scope,
            source_market_id,
            evaluation.config.configuration_id,
            evaluation.proposal.features.end_ns,
            evaluation.proposal.action,
        )
    )
