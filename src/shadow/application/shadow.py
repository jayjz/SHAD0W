"""Deterministic shadow observability. No operational admission authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from shadow.domain.market import AvailabilitySemantics, Bar, DatasetMetadata, Instrument, Quote
from shadow.features import FeatureSnapshot, calculate_z_scores
from shadow.risk.evaluator import evaluate_risk
from shadow.risk.models import (
    OperationalQuantityConfig, OperatorControls, OrderIntent, RiskDecision, RiskPolicy, RiskState,
)
from shadow.strategies import MeanReversionConfig, PositionState, Signal, evaluate_mean_reversion


class FeedHealth(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    session_id: str
    code_revision: str
    strategies: tuple[MeanReversionConfig, ...]
    quantities: tuple[OperationalQuantityConfig, ...]
    risk_policy: RiskPolicy
    source: str
    maximum_bar_age: timedelta
    maximum_quote_age: timedelta
    maximum_records: int = 100_000

    def __post_init__(self) -> None:
        instruments = tuple(c.instrument for c in self.strategies)
        if not self.session_id.strip() or not self.code_revision.strip() or not self.source.strip():
            raise ValueError("session, code revision and source identities required")
        if not instruments or len(set(instruments)) != len(instruments):
            raise ValueError("unique explicit strategy instruments required")
        if (set(q.instrument for q in self.quantities) != set(instruments)
                or len(self.quantities) != len(instruments)):
            raise ValueError("one quantity configuration per instrument required")
        if self.maximum_bar_age <= timedelta(0) or self.maximum_quote_age <= timedelta(0):
            raise ValueError("positive feed freshness bounds required")
        if isinstance(self.maximum_records, bool) or self.maximum_records < 1:
            raise ValueError("positive evidence record bound required")


@dataclass(frozen=True, slots=True)
class ShadowRecord:
    sequence: int
    session_id: str
    time: datetime
    action: str
    reason: str
    health: FeedHealth
    observation: Bar | Quote | None = None
    delivery_reference: str = ""
    feature: FeatureSnapshot | None = None
    signal: Signal | None = None
    intent: OrderIntent | None = None
    risk_state: RiskState | None = None
    risk: RiskDecision | None = None
    quote: Quote | None = None


def canonical_time(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware session time required")
    return now.astimezone(UTC)


class ShadowSession:
    """Single-owner bounded session; replay must preserve acceptance/control order.

    Flat is hypothetical strategy context only. No simulated fill or broker inventory
    is asserted. Risk always sees incomplete inventory and disabled trading.
    """

    def __init__(self, config: ShadowConfig) -> None:
        self.config = config
        self._health = FeedHealth.STARTING
        self._connected = False
        self._now: datetime | None = None
        self._records: list[ShadowRecord] = []
        self._bars: dict[Instrument, list[Bar]] = {c.instrument: [] for c in config.strategies}
        self._quotes: dict[Instrument, Quote] = {}
        self._seen: dict[tuple[object, ...], tuple[Bar | Quote, str]] = {}
        self._latest: dict[tuple[type[Bar] | type[Quote], Instrument], Bar | Quote] = {}
        self._generation_seen: set[tuple[type[Bar] | type[Quote], Instrument]] = set()

    @property
    def records(self) -> tuple[ShadowRecord, ...]:
        return tuple(self._records)

    @property
    def health(self) -> FeedHealth:
        return self._health

    def _advance(self, now: datetime) -> datetime:
        now = canonical_time(now)
        if self._now is not None and now < self._now:
            self._health = FeedHealth.FAILED
            raise ValueError("session clock moved backwards")
        if len(self._records) >= self.config.maximum_records:
            self._health = FeedHealth.FAILED
            raise ValueError("bounded session evidence limit reached")
        self._now = now
        return now

    def control(self, action: str, now: datetime, reason: str = "") -> ShadowRecord:
        now = self._advance(now)
        if action not in ("connected", "disconnected", "failed", "stopped", "tick"):
            raise ValueError("unknown shadow control")
        if self._health in (FeedHealth.FAILED, FeedHealth.STOPPED):
            raise ValueError("terminal shadow session")
        if action == "connected":
            self._connected = True
            self._generation_seen.clear()
            self._health = FeedHealth.STARTING
        elif action in ("disconnected", "failed", "stopped"):
            self._connected = False
            self._health = FeedHealth(action)
        else:
            self._refresh(now)
        record = ShadowRecord(len(self._records), self.config.session_id, now, action,
                              reason, self._health)
        self._records.append(record)
        return record

    def _refresh(self, now: datetime) -> None:
        if not self._connected or self._health in (FeedHealth.FAILED, FeedHealth.STOPPED):
            return
        for instrument in self._bars:
            for kind, age in ((Bar, self.config.maximum_bar_age),
                              (Quote, self.config.maximum_quote_age)):
                key = (kind, instrument)
                item = self._latest.get(key)
                if key not in self._generation_seen or item is None:
                    self._health = FeedHealth.STALE
                    return
                if now - item.observation_time > age or now - item.availability_time > age:
                    self._health = FeedHealth.STALE
                    return
        self._health = FeedHealth.HEALTHY

    def accept(self, observation: Bar | Quote, delivery_reference: str = "") -> ShadowRecord:
        now = self._advance(observation.availability_time)
        if self._health in (FeedHealth.FAILED, FeedHealth.STOPPED):
            raise ValueError("terminal shadow session")
        if (observation.instrument not in self._bars
                or observation.provenance.source != self.config.source
                or observation.availability_semantics is not AvailabilitySemantics.SYSTEM_RECEIVED
                or isinstance(observation, Bar) and observation.interval.duration != timedelta(minutes=1)):
            self._health = FeedHealth.FAILED
            raise ValueError("observation violates session scope")
        key = (type(observation), observation.instrument)
        identity = (*key, observation.observation_time)
        previous = self._seen.get(identity)
        reason = "accepted"
        if previous is not None:
            same = replace(observation, availability_time=previous[0].availability_time)
            reason = "duplicate" if (same, delivery_reference) == previous else "conflict"
        elif key in self._latest and observation.observation_time < self._latest[key].observation_time:
            reason = "out_of_order"
        elif not self._connected:
            reason = "disconnected"
        self._seen.setdefault(identity, (observation, delivery_reference))
        feature = None
        signal = None
        intent = None
        state = None
        risk = None
        quote = self._quotes.get(observation.instrument)
        if reason == "conflict":
            self._health = FeedHealth.FAILED
        elif reason == "accepted":
            self._latest[key] = observation
            self._generation_seen.add(key)
            if isinstance(observation, Quote):
                self._quotes[observation.instrument] = observation
                quote = observation
            else:
                history = self._bars[observation.instrument]
                history.append(observation)
                strategy = next(c for c in self.config.strategies if c.instrument == observation.instrument)
                # Keep only the exact trailing window; evidence retains the full accepted stream.
                del history[:-strategy.rolling_window]
                metadata = DatasetMetadata(
                    self.config.source, self.config.session_id, (observation.instrument,),
                    history[0].observation_time, observation.observation_time, observation.interval,
                )
                feature = calculate_z_scores(history, metadata, window=strategy.rolling_window)[-1]
            self._refresh(now)
            if feature is not None and self._health is FeedHealth.HEALTHY:
                signal = evaluate_mean_reversion(feature, strategy, PositionState.FLAT, decision_time=now)
                if signal is not None:
                    quantity = next(q for q in self.config.quantities if q.instrument == observation.instrument)
                    intent = OrderIntent.from_signal(operational_scope=self.config.session_id,
                                                     signal=signal, quantity_config=quantity)
                    state = RiskState(
                        self.config.session_id, "shadow-unreconciled", len(self._records), False,
                        (), (), now, now, OperatorControls(False, False, now, now),
                    )
                    assert quote is not None
                    risk = evaluate_risk(intent=intent, policy=self.config.risk_policy, state=state,
                                         feature=feature, quote=quote, decision_time=now)
        else:
            self._refresh(now)
        record = ShadowRecord(len(self._records), self.config.session_id, now, "observation",
                              reason, self._health, observation, delivery_reference, feature,
                              signal, intent, state, risk, quote)
        self._records.append(record)
        return record
