"""Read-only bridge coverage for the supervised PAPER canary."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from shadow.adapters.alpaca.paper_broker import PaperCredentials
from shadow.application import paper_canary
from shadow.application.paper_preparation import PaperRiskPreparer
from shadow.application.shadow import (
    FeedHealth,
    RiskObservability,
    RiskObservabilityStatus,
    ShadowRecord,
)
from shadow.domain import AvailabilitySemantics, Instrument, Provenance, Quote
from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Eligibility,
    Evidence,
    OrderStatus,
    SessionState,
)
from shadow.execution.journal_codec import canonical_bytes, decode_canonical
from shadow.risk import (
    OrderIntent,
    OrderSide,
    OrderTarget,
    OrderType,
    RiskDecisionStatus,
    RiskPolicy,
    RiskRejectionReason,
    TimeInForce,
)
from shadow.strategies import Signal
from tests.test_execution_journal_foundation import NOW, SPY, binding


def _quote(*, observed: datetime = NOW) -> Quote:
    return Quote(
        SPY,
        Decimal("100"),
        Decimal("101"),
        None,
        None,
        observed,
        observed,
        AvailabilitySemantics.SYSTEM_RECEIVED,
        Provenance("alpaca:iex"),
    )


def _candidate(
    *,
    signal_time: datetime = NOW,
    feature_time: datetime = NOW,
    quote_time: datetime = NOW,
) -> ShadowRecord:
    base = binding()
    if feature_time > signal_time:
        feature_time = signal_time
    feature = replace(base.feature, observation_time=feature_time, availability_time=feature_time)
    signal: Signal = replace(
        base.signal,
        decision_time=signal_time,
        availability_time=signal_time,
        feature_observation_time=feature_time,
        feature_availability_time=feature_time,
    )
    intent: OrderIntent = replace(base.intent, source_signal=signal, intent_time=signal_time)
    quote = _quote(observed=quote_time)
    return ShadowRecord(
        7,
        "capture-session",
        signal_time,
        "observation",
        "accepted",
        FeedHealth.HEALTHY,
        None,
        "candidate-7",
        feature,
        signal,
        intent,
        RiskObservability(
            RiskObservabilityStatus.QUOTE_NOT_READY,
            quote,
            None,
            "candidate quote retained for independent preparation",
        ),
    )


class ReadOnlyBroker:
    def __init__(
        self,
        *,
        evidence: Evidence | None = None,
        clock_state: SessionState = SessionState.REGULAR,
        positions: tuple[BrokerPosition, ...] = (),
        orders: tuple[BrokerOrder, ...] = (),
        complete: bool = True,
        account_evidence: Evidence | None = None,
    ) -> None:
        self.evidence = evidence or Evidence("snapshot", "paper-account", "paper-scope", NOW, NOW)
        self.account_evidence = account_evidence or self.evidence
        self.clock_state = clock_state
        self.positions = positions
        self.orders = orders
        self.complete = complete
        self.submit_calls = 0

    def read_account(self) -> BrokerAccount:
        return BrokerAccount(
            self.account_evidence,
            target=OrderTarget.PAPER,
            eligibility=Eligibility.ELIGIBLE,
            buying_power=Decimal("1000"),
            currency="USD",
        )

    def read_clock(self) -> BrokerClock:
        if self.clock_state is SessionState.REGULAR:
            return BrokerClock(
                self.evidence,
                SessionState.REGULAR,
                NOW.date(),
                NOW - timedelta(hours=1),
                NOW + timedelta(hours=1),
            )
        return BrokerClock(self.evidence, self.clock_state, NOW.date(), None, None)

    def read_asset(self, instrument: Instrument) -> BrokerAsset:
        return BrokerAsset(self.evidence, instrument, Eligibility.ELIGIBLE, Eligibility.ELIGIBLE)

    def read_snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(
            self.evidence,
            self.positions,
            self.orders,
            self.complete,
            self.complete,
            self.evidence.observation_time - timedelta(minutes=1),
            self.evidence.observation_time,
        )

    def submit(self, request: object) -> None:
        self.submit_calls += 1
        raise AssertionError("paper preparation must never submit")


def _preparer(broker: ReadOnlyBroker, *, now: datetime = NOW) -> PaperRiskPreparer:
    return PaperRiskPreparer(
        broker=broker,
        account_id="paper-account",
        operational_scope="paper-scope",
        instrument=SPY,
        policy=RiskPolicy(
            "paper-preparation-v1",
            True,
            (SPY,),
            Decimal(1),
            1,
            timedelta(seconds=15),
            timedelta(seconds=15),
            timedelta(seconds=15),
            timedelta(seconds=15),
        ),
        controls_enabled=True,
        now=lambda: now,
        expected_feed_lineage="feed-lineage-v1",
        expected_strategy_configuration_id="strategy-v1",
        expected_quantity_configuration_id="quantity-v1",
    )


def test_valid_candidate_produces_round_trippable_canonical_risk_decision_without_submit() -> None:
    broker = ReadOnlyBroker()
    result = _preparer(broker).prepare(_candidate())
    assert result.authorized
    assert result.decision is not None
    assert result.source_opportunity_id is not None
    assert decode_canonical(result.authorization or b"") == result.decision
    assert canonical_bytes(result.decision) == result.authorization
    assert broker.submit_calls == 0


def test_source_opportunity_identity_is_deterministic_and_changed_candidate_cannot_reuse_it() -> (
    None
):
    first = _preparer(ReadOnlyBroker()).prepare(_candidate())
    repeated = _preparer(ReadOnlyBroker()).prepare(_candidate())
    changed = _preparer(ReadOnlyBroker()).prepare(
        _candidate(signal_time=NOW - timedelta(seconds=1), feature_time=NOW - timedelta(seconds=1))
    )
    assert first.authorized and repeated.authorized and changed.authorized
    assert first.source_opportunity_id == repeated.source_opportunity_id
    assert first.source_opportunity_id != changed.source_opportunity_id
    assert first.decision is not None and changed.decision is not None
    assert first.decision.intent.intent_identity != changed.decision.intent.intent_identity


@pytest.mark.parametrize(
    ("candidate", "broker", "expected_halt", "expected_risk_reason"),
    [
        (
            _candidate(),
            ReadOnlyBroker(clock_state=SessionState.CLOSED),
            "market is closed",
            None,
        ),
        (
            _candidate(signal_time=NOW - timedelta(seconds=16)),
            ReadOnlyBroker(),
            None,
            RiskRejectionReason.STALE_SIGNAL,
        ),
        (
            _candidate(feature_time=NOW - timedelta(seconds=16)),
            ReadOnlyBroker(),
            None,
            RiskRejectionReason.STALE_FEATURE,
        ),
        (
            _candidate(quote_time=NOW - timedelta(seconds=16)),
            ReadOnlyBroker(),
            None,
            RiskRejectionReason.STALE_QUOTE,
        ),
        (
            _candidate(),
            ReadOnlyBroker(
                evidence=Evidence(
                    "stale",
                    "paper-account",
                    "paper-scope",
                    NOW - timedelta(seconds=16),
                    NOW - timedelta(seconds=16),
                )
            ),
            None,
            RiskRejectionReason.STALE_OPERATIONAL_STATE,
        ),
        (
            _candidate(),
            ReadOnlyBroker(complete=False),
            "broker snapshot is incomplete",
            None,
        ),
        (
            _candidate(),
            ReadOnlyBroker(
                positions=(
                    BrokerPosition(
                        Evidence("position", "paper-account", "paper-scope", NOW, NOW),
                        SPY,
                        Decimal(1),
                    ),
                )
            ),
            None,
            RiskRejectionReason.POSITION_ALREADY_OPEN,
        ),
        (
            _candidate(),
            ReadOnlyBroker(
                orders=(
                    BrokerOrder(
                        Evidence("order", "paper-account", "paper-scope", NOW, NOW),
                        "order-1",
                        "client-1",
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
                    ),
                )
            ),
            None,
            RiskRejectionReason.OUTSTANDING_ORDER_EXISTS,
        ),
        (
            _candidate(),
            ReadOnlyBroker(
                account_evidence=Evidence("wrong", "other-account", "paper-scope", NOW, NOW)
            ),
            "broker/account binding mismatch",
            None,
        ),
        (
            _candidate(signal_time=NOW + timedelta(seconds=1)),
            ReadOnlyBroker(),
            None,
            RiskRejectionReason.FUTURE_SIGNAL,
        ),
    ],
)
def test_preparation_fails_closed_for_market_candidate_and_broker_conditions(
    candidate: ShadowRecord,
    broker: ReadOnlyBroker,
    expected_halt: str | None,
    expected_risk_reason: RiskRejectionReason | None,
) -> None:
    result = _preparer(broker).prepare(candidate)
    assert not result.authorized
    assert result.authorization is None
    assert broker.submit_calls == 0
    if expected_halt is not None:
        assert result.reason == expected_halt
        assert result.decision is None
    else:
        assert result.decision is not None
        assert result.decision.status is RiskDecisionStatus.REJECTED
        assert expected_risk_reason in result.decision.reasons


def test_future_broker_evidence_fails_before_risk_authorization() -> None:
    future = Evidence(
        "future",
        "paper-account",
        "paper-scope",
        NOW + timedelta(seconds=1),
        NOW + timedelta(seconds=1),
    )
    result = _preparer(ReadOnlyBroker(evidence=future)).prepare(_candidate())
    assert not result.authorized
    assert result.decision is None
    assert result.reason == "broker evidence is from the future"


def test_armed_canary_rejects_source_identity_from_another_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preparer(ReadOnlyBroker()).prepare(_candidate())
    assert result.authorized and result.authorization is not None
    decision_path = tmp_path / "decision.canonical.json"
    decision_path.write_bytes(result.authorization)
    monkeypatch.setattr(
        PaperCredentials,
        "from_environment",
        classmethod(lambda cls: PaperCredentials("paper-key", "paper-secret")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "shadow-paper-canary",
            "--arm-paper-order",
            "--paper-acknowledgement",
            "I-UNDERSTAND-THIS-SUBMITS-ONE-ALPACA-PAPER-ORDER",
            "--account-id",
            "paper-account",
            "--operational-scope",
            "paper-scope",
            "--ownership-directory",
            str(tmp_path / "owner"),
            "--journal-path",
            str(tmp_path / "journal.sqlite"),
            "--evidence-path",
            str(tmp_path / "result.json"),
            "--risk-decision-path",
            str(decision_path),
            "--source-opportunity-id",
            "from-another-candidate",
            "--daily-submission-limit",
            "2",
        ],
    )
    with pytest.raises(SystemExit, match="source opportunity does not match"):
        paper_canary.main()
