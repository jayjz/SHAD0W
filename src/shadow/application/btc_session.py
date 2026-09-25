"""One durable BTC PAPER entry/exit lifecycle; strict net accounting remains mandatory."""

from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
from shadow.application.crypto_paper import (
    BtcPaperExperiment,
    HistoricalSource,
    LiveSource,
    PaperApplicationError,
)
from shadow.domain.crypto_market import CryptoQuote, CryptoTrade
from shadow.execution.broker import (
    BrokerPosition,
    BrokerSnapshot,
    SubmissionResult,
    SubmissionStatus,
    SubmitRequest,
)
from shadow.execution.btc_authority import btc_intent_identity
from shadow.execution.btc_dispatch import BtcDispatchAuthority, BtcDispatcher
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.execution.dispatch import DispatchHalted
from shadow.execution.reconciliation import OperationalState
from shadow.features.btc_trend import BTC, HOUR_NS, BtcTrendFeatures
from shadow.risk.btc import evaluate_btc_risk, linked_entry_ns, utc_ns
from shadow.risk.btc_models import BtcLifecycleAuthority, BtcRiskEvaluation
from shadow.risk.models import OrderSide, OrderTarget, OrderType, TimeInForce
from shadow.strategies.btc_trend import BtcAction, BtcProposal, high_water_since_entry, propose


class BtcPaperSession(BtcPaperExperiment):
    """A distinct bounded application. Restart spends no identity a second time.

    A plumbing probe is explicitly marked in every proposal and decision. It
    bypasses only strategy signal conditions, never lifecycle or cash/asset risk.
    Delayed fees freeze submission; no coverage flag is upgraded by this class.
    """

    submission_ceiling = 2

    def __init__(self, *, plumbing_probe: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.policy.maximum_entry_notional > 100:
            raise PaperApplicationError("session entry ceiling is $100")
        self.plumbing_probe = plumbing_probe
        self.trades = self.quotes = 0
        self.deadline = self._clock()
        self.mono_deadline = 0.0
        self.last_read: (
            tuple[BtcCashAccount, BtcBrokerAsset, BrokerSnapshot, CryptoActivityEvidence] | None
        ) = None
        self.stop_reason = "DURATION_EXPIRED"
        self.error: str | None = None

    def _record(self, kind: str, payload: dict[str, object]) -> None:
        self.journal.append_application_event(
            "btc-session", (kind, json.dumps(payload, sort_keys=True))
        )

    def _available(self, exposure: Decimal) -> Decimal:
        reader = getattr(self.broker, "read_btc_available", None)
        value = None if reader is None else reader()
        if (
            not isinstance(value, BrokerPosition)
            or value.instrument != BTC
            or (value.evidence.account_id, value.evidence.operational_scope)
            != (self.policy.account_id, self.policy.operational_scope)
            or not utc_ns(value.evidence.observation_time)
            <= utc_ns(value.evidence.availability_time)
            <= utc_ns(self._clock())
            or utc_ns(self._clock()) - utc_ns(value.evidence.observation_time)
            > self.policy.maximum_broker_age_ns
            or value.quantity != exposure
        ):
            raise DispatchHalted("net BTC and available BTC do not agree")
        self.journal.append_application_event("btc-session-availability", value)
        return value.quantity

    def _guard(self, request: SubmitRequest) -> None:
        if self._clock() >= self.deadline or time.monotonic() >= self.mono_deadline:
            raise DispatchHalted("session duration expired")
        others = tuple(a for a in self.store.attempts if a.request != request)
        if len(others) >= 2 or any(a.request.side is request.side for a in others):
            raise DispatchHalted("one entry and one exit only")
        if request.side is OrderSide.SELL:
            state = self.store.reconciliation
            if len(others) != 1 or state is None or state.state is not OperationalState.HOLDING:
                raise DispatchHalted("exit lacks linked holding")
            if request.quantity != self._available(state.exposure):
                raise DispatchHalted("exit must equal reconciled available BTC")

    def _dispatcher(self) -> BtcDispatcher:
        return BtcDispatcher(
            broker=self.broker,
            journal=self.store,
            now=self._clock,
            controls=self.controls,
            market=self._market,
            session_guard=self._guard,
        )

    def _poll(self) -> None:
        read = self._reads()
        if read is None:
            raise DispatchHalted("broker evidence unavailable")
        self.last_read = read
        self.store.record_reconciliation(read[2], read[3])

    def _evaluate(self, proposal: BtcProposal) -> BtcRiskEvaluation | None:
        read = self._reads()
        if read is None or self.quote is None:
            return None
        account, asset, snapshot, activities = read
        quote = self.quote
        state = self.store.reconciliation
        quantity = self.quantity
        if proposal.action is BtcAction.EXIT:
            if state is None:
                return None
            quantity = self._available(state.exposure)
        request = BtcSubmitRequest(
            self.policy.account_id,
            self.policy.operational_scope,
            "session-draft",
            BTC,
            OrderSide.BUY if proposal.action is BtcAction.ENTER else OrderSide.SELL,
            quantity,
            OrderTarget.PAPER,
            OrderType.MARKET,
            TimeInForce.GTC,
            False,
        )

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
                controls=self.controls(),
                now_ns=utc_ns(self._clock()),
                fills=activities.executions,
                crypto_evidence=activities,
                require_crypto_evidence=True,
                plumbing_probe=self.plumbing_probe,
                lifecycle_authority=(
                    BtcLifecycleAuthority.PROOF
                    if self.store.usable
                    else BtcLifecycleAuthority.INITIAL_EXPERIMENT
                ),
            )

        initial = evaluate(request)
        identity = derive_paper_client_order_identity(
            stable_account_binding=self.policy.account_id,
            operational_scope=self.policy.operational_scope,
            intent_identity=btc_intent_identity(initial, self.run_config.source_market_id),
        )
        return evaluate(replace(request, client_id=identity.client_order_id))

    def _decision(self, end_ns: int | None = None) -> BtcProposal | None:
        now_ns = utc_ns(self._clock())
        rows = self.evidence.history.intervals
        if end_ns is not None:
            rows = tuple(row for row in rows if row.end_ns <= end_ns)
        features = self.config.features(rows, now_ns)
        state = self.store.reconciliation
        holding = state is not None and state.state is OperationalState.HOLDING
        high = None
        if holding and self.last_read is not None:
            activities = self.last_read[3]
            assert state is not None
            entry = linked_entry_ns(
                fills=activities.executions,
                snapshot=self.last_read[2],
                reconciliation=state,
                crypto_evidence=activities,
            )
            if entry is not None:
                high = high_water_since_entry(
                    rows,
                    entry_ns=entry,
                    as_of_ns=now_ns,
                    interval_ns=HOUR_NS,
                )
        proposal = propose(
            self.config, features, now_ns=now_ns, holding=holding, high_water_mark=high
        )
        filters: dict[str, bool] = {}
        reasons: list[str] = []
        if features is None:
            reasons.append("FEATURES_UNAVAILABLE")
        else:
            filters = {
                "trend_positive": features.trend_distance > 0,
                "momentum_positive": features.fast_return > 0,
                "volatility_within_limit": features.volatility <= self.config.maximum_volatility,
                "cost_hurdle_passed": features.trend_distance
                > self.config.round_trip_cost + self.config.cost_safety_margin,
            }
            if now_ns - features.end_ns > self.config.maximum_evidence_age_ns:
                reasons.append("STALE_FEATURES")
            if not holding:
                reasons.extend(
                    reason
                    for key, reason in zip(
                        filters,
                        (
                            "NONPOSITIVE_TREND",
                            "NONPOSITIVE_MOMENTUM",
                            "VOLATILITY_LIMIT",
                            "COST_HURDLE",
                        ),
                        strict=True,
                    )
                    if not filters[key]
                )
            elif proposal is None:
                reasons.append("POSITION_HISTORY_UNAVAILABLE" if high is None else "EXIT_ABSENT")
        if self.plumbing_probe:
            if self.quote is None:
                return None
            # Explicit transport witness, not a fabricated historical strategy feature.
            features = BtcTrendFeatures(
                now_ns,
                now_ns,
                self.quote.ask_price,
                self.quote.ask_price,
                Decimal(0),
                Decimal(0),
                Decimal(0),
            )
            proposal = BtcProposal(
                BtcAction.EXIT if holding else BtcAction.ENTER,
                "paper_plumbing_probe",
                self.config.configuration_id,
                features,
                None,
                self.config.round_trip_cost,
                self.config.cost_safety_margin,
            )
            reasons = ["PAPER_PLUMBING_PROBE"]
            filters = {}
        self._record(
            "decision",
            {
                "mode": "probe" if self.plumbing_probe else "strategy",
                "end_ns": end_ns,
                "feature_status": "probe_price_witness"
                if self.plumbing_probe
                else ("unavailable" if features is None else "available"),
                "probe_ask": str(self.quote.ask_price)
                if self.plumbing_probe and self.quote is not None
                else None,
                **{
                    name: None
                    if features is None or self.plumbing_probe
                    else str(getattr(features, name))
                    for name in ("close", "baseline", "trend_distance", "fast_return", "volatility")
                },
                "filters": filters,
                "reasons": reasons or ["SIGNAL"],
                "cost_hurdle": str(self.config.round_trip_cost + self.config.cost_safety_margin),
                "proposal": None if proposal is None else proposal.action.value,
            },
        )
        return proposal

    def _submit(self, proposal: BtcProposal) -> None:
        evaluation = self._evaluate(proposal)
        self._record(
            "risk",
            {"reasons": ["BROKER_UNAVAILABLE"] if evaluation is None else list(evaluation.reasons)},
        )
        if evaluation is None or not evaluation.authorized:
            return
        dispatcher = self._dispatcher()
        dispatcher.recover()
        result = dispatcher.execute(
            evaluation,
            dispatch_deadline=min(self.deadline, self._clock() + timedelta(seconds=5)),
            expected_revision=self.store.revision,
            plumbing_probe=self.plumbing_probe,
            authority=(
                BtcDispatchAuthority.PROOF
                if self.store.usable
                else BtcDispatchAuthority.INITIAL_EXPERIMENT
            ),
        )
        if isinstance(result, SubmissionResult):
            self._record(
                "submission", {"status": result.status.value, "client_id": result.request.client_id}
            )
        self._poll()

    def run(
        self, historical: HistoricalSource, live: LiveSource, duration_seconds: float
    ) -> dict[str, object]:
        if not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 86400:
            raise PaperApplicationError("session duration must be within (0, 86400]")
        self.store.configure(self.run_config)
        bindings = self.journal.application_events("btc-session-binding")
        if bindings:
            binding = bindings[0]
            if not isinstance(binding, tuple) or len(binding) != 3:
                raise PaperApplicationError("invalid session binding")
            mode, quantity, deadline = binding
            if not isinstance(deadline, datetime):
                raise PaperApplicationError("invalid session deadline")
            if mode != self.plumbing_probe or quantity != self.quantity:
                raise PaperApplicationError("immutable session mode/quantity conflict")
            self.deadline = deadline
        else:
            if self.store.attempts:
                raise PaperApplicationError("cannot adopt another application's attempts")
            self.deadline = self._clock() + timedelta(seconds=duration_seconds)
            self.journal.append_application_event(
                "btc-session-binding", (self.plumbing_probe, self.quantity, self.deadline)
            )
        remaining = min(duration_seconds, (self.deadline - self._clock()).total_seconds())
        self.mono_deadline = time.monotonic() + max(0, remaining)
        events = None
        try:
            self._poll()  # Always reconcile on restart, even after duration expiry.
            if self.store.attempts and (
                self.store.halted
                or any(
                    a.submission is None or a.submission.status is SubmissionStatus.UNCERTAIN
                    for a in self.store.attempts
                )
                or (
                    self.store.reconciliation is not None
                    and self.store.reconciliation.state is OperationalState.FLAT
                )
            ):
                self.stop_reason = "RESTART_RECONCILIATION_ONLY"
                return self.summary()
            if remaining <= 0 or self.store.halted:
                return self.summary()
            fresh_start = ((utc_ns(self._clock()) + HOUR_NS - 1) // HOUR_NS) * HOUR_NS
            if not self.plumbing_probe:
                if self.evidence.events:
                    boundary = utc_ns(self._clock()) // HOUR_NS * HOUR_NS
                    start = self.evidence.history.intervals[-1].end_ns
                    self.evidence.begin(start_ns=start, live_boundary_ns=boundary)
                    for page in historical(start, boundary):
                        self.evidence.trades(page)
                else:
                    self.warm_start(historical)
            remaining = min(
                self.mono_deadline - time.monotonic(),
                (self.deadline - self._clock()).total_seconds(),
            )
            if remaining <= 0:
                return self.summary()
            events = live(remaining)
            for event in events:
                if self._clock() >= self.deadline or time.monotonic() >= self.mono_deadline:
                    break
                new_ends: list[int] = []
                if isinstance(event, CryptoQuote) and event.instrument == BTC:
                    self.quote = event
                    self.quotes += 1
                elif isinstance(event, CryptoTrade) and event.instrument == BTC:
                    self.trades += 1
                    if not self.plumbing_probe:
                        old_count = len(self.evidence.history.intervals)
                        self.evidence.trades((event,))
                        new_ends = [
                            r.end_ns
                            for r in self.evidence.history.intervals[old_count:]
                            if r.start_ns >= fresh_start
                        ]
                else:
                    continue
                if self.store.attempts:
                    self._poll()
                if self.store.halted:
                    self.stop_reason = "UNRESOLVED_OR_HALTED"
                    break
                state = self.store.reconciliation
                assert state is not None
                if self.store.attempts and state.state is OperationalState.FLAT:
                    self.stop_reason = (
                        "ROUND_TRIP_COMPLETE"
                        if len(self.store.attempts) == 2
                        else "ENTRY_TERMINATED"
                    )
                    break
                if state.state in (OperationalState.ENTRY_PENDING, OperationalState.EXIT_PENDING):
                    continue
                if len(self.store.attempts) >= 2:
                    self.stop_reason = "EXIT_UNRESOLVED"
                    break
                if self.plumbing_probe and self.quote is not None:
                    proposal = self._decision()
                    if proposal is not None:
                        self._submit(proposal)
                else:
                    for end in new_ends:
                        proposal = self._decision(end)
                        if (
                            proposal is not None
                            and end == self.evidence.history.intervals[-1].end_ns
                        ):
                            self._submit(proposal)
            self._poll()
            if (
                len(self.store.attempts) == 2
                and not self.store.halted
                and self.store.reconciliation is not None
                and self.store.reconciliation.state is OperationalState.FLAT
            ):
                self.stop_reason = "ROUND_TRIP_COMPLETE"
        except Exception as exc:
            self.stop_reason = "SESSION_HALTED"
            self.error = type(exc).__name__
            self.store.halt(self._clock(), f"session failed: {type(exc).__name__}")
            self._record("error", {"type": type(exc).__name__})
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()
        return self.summary()

    def summary(self) -> dict[str, object]:
        state = self.store.reconciliation
        decisions = [
            json.loads(row[1])
            for row in self.journal.application_events("btc-session")
            if isinstance(row, tuple) and row[0] == "decision"
        ]
        snapshot = None if self.last_read is None else self.last_read[2]
        activities = None if self.last_read is None else self.last_read[3]
        payload: dict[str, object] = {
            "mode": "paper_plumbing_probe" if self.plumbing_probe else "strategy",
            "stop_reason": self.stop_reason,
            "live_trades": self.trades,
            "live_quotes": self.quotes,
            "intervals_evaluated": sum(d["end_ns"] is not None for d in decisions),
            "decisions": decisions,
            "committed_attempts": len(self.store.attempts),
            "attempts": [
                {
                    "client_id": a.client_order_id,
                    "side": a.request.side.value,
                    "quantity": str(a.request.quantity),
                    "submission_status": None
                    if a.submission is None
                    else a.submission.status.value,
                    "error_category": None
                    if a.submission is None or a.submission.error is None
                    else a.submission.error.category.value,
                    "provider_order_id": None
                    if a.submission is None or a.submission.order is None
                    else a.submission.order.order_id,
                }
                for a in self.store.attempts
            ],
            "state": None if state is None else state.state.value,
            "reconciled_btc": None
            if state is None
            or state.state in (OperationalState.UNRESOLVED, OperationalState.HALTED)
            else str(state.exposure),
            "unresolved": self.store.halted
            or state is None
            or state.state
            in (
                OperationalState.UNRESOLVED,
                OperationalState.HALTED,
                OperationalState.ENTRY_PENDING,
                OperationalState.EXIT_PENDING,
            ),
            "reconciliation_reason": None if state is None else state.reason,
            "error_type": self.error,
            "remaining_exposure": None
            if snapshot is None
            else str(
                sum((p.quantity for p in snapshot.positions if p.instrument == BTC), Decimal(0))
            ),
            "orders": []
            if snapshot is None
            else [
                {
                    "id": o.order_id,
                    "status": o.status.value,
                    "side": o.side.value,
                    "filled": str(o.filled_quantity),
                }
                for o in snapshot.orders
            ],
            "observed_positions": []
            if snapshot is None
            else [
                {"symbol": p.instrument.identifier, "quantity": str(p.quantity)}
                for p in snapshot.positions
            ],
            "broker_observed_at": None
            if snapshot is None
            else snapshot.evidence.availability_time.isoformat(),
            "fills": []
            if activities is None
            else [
                {
                    "id": f.execution_id,
                    "order_id": f.order_id,
                    "quantity": str(f.quantity),
                    "price": str(f.price),
                }
                for f in activities.executions
            ],
            "risk_rejections": [
                json.loads(row[1])["reasons"]
                for row in self.journal.application_events("btc-session")
                if isinstance(row, tuple) and row[0] == "risk" and json.loads(row[1])["reasons"]
            ],
            "journal_path": str(self.journal.path),
            "market_path": str(self.evidence.path) if self.evidence.path.exists() else None,
        }
        self._record("summary", payload)
        return payload
