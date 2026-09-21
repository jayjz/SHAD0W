"""Prepare one live-shadow candidate for the supervised PAPER canary.

This is a read-only composition boundary.  It verifies a persisted live capture,
reads current PAPER evidence, runs the existing pure risk evaluator, and writes a
canonical ``RiskDecision`` only when it is authorized.  It does not create a
journal, capability, reservation, or broker submission.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from shadow.adapters.alpaca.paper_broker import AlpacaPaperBroker, PaperCredentials
from shadow.application.evidence import CaptureError, CaptureStatus, load_capture, verify_capture
from shadow.application.shadow import ShadowRecord
from shadow.domain import Instrument, Quote
from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    Eligibility,
    SessionState,
)
from shadow.execution.journal_codec import canonical_bytes
from shadow.execution.opportunity import (
    SourceOpportunityBinding,
    source_opportunity_key_for_intent,
)
from shadow.features import FeatureSnapshot
from shadow.risk import (
    OpenLongPosition,
    OperatorControls,
    OrderIntent,
    OutstandingOrder,
    RiskDecision,
    RiskDecisionStatus,
    RiskPolicy,
    RiskState,
    evaluate_risk,
)


class PreparationError(RuntimeError):
    """A preparation precondition is missing, inconsistent, or unsafe."""


class Clock(Protocol):
    def __call__(self) -> datetime: ...


class ReadOnlyPaperBroker(Protocol):
    """Exactly the broker reads required to form current risk evidence."""

    def read_account(self) -> BrokerAccount | BrokerError: ...
    def read_clock(self) -> BrokerClock | BrokerError: ...
    def read_asset(self, instrument: Instrument) -> BrokerAsset | BrokerError: ...
    def read_snapshot(self) -> BrokerSnapshot | BrokerError: ...


@dataclass(frozen=True, slots=True)
class PreparationResult:
    """Read-only result; only ``authorization`` may be handed to the canary."""

    decision: RiskDecision | None
    source_opportunity_id: str | None
    reason: str | None

    @property
    def authorized(self) -> bool:
        return self.decision is not None and self.decision.status is RiskDecisionStatus.AUTHORIZED

    @property
    def authorization(self) -> bytes | None:
        if not self.authorized or self.decision is None:
            return None
        return canonical_bytes(self.decision)


def _now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PreparationError("preparation clock must be timezone-aware")
    return value.astimezone(UTC)


def _evidence_times(
    account: BrokerAccount, clock: BrokerClock, asset: BrokerAsset, snapshot: BrokerSnapshot
) -> tuple[datetime, ...]:
    rows: tuple[BrokerPosition | BrokerOrder, ...] = (*snapshot.positions, *snapshot.orders)
    return tuple(
        time
        for evidence in (account.evidence, clock.evidence, asset.evidence, snapshot.evidence)
        for time in (evidence.observation_time, evidence.availability_time)
    ) + tuple(
        time
        for row in rows
        for time in (row.evidence.observation_time, row.evidence.availability_time)
    )


class PaperRiskPreparer:
    """Provider-neutral read-only bridge from a verified live candidate to risk."""

    def __init__(
        self,
        *,
        broker: ReadOnlyPaperBroker,
        account_id: str,
        operational_scope: str,
        instrument: Instrument,
        policy: RiskPolicy,
        controls_enabled: bool,
        now: Clock,
        expected_feed_lineage: str,
        expected_strategy_configuration_id: str,
        expected_quantity_configuration_id: str,
    ) -> None:
        if policy.maximum_concurrent_positions != 1 or policy.maximum_quantity_per_order != 1:
            raise PreparationError(
                "first PAPER preparation requires one-share, one-position policy"
            )
        for value in (
            account_id,
            operational_scope,
            expected_feed_lineage,
            expected_strategy_configuration_id,
            expected_quantity_configuration_id,
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise PreparationError("stable preparation identities are required")
        self._broker = broker
        self._account_id = account_id
        self._scope = operational_scope
        self._instrument = instrument
        self._policy = policy
        self._controls_enabled = controls_enabled
        self._now = now
        self._feed_lineage = expected_feed_lineage
        self._strategy_configuration_id = expected_strategy_configuration_id
        self._quantity_configuration_id = expected_quantity_configuration_id

    def _candidate(self, record: ShadowRecord) -> tuple[FeatureSnapshot, OrderIntent, Quote]:
        if (
            not isinstance(record, ShadowRecord)
            or record.feature is None
            or record.signal is None
            or record.intent is None
            or record.risk_observability is None
            or record.risk_observability.quote is None
        ):
            raise PreparationError("selected capture record is not a quote-ready live candidate")
        intent = record.intent
        if (
            intent.instrument != self._instrument
            or intent.quantity != 1
            or intent.quantity_configuration_id != self._quantity_configuration_id
            or intent.operational_scope != self._scope
            or intent.source_signal.configuration_id != self._strategy_configuration_id
            or intent.source_signal.source_dataset_id != self._feed_lineage
        ):
            raise PreparationError(
                "candidate does not match the explicit PAPER preparation binding"
            )
        return record.feature, intent, record.risk_observability.quote

    def _read(self) -> tuple[BrokerAccount, BrokerClock, BrokerAsset, BrokerSnapshot] | None:
        values = (
            self._broker.read_account(),
            self._broker.read_clock(),
            self._broker.read_asset(self._instrument),
            self._broker.read_snapshot(),
        )
        if any(isinstance(value, BrokerError) for value in values):
            return None
        account, clock, asset, snapshot = values
        assert isinstance(account, BrokerAccount)
        assert isinstance(clock, BrokerClock)
        assert isinstance(asset, BrokerAsset)
        assert isinstance(snapshot, BrokerSnapshot)
        return account, clock, asset, snapshot

    def prepare(self, record: ShadowRecord) -> PreparationResult:
        try:
            feature, intent, quote = self._candidate(record)
        except PreparationError as exc:
            return PreparationResult(None, None, str(exc))
        read = self._read()
        if read is None:
            return PreparationResult(None, None, "authoritative broker read failed")
        account, clock, asset, snapshot = read
        expected_binding = (self._account_id, self._scope)
        if any(
            (value.evidence.account_id, value.evidence.operational_scope) != expected_binding
            for value in (account, clock, asset, snapshot)
        ):
            return PreparationResult(None, None, "broker/account binding mismatch")
        now = _now(self._now())
        if any(value > now for value in _evidence_times(account, clock, asset, snapshot)):
            return PreparationResult(None, None, "broker evidence is from the future")
        if account.target.value != "paper" or account.eligibility is not Eligibility.ELIGIBLE:
            return PreparationResult(None, None, "PAPER account is not eligible")
        if clock.state is not SessionState.REGULAR or clock.session_close is None:
            return PreparationResult(None, None, "market is closed")
        if now >= clock.session_close:
            return PreparationResult(None, None, "broker session is closed")
        if (
            asset.instrument != self._instrument
            or asset.us_equity is not Eligibility.ELIGIBLE
            or asset.tradable is not Eligibility.ELIGIBLE
        ):
            return PreparationResult(None, None, "PAPER asset is not eligible")
        if not snapshot.complete:
            return PreparationResult(None, None, "broker snapshot is incomplete")
        try:
            positions = tuple(
                OpenLongPosition(
                    row.instrument, row.quantity, f"broker-position:{row.instrument.identifier}"
                )
                for row in snapshot.positions
            )
            outstanding = tuple(
                OutstandingOrder(
                    row.instrument,
                    f"broker:{row.order_id}",
                    row.side,
                    row.quantity,
                    row.order_id,
                )
                for row in snapshot.outstanding_orders
            )
            observation_times = _evidence_times(account, clock, asset, snapshot)
            state = RiskState(
                self._scope,
                "paper-preparation:" + snapshot.evidence.reference,
                0,
                True,
                positions,
                outstanding,
                min(observation_times),
                max(observation_times),
                OperatorControls(self._controls_enabled, False, now, now),
            )
            decision = evaluate_risk(
                intent=intent,
                policy=self._policy,
                state=state,
                feature=feature,
                quote=quote,
                decision_time=now,
            )
        except ValueError as exc:
            return PreparationResult(None, None, f"broker evidence cannot form risk state: {exc}")
        if decision.status is RiskDecisionStatus.REJECTED:
            return PreparationResult(decision, None, "independent risk rejected candidate")
        key = source_opportunity_key_for_intent(
            account_id=self._account_id, operational_scope=self._scope, intent=intent
        )
        try:
            binding = SourceOpportunityBinding(key, feature, intent.source_signal, intent)
        except ValueError as exc:
            return PreparationResult(None, None, f"source opportunity binding is invalid: {exc}")
        return PreparationResult(decision, binding.key.source_key, None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="read-only SHAD0W PAPER risk preparation")
    parser.add_argument("--capture-path", type=Path, required=True)
    parser.add_argument("--candidate-sequence", type=int, required=True)
    parser.add_argument("--risk-decision-path", type=Path, required=True)
    parser.add_argument("--evidence-path", type=Path, required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--operational-scope", required=True)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--feed-lineage", required=True)
    parser.add_argument("--strategy-configuration-id", required=True)
    parser.add_argument("--quantity-configuration-id", required=True)
    parser.add_argument("--risk-policy-id", required=True)
    parser.add_argument("--trading-enabled", action="store_true")
    parser.add_argument("--maximum-signal-age-seconds", type=int, default=15)
    parser.add_argument("--maximum-feature-age-seconds", type=int, default=15)
    parser.add_argument("--maximum-quote-age-seconds", type=int, default=15)
    parser.add_argument("--maximum-operational-state-age-seconds", type=int, default=15)
    return parser


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as file:
            file.write(payload)
    except FileExistsError as exc:
        raise PreparationError("refusing to overwrite existing authorization evidence") from exc


def _write_evidence(path: Path, result: PreparationResult) -> None:
    payload: dict[str, object] = {
        "mode": "paper_risk_preparation_read_only",
        "authorized": result.authorized,
        "reason": result.reason,
        "source_opportunity_id": result.source_opportunity_id,
        "decision_id": None if result.decision is None else result.decision.decision_id,
        "risk_status": None if result.decision is None else result.decision.status.value,
        "risk_reasons": None
        if result.decision is None
        else [reason.value for reason in result.decision.reasons],
    }
    _write_new(path, (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode())


def main() -> int:
    arguments = _parser().parse_args()
    try:
        capture = load_capture(arguments.capture_path)
        if capture.status is not CaptureStatus.COMPLETE_STOPPED:
            raise PreparationError("capture must be a complete stopped live-shadow session")
        session = verify_capture(capture)
        record = next(
            (item for item in session.records if item.sequence == arguments.candidate_sequence),
            None,
        )
        if record is None:
            raise PreparationError("candidate sequence is absent from verified capture")
        instrument = Instrument(arguments.symbol)
        policy = RiskPolicy(
            policy_id=arguments.risk_policy_id,
            enabled=arguments.trading_enabled,
            allowed_instruments=(instrument,),
            maximum_quantity_per_order=Decimal(1),
            maximum_concurrent_positions=1,
            maximum_signal_age=timedelta(seconds=arguments.maximum_signal_age_seconds),
            maximum_feature_age=timedelta(seconds=arguments.maximum_feature_age_seconds),
            maximum_quote_age=timedelta(seconds=arguments.maximum_quote_age_seconds),
            maximum_operational_state_age=timedelta(
                seconds=arguments.maximum_operational_state_age_seconds
            ),
        )
        result = PaperRiskPreparer(
            broker=AlpacaPaperBroker(
                credentials=PaperCredentials.from_environment(),
                account_id=arguments.account_id,
                operational_scope=arguments.operational_scope,
            ),
            account_id=arguments.account_id,
            operational_scope=arguments.operational_scope,
            instrument=instrument,
            policy=policy,
            controls_enabled=arguments.trading_enabled,
            now=lambda: datetime.now(UTC),
            expected_feed_lineage=arguments.feed_lineage,
            expected_strategy_configuration_id=arguments.strategy_configuration_id,
            expected_quantity_configuration_id=arguments.quantity_configuration_id,
        ).prepare(record)
        _write_evidence(arguments.evidence_path, result)
        if (
            not result.authorized
            or result.authorization is None
            or result.source_opportunity_id is None
        ):
            return 2
        _write_new(arguments.risk_decision_path, result.authorization)
        print(result.source_opportunity_id)
        return 0
    except (CaptureError, PreparationError, ValueError) as exc:
        raise SystemExit(f"PAPER preparation failed closed: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
