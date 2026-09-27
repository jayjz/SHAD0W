"""Bounded PAPER_SOAK orchestration; no proof journal or reducer authority."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from shadow.adapters.alpaca.crypto_stream import CryptoDataCredentials
from shadow.adapters.alpaca.paper_broker import (
    PAPER_TRADING_ORIGIN,
    AlpacaPaperBroker,
    PaperCredentials,
)
from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
from shadow.application.btc_history import MarketEvidence
from shadow.application.btc_soak_stream import heartbeat_events
from shadow.application.crypto_paper import (
    HistoricalSource,
    _direct_live,
    _historical_source,
    _relay_live,
)
from shadow.domain.crypto_market import CryptoQuote, CryptoTrade
from shadow.execution.broker import (
    BrokerPosition,
    BrokerSnapshot,
    SubmissionResult,
    SubmissionStatus,
)
from shadow.execution.btc_soak import (
    AUTHORITY,
    SoakAttempt,
    SoakBroker,
    SoakCut,
    SoakHalted,
    SoakState,
    operational_state,
)
from shadow.execution.crypto import BtcBrokerAsset, BtcCashAccount, BtcSubmitRequest
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.execution.journal import ExecutionJournal
from shadow.execution.journal_codec import canonical_digest
from shadow.execution.ownership import AccountOwner
from shadow.features.btc_trend import BTC, BTC_CONTEXT, HOUR_NS
from shadow.risk.btc import utc_ns
from shadow.risk.btc_models import BtcRiskPolicy
from shadow.risk.btc_soak import evaluate_soak_risk
from shadow.risk.models import OperatorControls, OrderSide, OrderTarget, OrderType, TimeInForce
from shadow.strategies.btc_trend import (
    BtcAction,
    BtcProposal,
    high_water_since_entry,
    paper_soak_canary,
    propose,
)

NAMESPACE = "btc-paper-soak-v1"
ACKNOWLEDGEMENT = "I-UNDERSTAND-BOUNDED-BTC-PAPER-SOAK"


class BtcPaperSoak:
    """Single owner, one position, at most BUY+SELL per cycle; restart never sends.

    ExecutionJournal supplies append/fsync and ownership, but its proof attempt
    tables and BtcJournal projections are never populated by this application.
    """

    def __init__(
        self,
        *,
        broker: SoakBroker,
        journal: ExecutionJournal,
        market_evidence: MarketEvidence,
        policy: BtcRiskPolicy,
        quantity: Decimal,
        maximum_cycles: int,
        duration_seconds: int,
        run_id: str,
        code_revision: str,
        controls: Callable[[], OperatorControls],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(duration_seconds) is not int or not 0 < duration_seconds <= 86400:
            raise ValueError("duration must be integer seconds in (0, 86400]")
        if type(maximum_cycles) is not int or not 0 < maximum_cycles <= 24:
            raise ValueError("maximum cycles must be in [1, 24]")
        if not quantity.is_finite() or not 0 < quantity <= policy.maximum_quantity:
            raise ValueError("positive bounded quantity required")
        if not run_id.strip() or not code_revision.strip() or policy.maximum_entry_notional > 100:
            raise ValueError("run/revision and maximum entry notional <= $100 required")
        if journal.identity.target is not OrderTarget.PAPER or (
            journal.identity.account_id,
            journal.identity.operational_scope,
        ) != (policy.account_id, policy.operational_scope):
            raise ValueError("PAPER account/scope binding required")
        if (
            journal.btc_events()
            or journal.committed_attempts()
            or journal.application_events("btc-session-binding")
            or journal.application_events("btc-experiment")
            or journal.has_legacy_halts()
        ):
            raise ValueError("cannot adopt proof/probe/legacy journal")
        self.broker, self.journal, self.evidence = broker, journal, market_evidence
        self.policy, self.quantity = policy, quantity
        self.maximum_cycles, self.duration_seconds = maximum_cycles, duration_seconds
        self.run_id, self.code_revision = run_id, code_revision
        self.controls, self.now, self.monotonic = controls, now, monotonic
        self.config = paper_soak_canary()
        self.attempts: tuple[SoakAttempt, ...] = ()
        self.quote: CryptoQuote | None = None
        self.cut: SoakCut | None = None
        self.state = SoakState("UNRESOLVED")
        self.decisions: list[dict[str, object]] = []
        self.abstentions: Counter[str] = Counter()
        self.maximum_exposure = Decimal(0)
        self.api_errors = 0
        self.stop_reason = "SOURCE_ENDED"
        self.started = self.now()
        self.deadline = self.started + timedelta(seconds=duration_seconds)
        self.mono_deadline = self.monotonic() + duration_seconds
        self.last_poll = float("-inf")
        self._finished = False
        self.finished_at: datetime | None = None

    def record(self, kind: str, payload: object) -> None:
        self.journal.append_application_event(NAMESPACE, (kind, self.now(), payload))

    def _expired(self) -> bool:
        return self.now() >= self.deadline or self.monotonic() >= self.mono_deadline

    def _poll(self) -> SoakCut:
        cut_time = self.now()
        try:
            account = self.broker.read_btc_account()
            asset = self.broker.read_btc_asset()
            activities = self.broker.read_btc_activities(
                history_start=self.started, history_end=cut_time
            )
            snapshot = self.broker.read_reconciliation_snapshot(
                earliest_attempt=self.started, history_end=cut_time
            )
            if (
                not isinstance(account, BtcCashAccount)
                or not isinstance(asset, BtcBrokerAsset)
                or not isinstance(snapshot, BrokerSnapshot)
                or not isinstance(activities, CryptoActivityEvidence)
            ):
                raise SoakHalted("BROKER_API_ERROR")
            available = None
            if snapshot.positions and not snapshot.outstanding_orders:
                value = self.broker.read_btc_available()
                if not isinstance(value, BrokerPosition):
                    raise SoakHalted("BROKER_AVAILABLE_ERROR")
                available = value
            cut = SoakCut(account, asset, snapshot, activities, available)
            # Persist the original provider evidence even when validation rejects it.
            self.record("broker", cut.payload())
            state = operational_state(
                cut,
                self.attempts,
                binding=(self.policy.account_id, self.policy.operational_scope),
                start=self.started,
                now_ns=utc_ns(self.now()),
                maximum_age_ns=self.policy.maximum_broker_age_ns,
            )
            if self.cut is not None:
                prior_orders = {o.order_id: o for o in self.cut.snapshot.orders}
                for order in snapshot.orders:
                    prior = prior_orders.get(order.order_id)
                    if prior is not None and (
                        order.filled_quantity < prior.filled_quantity
                        or (prior.is_terminal and replace(order, evidence=prior.evidence) != prior)
                    ):
                        raise SoakHalted("ORDER_HISTORY_CHANGED")
                old_fills = {f.execution_id: f for f in self.cut.activities.executions}
                new_fills = {f.execution_id: f for f in activities.executions}
                for identity, prior_fill in old_fills.items():
                    current_fill = new_fills.get(identity)
                    if current_fill is None or (
                        replace(current_fill, evidence=prior_fill.evidence) != prior_fill
                        or current_fill.evidence.observation_time
                        != prior_fill.evidence.observation_time
                    ):
                        raise SoakHalted("FILL_HISTORY_CHANGED")
            if state.completed_cycles < self.state.completed_cycles:
                raise SoakHalted("CYCLE_REGRESSION")
            if (
                self.state.state == "HOLDING"
                and state.state == "HOLDING"
                and state.quantity > self.state.quantity
            ):
                raise SoakHalted("UNEXPLAINED_POSITION_INCREASE")
            self.cut, self.state = cut, state
            self.maximum_exposure = max(self.maximum_exposure, state.quantity)
            self.record(
                "operational_authority",
                (AUTHORITY, state.state, state.quantity, state.entry_ns, state.completed_cycles),
            )
            self.last_poll = self.monotonic()
            return cut
        except Exception as exc:
            self.api_errors += 1
            self.record("broker_error", type(exc).__name__)
            raise

    def _decision(self, end_ns: int) -> BtcProposal | None:
        now_ns = utc_ns(self.now())
        rows = tuple(r for r in self.evidence.history.intervals if r.end_ns <= end_ns)
        features = self.config.features(rows, now_ns)
        high = (
            None
            if self.state.entry_ns is None
            else high_water_since_entry(
                rows, entry_ns=self.state.entry_ns, as_of_ns=now_ns, interval_ns=HOUR_NS
            )
        )
        proposal = propose(
            self.config,
            features,
            now_ns=now_ns,
            holding=self.state.state == "HOLDING",
            high_water_mark=high,
        )
        filters = (
            {}
            if features is None
            else {
                "trend_positive": features.trend_distance > 0,
                "momentum_positive": features.fast_return > 0,
                "volatility_within_limit": features.volatility <= self.config.maximum_volatility,
                "cost_hurdle_passed": features.trend_distance
                > self.config.round_trip_cost + self.config.cost_safety_margin,
            }
        )
        reasons = []
        if features is None:
            reasons.append("FEATURES_UNAVAILABLE")
        elif now_ns - features.end_ns > self.config.maximum_evidence_age_ns:
            reasons.append("STALE_FEATURES")
        elif self.state.state not in ("FLAT", "HOLDING"):
            reasons.append(self.state.state)
        elif proposal is None:
            if self.state.state == "HOLDING":
                reasons.append("POSITION_HISTORY_UNAVAILABLE" if high is None else "EXIT_ABSENT")
            else:
                reasons.extend(key.upper() for key, passed in filters.items() if not passed)
        payload: dict[str, object] = {
            "end_ns": end_ns,
            "cycle": self.state.completed_cycles + 1,
            "features": None
            if features is None
            else {k: str(v) for k, v in asdict(features).items()},
            "filters": filters,
            "reasons": reasons,
            "proposal": None if proposal is None else proposal.action.value,
            "proposal_reason": None if proposal is None else proposal.reason,
            "high_water_mark": None if high is None else str(high),
            "authority": AUTHORITY,
        }
        self.record("decision", json.dumps(payload, sort_keys=True))
        self.decisions.append(payload)
        self.abstentions.update(reasons)
        return proposal

    def _risk(
        self, request: BtcSubmitRequest, proposal: BtcProposal, cut: SoakCut
    ) -> tuple[str, ...]:
        controls = self.controls()
        reasons = (
            ("QUOTE_UNAVAILABLE",)
            if self.quote is None
            else evaluate_soak_risk(
                policy=self.policy,
                proposal=proposal,
                request=request,
                intervals=self.evidence.history.intervals,
                quote=self.quote,
                cut=cut,
                state=self.state,
                controls=controls,
                now_ns=utc_ns(self.now()),
                attempts=self.attempts,
                start=self.started,
            )
        )
        self.record(
            "risk",
            (
                AUTHORITY,
                request,
                proposal,
                reasons,
                controls,
                self.quote,
                self.evidence.history.intervals,
                self.evidence.digest,
            ),
        )
        return reasons

    def _submit(self, proposal: BtcProposal) -> None:
        if self._finished or self._expired():
            raise SoakHalted("DURATION_EXPIRED")
        if self.state.completed_cycles >= self.maximum_cycles:
            raise SoakHalted("MAX_CYCLES")
        cut = self._poll()  # Fresh broker cut for every proposed dispatch.
        expected_side = OrderSide.BUY if proposal.action is BtcAction.ENTER else OrderSide.SELL
        if (
            self.state.state not in ("FLAT", "HOLDING")
            or (expected_side is OrderSide.BUY and len(self.attempts) % 2 != 0)
            or (expected_side is OrderSide.SELL and len(self.attempts) % 2 != 1)
            or len(self.attempts) >= 2 * self.maximum_cycles
        ):
            self.abstentions.update(["LIFECYCLE_OR_ATTEMPT_BOUND"])
            self.record("abstention", "LIFECYCLE_OR_ATTEMPT_BOUND")
            return
        identity = derive_paper_client_order_identity(
            stable_account_binding=self.policy.account_id,
            operational_scope=self.policy.operational_scope,
            intent_identity=canonical_digest(
                (
                    AUTHORITY,
                    self.run_id,
                    self.config.configuration_id,
                    self.policy.policy_id,
                    self.state.completed_cycles + 1,
                    expected_side.value,
                )
            ),
        )
        if any(a.request.client_id == identity.client_order_id for a in self.attempts):
            raise SoakHalted("DUPLICATE_CLIENT_ID")
        request = BtcSubmitRequest(
            self.policy.account_id,
            self.policy.operational_scope,
            identity.client_order_id,
            BTC,
            expected_side,
            self.quantity if expected_side is OrderSide.BUY else self.state.quantity,
            OrderTarget.PAPER,
            OrderType.MARKET,
            TimeInForce.GTC,
            False,
        )
        reasons = self._risk(request, proposal, cut)
        if reasons:
            self.abstentions.update(reasons)
            return
        committed_at = self.now()
        started_mono = self.monotonic()
        self.record("attempt", (AUTHORITY, self.state.completed_cycles + 1, request, committed_at))
        self.attempts += (SoakAttempt(request, committed_at),)
        checked = False

        def final_check() -> None:
            nonlocal checked
            if checked:
                raise SoakHalted("SECOND_POST_CALLBACK")
            checked = True
            self.journal.assert_held()
            if (
                self._expired()
                or self.now() < committed_at
                or not 0 <= self.monotonic() - started_mono < 5
            ):
                raise SoakHalted("DISPATCH_EXPIRED")
            # The committed attempt is intentionally excluded from the pre-send
            # lifecycle; it has no response yet. Never excluded after transport.
            if self.quote is None:
                raise SoakHalted("QUOTE_UNAVAILABLE")
            reasons = evaluate_soak_risk(
                policy=self.policy,
                proposal=proposal,
                request=request,
                intervals=self.evidence.history.intervals,
                quote=self.quote,
                cut=cut,
                state=self.state,
                controls=self.controls(),
                now_ns=utc_ns(self.now()),
                attempts=self.attempts[:-1],
                start=self.started,
            )
            if reasons:
                self.record("final_risk_rejection", reasons)
                self.abstentions.update(reasons)
                raise SoakHalted("FINAL_RISK_REJECTED")

        try:
            result = self.broker.submit(request, before_post=final_check)
            if not checked or not isinstance(result, SubmissionResult) or result.request != request:
                raise SoakHalted("MALFORMED_SUBMISSION")
            self.record("submission", result)
            self.attempts = (*self.attempts[:-1], replace(self.attempts[-1], result=result))
            if result.status is not SubmissionStatus.ACCEPTED:
                raise SoakHalted("UNCERTAIN_OR_REJECTED_SUBMISSION")
        except Exception as exc:
            self.api_errors += 1
            reason = str(exc) if isinstance(exc, SoakHalted) else "UNCERTAIN_TRANSPORT"
            self.record("submission_error", (type(exc).__name__, reason))
            # A spent commit is durable even if the response or its persistence failed.
            raise SoakHalted("SUBMISSION_HALTED") from None
        self._poll()

    def run(
        self,
        historical: HistoricalSource,
        live: Callable[[float], Iterable[CryptoTrade | CryptoQuote | None]],
    ) -> dict[str, object]:
        if self.journal.application_events(NAMESPACE) or self.evidence.events:
            # Fresh-only launch is deliberate. An interrupted run never regains
            # send authority; its raw durable records remain available for audit.
            raise ValueError("soak requires a fresh journal and market file; no automatic restart")
        self.started = self.now()
        self.deadline = self.started + timedelta(seconds=self.duration_seconds)
        self.mono_deadline = self.monotonic() + self.duration_seconds
        self.record(
            "binding",
            (
                AUTHORITY,
                self.run_id,
                self.code_revision,
                self.config,
                self.policy,
                self.quantity,
                self.maximum_cycles,
                self.started,
                self.deadline,
                str(self.evidence.path),
            ),
        )
        events: Iterable[CryptoTrade | CryptoQuote | None] | None = None
        try:
            self._poll()
            boundary = utc_ns(self.started) // HOUR_NS * HOUR_NS
            fresh_start = ((utc_ns(self.started) + HOUR_NS - 1) // HOUR_NS) * HOUR_NS
            start = boundary - (self.config.minimum_history + 1) * HOUR_NS
            self.evidence.begin(start_ns=start, live_boundary_ns=boundary)
            for page in historical(start, boundary):
                if self._expired():
                    raise SoakHalted("DURATION_EXPIRED")
                self.evidence.trades(page)
            for row in self.evidence.history.intervals:
                self.record("interval", row)
            remaining = min(
                (self.deadline - self.now()).total_seconds(), self.mono_deadline - self.monotonic()
            )
            if remaining <= 0:
                raise SoakHalted("DURATION_EXPIRED")
            events = live(remaining)
            for event in events:
                if self._expired():
                    self.stop_reason = "DURATION_EXPIRED"
                    break
                controls = self.controls()
                if controls.kill_switch_active or not controls.trading_enabled:
                    raise SoakHalted("OPERATOR_DISABLED")
                if self.monotonic() - self.last_poll >= 5:
                    self._poll()
                if self.state.completed_cycles >= self.maximum_cycles:
                    self.stop_reason = "MAX_CYCLES"
                    break
                if event is None:
                    continue
                if isinstance(event, CryptoQuote) and event.instrument == BTC:
                    self.quote = event
                elif isinstance(event, CryptoTrade) and event.instrument == BTC:
                    old_count = len(self.evidence.history.intervals)
                    self.evidence.trades((event,))
                    rows = self.evidence.history.intervals[old_count:]
                    for row in rows:
                        self.record("interval", row)
                        if row.start_ns < fresh_start:
                            continue
                        self._poll()
                        proposal = self._decision(row.end_ns)
                        if proposal is not None and row == self.evidence.history.intervals[-1]:
                            self._submit(proposal)
                else:
                    raise SoakHalted("MALFORMED_MARKET_EVENT")
                if self.state.completed_cycles >= self.maximum_cycles:
                    self.stop_reason = "MAX_CYCLES"
                    break
            else:
                self.stop_reason = "DURATION_EXPIRED" if self._expired() else "SOURCE_ENDED"
            self._poll()
        except KeyboardInterrupt:
            self.stop_reason = "OPERATOR_INTERRUPTED"
            self.record("halt", (self.stop_reason, "KeyboardInterrupt"))
        except Exception as exc:
            self.stop_reason = str(exc) if isinstance(exc, SoakHalted) else "APPLICATION_ERROR"
            self.record("halt", (self.stop_reason, type(exc).__name__))
        finally:
            self._finished = True
            self.finished_at = self.now()
            close = getattr(events, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    self.stop_reason = "SOURCE_CLOSE_ERROR"
                    self.record("halt", (self.stop_reason, type(exc).__name__))
        result = self.summary()
        self.record("summary", json.dumps(result, sort_keys=True))
        return result

    def summary(self) -> dict[str, object]:
        """Deterministic given the same durable observations and terminal time."""
        fills = () if self.cut is None else self.cut.activities.executions
        cycles = []
        gross = Decimal(0)
        with localcontext(BTC_CONTEXT):
            for i in range(self.state.completed_cycles):
                buy, sell = self.attempts[2 * i : 2 * i + 2]
                assert buy.result is not None and buy.result.order is not None
                assert sell.result is not None and sell.result.order is not None
                entry = [f for f in fills if f.order_id == buy.result.order.order_id]
                exit_fills = [f for f in fills if f.order_id == sell.result.order.order_id]
                buy_qty = sum((f.quantity for f in entry), Decimal(0))
                sell_qty = sum((f.quantity for f in exit_fills), Decimal(0))
                entry_price = sum((f.quantity * f.price for f in entry), Decimal(0)) / buy_qty
                exit_price = sum((f.quantity * f.price for f in exit_fills), Decimal(0)) / sell_qty
                # Matched sold quantity at observed BUY VWAP. Unsold quantity
                # difference is unvalued, never labeled as a BTC fee.
                pnl = (exit_price - entry_price) * sell_qty
                gross += pnl
                cycles.append(
                    {
                        "cycle": i + 1,
                        "entry_price": str(entry_price),
                        "exit_price": str(exit_price),
                        "entry_at": min(f.evidence.observation_time for f in entry).isoformat(),
                        "exit_at": max(f.evidence.observation_time for f in exit_fills).isoformat(),
                        "buy_quantity": str(buy_qty),
                        "sell_quantity": str(sell_qty),
                        "gross_pnl_usd": str(pnl),
                    }
                )
        fees = (
            []
            if self.cut is None
            else [
                {
                    "activity_id": f.activity_id,
                    "amount": str(f.amount),
                    "asset": f.asset,
                    "execution_id": f.execution_id,
                }
                for f in self.cut.activities.fees
            ]
        )
        return {
            "authority": AUTHORITY,
            "profile": "paper_soak_canary",
            "configuration_id": self.config.configuration_id,
            "duration_seconds": str(
                ((self.finished_at or self.now()) - self.started).total_seconds()
            ),
            "hourly_decisions": len(self.decisions),
            "enter_signals": sum(d["proposal"] == "enter" for d in self.decisions),
            "exit_signals": sum(d["proposal"] == "exit" for d in self.decisions),
            "abstentions_by_reason": dict(sorted(self.abstentions.items())),
            "completed_cycles": self.state.completed_cycles,
            "unresolved_cycles": int(len(self.attempts) > self.state.completed_cycles * 2),
            "gross_realized_paper_pnl_usd": str(gross),
            "pnl_basis": "sold quantity * (SELL VWAP - BUY VWAP); quantity differences unvalued",
            "observed_fees": fees,
            "fee_accounting": "INCOMPLETE",
            "maximum_observed_exposure_btc": str(self.maximum_exposure),
            "broker_api_errors": self.api_errors,
            "halt_reason": self.stop_reason,
            "operational_state": self.state.state,
            "attempts": len(self.attempts),
            "cycles": cycles,
            "market_digest": self.evidence.digest,
        }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="bounded experimental BTC PAPER soak; never proof accounting"
    )
    result.add_argument("--paper-soak", required=True, action="store_true")
    result.add_argument("--paper-endpoint", required=True, choices=[PAPER_TRADING_ORIGIN])
    for name in (
        "account-id",
        "operational-scope",
        "run-id",
        "code-revision",
        "paper-acknowledgement",
    ):
        result.add_argument("--" + name, required=True)
    for name in (
        "journal-path",
        "market-evidence-path",
        "experiment-evidence-path",
        "ownership-directory",
        "kill-switch-path",
    ):
        result.add_argument("--" + name, required=True, type=Path)
    for name in ("quantity", "maximum-entry-notional", "cash-buffer"):
        result.add_argument("--" + name, required=True, type=Decimal)
    result.add_argument("--duration-seconds", required=True, type=int)
    result.add_argument("--maximum-cycles", required=True, type=int)
    result.add_argument("--trading-enabled", action="store_true")
    result.add_argument("--market-data-relay")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.paper_acknowledgement != ACKNOWLEDGEMENT or not args.trading_enabled:
        raise SystemExit("explicit PAPER soak acknowledgement and enablement required")
    paths = (args.journal_path, args.market_evidence_path, args.experiment_evidence_path)
    if any(p.exists() or p.is_symlink() for p in paths) or len({p.absolute() for p in paths}) != 3:
        raise SystemExit(
            "fresh distinct artifact paths required; existing journals are never adopted"
        )
    args.ownership_directory.mkdir(parents=True, exist_ok=True)
    with AccountOwner.acquire(
        ownership_directory=args.ownership_directory, account_id=args.account_id
    ) as owner:
        with ExecutionJournal.create(
            path=args.journal_path,
            owner=owner,
            account_id=args.account_id,
            operational_scope=args.operational_scope,
            created_at=datetime.now(UTC),
        ) as journal:

            def controls() -> OperatorControls:
                current = datetime.now(UTC)
                return OperatorControls(
                    args.trading_enabled, args.kill_switch_path.exists(), current, current
                )

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
            runner = BtcPaperSoak(
                broker=AlpacaPaperBroker(
                    credentials=PaperCredentials.from_environment(),
                    account_id=args.account_id,
                    operational_scope=args.operational_scope,
                ),
                journal=journal,
                market_evidence=MarketEvidence(args.market_evidence_path, journal),
                policy=policy,
                quantity=args.quantity,
                maximum_cycles=args.maximum_cycles,
                duration_seconds=args.duration_seconds,
                run_id=args.run_id,
                code_revision=args.code_revision,
                controls=controls,
            )
            credentials = CryptoDataCredentials.from_environment(os.environ)
            live: Callable[[float], Iterable[CryptoTrade | CryptoQuote | None]] = (
                (lambda duration: heartbeat_events(_relay_live(args.market_data_relay, duration)))
                if args.market_data_relay
                else (lambda duration: heartbeat_events(_direct_live(credentials, duration)))
            )
            payload = runner.run(_historical_source(credentials, args.duration_seconds), live)
            with args.experiment_evidence_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")
            print(json.dumps(payload, sort_keys=True))
    return 0
