"""Bounded, provider-neutral live-data shadow composition.

This module deliberately has no admission gate or dispatch boundary. A signal is
only an observed candidate; it may have a pure risk evaluation when a caller
supplies explicit non-authoritative state for offline observation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from shadow.domain.market import AvailabilitySemantics, Bar, DatasetMetadata, Instrument, Quote
from shadow.features import FeatureSnapshot, calculate_z_scores
from shadow.risk.evaluator import evaluate_risk
from shadow.risk.models import (
    OperationalQuantityConfig,
    OrderIntent,
    RiskDecision,
    RiskPolicy,
    RiskState,
)
from shadow.strategies import MeanReversionConfig, PositionState, Signal, evaluate_mean_reversion


class FeedHealth(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    STOPPED = "stopped"


class RiskObservabilityStatus(StrEnum):
    QUOTE_NOT_READY = "quote_not_ready"
    OPERATIONAL_STATE_UNAVAILABLE = "operational_state_unavailable"
    EVALUATED = "evaluated"


@dataclass(frozen=True, slots=True)
class BootstrapEvidence:
    """Verified prior completed bars used only to initialize a feature window.

    ``capture_digest`` identifies the exact source artifact without retaining its
    machine-local path.  The bars remain explicit so a derived capture can be
    replayed without treating the source artifact as an ambient dependency.
    """

    capture_digest: str
    bars: tuple[Bar, ...]
    accepted_bar_count: int
    newest_observation_time: datetime
    newest_availability_time: datetime

    def __post_init__(self) -> None:
        if len(self.capture_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.capture_digest
        ):
            raise ValueError("bootstrap capture digest must be a lowercase SHA-256 digest")
        if not self.bars or not all(isinstance(bar, Bar) for bar in self.bars):
            raise ValueError("bootstrap requires completed bars")
        if self.accepted_bar_count != len(self.bars):
            raise ValueError("bootstrap accepted bar count differs from bars")
        observation_time = canonical_time(self.newest_observation_time)
        availability_time = canonical_time(self.newest_availability_time)
        object.__setattr__(self, "newest_observation_time", observation_time)
        object.__setattr__(self, "newest_availability_time", availability_time)
        if observation_time != max(bar.observation_time for bar in self.bars):
            raise ValueError("bootstrap newest observation time differs from bars")
        if availability_time != max(bar.availability_time for bar in self.bars):
            raise ValueError("bootstrap newest availability time differs from bars")


@dataclass(frozen=True, slots=True)
class RiskObservability:
    status: RiskObservabilityStatus
    quote: Quote | None
    decision: RiskDecision | None
    detail: str


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    session_id: str
    operational_scope: str
    code_revision: str
    strategies: tuple[MeanReversionConfig, ...]
    quantities: tuple[OperationalQuantityConfig, ...]
    risk_policy: RiskPolicy
    source: str
    maximum_bar_age: timedelta
    maximum_quote_age: timedelta
    observability_state: RiskState | None = None
    maximum_records: int = 100_000
    source_dataset_id: str | None = None

    def __post_init__(self) -> None:
        instruments = tuple(config.instrument for config in self.strategies)
        if (
            not self.session_id.strip()
            or not self.operational_scope.strip()
            or not self.code_revision.strip()
            or not self.source.strip()
        ):
            raise ValueError("session, scope, code revision and source identities required")
        if not instruments or len(set(instruments)) != len(instruments):
            raise ValueError("unique explicit strategy instruments required")
        if set(item.instrument for item in self.quantities) != set(instruments):
            raise ValueError("one quantity configuration per instrument required")
        if len(self.quantities) != len(instruments):
            raise ValueError("one quantity configuration per instrument required")
        if self.maximum_bar_age <= timedelta(0) or self.maximum_quote_age <= timedelta(0):
            raise ValueError("positive feed freshness bounds required")
        if isinstance(self.maximum_records, bool) or self.maximum_records < 1:
            raise ValueError("positive evidence record bound required")
        if self.source_dataset_id is not None and (
            not self.source_dataset_id.strip()
            or self.source_dataset_id != self.source_dataset_id.strip()
        ):
            raise ValueError("source dataset identity must be a nonempty trimmed string")
        if (
            self.observability_state is not None
            and self.observability_state.operational_scope != self.operational_scope
        ):
            raise ValueError("observability state scope must match the shadow operational scope")


@dataclass(frozen=True, slots=True)
class ShadowRecord:
    sequence: int
    session_id: str
    time: datetime
    action: str
    disposition: str
    health: FeedHealth
    observation: Bar | Quote | None = None
    delivery_reference: str = ""
    feature: FeatureSnapshot | None = None
    signal: Signal | None = None
    intent: OrderIntent | None = None
    risk_observability: RiskObservability | None = None


def canonical_time(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware session time required")
    return now.astimezone(UTC)


class ShadowSession:
    """Single-owner, append-only live shadow state.

    Bars drive features and signals independently per instrument. Quotes are
    consulted only after a candidate exists. No position, fill, broker inventory,
    reservation, or authorization is created.
    """

    def __init__(self, config: ShadowConfig) -> None:
        self.config = config
        self._health = FeedHealth.STARTING
        self._connected = False
        self._now: datetime | None = None
        self._records: list[ShadowRecord] = []
        self._strategy = {item.instrument: item for item in config.strategies}
        self._quantity = {item.instrument: item for item in config.quantities}
        self._bars: dict[Instrument, list[Bar]] = {instrument: [] for instrument in self._strategy}
        self._quotes: dict[Instrument, Quote] = {}
        self._latest: dict[tuple[type[Bar] | type[Quote], Instrument], Bar | Quote] = {}
        self._seen: dict[
            tuple[type[Bar] | type[Quote], Instrument, datetime], tuple[Bar | Quote, str]
        ] = {}
        self._bootstrap_bar_identities: set[tuple[type[Bar], Instrument, datetime]] = set()

    @property
    def records(self) -> tuple[ShadowRecord, ...]:
        return tuple(self._records)

    @property
    def health(self) -> FeedHealth:
        return self._health

    def bootstrap(self, evidence: BootstrapEvidence, now: datetime) -> ShadowRecord:
        """Seed verified history without replaying it through the live session clock.

        Seed bars are deliberately not observations in this session: they create
        no records with old availability times, no features, and no strategy
        evaluation.  The first live accepted completed bar remains the sole
        causal trigger for a candidate after startup.
        """
        current = canonical_time(now)
        if not isinstance(evidence, BootstrapEvidence):
            raise ValueError("bootstrap evidence is required")
        if self._records or self._now is not None or self._connected:
            raise ValueError("bootstrap must precede live session activity")
        if len(self._records) >= self.config.maximum_records:
            self._health = FeedHealth.FAILED
            raise ValueError("bounded session evidence limit reached")
        if self.config.source_dataset_id is None:
            raise ValueError("bootstrap requires a stable source dataset identity")

        bars_by_instrument: dict[Instrument, list[Bar]] = {
            instrument: [] for instrument in self._strategy
        }
        latest_availability: dict[Instrument, datetime] = {}
        for bar in evidence.bars:
            if (
                bar.instrument not in bars_by_instrument
                or bar.provenance.source != self.config.source
                or bar.availability_semantics is not AvailabilitySemantics.SYSTEM_RECEIVED
                or bar.interval.duration != timedelta(minutes=1)
                or bar.observation_time > current
                or bar.availability_time > current
            ):
                self._health = FeedHealth.FAILED
                raise ValueError("bootstrap bar violates session scope or time boundary")
            history = bars_by_instrument[bar.instrument]
            if history and bar.observation_time <= history[-1].observation_time:
                self._health = FeedHealth.FAILED
                raise ValueError("bootstrap bars are duplicate or out of order")
            previous_availability = latest_availability.get(bar.instrument)
            if previous_availability is not None and bar.availability_time < previous_availability:
                self._health = FeedHealth.FAILED
                raise ValueError("bootstrap bars have out-of-order availability")
            history.append(bar)
            latest_availability[bar.instrument] = bar.availability_time

        for instrument, strategy in self._strategy.items():
            history = bars_by_instrument[instrument]
            if len(history) != strategy.rolling_window - 1:
                self._health = FeedHealth.FAILED
                raise ValueError("bootstrap bar count does not seed the configured feature window")

        for instrument, history in bars_by_instrument.items():
            self._bars[instrument] = history
            for bar in history:
                identity = (Bar, instrument, bar.observation_time)
                self._seen[identity] = (bar, "")
                self._bootstrap_bar_identities.add(identity)
            self._latest[(Bar, instrument)] = history[-1]
        return self._record(now=current, action="bootstrap", disposition="history_seeded")

    def _advance(self, now: datetime) -> datetime:
        current = canonical_time(now)
        if self._now is not None and current < self._now:
            self._health = FeedHealth.FAILED
            raise ValueError("session clock moved backwards")
        if len(self._records) >= self.config.maximum_records:
            self._health = FeedHealth.FAILED
            raise ValueError("bounded session evidence limit reached")
        self._now = current
        return current

    def _record(
        self,
        *,
        now: datetime,
        action: str,
        disposition: str,
        observation: Bar | Quote | None = None,
        delivery_reference: str = "",
        feature: FeatureSnapshot | None = None,
        signal: Signal | None = None,
        intent: OrderIntent | None = None,
        risk_observability: RiskObservability | None = None,
    ) -> ShadowRecord:
        record = ShadowRecord(
            len(self._records),
            self.config.session_id,
            now,
            action,
            disposition,
            self._health,
            observation,
            delivery_reference,
            feature,
            signal,
            intent,
            risk_observability,
        )
        self._records.append(record)
        return record

    def control(self, action: str, now: datetime, detail: str = "") -> ShadowRecord:
        current = self._advance(now)
        if action not in ("connected", "disconnected", "failed", "stopped", "tick"):
            raise ValueError("unknown shadow control")
        if self._health in (FeedHealth.FAILED, FeedHealth.STOPPED):
            raise ValueError("terminal shadow session")
        if action == "connected":
            self._connected = True
            self._refresh(current)
        elif action in ("disconnected", "failed", "stopped"):
            self._connected = False
            self._health = FeedHealth(action)
        else:
            self._refresh(current)
        return self._record(now=current, action=action, disposition=detail or action)

    def invalid(self, now: datetime, detail: str, delivery_reference: str = "") -> ShadowRecord:
        """Retain malformed input without mutating usable stream state."""
        current = self._advance(now)
        if self._health in (FeedHealth.FAILED, FeedHealth.STOPPED):
            raise ValueError("terminal shadow session")
        self._refresh(current)
        return self._record(
            now=current,
            action="observation",
            disposition=f"invalid:{detail}",
            delivery_reference=delivery_reference,
        )

    def _bar_is_fresh(self, instrument: Instrument, now: datetime) -> bool:
        item = self._latest.get((Bar, instrument))
        return (
            isinstance(item, Bar)
            and item.availability_time <= now
            and now - item.observation_time <= self.config.maximum_bar_age
        )

    def _quote_is_fresh(self, instrument: Instrument, now: datetime) -> Quote | None:
        item = self._quotes.get(instrument)
        if (
            item is None
            or item.availability_time > now
            or now - item.observation_time > self.config.maximum_quote_age
        ):
            return None
        return item

    def _refresh(self, now: datetime) -> None:
        if not self._connected or self._health in (FeedHealth.FAILED, FeedHealth.STOPPED):
            return
        received_bars = any((Bar, instrument) in self._latest for instrument in self._strategy)
        if all(self._bar_is_fresh(instrument, now) for instrument in self._strategy):
            self._health = FeedHealth.HEALTHY
        elif received_bars:
            self._health = FeedHealth.STALE
        else:
            self._health = FeedHealth.STARTING

    @staticmethod
    def _same_logical_observation(left: Bar | Quote, right: Bar | Quote) -> bool:
        return replace(left, availability_time=right.availability_time) == right

    def _risk_observability(
        self, *, intent: OrderIntent, feature: FeatureSnapshot, now: datetime
    ) -> RiskObservability:
        quote = self._quote_is_fresh(intent.instrument, now)
        if quote is None:
            return RiskObservability(
                RiskObservabilityStatus.QUOTE_NOT_READY,
                None,
                None,
                "no current quote evidence for this candidate",
            )
        state = self.config.observability_state
        if state is None:
            return RiskObservability(
                RiskObservabilityStatus.OPERATIONAL_STATE_UNAVAILABLE,
                quote,
                None,
                "broker-authoritative inventory and controls are unavailable in shadow mode",
            )
        return RiskObservability(
            RiskObservabilityStatus.EVALUATED,
            quote,
            evaluate_risk(
                intent=intent,
                policy=self.config.risk_policy,
                state=state,
                feature=feature,
                quote=quote,
                decision_time=now,
            ),
            "pure evaluation over explicitly supplied non-authoritative operational evidence",
        )

    def accept(self, observation: Bar | Quote, delivery_reference: str = "") -> ShadowRecord:
        current = self._advance(observation.availability_time)
        if self._health in (FeedHealth.FAILED, FeedHealth.STOPPED):
            raise ValueError("terminal shadow session")
        if (
            observation.instrument not in self._strategy
            or observation.provenance.source != self.config.source
            or observation.availability_semantics is not AvailabilitySemantics.SYSTEM_RECEIVED
            or isinstance(observation, Bar)
            and observation.interval.duration != timedelta(minutes=1)
        ):
            self._health = FeedHealth.FAILED
            raise ValueError("observation violates session scope")

        key = (type(observation), observation.instrument)
        identity = (*key, observation.observation_time)
        prior = self._seen.get(identity)
        disposition = "accepted"
        if not self._connected:
            disposition = "disconnected"
        elif prior is not None:
            if isinstance(observation, Bar) and identity in self._bootstrap_bar_identities:
                if not self._same_logical_observation(observation, prior[0]):
                    self._health = FeedHealth.FAILED
                    raise ValueError("conflicting live bar overlaps bootstrap history")
                disposition = "duplicate"
            else:
                same_value = self._same_logical_observation(observation, prior[0])
                same_delivery = (
                    not delivery_reference or not prior[1] or delivery_reference == prior[1]
                )
                disposition = "duplicate" if same_value and same_delivery else "same_time_variant"
        elif (
            latest := self._latest.get(key)
        ) is not None and observation.observation_time < latest.observation_time:
            disposition = "out_of_order"

        feature = None
        signal = None
        intent = None
        risk_observability = None
        if disposition == "accepted":
            self._seen[identity] = (observation, delivery_reference)
            self._latest[key] = observation
            if isinstance(observation, Quote):
                self._quotes[observation.instrument] = observation
            else:
                history = self._bars[observation.instrument]
                history.append(observation)
                strategy = self._strategy[observation.instrument]
                del history[: -strategy.rolling_window]
                metadata = DatasetMetadata(
                    self.config.source,
                    self.config.source_dataset_id or self.config.source,
                    (observation.instrument,),
                    history[0].observation_time,
                    observation.observation_time,
                    observation.interval,
                )
                feature = calculate_z_scores(history, metadata, window=strategy.rolling_window)[-1]
                if self._bar_is_fresh(observation.instrument, current):
                    signal = evaluate_mean_reversion(
                        feature, strategy, PositionState.FLAT, decision_time=current
                    )
                    if signal is not None:
                        intent = OrderIntent.from_signal(
                            operational_scope=self.config.operational_scope,
                            signal=signal,
                            quantity_config=self._quantity[observation.instrument],
                        )
                        risk_observability = self._risk_observability(
                            intent=intent, feature=feature, now=current
                        )
        self._refresh(current)
        return self._record(
            now=current,
            action="observation",
            disposition=disposition,
            observation=observation,
            delivery_reference=delivery_reference,
            feature=feature,
            signal=signal,
            intent=intent,
            risk_observability=risk_observability,
        )
